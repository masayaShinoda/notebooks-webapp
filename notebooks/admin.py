from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Note, Notebook, NoteRevision


@admin.register(Notebook)
class NotebookAdmin(ModelAdmin):
    list_display = ('title', 'owner', 'color', 'updated_at', 'deleted_at')
    list_filter = ('color',)
    search_fields = ('title', 'owner__username')

    # The default manager hides trashed rows; the admin shows everything.
    def get_queryset(self, request):
        return Notebook.all_objects.all()


@admin.register(Note)
class NoteAdmin(ModelAdmin):
    # `encrypted_content` is shown read-only, as ciphertext, on purpose --
    # decrypting it here would defeat the point of encrypting it at all.
    # It's expected (and reassuring) that this field looks like garbage.
    list_display = ('title', 'notebook', 'updated_at', 'deleted_at')
    search_fields = ('title', 'notebook__title')
    readonly_fields = ('encrypted_content', 'created_at', 'updated_at')

    def get_queryset(self, request):
        return Note.all_objects.select_related('notebook')


@admin.register(NoteRevision)
class NoteRevisionAdmin(ModelAdmin):
    # History is written only by note_save/note_restore; editing a revision
    # here would make "restore" bring back something the user never wrote.
    list_display = ('note', 'version', 'title', 'created_at')
    search_fields = ('title', 'note__title')
    readonly_fields = ('note', 'version', 'title', 'encrypted_content', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
