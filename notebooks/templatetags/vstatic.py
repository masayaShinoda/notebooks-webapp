"""Cache-busting variant of ``{% static %}``.

``runserver`` serves ``STATICFILES_DIRS`` through ``django.views.static.serve``,
which sends only ``Last-Modified`` -- no ``Cache-Control``, no ``ETag``. With no
freshness directive the browser falls back to heuristic caching and reuses the
file *without revalidating*, so a rebuilt ``static/css/app.css`` silently keeps
rendering with the previous build's rules until a hard refresh.

Appending the file's mtime as ``?v=`` gives each build its own URL, so a normal
reload picks up the new bytes.

With DEBUG off the mtime comes from the collected copy under ``STATIC_ROOT`` --
the bytes nginx actually serves (``expires 1y``, ``immutable``) -- not from the
source in ``STATICFILES_DIRS``. Versioning by the source let a deploy that
pulled new code but skipped ``collectstatic`` hand out the *old* file under a
*new* URL, which browsers then cached as immutable: new templates and views
ran against old JavaScript even after ``collectstatic`` was run.
"""

import os

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

# Only populated when DEBUG is off: in development every rebuild must be seen,
# in production the mtime never changes for the life of the process.
_cache = {}


def _served_file(path):
    """The file a request for ``path`` gets: the collected copy in production,
    the source file under ``runserver``."""
    if not settings.DEBUG and settings.STATIC_ROOT:
        return os.path.join(settings.STATIC_ROOT, path)
    return finders.find(path)


def _version(path):
    """Return the int mtime of ``path``, or None when it can't be resolved."""
    try:
        absolute = _served_file(path)
    except Exception:
        return None
    if not absolute:
        return None
    if isinstance(absolute, (list, tuple)):
        absolute = absolute[0]
    try:
        return int(os.path.getmtime(absolute))
    except OSError:
        return None


@register.simple_tag
def vstatic(path):
    """``{% vstatic 'css/app.css' %}`` -> ``/static/css/app.css?v=1758391452``.

    Falls back to the plain static URL when the file isn't on disk -- the
    compiled stylesheet is a gitignored build artifact and may legitimately be
    missing before the first Tailwind run, which must not break rendering.
    """
    url = static(path)
    if settings.DEBUG:
        version = _version(path)
    else:
        if path not in _cache:
            _cache[path] = _version(path)
        version = _cache[path]
    if version is None:
        return url
    return f'{url}?v={version}'
