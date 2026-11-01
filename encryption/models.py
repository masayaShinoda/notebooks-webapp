from django.conf import settings
from django.db import models


class UserEncryptionKey(models.Model):
    """Holds one user's Data Encryption Key (DEK), wrapped by the server's
    master Key Encryption Key (KEK, settings.NOTES_KEK). The DEK itself never
    touches the database in plaintext -- see encryption/crypto.py.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='encryption_key',
    )
    wrapped_dek = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'encryption key for {self.user}'
