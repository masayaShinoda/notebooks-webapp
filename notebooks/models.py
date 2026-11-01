from django.conf import settings
from django.db import models
from django.utils.text import slugify

from encryption.crypto import decrypt_note_content, encrypt_note_content

NOTEBOOK_COLORS = [
    ('slate', 'Slate'),
    ('blue', 'Blue'),
    ('teal', 'Teal'),
    ('green', 'Green'),
    ('amber', 'Amber'),
    ('rose', 'Rose'),
    ('purple', 'Purple'),
    ('grey', 'Grey'),
]


def _unique_slug(base_qs, base_value, instance_pk=None):
    """Slugify base_value and make it unique within base_qs (which the
    caller has already scoped, e.g. to one owner or one notebook), skipping
    the instance's own current row on update.
    """
    slug = slugify(base_value) or 'untitled'
    candidate = slug
    qs = base_qs.exclude(pk=instance_pk) if instance_pk else base_qs
    suffix = 2
    while qs.filter(slug=candidate).exists():
        candidate = f'{slug}-{suffix}'
        suffix += 1
    return candidate


class LiveNotebookManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class LiveNoteManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(
            deleted_at__isnull=True, notebook__deleted_at__isnull=True
        )


class Notebook(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notebooks'
    )
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, blank=True)
    color = models.CharField(max_length=20, choices=NOTEBOOK_COLORS, default='slate')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = LiveNotebookManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['title']
        constraints = [
            models.UniqueConstraint(fields=['owner', 'slug'], name='unique_notebook_slug_per_owner'),
        ]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Slugs are generated once at creation and never regenerated on
        # rename, so links stay stable even after a notebook is retitled.
        if not self.slug:
            self.slug = _unique_slug(
                Notebook.all_objects.filter(owner=self.owner), self.title, self.pk
            )
        super().save(*args, **kwargs)


class Note(models.Model):
    notebook = models.ForeignKey(Notebook, on_delete=models.CASCADE, related_name='notes')
    title = models.CharField(max_length=200, blank=True, default='Untitled note')
    slug = models.SlugField(max_length=220, blank=True)
    # Fernet token text produced by encryption.crypto -- never plaintext.
    # Named `encrypted_content`, not `content`, so every call site is
    # unmistakable about what this column actually holds.
    encrypted_content = models.TextField(blank=True, default='')
    # Bumped on every save. The editor sends back the version it loaded, and
    # note_save refuses a save based on an older one -- otherwise a stale tab
    # or a leftover local draft silently overwrites newer content.
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = LiveNoteManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['notebook', 'slug'], name='unique_note_slug_per_notebook'),
        ]

    def __str__(self):
        return self.title

    @property
    def owner(self):
        return self.notebook.owner

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(
                Note.all_objects.filter(notebook=self.notebook), self.title, self.pk
            )
        super().save(*args, **kwargs)

    def get_content(self) -> str:
        """Decrypt and return this note's body. Does a DB hit (for the
        owner's wrapped key) plus a crypto op -- called out as a method,
        not a property, so that cost is visible at every call site.
        """
        return decrypt_note_content(self.owner, self.encrypted_content)

    def set_content(self, plaintext: str) -> None:
        """Encrypt `plaintext` and stage it on `encrypted_content`. Caller
        is still responsible for calling save().
        """
        self.encrypted_content = encrypt_note_content(self.owner, plaintext)


class NoteRevision(models.Model):
    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name='revisions')
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    encrypted_content = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.note} (v{self.version})'

    def get_content(self) -> str:
        return decrypt_note_content(self.note.owner, self.encrypted_content)
