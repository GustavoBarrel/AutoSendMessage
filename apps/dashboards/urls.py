from django.urls import path

from apps.dashboards import views, views_home

app_name = "dashboards"

urlpatterns = [
    path("", views_home.home, name="home"),
    path("stats/", views.stats_dashboard, name="stats_dashboard"),
    path("stats/export/", views.stats_export_csv, name="stats_export_csv"),
    path("stats/dashboards/", views.custom_dashboard_list, name="custom_dashboard_list"),
    path(
        "stats/dashboards/new/",
        views.custom_dashboard_create,
        name="custom_dashboard_create",
    ),
    path(
        "stats/dashboards/<int:pk>/",
        views.custom_dashboard_view,
        name="custom_dashboard_view",
    ),
    path(
        "stats/dashboards/<int:pk>/edit/",
        views.custom_dashboard_edit,
        name="custom_dashboard_edit",
    ),
    path(
        "stats/dashboards/<int:pk>/delete/",
        views.custom_dashboard_delete,
        name="custom_dashboard_delete",
    ),
    path(
        "stats/dashboards/<int:dashboard_pk>/widget/preview/",
        views.widget_preview,
        name="widget_preview",
    ),
    path(
        "stats/dashboards/<int:dashboard_pk>/widget/new/",
        views.widget_create,
        name="widget_create",
    ),
    path(
        "stats/dashboards/<int:dashboard_pk>/widget/<int:widget_pk>/edit/",
        views.widget_edit,
        name="widget_edit",
    ),
    path(
        "stats/dashboards/<int:dashboard_pk>/widget/<int:widget_pk>/delete/",
        views.widget_delete,
        name="widget_delete",
    ),
    path(
        "stats/dashboards/<int:dashboard_pk>/widget/<int:widget_pk>/duplicate/",
        views.widget_duplicate,
        name="widget_duplicate",
    ),
    path(
        "stats/dashboards/<int:dashboard_pk>/widget/reorder/",
        views.widget_reorder,
        name="widget_reorder",
    ),
]
