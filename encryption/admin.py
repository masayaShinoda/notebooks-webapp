from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import UserEncryptionKey


@admin.register(UserEncryptionKey)
class UserEncryptionKeyAdmin(ModelAdmin):
    # wrapped_dek is deliberately excluded from list_display and marked
    # readonly: hand-editing it would make a user's notes permanently
    # unrecoverable, and there's no legitimate reason to view/copy it here.
    list_display = ('user', 'created_at', 'rotated_at')
    readonly_fields = ('user', 'wrapped_dek', 'created_at', 'rotated_at')

    def has_add_permission(self, request):
        return False
