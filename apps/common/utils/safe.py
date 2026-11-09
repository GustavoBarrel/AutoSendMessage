import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect

logger = logging.getLogger(__name__)


def safe_user_message(action: str, exc: Exception | None = None) -> str:
    if getattr(settings, "DEBUG", False) and exc is not None:
        return f"Erro ao {action}: {exc}"
    return f"Erro ao {action}. Tente novamente mais tarde."


def safe_action(action: str, *, fallback: str = "dashboards:home"):
    """
    Decorator for HTML views:
    - catches unhandled exceptions
    - logs the details (server-side)
    - shows a toast (Django messages)
    - redirects to the referer (if any) or `fallback`
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                return view_func(request, *args, **kwargs)
            except Exception as exc:
                logger.exception("Unhandled error (%s)", action)
                messages.error(request, safe_user_message(action, exc))
                referer = request.META.get("HTTP_REFERER")
                if referer:
                    return redirect(referer)
                return redirect(fallback)

        return wrapper

    return decorator


def safe_json_action(action: str, *, status: int = 500):
    """
    Decorator for JSON views:
    - catches unhandled exceptions
    - logs the details (server-side)
    - returns a standardized JSON
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                return view_func(request, *args, **kwargs)
            except Exception as exc:
                logger.exception("Unhandled error (%s)", action)
                return JsonResponse({"error": safe_user_message(action, exc)}, status=status)

        return wrapper

    return decorator
