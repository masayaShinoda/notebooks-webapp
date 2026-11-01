"""Server-side markdown rendering, used in exactly two places: the stateless
live-preview endpoint (raw POSTed text, no DB) and note_detail (rendering
just-decrypted stored content). One function, one sanitization policy.
"""

import markdown
import nh3

_EXTENSIONS = ['fenced_code', 'tables', 'sane_lists', 'nl2br', 'codehilite']
_EXTENSION_CONFIGS = {'codehilite': {'guess_lang': False}}

_ALLOWED_TAGS = {
    'p', 'br', 'strong', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'span', 'div', 'a',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'img',
}

# codehilite emits `class="..."` on its highlighting spans/divs for Pygments
# CSS to hook into. nh3 strips `class` by default -- without this explicit
# allowlist, every code block would silently render unstyled after
# sanitization (see notebooks/tests.py for a regression test).
_ALLOWED_ATTRIBUTES = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title'},
    'span': {'class'},
    'code': {'class'},
    'div': {'class'},
    'pre': {'class'},
    'th': {'align'},
    'td': {'align'},
}


def render_markdown(raw_text: str) -> str:
    html = markdown.markdown(
        raw_text or '',
        extensions=_EXTENSIONS,
        extension_configs=_EXTENSION_CONFIGS,
    )
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        link_rel='nofollow noopener noreferrer',
    )
