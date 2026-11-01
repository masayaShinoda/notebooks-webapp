/*
 * App-wide failure handling for htmx requests.
 *
 * htmx on its own does nothing visible when a request fails: a network error
 * or a 4xx/5xx is simply not swapped, so a click looks ignored. This script
 * owns the `app` Alpine store that the banner, toasts and progress bar in
 * templates/base.html render from:
 *
 *   offline         -- a request could not reach the server (not just
 *                      navigator.onLine, which is true on captive portals or
 *                      when only the server is down). Cleared by any response.
 *   sessionExpired  -- 401 (HtmxErrorMiddleware turns login_required's
 *                      redirect into one).
 *   stale           -- 403: the CSRF token baked into <body hx-headers> no
 *                      longer matches, e.g. after signing in from another tab.
 *
 * The note editor's own save state (retries, drafts) lives in
 * offline-notes.js, which reads and writes this same store.
 *
 * Requests from an element marked `data-background` (autosave, live preview)
 * never show a toast or the progress bar -- they fire while typing, and the
 * editor has its own status line.
 *
 * Must load before alpine.min.js, like offline-notes.js (see base.html).
 */
(function () {
  'use strict';

  var TOAST_MS = 5000;
  var MAX_TOASTS = 3;
  var PROGRESS_DELAY_MS = 250;

  var nextToastId = 1;
  var pending = 0;
  var progressTimer = null;

  function store() {
    return window.Alpine ? Alpine.store('app') : null;
  }

  function isBackground(elt) {
    return !!(elt && elt.closest && elt.closest('[data-background]'));
  }

  document.addEventListener('alpine:init', function () {
    Alpine.store('app', {
      offline: !navigator.onLine,
      sessionExpired: false,
      stale: false,
      // null | 'server' | 'rejected' | 'gone' | 'conflict' -- see offline-notes.js
      saveError: null,
      // The server's Note.version on the last 409, for "Keep mine".
      conflictVersion: null,
      loading: false,
      toasts: [],

      // Only one banner at a time; the state that needs the user to act wins.
      get banner() {
        if (this.sessionExpired) return 'session';
        if (this.stale) return 'stale';
        if (this.offline) return 'offline';
        return null;
      },

      // The editor's status line (note_editor.html). 'rejected', 'gone' and 'conflict'
      // carry the server's own message in #save-status instead.
      get saveIssue() {
        if (this.sessionExpired) return 'Session expired — saved on this device';
        if (this.stale) return 'Page out of date — saved on this device';
        if (this.offline) return 'Offline — saved on this device, will sync automatically';
        if (this.saveError === 'server') return "Couldn't save — retrying";
        return null;
      },

      // The phone's one-line app bar has no room for the full text; the
      // banner above it already explains the state in full.
      get saveIssueShort() {
        if (this.sessionExpired) return 'Session expired';
        if (this.stale) return 'Page out of date';
        if (this.offline) return 'Offline';
        if (this.saveError === 'server') return "Couldn't save";
        return null;
      },

      get saveRetryable() {
        return !this.sessionExpired && !this.stale && (this.offline || this.saveError === 'server');
      },

      // `undo` ({url, target}, optional) adds an Undo button that POSTs to
      // url and swaps the response into target (see views._toast).
      toast: function (message, undo) {
        if (this.toasts.some(function (t) { return t.message === message; })) return;
        this.toasts.push({ id: nextToastId++, message: message, undo: undo || null });
        if (this.toasts.length > MAX_TOASTS) this.toasts.shift();
      },

      dismiss: function (id) {
        this.toasts = this.toasts.filter(function (t) { return t.id !== id; });
      },

      undo: function (t) {
        this.dismiss(t.id);
        htmx.ajax('POST', t.undo.url, { target: t.undo.target, swap: 'innerHTML' });
      },
    });

    // One toast's auto-dismiss timer, paused while hovered or focused.
    Alpine.data('toast', function (id) {
      return {
        timer: null,
        init: function () { this.resume(); },
        pause: function () { clearTimeout(this.timer); },
        resume: function () {
          var self = this;
          clearTimeout(this.timer);
          this.timer = setTimeout(function () { Alpine.store('app').dismiss(id); }, TOAST_MS);
        },
        destroy: function () { clearTimeout(this.timer); },
      };
    });
  });

  // --- Progress bar: shown only if a request is still running after
  // PROGRESS_DELAY_MS, so fast responses don't flicker.
  document.addEventListener('htmx:beforeRequest', function (evt) {
    if (isBackground(evt.detail.elt)) return;
    pending++;
    if (!progressTimer) {
      progressTimer = setTimeout(function () {
        var s = store();
        if (s && pending > 0) s.loading = true;
      }, PROGRESS_DELAY_MS);
    }
  });

  document.addEventListener('htmx:afterRequest', function (evt) {
    var s = store();
    var xhr = evt.detail.xhr;
    var status = xhr ? xhr.status : 0;

    if (!isBackground(evt.detail.elt)) {
      pending = Math.max(0, pending - 1);
      if (pending === 0) {
        clearTimeout(progressTimer);
        progressTimer = null;
        if (s) s.loading = false;
      }
    }

    // Status 0 (network error, timeout, abort) is handled by the dedicated
    // events below; htmx fires afterRequest before them, and an abort
    // (superseded request) must not look like a failure.
    if (!s || status === 0) return;
    // Any HTTP response at all proves the server is reachable.
    s.offline = false;
    if (evt.detail.successful) {
      s.sessionExpired = false;
      return;
    }
    if (status === 401) {
      s.sessionExpired = true;
      return;
    }
    if (status === 403) {
      s.stale = true;
      return;
    }
    if (isBackground(evt.detail.elt)) return;
    if (status === 404 || status === 422) {
      // Swappable 404/422s carry their own message (see beforeSwap below).
      if (!xhr.getResponseHeader('X-Swap-Error')) s.toast("That page doesn't exist.");
    } else {
      s.toast('Something went wrong — please try again.');
    }
  });

  document.addEventListener('htmx:sendError', function (evt) {
    var s = store();
    if (!s) return;
    s.offline = true;
    if (!isBackground(evt.detail.elt)) {
      s.toast(navigator.onLine
        ? "Couldn't reach the server — check your connection and try again."
        : "You're offline — that didn't go through.");
    }
  });

  document.addEventListener('htmx:timeout', function (evt) {
    var s = store();
    if (s && !isBackground(evt.detail.elt)) s.toast('The server took too long to respond — please try again.');
  });

  // Let the server's deliberate error fragments through (views._swappable_error),
  // and nothing else: a stray error page must never land inside a pane.
  // isError is deliberately left true -- htmx derives afterRequest's
  // `successful` from it, and a rejected save reported as successful would
  // clear the local draft (offline-notes.js).
  document.addEventListener('htmx:beforeSwap', function (evt) {
    var xhr = evt.detail.xhr;
    if (xhr.status >= 400 && xhr.getResponseHeader('X-Swap-Error')) {
      evt.detail.shouldSwap = true;
    }
  });

  // Server-sent toasts: HX-Trigger {"toast": {...}} (views._toast).
  window.addEventListener('toast', function (evt) {
    var s = store();
    var d = evt.detail || {};
    if (!s || !d.message) return;
    s.toast(d.message, d.undoUrl ? { url: d.undoUrl, target: d.undoTarget } : null);
  });

  window.addEventListener('online', function () {
    var s = store();
    if (s) s.offline = false;
  });

  window.addEventListener('offline', function () {
    var s = store();
    if (s) s.offline = true;
  });
})();
