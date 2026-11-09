"""
Send worker: continuous loop that sleeps adaptively when the queue is empty.
Shuts down gracefully on SIGINT/SIGTERM (Ctrl+C).
"""

import logging
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from hashlib import md5

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.campaigns.models import Campaign, CampaignSendJob
from apps.campaigns.services.campaign_chip_offline import try_resume_chips_with_waiting_jobs
from apps.campaigns.services.campaign_lifecycle import auto_finish_if_complete
from apps.campaigns.services.campaign_send_execution import claim_and_process_one
from apps.campaigns.services.campaign_send_queue import new_worker_id, reclaim_stale_locks

logger = logging.getLogger(__name__)

_IDLE_SLEEP_SECONDS = 5
_RECLAIM_INTERVAL_SECONDS = 30


def _worker_lock_path() -> str:
    base = str(settings.BASE_DIR).encode("utf-8")
    suffix = md5(base, usedforsecurity=False).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"autosend_campaign_worker_{suffix}.lock")


def _lock_file(lock_file) -> None:
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write("0")
            lock_file.flush()
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise CommandError("Já existe um worker de campanha rodando.") from exc


def _unlock_file(lock_file) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _single_worker_lock():
    lock_path = _worker_lock_path()
    lock_file = open(lock_path, "a+", encoding="utf-8")
    locked = False
    try:
        _lock_file(lock_file)
        locked = True
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        yield lock_path
    finally:
        if locked:
            _unlock_file(lock_file)
        lock_file.close()
        if locked:
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass


class Command(BaseCommand):
    help = """Continuous send worker (CampaignSendJob).

How to run:
  python manage.py run_campaign_send_worker

Endless loop: take the next eligible PENDING job, process it and repeat.
When the queue is empty, wait --idle-sleep seconds before checking again.
Release expired locks every --reclaim-interval seconds.
Shut down gracefully on Ctrl+C (SIGINT) or SIGTERM.

Parameters:
  --lease            Lock validity in seconds after the claim (default 120).
  --idle-sleep       Seconds to wait when the queue is empty (default 5).
  --reclaim-interval Seconds between releases of expired locks (default 30)."""

    def add_arguments(self, parser):
        parser.add_argument(
            "--lease",
            type=int,
            default=120,
            help="Lock lease in seconds after the claim (default 120).",
        )
        parser.add_argument(
            "--idle-sleep",
            type=int,
            default=_IDLE_SLEEP_SECONDS,
            help="Seconds to wait when the queue is empty (default 5).",
        )
        parser.add_argument(
            "--reclaim-interval",
            type=int,
            default=_RECLAIM_INTERVAL_SECONDS,
            help="Seconds between releases of expired locks (default 30).",
        )

    def handle(self, *args, **options):
        with _single_worker_lock() as lock_path:
            lease: int = max(30, int(options["lease"]))
            idle_sleep: int = max(1, int(options["idle_sleep"]))
            reclaim_interval: int = max(10, int(options["reclaim_interval"]))

            worker_id = new_worker_id()
            self._running = True

            def _stop(signum, frame):
                self.stdout.write(
                    self.style.WARNING(f"\nworker={worker_id} — signal received, shutting down...")
                )
                self._running = False

            signal.signal(signal.SIGINT, _stop)
            signal.signal(signal.SIGTERM, _stop)

            self.stdout.write(
                self.style.SUCCESS(
                    f"worker={worker_id} started | lock={lock_path} | lease={lease}s "
                    f"idle_sleep={idle_sleep}s reclaim_interval={reclaim_interval}s"
                )
            )

            summary: dict[str, int] = {}
            last_reclaim = 0.0

            while self._running:
                now_mono = time.monotonic()
                if now_mono - last_reclaim >= reclaim_interval:
                    reclaim_stale_locks(lease_seconds=lease)
                    # Cancel jobs of deleted campaigns (campaign=NULL) so they do not
                    # pile up forever as phantom PENDING jobs.
                    orphaned = CampaignSendJob.objects.filter(
                        campaign__isnull=True,
                        status__in=[
                            CampaignSendJob.Status.PENDING,
                            CampaignSendJob.Status.LOCKED,
                            CampaignSendJob.Status.WAITING_CHIP,
                        ],
                    ).update(
                        status=CampaignSendJob.Status.CANCELLED,
                        schedule_defer_reason="",
                        locked_at=None,
                        locked_by="",
                        lease_expires_at=None,
                    )
                    if orphaned:
                        logger.info(
                            "worker=%s cancelled %d orphan job(s) (campaign deleted)",
                            worker_id,
                            orphaned,
                        )
                    last_reclaim = now_mono

                try:
                    n_resumed = try_resume_chips_with_waiting_jobs()
                    if n_resumed:
                        logger.info("worker=%s chips_recovered=%s", worker_id, n_resumed)
                except Exception:
                    logger.exception("worker=%s try_resume_chips_with_waiting_jobs", worker_id)

                now = timezone.now()
                jid = (
                    CampaignSendJob.objects.filter(
                        status=CampaignSendJob.Status.PENDING,
                        campaign__isnull=False,
                        campaign__status=Campaign.Status.ACTIVE,
                    )
                    .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now))
                    .order_by("pk")
                    .values_list("pk", flat=True)
                    .first()
                )

                if jid is None:
                    time.sleep(idle_sleep)
                    continue

                result = claim_and_process_one(jid, worker_id, lease_seconds=lease)
                summary[result] = summary.get(result, 0) + 1
                logger.debug("worker=%s job_id=%s result=%s", worker_id, jid, result)

                if result in (
                    "sent",
                    "failed",
                    "retry_scheduled",
                    "deferred",
                    "cancelled",
                    "waiting_chip",
                ):
                    from apps.campaigns.models import CampaignSendJob as _Job

                    _j = (
                        _Job.objects.filter(pk=jid)
                        .values("status", "last_error", "scheduled_at", "attempts")
                        .first()
                    )
                    if _j:
                        if result == "sent":
                            self.stdout.write(self.style.SUCCESS(f"  ✓ job={jid} SENT"))
                        elif result in ("failed", "retry_scheduled"):
                            self.stdout.write(
                                self.style.ERROR(
                                    f"  ✗ job={jid} {result.upper()} attempt={_j['attempts']} "
                                    f"error: {_j['last_error'] or '—'}"
                                )
                            )
                        elif result == "deferred":
                            self.stdout.write(
                                f"  ⏳ job={jid} DEFERRED (rate limit) until {_j['scheduled_at']}"
                            )
                        elif result == "waiting_chip":
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  ⏸ job={jid} WAITING CHIP (chip queue suspended)"
                                )
                            )

                if result in ("sent", "failed", "cancelled"):
                    job_campaign_id = (
                        CampaignSendJob.objects.filter(pk=jid)
                        .values_list("campaign_id", flat=True)
                        .first()
                    )
                    if job_campaign_id:
                        finished = auto_finish_if_complete(job_campaign_id)
                        if finished:
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"worker={worker_id} campaign campaign_id={job_campaign_id} "
                                    "finished automatically."
                                )
                            )

            self.stdout.write(self.style.SUCCESS(f"worker={worker_id} stopped | results={summary}"))
