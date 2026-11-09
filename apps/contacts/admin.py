from django.contrib import admin

from apps.contacts.models import Contact


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("mobile_phone", "name", "has_whatsapp", "city", "email")
    list_filter = ("has_whatsapp",)
    search_fields = ("mobile_phone", "name", "email")
