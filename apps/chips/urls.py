from django.urls import path

from apps.chips import views

app_name = "chips"

urlpatterns = [
    path("chips/", views.chip_list, name="chip_list"),
    path("chips/new/", views.chip_create, name="chip_create"),
    path("chips/<int:pk>/edit/", views.chip_edit, name="chip_edit"),
    path(
        "chips/<int:pk>/clear-deferrals/",
        views.chip_clear_pending_schedule,
        name="chip_clear_pending_schedule",
    ),
    path("chips/<int:pk>/delete/", views.chip_confirm_delete, name="chip_confirm_delete"),
    path("chips/<int:pk>/delete/confirm/", views.chip_delete, name="chip_delete"),
    path("chips/<int:pk>/connect/", views.chip_connect, name="chip_connect"),
    path("chips/<int:pk>/connect/json/", views.chip_connect_json, name="chip_connect_json"),
    path("chips/<int:pk>/connect/status/", views.chip_connect_status, name="chip_connect_status"),
    path(
        "chips/<int:pk>/connect/success/", views.chip_connect_success, name="chip_connect_success"
    ),
    path(
        "webhook/evolution/qrcode/<str:token>/",
        views.evolution_webhook_qrcode,
        name="evolution_webhook_qrcode",
    ),
    path("debug/evolution/", views.debug_evolution, name="debug_evolution"),
]
