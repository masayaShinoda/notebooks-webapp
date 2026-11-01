from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import resolve_url
from django.utils.cache import add_never_cache_headers, patch_vary_headers

from .views import htmx_not_found


class HtmxCacheMiddleware:
    """Keep htmx fragments out of the browser's HTTP cache.

    Views answer the same URL with a full page or, for HX-Request, a bare
    fragment, and hx-push-url puts that URL in history. Back/Forward and
    "reopen closed tab" load history entries from the HTTP cache, so without
    this the browser restores the fragment: no <head>, no CSS, plain text.

    Vary alone is not enough (Chrome still restores the fragment on Back), and
    an authenticated response carries decrypted note content that should not
    land in the browser's disk cache anyway -- so it is also no-store.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        patch_vary_headers(response, ('HX-Request',))
        if request.user.is_authenticated:
            add_never_cache_headers(response)
        return response


class HtmxErrorMiddleware:
    """Turn two failure shapes that are wrong for an htmx request into ones
    the client (static/js/app-errors.js) can act on.

    1. Expired session: @login_required answers with a 302 to the login page.
       XHR follows redirects transparently, so htmx would see a *200* login
       page -- a failed autosave would look successful and its local draft
       would be cleared. Answer 401 instead.
    2. Http404 (the item was deleted in another tab/device): htmx does not
       swap 4xx, so the click would silently do nothing. Answer with a
       fragment the client is told it may swap (X-Swap-Error).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.headers.get('HX-Request') == 'true'
            and response.status_code in (301, 302)
            and response.get('Location', '').startswith(resolve_url(settings.LOGIN_URL))
        ):
            return HttpResponse('Session expired', status=401, content_type='text/plain')
        return response

    def process_exception(self, request, exception):
        if not isinstance(exception, Http404) or request.headers.get('HX-Request') != 'true':
            return None
        match = request.resolver_match
        if match is None or match.namespace != 'notebooks':
            return None
        return htmx_not_found(request, match)
