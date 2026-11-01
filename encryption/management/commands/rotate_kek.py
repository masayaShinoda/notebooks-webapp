from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from cryptography.fernet import InvalidToken

from encryption.crypto import unwrap_dek, wrap_dek
from encryption.models import UserEncryptionKey


class Command(BaseCommand):
    help = (
        'Rotate the master Key Encryption Key (KEK). Re-wraps every user\'s '
        'Data Encryption Key under a new KEK -- note content itself is never '
        'touched, so this is cheap regardless of how many notes exist.\n\n'
        'IMPORTANT: run this command BEFORE updating NOTES_KEK in .env. It '
        'needs the OLD key (currently in settings.NOTES_KEK/`.env`) to '
        'unwrap existing keys, then re-wraps them with the NEW key you pass '
        'in. Only after this command succeeds should you update `.env` to '
        'the new key and restart the app -- doing it in the other order '
        'makes every wrapped key unreadable.'
    )

    def add_arguments(self, parser):
        parser.add_argument('new_kek', help='The new Fernet key to rotate to.')

    def handle(self, *args, **options):
        new_kek = options['new_kek'].encode()

        with transaction.atomic():
            keys = UserEncryptionKey.objects.select_for_update()
            count = 0
            for key in keys:
                try:
                    dek = unwrap_dek(key.wrapped_dek)
                except InvalidToken as exc:
                    raise CommandError(
                        f'Could not unwrap key for user_id={key.user_id} with the '
                        'current NOTES_KEK. Aborting without changing anything.'
                    ) from exc
                key.wrapped_dek = wrap_dek(dek, kek=new_kek)
                key.rotated_at = timezone.now()
                key.save(update_fields=['wrapped_dek', 'rotated_at'])
                count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Rotated {count} user key(s). Now update NOTES_KEK in .env to the '
            'new key and restart the app.'
        ))
