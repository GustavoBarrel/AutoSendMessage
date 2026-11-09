from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.accounts.forms import WorkspaceForm
from apps.accounts.models import Workspace
from apps.common.utils.safe import safe_action


def _render_workspace_form(
    request, *, form: WorkspaceForm, title: str, workspace: Workspace | None = None
):
    ctx = {"form": form, "title": title}
    if workspace is not None:
        ctx["workspace"] = workspace
    return render(request, "accounts/workspace_form.html", ctx)


@login_required
def workspace_select(request):
    """Netflix-style screen to choose which workspace to use."""
    workspaces = Workspace.objects.all().order_by("name")
    return render(request, "accounts/workspace_select.html", {"workspaces": workspaces})


@login_required
@require_POST
@csrf_protect
@safe_action("setar perfil", fallback="dashboards:home")
def workspace_set(request):
    workspace_id = request.POST.get("workspace_id")
    if not workspace_id:
        return redirect("dashboards:home")
    try:
        workspace = Workspace.objects.get(pk=workspace_id)
    except Workspace.DoesNotExist:
        return redirect("dashboards:home")
    request.session["workspace_id"] = workspace.pk
    return redirect("dashboards:home")


@login_required
def workspace_list(request):
    """List workspaces to create, edit or delete."""
    workspaces = Workspace.objects.all().order_by("name")
    return render(request, "accounts/workspace_list.html", {"workspaces": workspaces})


@login_required
@csrf_protect
@safe_action("criar perfil", fallback="accounts:workspace_list")
def workspace_create(request):
    if request.method != "POST":
        return _render_workspace_form(request, form=WorkspaceForm(), title="Novo perfil")

    form = WorkspaceForm(request.POST)
    if not form.is_valid():
        return _render_workspace_form(request, form=form, title="Novo perfil")

    form.save()
    messages.success(request, "Perfil criado com sucesso.")
    return redirect("accounts:workspace_select")


@login_required
@csrf_protect
@safe_action("editar perfil", fallback="accounts:workspace_list")
def workspace_edit(request, pk):
    workspace = get_object_or_404(Workspace, pk=pk)
    title = f"Editar {workspace.name}"
    if request.method != "POST":
        return _render_workspace_form(
            request, form=WorkspaceForm(instance=workspace), title=title, workspace=workspace
        )

    form = WorkspaceForm(request.POST, instance=workspace)
    if not form.is_valid():
        return _render_workspace_form(request, form=form, title=title, workspace=workspace)

    form.save()
    messages.success(request, "Perfil atualizado.")
    return redirect("accounts:workspace_list")


@login_required
@require_POST
@csrf_protect
@safe_action("excluir perfil", fallback="accounts:workspace_list")
def workspace_delete(request, pk):
    workspace = get_object_or_404(Workspace, pk=pk)
    name = workspace.name
    workspace.delete()
    if str(request.session.get("workspace_id")) == str(pk):
        request.session.pop("workspace_id", None)
    messages.success(request, f'Perfil "{name}" excluído.')
    return redirect("accounts:workspace_list")


@login_required
def workspace_confirm_delete(request, pk):
    workspace = get_object_or_404(Workspace, pk=pk)
    return render(request, "accounts/workspace_confirm_delete.html", {"workspace": workspace})
