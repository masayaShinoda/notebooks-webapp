import json
import logging
from datetime import timedelta

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import NotebookForm, NoteForm
from .markdown import render_markdown
from .models import Note, Notebook, NoteRevision

logger = logging.getLogger(__name__)


def _is_htmx(request):
    return request.headers.get('HX-Request') == 'true'


def _pane(response, pane):
    """Tell the single-pane mobile layout which pane this response belongs to.

    The client cannot infer it: htmx fires htmx:afterSwap for out-of-band
    swaps too, so a response that swaps #editor-pane directly and #notes-pane
    OOB (see _note_detail_response) is indistinguishable from the reverse.
    Saying it explicitly is also the only order-independent answer.

    Deliberately not set by note_save/note_preview -- those fire on an autosave
    debounce and must never move the user off the pane they are on.
    """
    response['HX-Trigger'] = json.dumps({'pane': pane})
    return response


def _toast(response, message, undo_url=None, undo_target=None):
    """Show a toast (static/js/app-errors.js) once this response lands,
    optionally with an Undo button that POSTs to `undo_url` and swaps the
    result into `undo_target`. Merges with any HX-Trigger set by _pane.
    """
    triggers = json.loads(response.get('HX-Trigger', '{}'))
    triggers['toast'] = {'message': message, 'undoUrl': undo_url, 'undoTarget': undo_target}
    response['HX-Trigger'] = json.dumps(triggers)
    return response


def _swappable_error(response):
    """Mark a 4xx response as a deliberate fragment. htmx never swaps 4xx on
    its own; static/js/app-errors.js lets these (and only these) through, so
    a stray error page can never be swapped into a pane.
    """
    response['X-Swap-Error'] = 'true'
    return response


def htmx_not_found(request, match):
    """Http404 raised by a notebooks view during an htmx request -- almost
    always an item deleted in another tab or on another device. Called from
    HtmxErrorMiddleware.process_exception.

    Every lookup stays owner-scoped, and the message is the same whether the
    item never existed or belongs to someone else, so nothing is leaked.
    """
    if match.url_name in ('note_preview', 'note_version') or not request.user.is_authenticated:
        # Background requests; the editor keeps the last render (and
        # offline-notes.js reloads the editor pane itself on a 404 version
        # check, which lands back here with the full "gone" response).
        return HttpResponse(status=404)
    if match.url_name == 'note_save':
        # Swap only the status line: replacing the editor would throw away
        # the text the user is still looking at.
        body = render_to_string('notebooks/partials/save_error.html', {
            'message': 'This note was deleted elsewhere. Copy your text to keep it.',
        }, request=request)
        return _swappable_error(HttpResponse(body, status=404))

    if match.url_name.startswith('trash'):
        # Already restored or deleted forever in another tab: show the trash
        # as it is now.
        response = _trash_response(request, push_url=True)
        response['HX-Retarget'] = '#notes-pane'
        response['HX-Reswap'] = 'innerHTML'
        return response

    notebook = None
    if 'notebook_id' in match.kwargs:
        notebook = Notebook.objects.filter(pk=match.kwargs['notebook_id'], owner=request.user).first()
    context = _base_context(
        request, active_notebook=notebook, notes=notebook.notes.all() if notebook else None,
        gone_noun='note' if notebook and 'note_id' in match.kwargs else 'notebook',
    )
    body = render_to_string('notebooks/partials/note_gone.html', context, request=request)
    body += render_to_string('notebooks/partials/oob_rail.html', context, request=request)
    body += render_to_string('notebooks/partials/oob_notes_pane.html', context, request=request)
    response = _pane(_swappable_error(HttpResponse(body, status=404)), 'editor')
    response['HX-Retarget'] = '#editor-pane'
    response['HX-Reswap'] = 'innerHTML'
    response['HX-Push-Url'] = (
        reverse('notebooks:notebook_detail', args=[notebook.id]) if notebook else reverse('notebooks:workspace')
    )
    return response


def _base_context(request, active_notebook=None, notes=None, active_note=None, **extra):
    context = {
        'notebooks': request.user.notebooks.all(),
        'active_notebook': active_notebook,
        'notes': notes,
        'active_note': active_note,
    }
    context.update(extra)
    return context


def _note_form_for(notebook, note):
    """Build the NoteForm for an existing note, with the live-preview HTMX
    attributes wired onto the body textarea's widget (the note's URL is only
    known at this point, not inside the NoteForm class itself).
    """
    content = note.get_content()
    form = NoteForm(initial={'title': note.title, 'body': content, 'base_version': note.version})
    form.fields['body'].widget.attrs.update({
        'hx-post': reverse('notebooks:note_preview', args=[notebook.id, note.id]),
        'hx-trigger': 'keyup changed delay:400ms',
        'hx-target': '#preview-pane',
        'hx-swap': 'innerHTML',
    })
    return form, content


def _notebook_form_response(request, form, active_notebook=None):
    """Shared by notebook_create/notebook_update for every non-success case
    (initial GET, or POST with validation errors): render the form back
    into #notes-pane, resetting #editor-pane for HTMX requests.
    """
    notes = active_notebook.notes.all() if active_notebook else None
    context = _base_context(request, active_notebook=active_notebook, notes=notes, notebook_form=form)
    if _is_htmx(request):
        body = render_to_string('notebooks/partials/notebook_form.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_editor_pane.html', context, request=request)
        return _pane(HttpResponse(body), 'notes')
    return render(request, 'notebooks/workspace.html', context)


def _notebook_detail_response(request, notebook, push_url=False):
    """Shared by notebook_detail (GET) and notebook_create/notebook_update
    on success: the notes list for `notebook`, with the rail and editor pane
    kept in sync via OOB swaps for HTMX requests.
    """
    notes = notebook.notes.all()
    context = _base_context(request, active_notebook=notebook, notes=notes)
    if _is_htmx(request):
        body = render_to_string('notebooks/partials/notes_list.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_rail.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_editor_pane.html', context, request=request)
        response = _pane(HttpResponse(body), 'notes')
        if push_url:
            response['HX-Push-Url'] = reverse('notebooks:notebook_detail', args=[notebook.id])
        return response
    return render(request, 'notebooks/workspace.html', context)


def _note_detail_response(request, notebook, note, push_url=False, new=False):
    """Shared by note_detail (GET) and note_create on success: the editor
    for `note`, with the notes list kept in sync via an OOB swap (to update
    which row is highlighted as active) for HTMX requests.
    """
    try:
        form, content = _note_form_for(notebook, note)
    except InvalidToken:
        # Never fall through to the editor: its autosave would overwrite the
        # ciphertext with an empty body, destroying data that may still be
        # recoverable with the right key (e.g. after a botched rotate_kek).
        logger.exception('Could not decrypt note %s (owner %s)', note.pk, request.user.pk)
        context = _base_context(
            request, active_notebook=notebook, notes=notebook.notes.all(),
            active_note=note, undecryptable=True,
        )
    else:
        if new:
            form.fields['title'].widget.attrs.update({'autofocus': True, 'x-init': '$el.select()'})
        context = _base_context(
            request, active_notebook=notebook, notes=notebook.notes.all(),
            active_note=note, note_form=form, preview_html=render_markdown(content),
        )
    if _is_htmx(request):
        editor = 'note_undecryptable.html' if context.get('undecryptable') else 'note_editor.html'
        body = render_to_string(f'notebooks/partials/{editor}', context, request=request)
        body += render_to_string('notebooks/partials/oob_notes_pane.html', context, request=request)
        response = HttpResponse(body)
        if request.headers.get('X-Editor-Refresh') != 'true':
            # A background refresh of the open note (offline-notes.js) must
            # not move the user off the pane they are on.
            response = _pane(response, 'editor')
        if push_url:
            response['HX-Push-Url'] = reverse('notebooks:note_detail', args=[notebook.id, note.id])
        return response
    return render(request, 'notebooks/workspace.html', context)


@login_required
def workspace(request):
    context = _base_context(request)
    if _is_htmx(request):
        body = render_to_string('notebooks/partials/notes_empty.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_rail.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_editor_pane.html', context, request=request)
        return _pane(HttpResponse(body), 'notebooks')
    return render(request, 'notebooks/workspace.html', context)


@login_required
def notebook_create(request):
    if request.method == 'POST':
        form = NotebookForm(request.POST)
        if form.is_valid():
            notebook = form.save(commit=False)
            notebook.owner = request.user
            notebook.save()
            return _notebook_detail_response(request, notebook, push_url=True)
    else:
        form = NotebookForm()
        form.fields['title'].widget.attrs['autofocus'] = True
    return _notebook_form_response(request, form)


@login_required
def notebook_detail(request, notebook_id):
    # Scoped to request.user: a 404 (not 403) for another user's notebook,
    # so we never leak whether it exists.
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    return _notebook_detail_response(request, notebook)


@login_required
def notebook_update(request, notebook_id):
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    if request.method == 'POST':
        form = NotebookForm(request.POST, instance=notebook)
        if form.is_valid():
            form.save()
            return _notebook_detail_response(request, notebook, push_url=True)
    else:
        form = NotebookForm(instance=notebook)
    return _notebook_form_response(request, form, active_notebook=notebook)


@login_required
@require_POST
def notebook_delete(request, notebook_id):
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    # To the trash, not deleted: its notes go with it (LiveNoteManager hides
    # them through the notebook) and come back with it.
    notebook.deleted_at = timezone.now()
    notebook.save(update_fields=['deleted_at'])

    if _is_htmx(request):
        context = _base_context(request)
        body = render_to_string('notebooks/partials/notes_empty.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_rail.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_editor_pane.html', context, request=request)
        response = _pane(HttpResponse(body), 'notebooks')
        response['HX-Push-Url'] = reverse('notebooks:workspace')
        return _toast(
            response, 'Notebook moved to trash',
            reverse('notebooks:trash_restore_notebook', args=[notebook.id]), '#notes-pane',
        )
    return redirect('notebooks:workspace')


@login_required
@require_POST
def note_create(request, notebook_id):
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    note = Note(notebook=notebook, title='Untitled note')
    note.set_content('')
    note.save()
    return _note_detail_response(request, notebook, note, push_url=True, new=True)


@login_required
def note_detail(request, notebook_id, note_id):
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    note = get_object_or_404(Note, pk=note_id, notebook=notebook)
    return _note_detail_response(request, notebook, note)


@login_required
@require_POST
def note_save(request, notebook_id, note_id):
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    note = get_object_or_404(Note, pk=note_id, notebook=notebook)
    form = NoteForm(request.POST)
    if not form.is_valid():
        # 422, not the usual "Saved" -- a rejected save must never look like
        # a successful one (the client would clear its local draft).
        error = next(iter(form.errors.values()))[0]
        body = render_to_string('notebooks/partials/save_error.html', {'message': error}, request=request)
        return _swappable_error(HttpResponse(body, status=422))

    with transaction.atomic():
        # Re-read inside the transaction (SQLite runs it IMMEDIATE, so this
        # holds the write lock) so the version check and the write can't
        # interleave with another save of the same note.
        note = get_object_or_404(Note, pk=note.pk)
        if form.cleaned_data['base_version'] != note.version:
            return _save_conflict_response(request, note)
        _snapshot(note, force=request.POST.get('force_snapshot') == '1')
        note.title = form.cleaned_data['title'] or 'Untitled note'
        note.set_content(form.cleaned_data['body'])
        note.version += 1
        note.save()

    if _is_htmx(request):
        # Precise single-row OOB update -- never re-renders (or decrypts)
        # the whole notes list just to refresh one title/timestamp, since
        # this fires on every autosave debounce tick.
        body = render_to_string('notebooks/partials/save_status.html', {}, request=request)
        body += render_to_string(
            'notebooks/partials/note_row.html',
            {'note': note, 'active_note': note, 'oob': True},
            request=request,
        )
        response = HttpResponse(body)
        # The editor's next save is based on this version (offline-notes.js).
        response['X-Note-Version'] = str(note.version)
        return response
    return redirect('notebooks:note_detail', notebook_id=notebook.id, note_id=note.id)


def _snapshot(note, force=False):
    """Copy the note's current (pre-save) state into a NoteRevision. Call
    inside the save's transaction, before overwriting it.

    At most one per NOTE_REVISION_WINDOW_MINUTES unless `force` (a "Keep
    mine" overwrite or a restore -- the state being replaced may be another
    device's work). An empty body or a copy of the newest revision is skipped.
    The ciphertext is copied as-is: it is already under the owner's DEK.
    """
    latest = note.revisions.first()
    window = timedelta(minutes=settings.NOTE_REVISION_WINDOW_MINUTES)
    if not force and latest and timezone.now() - latest.created_at < window:
        return
    content = note.get_content()
    if not content:
        return
    if latest and latest.title == note.title and latest.get_content() == content:
        return
    NoteRevision.objects.create(
        note=note, version=note.version, title=note.title, encrypted_content=note.encrypted_content,
    )
    stale = list(note.revisions.values_list('pk', flat=True)[settings.NOTE_REVISION_LIMIT:])
    NoteRevision.objects.filter(pk__in=stale).delete()


def _save_conflict_response(request, note):
    """409 for a save based on an older version than the stored one: the
    note changed elsewhere (another device or tab) since this editor loaded
    it, or the save replays a stale local draft. Nothing is written; the user
    picks which version wins (partials/save_conflict.html).
    """
    if not _is_htmx(request):
        return HttpResponse(
            'This note changed since you opened it. Reload to see the latest version.',
            status=409, content_type='text/plain',
        )
    body = render_to_string('notebooks/partials/save_conflict.html', {'note': note}, request=request)
    response = _swappable_error(HttpResponse(body, status=409))
    response['X-Note-Version'] = str(note.version)
    return response


@login_required
def note_version(request, notebook_id, note_id):
    """The stored version of a note, for the editor to check whether what it
    shows is still current after the tab was in the background (offline-
    notes.js). Deliberately cheap: no decryption, no rendering.
    """
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    note = get_object_or_404(Note, pk=note_id, notebook=notebook)
    return JsonResponse({'version': note.version})


@login_required
@require_POST
def note_preview(request, notebook_id, note_id):
    # Deliberately no DB read/write and no decryption here: the textarea's
    # current (unsaved) value is rendered directly from the request body.
    # This lets live preview run on every keystroke without ever touching
    # stored note content.
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    get_object_or_404(Note, pk=note_id, notebook=notebook)
    html = render_markdown(request.POST.get('body', ''))
    return HttpResponse(html)


@login_required
def note_history(request, notebook_id, note_id, revision_id=None):
    """The note's revisions, with one of them (or, with no revision_id, the
    current version) rendered read-only. Swapped into #editor-pane in place
    of the editor -- offline-notes.js saves any unsaved edits first.
    """
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    note = get_object_or_404(Note, pk=note_id, notebook=notebook)
    revision = get_object_or_404(NoteRevision, pk=revision_id, note=note) if revision_id else None
    try:
        preview_html = render_markdown((revision or note).get_content())
    except InvalidToken:
        logger.exception('Could not decrypt note %s revision %s (owner %s)', note.pk, revision_id, request.user.pk)
        preview_html = None
    context = _base_context(
        request, active_notebook=notebook, notes=notebook.notes.all(), active_note=note,
        history=True, revisions=note.revisions.all(), selected_revision=revision, preview_html=preview_html,
    )
    if _is_htmx(request):
        body = render_to_string('notebooks/partials/note_history.html', context, request=request)
        return _pane(HttpResponse(body), 'editor')
    return render(request, 'notebooks/workspace.html', context)


@login_required
@require_POST
def note_restore(request, notebook_id, note_id, revision_id):
    """Make an old revision the current version. Non-destructive: the state
    it replaces is snapshotted first, and the version bump sends any other
    open editor of this note to the 409 conflict bar on its next save.
    """
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    with transaction.atomic():
        note = get_object_or_404(Note, pk=note_id, notebook=notebook)
        revision = get_object_or_404(NoteRevision, pk=revision_id, note=note)
        try:
            content = revision.get_content()
        except InvalidToken:
            body = render_to_string('notebooks/partials/save_error.html', {
                'message': "this version can't be decrypted",
            }, request=request)
            response = _swappable_error(HttpResponse(body, status=422))
            response['HX-Retarget'] = '#history-status'
            return response
        _snapshot(note, force=True)
        note.title = revision.title
        note.set_content(content)
        note.version += 1
        note.save()
    return _note_detail_response(request, notebook, note)


@login_required
@require_POST
def note_delete(request, notebook_id, note_id):
    notebook = get_object_or_404(Notebook, pk=notebook_id, owner=request.user)
    note = get_object_or_404(Note, pk=note_id, notebook=notebook)
    # Only deleted_at: updated_at keeps the time of the last real edit.
    note.deleted_at = timezone.now()
    note.save(update_fields=['deleted_at'])

    if _is_htmx(request):
        context = _base_context(request, active_notebook=notebook, notes=notebook.notes.all())
        body = render_to_string('notebooks/partials/editor_empty.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_notes_pane.html', context, request=request)
        response = _pane(HttpResponse(body), 'notes')
        response['HX-Push-Url'] = reverse('notebooks:notebook_detail', args=[notebook.id])
        return _toast(
            response, 'Note moved to trash',
            reverse('notebooks:trash_restore_note', args=[note.id]), '#editor-pane',
        )
    return redirect('notebooks:notebook_detail', notebook_id=notebook.id)


def _trashed_notebook(request, notebook_id):
    return get_object_or_404(
        Notebook.all_objects, pk=notebook_id, owner=request.user, deleted_at__isnull=False,
    )


def _trashed_note(request, note_id):
    """A note trashed on its own. Notes inside a trashed notebook are not
    listed or reachable individually -- they come back with the notebook.
    """
    return get_object_or_404(
        Note.all_objects.select_related('notebook'), pk=note_id, notebook__owner=request.user,
        notebook__deleted_at__isnull=True, deleted_at__isnull=False,
    )


def _trash_context(request, **extra):
    trashed_notebooks = (
        Notebook.all_objects.filter(owner=request.user, deleted_at__isnull=False)
        .annotate(note_count=Count('notes', filter=Q(notes__deleted_at__isnull=True)))
        .order_by('-deleted_at')
    )
    trashed_notes = (
        Note.all_objects.filter(
            notebook__owner=request.user, notebook__deleted_at__isnull=True, deleted_at__isnull=False,
        )
        .select_related('notebook')
        .order_by('-deleted_at')
    )
    return _base_context(
        request, trash=True, trashed_notebooks=trashed_notebooks, trashed_notes=trashed_notes, **extra,
    )


def _trash_response(request, push_url=False):
    """The trash list in #notes-pane, with the rail (a restored notebook
    reappears there) and an empty editor pane kept in sync via OOB swaps.
    """
    context = _trash_context(request)
    if _is_htmx(request):
        body = render_to_string('notebooks/partials/trash_list.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_rail.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_editor_pane.html', context, request=request)
        response = _pane(HttpResponse(body), 'notes')
        if push_url:
            response['HX-Push-Url'] = reverse('notebooks:trash')
        return response
    return render(request, 'notebooks/workspace.html', context)


@login_required
def trash(request):
    return _trash_response(request)


@login_required
def trash_note(request, note_id):
    """Read-only preview of a trashed note. Deliberately not the editor:
    nothing here can autosave over a trashed note.
    """
    note = _trashed_note(request, note_id)
    try:
        preview_html = render_markdown(note.get_content())
    except InvalidToken:
        logger.exception('Could not decrypt note %s (owner %s)', note.pk, request.user.pk)
        preview_html = None
    context = _trash_context(request, trash_note=note, preview_html=preview_html)
    if _is_htmx(request):
        body = render_to_string('notebooks/partials/trash_note.html', context, request=request)
        body += render_to_string('notebooks/partials/oob_notes_pane.html', context, request=request)
        return _pane(HttpResponse(body), 'editor')
    return render(request, 'notebooks/workspace.html', context)


@login_required
@require_POST
def trash_restore_note(request, note_id):
    """From the trash list (`next=trash`) the list is re-rendered; from the
    Undo toast or the preview, the restored note opens in the editor.
    """
    note = _trashed_note(request, note_id)
    note.deleted_at = None
    note.save(update_fields=['deleted_at'])
    if request.POST.get('next') == 'trash':
        if _is_htmx(request):
            return _toast(_trash_response(request, push_url=True), f'Restored to {note.notebook.title}')
        return redirect('notebooks:trash')
    response = _note_detail_response(request, note.notebook, note, push_url=True)
    if _is_htmx(request):
        # Coming from the trash, the rail still highlights Trash.
        context = _base_context(request, active_notebook=note.notebook)
        response.content += render_to_string('notebooks/partials/oob_rail.html', context, request=request).encode()
    return response


@login_required
@require_POST
def trash_restore_notebook(request, notebook_id):
    notebook = _trashed_notebook(request, notebook_id)
    notebook.deleted_at = None
    notebook.save(update_fields=['deleted_at'])
    if request.POST.get('next') == 'trash':
        if _is_htmx(request):
            return _toast(_trash_response(request, push_url=True), f'Restored {notebook.title}')
        return redirect('notebooks:trash')
    return _notebook_detail_response(request, notebook, push_url=True)


@login_required
@require_POST
def trash_delete_note(request, note_id):
    _trashed_note(request, note_id).delete()
    if _is_htmx(request):
        return _trash_response(request, push_url=True)
    return redirect('notebooks:trash')


@login_required
@require_POST
def trash_delete_notebook(request, notebook_id):
    # Cascades to all of its notes, including ones trashed on their own.
    _trashed_notebook(request, notebook_id).delete()
    if _is_htmx(request):
        return _trash_response(request, push_url=True)
    return redirect('notebooks:trash')


@login_required
@require_POST
def trash_empty(request):
    with transaction.atomic():
        Notebook.all_objects.filter(owner=request.user, deleted_at__isnull=False).delete()
        Note.all_objects.filter(notebook__owner=request.user, deleted_at__isnull=False).delete()
    if _is_htmx(request):
        return _trash_response(request, push_url=True)
    return redirect('notebooks:trash')
