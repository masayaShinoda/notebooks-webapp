from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .crypto import provision_user_encryption_key


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_encryption_key(sender, instance, created, **kwargs):
    """Every user needs a DEK the moment they exist, regardless of whether
    they came from self-registration, `createsuperuser`, or the admin.
    """
    if created and not hasattr(instance, 'encryption_key'):
        provision_user_encryption_key(instance)
