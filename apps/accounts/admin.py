from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.accounts.models import User, Workspace

admin.site.register(User, UserAdmin)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "color")
    prepopulated_fields = {"slug": ("name",)}
