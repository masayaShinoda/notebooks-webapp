from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from cryptography.fernet import Fernet, InvalidToken

from . import crypto
from .models import UserEncryptionKey


class EncryptionRoundTripTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw')

    def test_signal_provisions_key_on_user_creation(self):
        self.assertTrue(hasattr(self.user, 'encryption_key'))
        self.assertIsInstance(self.user.encryption_key, UserEncryptionKey)

    def test_round_trip_basic(self):
        token = crypto.encrypt_note_content(self.user, 'hello world')
        self.assertEqual(crypto.decrypt_note_content(self.user, token), 'hello world')

    def test_round_trip_empty_unicode_long(self):
        cases = [
            '',
            'héllo 日本語 emoji 🎉',
            'line1\nline2\r\nline3',
            'x' * 50_000,
        ]
        for plaintext in cases:
            token = crypto.encrypt_note_content(self.user, plaintext)
            self.assertEqual(crypto.decrypt_note_content(self.user, token), plaintext)

    def test_ciphertext_is_not_plaintext(self):
        token = crypto.encrypt_note_content(self.user, 'a secret note')
        self.assertNotIn('a secret note', token)

    def test_wrapped_dek_differs_from_raw_dek(self):
        dek = crypto.generate_dek()
        wrapped = crypto.wrap_dek(dek)
        self.assertNotEqual(wrapped.encode(), dek)

    def test_distinct_deks_per_user(self):
        bob = User.objects.create_user(username='bob', password='pw')
        alice_dek = crypto.unwrap_dek(self.user.encryption_key.wrapped_dek)
        bob_dek = crypto.unwrap_dek(bob.encryption_key.wrapped_dek)
        self.assertNotEqual(alice_dek, bob_dek)

    def test_user_cannot_decrypt_with_wrong_users_key(self):
        bob = User.objects.create_user(username='bob', password='pw')
        token = crypto.encrypt_note_content(self.user, 'alice only')
        with self.assertRaises(InvalidToken):
            crypto.decrypt_note_content(bob, token)

    def test_corrupted_wrapped_dek_raises_invalid_token(self):
        key = self.user.encryption_key
        key.wrapped_dek = Fernet.generate_key().decode()  # not a valid wrapped token
        key.save()
        with self.assertRaises(InvalidToken):
            crypto.decrypt_note_content(self.user, 'anything')

    @override_settings(NOTES_KEK=Fernet.generate_key().decode())
    def test_wrong_kek_raises_invalid_token(self):
        # settings.NOTES_KEK changed after the user's key was wrapped under
        # the original KEK -- decryption must fail loudly, not silently.
        with self.assertRaises(InvalidToken):
            crypto.decrypt_note_content(self.user, 'anything')


class RotateKekCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw')
        self.token = crypto.encrypt_note_content(self.user, 'rotate me')

    def test_rotate_kek_end_to_end(self):
        old_kek = crypto._kek()
        new_kek = Fernet.generate_key()

        call_command('rotate_kek', new_kek.decode())

        # old KEK no longer unwraps the (now re-wrapped) key
        self.user.refresh_from_db()
        with self.assertRaises(InvalidToken):
            crypto.unwrap_dek(self.user.encryption_key.wrapped_dek, kek=old_kek)

        # new KEK does, and the note content is still readable through it
        dek = crypto.unwrap_dek(self.user.encryption_key.wrapped_dek, kek=new_kek)
        self.assertTrue(dek)
        with override_settings(NOTES_KEK=new_kek.decode()):
            self.assertEqual(crypto.decrypt_note_content(self.user, self.token), 'rotate me')

        self.assertIsNotNone(self.user.encryption_key.rotated_at)
