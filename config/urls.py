"""
URL configuration for config project.

See https://docs.djangoproject.com/en/6.0/topics/http/urls/
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.templatetags.static import static
from django.views.generic import RedirectView, TemplateView

urlpatterns = [
    # Mounted from settings so the admin can be moved off the default
    # /admin/ path in .env (see ADMIN_URL).
    path(settings.ADMIN_URL, admin.site.urls),
    # Served by Django rather than nginx so there is a single source of truth
    # and it behaves identically under `runserver`.
    path(
        'robots.txt',
        TemplateView.as_view(template_name='robots.txt', content_type='text/plain'),
        name='robots_txt',
    ),
    path('favicon.ico', RedirectView.as_view(url=static('favicons/favicon.ico'))),
    path('accounts/', include('accounts.urls')),
    path('', include('notebooks.urls')),
]

# 404/500 are served from templates/404.html and templates/500.html, which
# Django picks up automatically when DEBUG is off -- no handler404/handler500
# needed.
