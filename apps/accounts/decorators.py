from django.shortcuts import redirect

from apps.accounts.models import Workspace


def workspace_required(view_func):
    def wrapped(request, *args, **kwargs):
        workspace = get_workspace_from_session(request)
        if not workspace:
            return redirect("accounts:workspace_select")
        request.current_workspace = workspace
        return view_func(request, *args, **kwargs)

    return wrapped


def get_workspace_from_session(request):
    """Return the workspace from the session, or None."""
    workspace_id = request.session.get("workspace_id")
    if not workspace_id:
        return None
    try:
        return Workspace.objects.get(pk=workspace_id)
    except Workspace.DoesNotExist:
        request.session.pop("workspace_id", None)
        return None
