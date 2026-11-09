from django.urls import path

from apps.campaigns import views, views_wizard

app_name = "campaigns"

urlpatterns = [
    path("campaigns/", views.campaign_list, name="campaign_list"),
    path("campaigns/new/", views.campaign_create, name="campaign_create"),
    path("campaigns/<int:pk>/edit/", views.campaign_edit, name="campaign_edit"),
    path("campaigns/<int:pk>/dashboard/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<int:pk>/action/", views.campaign_action, name="campaign_action"),
    path(
        "campaigns/<int:pk>/delete/",
        views.campaign_confirm_delete,
        name="campaign_confirm_delete",
    ),
    path(
        "campaigns/<int:pk>/delete/confirm/",
        views.campaign_delete,
        name="campaign_delete",
    ),
    path(
        "campaigns/<int:pk>/wizard/<slug:step>/",
        views_wizard.campaign_wizard,
        name="campaign_wizard",
    ),
    path(
        "campaigns/<int:pk>/wizard/audience/search-contact/",
        views_wizard.campaign_audience_search_contact,
        name="campaign_audience_search_contact",
    ),
]
