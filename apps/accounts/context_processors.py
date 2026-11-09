from apps.accounts.models import Workspace


def current_workspace(request):
    """Inject current_workspace and workspaces into the context (authenticated users only)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    workspaces = list(Workspace.objects.all())
    workspace_id = request.session.get("workspace_id")
    current_workspace_obj = None
    if workspace_id:
        try:
            current_workspace_obj = Workspace.objects.get(pk=workspace_id)
        except Workspace.DoesNotExist:
            pass
    return {
        "current_workspace": current_workspace_obj,
        "workspaces": workspaces,
    }
