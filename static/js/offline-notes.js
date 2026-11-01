/*
 * Offline-resilient autosave for the note editor.
 *
 * The editor's textarea already holds the note's full plaintext (server-
 * rendered on load), so typing itself has no network dependency -- nothing
 * changes there. What's missing without this file:
 *
 *   1. A failed autosave (offline, or a transient server error) looks
 *      identical to a successful one, so "Unsaved changes" silently clears
 *      when it shouldn't.
 *   2. Nothing retries a failed save once the connection returns.
 *   3. Closing the tab/browser while offline loses whatever wasn't yet
 *      synced.
 *
 * This script fixes all three by piggybacking on the *existing* save flow
 * (the `save` htmx trigger + POST to note_save) rather than adding a new
 * endpoint: every save attempt snapshots the form into localStorage, the
 * snapshot is cleared on confirmed success, and reconnecting (or reopening
 * a note with a leftover snapshot) re-fires the same save.
 *
 * Deliberately does NOT use the Background Sync API (retrying while the tab
 * is closed) -- it's Chromium-only. Every resync trigger here is a
 * foreground signal (`online`, page load, htmx settle), which behaves
 * identically in Firefox, Chrome and Safari.
 *
 * Security note: a pending draft sits in localStorage as plaintext until it
 * syncs, outside the server's per-user envelope encryption. Kept narrow on
 * purpose -- see writeDraft()/MAX_DRAFT_AGE_MS below.
 *
 * Stale content must never overwrite newer content. A tab left open for days,
 * a restored tab or a leftover draft all hold an old copy of the note, so:
 *
 *   - Every save carries `base_version`, the Note.version the text is based
 *     on; the server answers 409 if the note has changed since (e.g. on
 *     another device), and the user picks a side (save_conflict.html).
 *   - Nothing saves, drafts or retries unless the editor has unsaved edits
 *     (`dirty`): blurring the editor, coming back online or closing the tab
 *     with nothing typed sends nothing.
 *   - Returning to the tab re-checks the note's version and quietly reloads
 *     the editor if it is clean and out of date.
 */
(function () {
  'use strict';

  // v1 drafts (from before base_version existed) are still read: with no
  // base version, replaying one gets a 409 rather than overwriting anything.
  var DRAFT_PREFIX = 'notebooks:draft:v2:';
  var LEGACY_DRAFT_PREFIX = 'notebooks:draft:v1:';
  var MAX_DRAFT_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

  function draftKey(noteId) {
    return DRAFT_PREFIX + noteId;
  }

  function readDraft(noteId) {
    var raw;
    try {
      raw = localStorage.getItem(draftKey(noteId)) || localStorage.getItem(LEGACY_DRAFT_PREFIX + noteId);
    } catch (e) {
      return null; // localStorage unavailable (private mode, disabled, ...)
    }
    if (!raw) return null;
    var draft;
    try {
      draft = JSON.parse(raw);
    } catch (e) {
      return null;
    }
    if (!draft || typeof draft.capturedAt !== 'number') return null;
    if (Date.now() - draft.capturedAt > MAX_DRAFT_AGE_MS) {
      clearDraft(noteId);
      return null;
    }
    return draft;
  }

  function writeDraft(noteId, draft) {
    try {
      localStorage.setItem(draftKey(noteId), JSON.stringify(draft));
      return true;
    } catch (e) {
      // Storage full/unavailable: offline resilience degrades to
      // in-memory-only for this tab (see the beforeunload guard below).
      return false;
    }
  }

  function clearDraft(noteId) {
    try {
      localStorage.removeItem(draftKey(noteId));
      localStorage.removeItem(LEGACY_DRAFT_PREFIX + noteId);
    } catch (e) {
      // ignore
    }
  }

  function isSaveForm(elt) {
    return !!elt && elt.id === 'note-editor-form';
  }

  function getForm() {
    return document.getElementById('note-editor-form');
  }

  function fieldValue(form, name) {
    var field = form.querySelector('[name="' + name + '"]');
    return field ? field.value : '';
  }

  function setBaseVersion(form, version) {
    var field = form.querySelector('[name="base_version"]');
    if (field) field.value = version == null ? '' : String(version);
  }

  function formSnapshot(form) {
    return {
      notebookId: form.dataset.notebookId,
      noteId: form.dataset.noteId,
      title: fieldValue(form, 'title'),
      body: fieldValue(form, 'body'),
      baseVersion: fieldValue(form, 'base_version') || null,
      capturedAt: Date.now(),
    };
  }

  function sameContent(a, b) {
    return a.title === b.title && a.body === b.body;
  }

  function applyDraftToForm(form, draft) {
    var titleField = form.querySelector('[name="title"]');
    var bodyField = form.querySelector('[name="body"]');
    if (titleField && typeof draft.title === 'string') titleField.value = draft.title;
    if (bodyField && typeof draft.body === 'string') bodyField.value = draft.body;
    // The draft's text is based on the version it was typed against, not on
    // whatever the server has now -- that difference is the conflict check.
    setBaseVersion(form, draft.baseVersion);
  }

  function appStore() {
    return window.Alpine ? Alpine.store('app') : null;
  }

  // `dirty` lives on note_editor.html's wrapper; $data merges ancestor scope.
  function editorScope(form) {
    return window.Alpine ? Alpine.$data(form) : null;
  }

  function isDirty(form) {
    var scope = editorScope(form);
    return !!(scope && scope.dirty);
  }

  function setDirty(form, dirty) {
    var scope = editorScope(form);
    if (scope) scope.dirty = dirty;
  }

  // Re-render the open note from the server into #editor-pane (also used to
  // show the "deleted elsewhere" notice). X-Editor-Refresh keeps the phone
  // layout on whichever pane the user is looking at (see views.py).
  function reloadEditor(form) {
    if (!window.htmx || !form.dataset.detailUrl) return;
    htmx.ajax('GET', form.dataset.detailUrl, {
      target: '#editor-pane',
      swap: 'innerHTML',
      headers: { 'X-Editor-Refresh': 'true' },
    });
  }

  // --- Retry with backoff after a failed save (offline, 5xx, timeout).
  // Deliberately NOT for 401/403 (the user has to sign in or reload -- see
  // the banner in base.html), 422 (resending the same content fails the same
  // way) or 404 (the note is gone).
  var RETRY_DELAYS_MS = [2000, 5000, 15000, 30000];
  var retryAttempt = 0;
  var retryTimer = null;

  function cancelRetry() {
    clearTimeout(retryTimer);
    retryTimer = null;
  }

  function scheduleRetry() {
    cancelRetry();
    var delay = RETRY_DELAYS_MS[Math.min(retryAttempt, RETRY_DELAYS_MS.length - 1)];
    retryAttempt++;
    retryTimer = setTimeout(function () {
      retryTimer = null;
      var form = getForm();
      if (form && window.htmx) htmx.trigger(form, 'save');
    }, delay);
  }

  // Exposed for the editor's "Retry now" button.
  window.notebooksRetrySave = function () {
    cancelRetry();
    var form = getForm();
    if (form && window.htmx) htmx.trigger(form, 'save');
  };

  // Set by "Keep mine" until that save lands: the server then snapshots the
  // version being overwritten into the note's history even inside the
  // revision window (views._snapshot) -- it is another device's work.
  var forceSnapshot = false;

  // Set by the History button while it waits for unsaved edits to save.
  var pendingHistoryUrl = null;

  function openHistory(url) {
    pendingHistoryUrl = null;
    htmx.ajax('GET', url, { target: '#editor-pane', swap: 'innerHTML' });
  }

  // The editor's History button. Unsaved edits are saved first, so what the
  // history shows as current is what the user sees, and a restore can never
  // replace text that only existed in the textarea. A failed save leaves the
  // editor open with its usual error state.
  window.notebooksOpenHistory = function (url) {
    var form = getForm();
    if (!form || !window.htmx) return;
    if (!isDirty(form)) {
      openHistory(url);
      return;
    }
    pendingHistoryUrl = url;
    htmx.trigger(form, 'save');
  };

  // Conflict choices (the conflict bar in note_editor.html).
  window.notebooksKeepMine = function (serverVersion) {
    var form = getForm();
    if (!form || !window.htmx) return;
    forceSnapshot = true;
    setBaseVersion(form, serverVersion);
    setDirty(form, true);
    var store = appStore();
    if (store) store.saveError = null;
    htmx.trigger(form, 'save');
  };

  window.notebooksLoadLatest = function () {
    var form = getForm();
    if (!form) return;
    cancelRetry();
    clearDraft(form.dataset.noteId);
    setDirty(form, false);
    reloadEditor(form);
  };

  // --- Draft persistence, piggybacked on the existing save cycle.
  var storageOk = true;
  var inFlight = null; // snapshot of the save currently on the wire

  document.addEventListener('htmx:beforeRequest', function (evt) {
    var form = evt.detail.elt;
    if (!isSaveForm(form) || !form.dataset.noteId) return;
    cancelRetry();
    // Only unsaved edits are ever sent. `focusout`, the `online` resync and
    // retries all land here, and a clean editor may be showing an old copy
    // of the note (a tab left open for days) -- saving it would overwrite
    // newer content for nothing.
    if (!isDirty(form)) {
      evt.preventDefault();
      return;
    }
    inFlight = formSnapshot(form);
    storageOk = writeDraft(form.dataset.noteId, inFlight);
    // If another note is opened while this save is on the wire, the form is
    // detached when it lands, so its afterRequest never reaches the document
    // handler below -- and the draft would outlive the save, to be replayed
    // as a false conflict the next time this note is opened.
    var sent = inFlight;
    form.addEventListener('htmx:afterRequest', function settle(e) {
      if (e.detail.elt !== form) return; // e.g. the live preview's request
      form.removeEventListener('htmx:afterRequest', settle);
      if (getForm() === form || !e.detail.successful) return;
      var version = e.detail.xhr.getResponseHeader('X-Note-Version');
      if (version) setBaseVersion(form, version);
      var now = formSnapshot(form);
      if (sameContent(sent, now)) clearDraft(form.dataset.noteId);
      else writeDraft(form.dataset.noteId, now);
      // htmx also drops a detached form's response, the OOB row update with
      // it: copy the row's new title/time over and move it to the top here.
      // The row keeps its own classes -- it is no longer the open note.
      var row = document.getElementById('note-row-' + form.dataset.noteId);
      var fresh = new DOMParser().parseFromString(e.detail.xhr.responseText, 'text/html')
        .querySelector('a#note-row-' + form.dataset.noteId);
      if (row && fresh) {
        row.replaceChild(fresh.firstElementChild, row.firstElementChild);
        row.parentNode.prepend(row);
      }
    });
  });

  document.addEventListener('htmx:configRequest', function (evt) {
    if (isSaveForm(evt.detail.elt) && forceSnapshot) evt.detail.parameters.force_snapshot = '1';
  });

  document.addEventListener('htmx:afterRequest', function (evt) {
    var form = evt.detail.elt;
    if (!isSaveForm(form)) return;
    var store = appStore();
    var xhr = evt.detail.xhr;
    var status = xhr ? xhr.status : 0;
    var sent = inFlight;
    inFlight = null;
    if (evt.detail.successful) {
      var version = xhr.getResponseHeader('X-Note-Version');
      if (version) setBaseVersion(form, version);
      retryAttempt = 0;
      forceSnapshot = false;
      if (store) store.saveError = null;
      // Text typed while the save was on the wire is still unsaved; its
      // keyup debounce sends it next, now based on the version just saved.
      var now = formSnapshot(form);
      if (sent && !sameContent(sent, now)) {
        storageOk = writeDraft(form.dataset.noteId, now);
      } else {
        clearDraft(form.dataset.noteId);
        setDirty(form, false);
        if (pendingHistoryUrl) openHistory(pendingHistoryUrl);
      }
      return;
    }
    pendingHistoryUrl = null;
    // Network failures (status 0) are handled by sendError/timeout below:
    // htmx fires afterRequest first, and an aborted request needs nothing.
    if (!store || status === 0) return;
    if (status === 401 || status === 403) {
      store.saveError = null; // the banner explains it
    } else if (status === 409) {
      // The draft stays: it is the user's text until they pick a side
      // (the conflict bar in note_editor.html).
      store.conflictVersion = xhr.getResponseHeader('X-Note-Version');
      store.saveError = 'conflict';
    } else if (status === 422) {
      store.saveError = 'rejected';
    } else if (status === 404) {
      store.saveError = 'gone';
    } else {
      store.saveError = 'server';
      scheduleRetry();
    }
  });

  function onNetworkFailure(evt) {
    if (!isSaveForm(evt.detail.elt)) return;
    pendingHistoryUrl = null;
    var store = appStore();
    if (store) store.saveError = null; // app-errors.js sets `offline`
    scheduleRetry();
  }
  document.addEventListener('htmx:sendError', onNetworkFailure);
  document.addEventListener('htmx:timeout', onNetworkFailure);

  // Cheap extra flush: a tab can close mid-debounce (typed, then closed
  // within the 1.5s window) before any htmx:beforeRequest fires.
  // Only with unsaved edits: a draft of an unchanged note would be replayed
  // the next time it is opened, after it may have been edited elsewhere.
  window.addEventListener('pagehide', function () {
    var form = getForm();
    if (form && form.dataset.noteId && isDirty(form)) writeDraft(form.dataset.noteId, formSnapshot(form));
  });

  // Normally a draft in localStorage makes leaving safe. Only when storage
  // is unavailable (private mode, quota full) would unsaved text be lost, so
  // only then ask the browser to confirm.
  window.addEventListener('beforeunload', function (evt) {
    var form = getForm();
    if (!form || storageOk || !window.Alpine) return;
    var scope = Alpine.$data(form);
    if (scope && scope.dirty) {
      evt.preventDefault();
      evt.returnValue = '';
    }
  });

  // --- Resync on reconnect / on returning to the tab.
  // A clean editor is checked against the server's version and reloaded if
  // the note changed elsewhere; unsaved edits are sent (and get a 409 if the
  // note changed meanwhile). Covers tabs left open for days, a laptop waking
  // from sleep, and pages restored from the back/forward cache.
  function resync() {
    var form = getForm();
    if (!form || !form.dataset.noteId) return;
    if (isDirty(form)) {
      if (window.htmx) htmx.trigger(form, 'save');
      return;
    }
    if (!form.dataset.versionUrl || !window.fetch) return;
    fetch(form.dataset.versionUrl, {
      headers: { 'HX-Request': 'true' },
      credentials: 'same-origin',
      cache: 'no-store',
    }).then(function (resp) {
      if (getForm() !== form || isDirty(form)) return;
      if (resp.status === 404) {
        reloadEditor(form); // deleted elsewhere: show the notice
        return;
      }
      if (!resp.ok) return;
      return resp.json().then(function (data) {
        if (getForm() !== form || isDirty(form)) return;
        if (String(data.version) !== fieldValue(form, 'base_version')) reloadEditor(form);
      });
    }).catch(function () {
      // Offline: nothing to compare against; the next resync tries again.
    });
  }

  window.addEventListener('online', function () {
    cancelRetry();
    retryAttempt = 0;
    resync();
  });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') resync();
  });

  window.addEventListener('pageshow', function (evt) {
    if (evt.persisted) resync();
  });

  // --- Orphaned-draft recovery: the tab/browser closed while offline and
  // was reopened later. Two hook points because the app mixes full page
  // loads with htmx fragment swaps (same pattern as workspace.html's
  // document-level `x-on:htmx:after-swap.window` listener).
  function recoverDraftForOpenNote() {
    var form = getForm();
    if (!form || !form.dataset.noteId) return;
    var draft = readDraft(form.dataset.noteId);
    if (!draft) return;
    if (sameContent(draft, formSnapshot(form))) {
      // Already what the server has (e.g. the save landed but its response
      // was lost): nothing to recover.
      clearDraft(form.dataset.noteId);
      return;
    }
    applyDraftToForm(form, draft);
    setDirty(form, true);
    // If online, this syncs right away -- or gets a 409 if the note changed
    // since the draft was typed; if offline, it fails the same way any other
    // save does and is retried with backoff.
    if (window.htmx) htmx.trigger(form, 'save');
  }

  document.addEventListener('DOMContentLoaded', recoverDraftForOpenNote);
  document.addEventListener('htmx:afterSettle', function (evt) {
    // Only for a swap that just opened/replaced the editor pane (matching
    // workspace.html's own `target.id === 'editor-pane'` check) -- not for
    // the save/preview forms' own narrower swaps (#save-status,
    // #preview-pane), which would otherwise re-trigger recovery (and thus
    // another `save`) after every autosave tick while offline.
    if (evt.detail.target && evt.detail.target.id === 'editor-pane') {
      // A different note (or none) is open now: the old one's save state
      // and pending retry no longer apply.
      cancelRetry();
      retryAttempt = 0;
      forceSnapshot = false;
      pendingHistoryUrl = null;
      var store = appStore();
      if (store) store.saveError = null;
      recoverDraftForOpenNote();
    }
  });
})();
