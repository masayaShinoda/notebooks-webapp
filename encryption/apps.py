from django.apps import AppConfig


class EncryptionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'encryption'

    def ready(self):
        from . import signals  # noqa: F401
