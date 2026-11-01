from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


class RegistrationGatingTests(TestCase):
    @override_settings(ACCOUNT_SIGNUP_ENABLED=False)
    def test_registration_disabled_by_default(self):
        resp = self.client.get(reverse('accounts:register'))
        self.assertEqual(resp.status_code, 403)

        resp = self.client.post(reverse('accounts:register'), {
            'username': 'newuser', 'email': 'new@example.com',
            'password1': 'a-strong-password-123', 'password2': 'a-strong-password-123',
        })
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(User.objects.filter(username='newuser').exists())

    @override_settings(ACCOUNT_SIGNUP_ENABLED=True)
    def test_registration_enabled_allows_signup_and_provisions_key(self):
        resp = self.client.post(reverse('accounts:register'), {
            'username': 'newuser', 'email': 'new@example.com',
            'password1': 'a-strong-password-123', 'password2': 'a-strong-password-123',
        })
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username='newuser')
        self.assertTrue(hasattr(user, 'encryption_key'))

        # registering also logs the user in
        resp = self.client.get(reverse('notebooks:workspace'))
        self.assertEqual(resp.status_code, 200)
