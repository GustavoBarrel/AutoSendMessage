from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("workspaces/select/", views.workspace_select, name="workspace_select"),
    path("workspaces/set/", views.workspace_set, name="workspace_set"),
    path("workspaces/", views.workspace_list, name="workspace_list"),
    path("workspaces/new/", views.workspace_create, name="workspace_create"),
    path("workspaces/<int:pk>/edit/", views.workspace_edit, name="workspace_edit"),
    path(
        "workspaces/<int:pk>/delete/",
        views.workspace_confirm_delete,
        name="workspace_confirm_delete",
    ),
    path("workspaces/<int:pk>/delete/confirm/", views.workspace_delete, name="workspace_delete"),
    path("login/", LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
