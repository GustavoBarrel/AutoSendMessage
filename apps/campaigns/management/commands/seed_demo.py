"""Create fictional demo data so the system can be explored without real contacts or chips."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.campaigns.services.demo_seed import SeedSummary, seed_demo


class Command(BaseCommand):
    help = (
        "Create fictional demo data (workspaces, contacts, simulated chips, campaigns and a "
        "send history). Deterministic and idempotent. It creates no users or passwords."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even with DEBUG=False (the command refuses by default outside development).",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_demo only runs with DEBUG=True. Pass --force to run it anyway "
                "(e.g. in the Docker demo)."
            )
        summary = seed_demo()
        self.stdout.write(self.style.SUCCESS(self._format(summary)))

    @staticmethod
    def _format(summary: SeedSummary) -> str:
        return (
            "Demo data ready. Created now: "
            f"{summary.workspaces} workspaces, {summary.chips} chips, {summary.tags} tags, "
            f"{summary.contacts} contacts, {summary.campaigns} campaigns, "
            f"{summary.send_jobs} send jobs, {summary.dashboards} dashboards."
        )
