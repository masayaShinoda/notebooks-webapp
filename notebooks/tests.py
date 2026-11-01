import json
import os
import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .markdown import render_markdown
from .models import Note, Notebook, NoteRevision


class SlugUniquenessTests(TestCase):
    def test_notebook_slug_unique_per_owner_not_global(self):
        # Regression test for the old app's exact bug: title/slug were
        # globally unique, which breaks the moment a second user exists.
        alice = User.objects.create_user(username='alice', password='pw')
        bob = User.objects.create_user(username='bob', password='pw')

        nb1 = Notebook.objects.create(owner=alice, title='Personal')
        nb2 = Notebook.objects.create(owner=bob, title='Personal')

        self.assertEqual(nb1.slug, 'personal')
        self.assertEqual(nb2.slug, 'personal')

    def test_notebook_slug_collision_within_owner_gets_suffixed(self):
        alice = User.objects.create_user(username='alice', password='pw')
        nb1 = Notebook.objects.create(owner=alice, title='Personal')
        nb2 = Notebook.objects.create(owner=alice, title='Personal')
        self.assertEqual(nb1.slug, 'personal')
        self.assertEqual(nb2.slug, 'personal-2')

    def test_note_slug_unique_per_notebook(self):
        alice = User.objects.create_user(username='alice', password='pw')
        nb = Notebook.objects.create(owner=alice, title='Notebook')
        note1 = Note.objects.create(notebook=nb, title='First note')
        note2 = Note.objects.create(notebook=nb, title='First note')
        self.assertEqual(note1.slug, 'first-note')
        self.assertEqual(note2.slug, 'first-note-2')

    def test_note_slug_scope_is_per_notebook_not_global(self):
        alice = User.objects.create_user(username='alice', password='pw')
        nb1 = Notebook.objects.create(owner=alice, title='NB1')
        nb2 = Notebook.objects.create(owner=alice, title='NB2')
        note1 = Note.objects.create(notebook=nb1, title='Same title')
        note2 = Note.objects.create(notebook=nb2, title='Same title')
        self.assertEqual(note1.slug, 'same-title')
        self.assertEqual(note2.slug, 'same-title')


class PerUserIsolationTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Alice notebook')
        self.note = Note(notebook=self.notebook, title='Alice note')
        self.note.set_content('alice secret')
        self.note.save()

    def test_owner_can_access_their_own_notebook_and_note(self):
        self.client.login(username='alice', password='pw')
        resp = self.client.get(reverse('notebooks:notebook_detail', args=[self.notebook.id]))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get(reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id]))
        self.assertEqual(resp.status_code, 200)

    def test_other_user_gets_404_not_403(self):
        self.client.login(username='bob', password='pw')
        urls = [
            reverse('notebooks:notebook_detail', args=[self.notebook.id]),
            reverse('notebooks:notebook_update', args=[self.notebook.id]),
            reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 404, url)

    def test_other_user_cannot_save_or_delete(self):
        self.client.login(username='bob', password='pw')
        resp = self.client.post(
            reverse('notebooks:note_save', args=[self.notebook.id, self.note.id]),
            {'title': 'hacked', 'body': 'hacked'},
        )
        self.assertEqual(resp.status_code, 404)
        self.note.refresh_from_db()
        self.assertEqual(self.note.title, 'Alice note')
        self.assertEqual(self.note.get_content(), 'alice secret')

        resp = self.client.post(reverse('notebooks:note_delete', args=[self.notebook.id, self.note.id]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Note.objects.filter(pk=self.note.pk).exists())

        resp = self.client.post(reverse('notebooks:notebook_delete', args=[self.notebook.id]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Notebook.objects.filter(pk=self.notebook.pk).exists())

    def test_unauthenticated_redirects_to_login(self):
        urls = [
            reverse('notebooks:workspace'),
            reverse('notebooks:notebook_detail', args=[self.notebook.id]),
            reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id]),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn(reverse('accounts:login'), resp.headers['Location'])


class NoteEncryptionViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Notebook')
        self.client.login(username='alice', password='pw')

    def test_note_save_encrypts_content_not_plaintext_in_db(self):
        note = Note.objects.create(notebook=self.notebook, title='Untitled note')
        secret = 'this must never be stored in plaintext'
        self.client.post(
            reverse('notebooks:note_save', args=[self.notebook.id, note.id]),
            {'title': 'My note', 'body': secret, 'base_version': note.version},
        )
        note.refresh_from_db()
        self.assertNotIn(secret, note.encrypted_content)
        self.assertEqual(note.get_content(), secret)
        self.assertEqual(note.title, 'My note')

    def test_note_preview_never_touches_db(self):
        note = Note.objects.create(notebook=self.notebook, title='Untitled note')
        note.set_content('original')
        note.save()
        updated_at_before = note.updated_at
        count_before = Note.objects.count()

        resp = self.client.post(
            reverse('notebooks:note_preview', args=[self.notebook.id, note.id]),
            {'body': '# Some heading\n\nUnsaved text'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Some heading', resp.content)

        note.refresh_from_db()
        self.assertEqual(note.updated_at, updated_at_before)
        self.assertEqual(note.get_content(), 'original')
        self.assertEqual(Note.objects.count(), count_before)


class HtmxErrorHandlingTests(TestCase):
    """Server half of the client-side failure handling in static/js/app-errors.js
    and offline-notes.js: every failure an htmx request can hit must come back
    as a status the client can tell apart from success.
    """

    HX = {'HTTP_HX_REQUEST': 'true'}

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Alice notebook')
        self.note = Note(notebook=self.notebook, title='Alice note')
        self.note.set_content('original')
        self.note.save()
        self.save_url = reverse('notebooks:note_save', args=[self.notebook.id, self.note.id])

    def test_expired_session_htmx_save_is_401_not_a_followed_redirect(self):
        # A redirect would be followed by XHR into a 200 login page, and the
        # client would clear its local draft as if the save had succeeded.
        resp = self.client.post(self.save_url, {'title': 't', 'body': 'b'}, **self.HX)
        self.assertEqual(resp.status_code, 401)

    def test_expired_session_plain_request_still_redirects_to_login(self):
        resp = self.client.post(self.save_url, {'title': 't', 'body': 'b'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('accounts:login'), resp['Location'])

    def test_rejected_save_is_422_and_does_not_write(self):
        self.client.login(username='alice', password='pw')
        resp = self.client.post(self.save_url, {'title': 'x' * 201, 'body': 'changed'}, **self.HX)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp['X-Swap-Error'], 'true')
        self.assertContains(resp, 'Not saved', status_code=422)
        self.note.refresh_from_db()
        self.assertEqual(self.note.get_content(), 'original')

    def test_saving_a_deleted_note_reports_it_without_replacing_the_editor(self):
        self.client.login(username='alice', password='pw')
        self.note.delete()
        resp = self.client.post(self.save_url, {'title': 't', 'body': 'b'}, **self.HX)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp['X-Swap-Error'], 'true')
        self.assertNotIn('HX-Retarget', resp)
        self.assertContains(resp, 'deleted elsewhere', status_code=404)

    def test_opening_a_deleted_note_shows_gone_message_and_refreshes_panes(self):
        self.client.login(username='alice', password='pw')
        url = reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id])
        self.note.delete()
        resp = self.client.get(url, **self.HX)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp['X-Swap-Error'], 'true')
        self.assertEqual(resp['HX-Retarget'], '#editor-pane')
        self.assertEqual(resp['HX-Push-Url'], reverse('notebooks:notebook_detail', args=[self.notebook.id]))
        self.assertContains(resp, 'This note no longer exists', status_code=404)
        self.assertContains(resp, 'id="notes-pane" hx-swap-oob', status_code=404)

    def test_opening_a_deleted_notebook_says_notebook(self):
        self.client.login(username='alice', password='pw')
        url = reverse('notebooks:notebook_detail', args=[self.notebook.id])
        self.notebook.delete()
        resp = self.client.get(url, **self.HX)
        self.assertContains(resp, 'This notebook no longer exists', status_code=404)
        self.assertEqual(resp['HX-Push-Url'], reverse('notebooks:workspace'))

    def test_other_users_note_gets_the_same_gone_message_and_no_data(self):
        self.client.login(username='bob', password='pw')
        url = reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id])
        resp = self.client.get(url, **self.HX)
        self.assertContains(resp, 'This notebook no longer exists', status_code=404)
        self.assertNotContains(resp, 'Alice', status_code=404)

    def test_preview_404_is_silent(self):
        self.client.login(username='alice', password='pw')
        url = reverse('notebooks:note_preview', args=[self.notebook.id, self.note.id])
        self.notebook.delete()
        resp = self.client.post(url, {'body': 'x'}, **self.HX)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('X-Swap-Error', resp)

    def test_plain_404_is_unchanged(self):
        self.client.login(username='alice', password='pw')
        resp = self.client.get(reverse('notebooks:note_detail', args=[self.notebook.id, 999999]))
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('X-Swap-Error', resp)

    def test_undecryptable_note_renders_notice_without_editor(self):
        # The editor's autosave would overwrite the (possibly recoverable)
        # ciphertext, so it must not be rendered at all.
        self.client.login(username='alice', password='pw')
        Note.objects.filter(pk=self.note.pk).update(encrypted_content='not-a-fernet-token')
        url = reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id])
        for headers in ({}, self.HX):
            with self.assertLogs('notebooks.views', level='ERROR'):
                resp = self.client.get(url, **headers)
            self.assertContains(resp, "This note can't be decrypted")
            self.assertNotContains(resp, 'note-editor-form')


class HtmxCacheHeaderTests(TestCase):
    """Back/Forward must never restore a cached htmx fragment in place of the
    full page served at the same URL (see HtmxCacheMiddleware).
    """

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Alice notebook')
        self.url = reverse('notebooks:notebook_detail', args=[self.notebook.id])

    def test_authenticated_page_and_fragment_are_vary_and_no_store(self):
        self.client.force_login(self.alice)
        for headers in ({}, {'HTTP_HX_REQUEST': 'true'}):
            resp = self.client.get(self.url, **headers)
            self.assertEqual(resp.status_code, 200)
            self.assertIn('HX-Request', resp['Vary'])
            self.assertIn('no-store', resp['Cache-Control'])

    def test_anonymous_response_still_varies_on_hx_request(self):
        resp = self.client.get(reverse('accounts:login'))
        self.assertIn('HX-Request', resp['Vary'])


class StaleSaveTests(TestCase):
    """A save based on an older copy of a note -- a tab left open for days, a
    restored tab, a leftover local draft -- must never overwrite newer content
    (see note_save and static/js/offline-notes.js).
    """

    HX = {'HTTP_HX_REQUEST': 'true'}

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Notebook')
        self.note = Note(notebook=self.notebook, title='Note')
        self.note.set_content('v1 text')
        self.note.save()
        self.save_url = reverse('notebooks:note_save', args=[self.notebook.id, self.note.id])
        self.version_url = reverse('notebooks:note_version', args=[self.notebook.id, self.note.id])
        self.detail_url = reverse('notebooks:note_detail', args=[self.notebook.id, self.note.id])
        self.client.login(username='alice', password='pw')

    def save(self, body, base_version, **extra):
        data = {'title': 'Note', 'body': body}
        if base_version is not None:
            data['base_version'] = base_version
        return self.client.post(self.save_url, data, **self.HX, **extra)

    def test_save_at_current_version_writes_and_bumps_version(self):
        resp = self.save('v2 text', 1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['X-Note-Version'], '2')
        self.note.refresh_from_db()
        self.assertEqual(self.note.version, 2)
        self.assertEqual(self.note.get_content(), 'v2 text')

    def test_save_refreshes_the_editor_updated_label(self):
        Note.objects.filter(pk=self.note.pk).update(updated_at=timezone.now() - timedelta(days=3))
        self.assertContains(self.client.get(self.detail_url, **self.HX), '3\xa0days ago')
        resp = self.save('v2 text', 1)
        self.assertContains(resp, '<span id="note-updated" hx-swap-oob="true" class="max-lg:hidden">Updated just now</span>', html=True)

    def test_save_moves_the_row_to_the_top_of_its_notebook_list(self):
        resp = self.save('v2 text', 1)
        self.assertContains(resp, f'<div id="note-row-{self.note.id}" hx-swap-oob="delete"></div>', html=True)
        self.assertContains(resp, f'hx-swap-oob="afterbegin:#notes-list-{self.notebook.id}"')
        self.assertContains(self.client.get(self.detail_url, **self.HX), f'id="notes-list-{self.notebook.id}"')

    def test_save_based_on_older_version_is_409_and_does_not_write(self):
        self.save('newer text from another device', 1)
        resp = self.save('stale text from an old tab', 1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp['X-Swap-Error'], 'true')
        self.assertEqual(resp['X-Note-Version'], '2')
        self.assertContains(resp, 'changed elsewhere', status_code=409)
        self.note.refresh_from_db()
        self.assertEqual(self.note.version, 2)
        self.assertEqual(self.note.get_content(), 'newer text from another device')

    def test_save_without_base_version_is_a_conflict(self):
        # A page cached from before base_version existed, or a legacy draft.
        resp = self.save('stale', None)
        self.assertEqual(resp.status_code, 409)
        self.note.refresh_from_db()
        self.assertEqual(self.note.get_content(), 'v1 text')

    def test_keep_mine_overwrites_by_resending_at_the_server_version(self):
        self.save('other device', 1)
        self.assertEqual(self.save('mine', 1).status_code, 409)
        self.assertEqual(self.save('mine', 2).status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.get_content(), 'mine')
        self.assertEqual(self.note.version, 3)

    def test_plain_post_conflict_is_409_without_writing(self):
        self.save('other device', 1)
        resp = self.client.post(self.save_url, {'title': 'Note', 'body': 'stale', 'base_version': 1})
        self.assertEqual(resp.status_code, 409)
        self.note.refresh_from_db()
        self.assertEqual(self.note.get_content(), 'other device')

    def test_editor_carries_the_loaded_version(self):
        self.save('v2', 1)
        resp = self.client.get(self.detail_url, **self.HX)
        self.assertContains(resp, 'name="base_version" value="2"')
        self.assertContains(resp, f'data-version-url="{self.version_url}"')

    def test_version_endpoint(self):
        resp = self.client.get(self.version_url, **self.HX)
        self.assertEqual(resp.json(), {'version': 1})

    def test_version_endpoint_is_owner_scoped_and_silent(self):
        self.client.login(username='bob', password='pw')
        resp = self.client.get(self.version_url, **self.HX)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.content, b'')

    def test_background_editor_refresh_does_not_move_the_mobile_pane(self):
        resp = self.client.get(self.detail_url, **self.HX)
        self.assertIn('HX-Trigger', resp)
        resp = self.client.get(self.detail_url, HTTP_X_EDITOR_REFRESH='true', **self.HX)
        self.assertNotIn('HX-Trigger', resp)

    def test_editor_and_version_check_are_never_cached(self):
        # HtmxCacheMiddleware: a cached editor would hold an old copy of the note.
        for url in (reverse('notebooks:workspace'), self.detail_url, self.version_url):
            for extra in ({}, self.HX):
                resp = self.client.get(url, **extra)
                self.assertIn('no-store', resp['Cache-Control'], (url, extra))


class MarkdownRenderingTests(TestCase):
    def test_basic_prose_renders(self):
        html = render_markdown('# Title\n\nSome **bold** text.')
        self.assertIn('<h1>Title</h1>', html)
        self.assertIn('<strong>bold</strong>', html)

    def test_script_tag_is_stripped(self):
        html = render_markdown("<script>alert('xss')</script>Hello")
        self.assertNotIn('<script', html)
        self.assertIn('Hello', html)

    def test_javascript_href_is_stripped(self):
        html = render_markdown('[click me](javascript:alert(1))')
        self.assertNotIn('javascript:', html)

    def test_onerror_attribute_is_stripped(self):
        html = render_markdown('<img src=x onerror="alert(1)">')
        self.assertNotIn('onerror', html)

    def test_codehilite_class_survives_sanitization(self):
        # Regression test for the specific gotcha: nh3 strips `class` by
        # default, which would silently break all code-block styling.
        html = render_markdown('```python\nprint(1)\n```')
        self.assertIn('class=', html)
        self.assertIn('codehilite', html)

    def test_fenced_code_tables_lists_render(self):
        html = render_markdown('- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n')
        self.assertIn('<li>one</li>', html)
        self.assertIn('<table>', html)


class WorkspaceAccessibilityTests(TestCase):
    """Guards the accessibility markup added in the interactive-element pass:
    a labelled color radio group, a named "new notebook" control, a skip link.
    """

    def setUp(self):
        User.objects.create_user(username='alice', password='pw')
        self.client.login(username='alice', password='pw')

    def test_new_notebook_form_labels_the_color_radio_group(self):
        resp = self.client.get(reverse('notebooks:notebook_create'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('<fieldset', body)
        self.assertIn('<legend', body)
        # RadioSelect emits real radio inputs, not a <select>.
        self.assertIn('type="radio"', body)
        self.assertNotIn('<select', body)

    def test_add_notebook_control_has_accessible_name(self):
        resp = self.client.get(reverse('notebooks:workspace'))
        self.assertContains(resp, 'aria-label="New notebook"')

    def test_base_template_has_skip_link_and_main_landmark(self):
        resp = self.client.get(reverse('notebooks:workspace'))
        self.assertContains(resp, 'href="#main"')
        self.assertContains(resp, 'id="main"')

    def test_invalid_title_marks_the_field_and_names_the_error(self):
        resp = self.client.post(reverse('notebooks:notebook_create'), {'title': '', 'color': 'slate'})
        body = resp.content.decode()
        self.assertIn('aria-invalid="true"', body)
        self.assertIn('id="id_title_error"', body)
        self.assertIn('aria-describedby="id_title_error"', body)


class VStaticTests(TestCase):
    """The stale-asset regression: a rebuilt file must get a new URL."""

    def _render(self, path):
        return Template(
            "{% load vstatic %}{% vstatic path %}"
        ).render(Context({'path': path}))

    def test_existing_file_gets_mtime_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'probe.css')
            with open(target, 'w') as fh:
                fh.write('body{}')
            with override_settings(STATICFILES_DIRS=[tmp], DEBUG=True):
                url = self._render('probe.css')

        self.assertIn('?v=', url)
        self.assertTrue(url.split('?v=')[1].isdigit())

    def test_missing_file_falls_back_to_plain_url(self):
        # app.css is a gitignored build artifact -- a missing file must render,
        # not raise.
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(STATICFILES_DIRS=[tmp], DEBUG=True):
                url = self._render('css/not-built-yet.css')

        self.assertNotIn('?v=', url)
        self.assertTrue(url.endswith('css/not-built-yet.css'))

    def test_version_changes_when_file_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'probe.css')
            with open(target, 'w') as fh:
                fh.write('body{}')

            with override_settings(STATICFILES_DIRS=[tmp], DEBUG=True):
                before = self._render('probe.css')
                # Simulate a Tailwind rebuild bumping the mtime.
                bumped = os.path.getmtime(target) + 120
                os.utime(target, (bumped, bumped))
                after = self._render('probe.css')

        self.assertNotEqual(before, after)

    def test_production_versions_the_collected_copy_not_the_source(self):
        # A deploy that pulls new code but skips collectstatic must not serve
        # the old collected file under the new source file's URL -- nginx marks
        # /static/ immutable, so browsers would keep the old bytes for good.
        from notebooks.templatetags import vstatic

        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as root:
            for base, mtime in ((src, 2_000_000_000), (root, 1_000_000_000)):
                target = os.path.join(base, 'probe.js')
                with open(target, 'w') as fh:
                    fh.write('')
                os.utime(target, (mtime, mtime))

            vstatic._cache.clear()
            try:
                with override_settings(STATICFILES_DIRS=[src], STATIC_ROOT=root, DEBUG=False):
                    url = self._render('probe.js')
            finally:
                vstatic._cache.clear()

        self.assertTrue(url.endswith('probe.js?v=1000000000'))


class ProductionHardeningTests(TestCase):
    """Deployment-facing behaviour that only shows up with DEBUG off."""

    def test_robots_txt_is_served_as_plain_text_and_disallows_everything(self):
        response = self.client.get('/robots.txt')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/plain')
        body = response.content.decode()
        self.assertIn('User-agent: *', body)
        self.assertIn('Disallow: /', body)

    def test_robots_txt_names_ai_crawlers_explicitly(self):
        # A wildcard block is not enough: several AI crawlers have treated
        # `User-agent: *` as not applying to them.
        body = self.client.get('/robots.txt').content.decode()

        for agent in ('GPTBot', 'ClaudeBot', 'CCBot', 'Google-Extended'):
            self.assertIn(f'User-agent: {agent}', body)

    def test_base_template_emits_noindex_robots_meta(self):
        User.objects.create_user(username='meta-user', password='pw')
        self.client.login(username='meta-user', password='pw')

        body = self.client.get(reverse('notebooks:workspace')).content.decode()

        self.assertIn('name="robots"', body)
        self.assertIn('noindex', body)

    def test_missing_url_renders_the_custom_404_page(self):
        # The custom template is only used when DEBUG is off; with DEBUG on
        # Django serves its own debug 404 instead.
        with self.settings(DEBUG=False):
            response = self.client.get('/no-such-page/')

        self.assertEqual(response.status_code, 404)
        body = response.content.decode()
        self.assertIn('Error 404: Not Found', body)
        self.assertIn('Back to home', body)
        self.assertIn(reverse('notebooks:workspace'), body)

    def test_cross_user_404_uses_the_custom_page_too(self):
        # Ownership checks 404 rather than 403 so another user's data is never
        # revealed to exist -- that makes this a routinely reachable page.
        owner = User.objects.create_user(username='owner-404', password='pw')
        other = User.objects.create_user(username='other-404', password='pw')
        notebook = Notebook.objects.create(owner=owner, title='Private')

        self.client.login(username=other.username, password='pw')
        with self.settings(DEBUG=False):
            response = self.client.get(
                reverse('notebooks:notebook_detail', args=[notebook.id])
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn('Error 404: Not Found', response.content.decode())


class NoteRevisionTests(TestCase):
    """Version history: note_save snapshots the pre-save state at most once
    per revision window (or always, when forced), capped per note, and a
    restore never destroys the version it replaces.
    """

    HX = {'HTTP_HX_REQUEST': 'true'}

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Notebook')
        self.note = Note(notebook=self.notebook, title='Note')
        self.note.set_content('v1 text')
        self.note.save()
        self.save_url = reverse('notebooks:note_save', args=[self.notebook.id, self.note.id])
        self.history_url = reverse('notebooks:note_history', args=[self.notebook.id, self.note.id])
        self.client.login(username='alice', password='pw')

    def save(self, body, force=False):
        self.note.refresh_from_db()
        data = {'title': 'Note', 'body': body, 'base_version': self.note.version}
        if force:
            data['force_snapshot'] = '1'
        resp = self.client.post(self.save_url, data, **self.HX)
        self.assertEqual(resp.status_code, 200)
        return resp

    def contents(self):
        return [r.get_content() for r in self.note.revisions.all()]

    def test_save_snapshots_the_pre_save_state(self):
        self.save('v2 text')
        revision = self.note.revisions.get()
        self.assertEqual(revision.get_content(), 'v1 text')
        self.assertEqual(revision.version, 1)
        self.assertEqual(revision.title, 'Note')
        self.assertNotIn('v1 text', revision.encrypted_content)

    def test_empty_note_is_not_snapshotted(self):
        note = Note(notebook=self.notebook, title='Untitled note')
        note.set_content('')
        note.save()
        url = reverse('notebooks:note_save', args=[self.notebook.id, note.id])
        self.client.post(url, {'title': 'x', 'body': 'first words', 'base_version': 1}, **self.HX)
        self.assertFalse(note.revisions.exists())

    def test_saves_inside_the_window_share_one_snapshot(self):
        self.save('v2 text')
        self.save('v3 text')
        self.save('v4 text')
        self.assertEqual(self.contents(), ['v1 text'])

    def test_save_after_the_window_snapshots_again(self):
        self.save('v2 text')
        NoteRevision.objects.update(created_at=timezone.now() - timedelta(minutes=11))
        self.save('v3 text')
        self.assertEqual(self.contents(), ['v2 text', 'v1 text'])

    def test_forced_snapshot_ignores_the_window(self):
        self.save('v2 text')
        self.save('v3 text', force=True)
        self.assertEqual(self.contents(), ['v2 text', 'v1 text'])

    def test_copy_of_newest_revision_is_skipped(self):
        self.save('v1 text')  # same content: snapshot of v1, note unchanged
        self.save('v2 text', force=True)
        self.assertEqual(self.contents(), ['v1 text'])

    @override_settings(NOTE_REVISION_LIMIT=3)
    def test_oldest_revisions_are_pruned_past_the_cap(self):
        for i in range(2, 7):
            self.save(f'v{i} text', force=True)
        self.assertEqual(self.contents(), ['v5 text', 'v4 text', 'v3 text'])

    def test_restore_is_non_destructive_and_bumps_the_version(self):
        self.save('v2 text')
        revision = self.note.revisions.get()
        url = reverse('notebooks:note_restore', args=[self.notebook.id, self.note.id, revision.id])
        resp = self.client.post(url, **self.HX)
        self.assertEqual(resp.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.get_content(), 'v1 text')
        self.assertEqual(self.note.version, 3)
        self.assertEqual(self.contents(), ['v2 text', 'v1 text'])
        # An editor still open at version 2 must not overwrite the restore.
        resp = self.client.post(self.save_url, {'title': 'Note', 'body': 'stale', 'base_version': 2}, **self.HX)
        self.assertEqual(resp.status_code, 409)

    def test_history_renders_list_and_revision_preview(self):
        self.save('v2 text')
        revision = self.note.revisions.get()
        resp = self.client.get(self.history_url, **self.HX)
        self.assertContains(resp, 'Current version')
        self.assertContains(resp, 'v2 text')
        resp = self.client.get(
            reverse('notebooks:note_revision', args=[self.notebook.id, self.note.id, revision.id]), **self.HX,
        )
        self.assertContains(resp, 'v1 text')
        self.assertContains(resp, 'Restore this version')

    def test_history_is_owner_scoped(self):
        self.save('v2 text')
        revision = self.note.revisions.get()
        self.client.login(username='bob', password='pw')
        revision_url = reverse('notebooks:note_revision', args=[self.notebook.id, self.note.id, revision.id])
        restore_url = reverse('notebooks:note_restore', args=[self.notebook.id, self.note.id, revision.id])
        self.assertEqual(self.client.get(self.history_url).status_code, 404)
        self.assertEqual(self.client.get(revision_url).status_code, 404)
        self.assertEqual(self.client.post(restore_url).status_code, 404)

    def test_revision_of_another_note_is_404(self):
        self.save('v2 text')
        revision = self.note.revisions.get()
        other = Note(notebook=self.notebook, title='Other')
        other.set_content('other')
        other.save()
        url = reverse('notebooks:note_restore', args=[self.notebook.id, other.id, revision.id])
        self.assertEqual(self.client.post(url).status_code, 404)
        other.refresh_from_db()
        self.assertEqual(other.get_content(), 'other')


class TrashTests(TestCase):
    """Deleting moves to the trash; trashed items behave as gone everywhere
    else, and only their owner can restore or delete them forever.
    """

    HX = {'HTTP_HX_REQUEST': 'true'}

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        self.notebook = Notebook.objects.create(owner=self.alice, title='Recipes')
        self.note = Note(notebook=self.notebook, title='Soup')
        self.note.set_content('soup text')
        self.note.save()
        self.client.login(username='alice', password='pw')

    def delete_note(self, note=None):
        note = note or self.note
        return self.client.post(reverse('notebooks:note_delete', args=[self.notebook.id, note.id]), **self.HX)

    def test_deleting_a_note_trashes_it_and_offers_undo(self):
        resp = self.delete_note()
        self.assertEqual(resp.status_code, 200)
        toast = json.loads(resp['HX-Trigger'])['toast']
        self.assertEqual(toast['undoUrl'], reverse('notebooks:trash_restore_note', args=[self.note.id]))
        self.assertFalse(Note.objects.filter(pk=self.note.pk).exists())
        self.assertIsNotNone(Note.all_objects.get(pk=self.note.pk).deleted_at)

    def test_trashed_note_is_404_outside_the_trash(self):
        self.delete_note()
        args = [self.notebook.id, self.note.id]
        self.assertEqual(self.client.get(reverse('notebooks:note_detail', args=args)).status_code, 404)
        self.assertEqual(self.client.get(reverse('notebooks:note_version', args=args)).status_code, 404)
        self.assertEqual(self.client.get(reverse('notebooks:note_history', args=args)).status_code, 404)
        resp = self.client.post(reverse('notebooks:note_preview', args=args), {'body': 'x'})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(
            reverse('notebooks:note_save', args=args), {'title': 'Soup', 'body': 'x', 'base_version': 1},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Note.all_objects.get(pk=self.note.pk).get_content(), 'soup text')

    def test_restoring_a_note(self):
        self.delete_note()
        resp = self.client.post(reverse('notebooks:trash_restore_note', args=[self.note.id]), **self.HX)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'soup text')
        self.assertTrue(Note.objects.filter(pk=self.note.pk).exists())

    def test_trashing_a_notebook_hides_its_notes_and_restores_them(self):
        kept = Note(notebook=self.notebook, title='Stew')
        kept.set_content('')
        kept.save()
        self.delete_note(kept)  # trashed on its own first
        resp = self.client.post(reverse('notebooks:notebook_delete', args=[self.notebook.id]), **self.HX)
        self.assertIn('toast', json.loads(resp['HX-Trigger']))
        self.assertFalse(Note.objects.filter(notebook_id=self.notebook.id).exists())
        resp = self.client.get(reverse('notebooks:notebook_detail', args=[self.notebook.id]))
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get(reverse('notebooks:trash'))
        self.assertContains(resp, 'Recipes')
        self.assertContains(resp, '1 note ')
        self.assertNotContains(resp, 'Stew')  # comes back with its notebook, not on its own

        self.client.post(reverse('notebooks:trash_restore_notebook', args=[self.notebook.id]), {'next': 'trash'})
        self.assertTrue(Note.objects.filter(pk=self.note.pk).exists())
        self.assertFalse(Note.objects.filter(pk=kept.pk).exists())

    def test_note_inside_a_trashed_notebook_cannot_be_restored_alone(self):
        self.delete_note()
        self.client.post(reverse('notebooks:notebook_delete', args=[self.notebook.id]))
        resp = self.client.post(reverse('notebooks:trash_restore_note', args=[self.note.id]))
        self.assertEqual(resp.status_code, 404)

    def test_trashed_slug_stays_reserved(self):
        self.delete_note()
        resp = self.client.post(reverse('notebooks:note_create', args=[self.notebook.id]))
        self.assertEqual(resp.status_code, 200)
        clone = Note(notebook=self.notebook, title='Soup')
        clone.set_content('')
        clone.save()
        self.assertNotEqual(clone.slug, self.note.slug)

    def test_delete_forever_and_empty_trash(self):
        self.client.post(
            reverse('notebooks:note_save', args=[self.notebook.id, self.note.id]),
            {'title': 'Soup', 'body': 'v2', 'base_version': 1},
        )
        self.assertTrue(NoteRevision.objects.exists())
        self.delete_note()
        self.client.post(reverse('notebooks:trash_delete_note', args=[self.note.id]))
        self.assertFalse(Note.all_objects.filter(pk=self.note.pk).exists())
        self.assertFalse(NoteRevision.objects.exists())

        other = Notebook.objects.create(owner=self.alice, title='Work')
        self.client.post(reverse('notebooks:notebook_delete', args=[other.id]))
        self.client.post(reverse('notebooks:notebook_delete', args=[self.notebook.id]))
        self.client.post(reverse('notebooks:trash_empty'))
        self.assertFalse(Notebook.all_objects.filter(owner=self.alice).exists())

    def test_trash_is_owner_scoped(self):
        self.delete_note()
        self.client.login(username='bob', password='pw')
        self.assertNotContains(self.client.get(reverse('notebooks:trash')), 'Soup')
        for name in ('trash_restore_note', 'trash_delete_note'):
            self.assertEqual(self.client.post(reverse(f'notebooks:{name}', args=[self.note.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('notebooks:trash_note', args=[self.note.id])).status_code, 404)
        self.client.post(reverse('notebooks:trash_empty'))
        self.assertTrue(Note.all_objects.filter(pk=self.note.pk).exists())

        self.client.login(username='alice', password='pw')
        self.client.post(reverse('notebooks:notebook_delete', args=[self.notebook.id]))
        self.client.login(username='bob', password='pw')
        for name in ('trash_restore_notebook', 'trash_delete_notebook'):
            url = reverse(f'notebooks:{name}', args=[self.notebook.id])
            self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(Notebook.all_objects.filter(pk=self.notebook.pk).exists())
