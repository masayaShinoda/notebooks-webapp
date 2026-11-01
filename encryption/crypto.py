"""Envelope encryption for note content.

Each user has a random Data Encryption Key (DEK). The DEK is stored in the
database only in "wrapped" form -- encrypted with a single master Key
Encryption Key (KEK) that lives outside the database, in settings.NOTES_KEK
(loaded from .env). Note content is encrypted/decrypted with the user's own
DEK, never directly with the KEK.

This is explicit-call encryption, not a transparent Django model field:
per-user keys don't fit Field.from_db_value/to_python, which never receive
the model instance (and therefore never know whose key to use).
"""

from django.conf import settings

from cryptography.fernet import Fernet

from .models import UserEncryptionKey


def _kek() -> bytes:
    return settings.NOTES_KEK.encode()


def generate_dek() -> bytes:
    return Fernet.generate_key()


def wrap_dek(dek: bytes, kek: bytes | None = None) -> str:
    """Encrypt a DEK with a KEK for storage. `kek` defaults to the current
    settings.NOTES_KEK but can be passed explicitly during key rotation.
    """
    kek = kek if kek is not None else _kek()
    return Fernet(kek).encrypt(dek).decode()


def unwrap_dek(wrapped: str, kek: bytes | None = None) -> bytes:
    kek = kek if kek is not None else _kek()
    return Fernet(kek).decrypt(wrapped.encode())


def get_user_fernet(user) -> Fernet:
    dek = unwrap_dek(user.encryption_key.wrapped_dek)
    return Fernet(dek)


def encrypt_note_content(user, plaintext: str) -> str:
    return get_user_fernet(user).encrypt(plaintext.encode()).decode()


def decrypt_note_content(user, token: str) -> str:
    if not token:
        return ''
    return get_user_fernet(user).decrypt(token.encode()).decode()


def provision_user_encryption_key(user) -> UserEncryptionKey:
    """Create a UserEncryptionKey for a user who doesn't have one yet."""
    dek = generate_dek()
    return UserEncryptionKey.objects.create(user=user, wrapped_dek=wrap_dek(dek))
