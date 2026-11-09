from django.contrib import admin

from apps.chips.models import Chip


@admin.register(Chip)
class ChipAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "instance_name", "account_type", "is_active")
    list_filter = ("workspace", "is_active", "account_type")
