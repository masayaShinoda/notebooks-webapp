# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them
privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(the repository's **Security** tab, then **Report a vulnerability**).

Include the affected version or commit, steps to reproduce, and the impact you
expect. You should get an acknowledgement within a week.

Only the latest commit on `main` is supported. There are no maintained release
branches.

## Threat model

Notebooks encrypts note content at rest with envelope encryption:

- Every user has a random data-encryption key (DEK). Note bodies and revisions
  are encrypted with that user's DEK (Fernet: AES-128-CBC with HMAC-SHA256).
- DEKs are stored only in wrapped form, encrypted by one master key-encryption
  key, `NOTES_KEK`. That key is read from the server's environment and is never
  stored in the database.

### What it protects against

- Theft or leakage of the SQLite database file or its backups, provided
  `NOTES_KEK` is stored separately.
- Reading notes through the Django admin. The admin never shows decrypted
  content or editable keys.
- One user reading another's notes through the app. Every lookup is scoped to
  the signed-in owner and returns 404 on a mismatch.

### What it does not protect against

- **A compromised server.** The running process holds `NOTES_KEK` and can decrypt
  every note. Anyone with root, shell access as the service user, or read access
  to `.env` can do the same. This is **not end-to-end encryption**: the server
  operator can read all notes.
- **Note titles and metadata.** Titles of notebooks and notes, timestamps, and
  slugs are stored in plaintext.
- **A compromised account.** Anyone who signs in with a user's password sees that
  user's notes. There is no built-in two-factor authentication or account lockout.
- **Losing `NOTES_KEK`.** Without it, all notes are permanently unrecoverable.
  That is by design.

## Hardening notes for operators

- Keep `DEBUG=False` in production, and set strong `DJANGO_SECRET_KEY` and
  `NOTES_KEK` values. The app refuses to start without them.
- Leave `ACCOUNT_SIGNUP_ENABLED=False` unless you intend to run a public
  instance.
- The application has no login throttling of its own. The sample nginx config in
  `deploy/nginx/notebooks.conf` rate-limits `/accounts/login/`. Keep that, or
  add equivalent protection if you use a different proxy.
- No Content-Security-Policy is sent. Alpine.js's standard build evaluates
  expressions at runtime, so a strict CSP would need `'unsafe-eval'` or a
  switch to Alpine's CSP build. Rendered Markdown is sanitised server-side with
  nh3 using an explicit allowlist.
- Follow [DEPLOY.md](DEPLOY.md) for TLS, HSTS, and backups.
