# Notebooks

A self-hosted Markdown notebook app with notes encrypted at rest. Organise notes
into notebooks, write in Markdown with a live preview, and run it on your own
server.

Built with Django, HTMX, Alpine.js and Tailwind CSS v4. No Node toolchain is
needed to run or deploy it.

## Features

- **Notebooks and notes** in a three-pane, single-page UI that also works on mobile.
- **Markdown** with tables, fenced code blocks and syntax highlighting. The rendered
  HTML is sanitised with [nh3](https://github.com/messense/nh3).
- **Autosave** with a live preview, plus offline drafts and retry when the
  connection drops.
- **Conflict-safe saves.** A stale tab or old draft can never overwrite newer
  content; you choose "Load latest" or "Keep mine".
- **Version history.** Notes are snapshotted while you edit, and restoring is
  non-destructive.
- **Trash** with undo and restore for notes and notebooks.
- **Encryption at rest** using per-user keys (see below).
- **Multi-user**, with strict per-user isolation. Self-registration is off by default.
- **Dark mode.**

## How encryption works

Each user has a random data-encryption key (DEK). Note content is encrypted with
that user's DEK, and the DEK itself is stored only in wrapped form, encrypted by a
single master key (`NOTES_KEK`) that lives in the server's environment and not in
the database.

This protects notes against theft or leakage of the database or its backups.
It does **not** protect against someone who controls the running server, because
the server holds the master key. This is not end-to-end encryption. See
[SECURITY.md](SECURITY.md) for the full threat model.

> **Back up `NOTES_KEK` separately from the database.** Without it, every note is
> permanently unreadable.

## Quickstart (development)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> notebooks && cd notebooks
uv sync
cp .env.example .env
```

Edit `.env`: set `DEBUG=True`, and set `NOTES_KEK` to a fresh key:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then:

```bash
uv run manage.py migrate
uv run manage.py createsuperuser
uv run manage.py runserver
```

Open <http://127.0.0.1:8000/> and sign in.

### Tests

```bash
uv run manage.py test
```

### CSS

Styles are written in `theme/static_src/app.css`, and the compiled
`static/css/app.css` is committed. If you add new Tailwind classes, rebuild it
with the [standalone Tailwind CLI](https://github.com/tailwindlabs/tailwindcss/releases/latest)
saved as `./tailwindcss` in the repo root:

```bash
./tailwindcss -i theme/static_src/app.css -o static/css/app.css --watch    # while developing
./tailwindcss -i theme/static_src/app.css -o static/css/app.css --minify   # before committing
```

## Deployment

[DEPLOY.md](DEPLOY.md) is a step-by-step guide for a Debian VPS with gunicorn,
nginx and SQLite. Sample configs are in [`deploy/`](deploy/). Every setting is
documented in [`.env.example`](.env.example).

## Security

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
not in public issues.

## License

Copyright (C) 2026 Shida Masaya

Notebooks is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE).

If you run a modified version as a network service, the AGPL requires you to
offer its source code to that service's users.

Bundled third-party components keep their own licenses; see
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
