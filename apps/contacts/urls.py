from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/ids/", views.contact_ids, name="contact_ids"),
    path("contacts/bulk-archive/", views.contact_bulk_archive, name="contact_bulk_archive"),
    path(
        "contacts/bulk-archive/confirm/",
        views.contact_bulk_archive_confirm,
        name="contact_bulk_archive_confirm",
    ),
    path("contacts/<int:pk>/tags/", views.contact_tags, name="contact_tags"),
    path("tags/", views.manual_tag_list, name="manual_tag_list"),
    path("tags/new/", views.manual_tag_create, name="manual_tag_create"),
    path("tags/<int:pk>/edit/", views.manual_tag_edit, name="manual_tag_edit"),
    path(
        "tags/<int:pk>/delete/", views.manual_tag_confirm_delete, name="manual_tag_confirm_delete"
    ),
    path("tags/<int:pk>/delete/confirm/", views.manual_tag_delete, name="manual_tag_delete"),
    path("contacts/<int:pk>/delete/", views.contact_confirm_delete, name="contact_confirm_delete"),
    path("contacts/<int:pk>/delete/confirm/", views.contact_delete, name="contact_delete"),
    path("contacts/import/template/", views.import_template, name="contacts_import_template"),
    path("contacts/import/", views.import_upload, name="contacts_import"),
    path("contacts/import/map/", views.import_map, name="contacts_import_map"),
    path("contacts/import/preview/", views.import_preview, name="contacts_import_preview"),
    path(
        "contacts/import/duplicates/",
        views.import_duplicates,
        name="contacts_import_duplicates",
    ),
    path("contacts/import/summary/", views.import_summary, name="contacts_import_summary"),
    path("contacts/import/run/", views.import_run, name="contacts_import_run"),
]
