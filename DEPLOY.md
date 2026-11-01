# Deploying Notebooks

Target: a **Debian** VPS running the app under **gunicorn**, behind **nginx**
terminating TLS, with **SQLite** on local disk.

Worked example throughout:

| Thing | Value used below |
| --- | --- |
| Domain | `notes.example.com` |
| Install path | `/home/deploy/web/notebooks` |
| Service user | `deploy` |
| nginx user | `www-data` (Debian default) |
| OS | Debian 12 (bookworm), nginx 1.22 |
| nginx | shared with other sites — only this app's vhost is added |
| Admin path | `/siteadmin/` |

Substitute your own values. Sample config files live in [`deploy/`](deploy/)
and are referenced at each step. They stay generic in the repo: server-specific
changes (worker count, nginx version syntax) are made **only in the copies
under `/etc`**, so the checkout stays clean and `git pull` never conflicts.

> **Two keys, two different consequences.** `DJANGO_SECRET_KEY` can be rotated
> freely — it only invalidates sessions. `NOTES_KEK` is the master key for note
> encryption: **lose it and every note is permanently unreadable**, and
> changing it incorrectly destroys all of them. See
> [Rotating the encryption key](#rotating-the-encryption-key) before you touch it.

---

## 1. Server prerequisites

```bash
sudo apt update
sudo apt install -y nginx git curl
```

### Python

The project requires **Python ≥ 3.12**. Debian 12 (bookworm) ships 3.11, so the
system interpreter is too old and `uv sync` will refuse to build the
environment. Rather than adding a third-party APT repo, let `uv` supply its own
interpreter — this works unchanged on trixie (3.13) too.

`uv` is not packaged by Debian, so install it from the official script:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# puts uv in ~/.local/bin -- start a new shell, or:
source "$HOME/.local/bin/env"

uv python install 3.12
```

## 2. Get the code

```bash
sudo mkdir -p /home/deploy/web
sudo chown deploy:deploy /home/deploy/web

git clone <your-repo-url> /home/deploy/web/notebooks
cd /home/deploy/web/notebooks
uv sync --frozen
```

`--frozen` installs exactly what `uv.lock` pins and fails if the lockfile is
out of date, so a deploy can never silently resolve different versions than you
tested.

### Let nginx reach the static files

The install path is under `/home`, and nginx must be able to traverse every
directory above `staticfiles/` to serve it. Debian's default home permissions
vary by release and by how the account was created, so **check rather than
assume**:

```bash
ls -ld /home/deploy /home/deploy/web
```

If either lacks the final `x` for "other" (i.e. is not `drwxr-xr-x` or
similar):

```bash
chmod o+x /home/deploy /home/deploy/web
```

This is the single most common cause of a working site that serves every page
unstyled with `403` on `/static/…`.

## 3. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Generate a fresh `DJANGO_SECRET_KEY`:

```bash
uv run python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```

`NOTES_KEK` depends on where the database comes from:

- **Bringing an existing database** (see [step 4](#bringing-an-existing-database)):
  **do not generate a new key.** Copy `NOTES_KEK` verbatim from the `.env` of
  the machine that database came from. Every note in it is wrapped by that key;
  a different one makes all of them unreadable.
- **Starting empty:** generate one:

  ```bash
  uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

Minimum production set:

```ini
DEBUG=False
DJANGO_SECRET_KEY=<generated above>
NOTES_KEK=<copied from the source .env, or generated above>
ALLOWED_HOSTS=notes.example.com
CSRF_TRUSTED_ORIGINS=https://notes.example.com
ACCOUNT_SIGNUP_ENABLED=False
ADMIN_URL=siteadmin/
SECURE_HSTS_SECONDS=0
```

**Back up `NOTES_KEK` somewhere separate from the database now.** A database
backup without this key is unreadable ciphertext.

On `ADMIN_URL`: it must end with a slash. Moving it off `admin/` is obscurity,
not security — Django's auth is still what protects the admin — but it removes
essentially all drive-by bot probing from your logs.

## 4. Database and static files

### Bringing an existing database

Skip this if starting empty. Otherwise copy the database in **before**
`migrate`:

1. On the source machine, stop `runserver` (or whatever has the database
   open). A clean shutdown folds the WAL back into the main file.
2. Confirm there is no `db.sqlite3-wal` next to it. If there is, something
   still has the database open — a plain copy would miss those commits.
3. Copy only `db.sqlite3` (never `-wal`/`-shm`) into the checkout:

   ```bash
   scp db.sqlite3 deploy@<vps>:/home/deploy/web/notebooks/
   ```

The file must end up owned by `deploy`. Its user accounts come with it, so
skip `createsuperuser` below.

### Migrate and collect static

```bash
uv run manage.py migrate
uv run manage.py createsuperuser          # starting empty only
uv run manage.py collectstatic --noinput
```

`collectstatic` reporting *"2 skipped due to conflict"* is expected:
django-unfold deliberately overrides two Django admin JavaScript files.

Confirm the deploy checklist is clean:

```bash
uv run manage.py check --deploy
```

This should print `System check identified no issues`. If it warns about
`SECRET_KEY`, `DEBUG`, or cookies, `.env` is not being read — check that it
sits next to `manage.py` and that `DEBUG=False`.

### Where the CSS comes from

`static/css/app.css` is **committed to the repository**, so the server needs
neither Node nor the ~111 MB Tailwind binary. Build it on your development
machine and commit the result *before* deploying:

```bash
./tailwindcss -i theme/static_src/app.css -o static/css/app.css --minify
git commit -am "Rebuild CSS"
```

If you change a template and forget this step, the deployed site renders
without the new styles.

## 5. gunicorn under systemd

Two units. The **socket** unit is the one you enable — systemd creates the unix
socket, owns it, and hands it to gunicorn. That is what lets the socket be
owned by `deploy` but group-readable by `www-data`, without adding either user
to the other's group.

```bash
sudo cp deploy/systemd/notebooks.socket /etc/systemd/system/
sudo cp deploy/systemd/notebooks.service /etc/systemd/system/
```

Edit both if your user or paths differ.

**Worker count.** `deploy/gunicorn.conf.py` sets `workers = 3`. To change it
for this server without touching the repo, add a flag to `ExecStart` in
`/etc/systemd/system/notebooks.service` — command-line flags override the
config file:

```ini
ExecStart=/home/deploy/web/notebooks/.venv/bin/gunicorn \
    --config deploy/gunicorn.conf.py \
    --workers 2 \
    config.wsgi:application
```

Budget roughly 60–100 MB per worker, and keep it low either way: every worker
is another potential SQLite writer.

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now notebooks.socket
sudo systemctl status notebooks.socket
```

The service starts on the first request to the socket. Force it and check:

```bash
sudo -u www-data curl --unix-socket /run/notebooks/gunicorn.sock -I \
  -H 'Host: notes.example.com' -H 'X-Forwarded-Proto: https' \
  http://localhost/accounts/login/
sudo systemctl status notebooks
journalctl -u notebooks -n 50
```

A `200` here means the application layer is working, and anything still
broken afterwards is nginx. Run it as `www-data`, not as yourself: the
socket's owner can always connect, so a test as `deploy` passes even when
nginx gets `Permission denied`. The two headers stand in for what nginx will send:
without `Host`, curl sends `localhost`, which is not in `ALLOWED_HOSTS` — you
get a `400` and a `DisallowedHost` traceback in the journal (the app is fine;
the test is wrong). Without `X-Forwarded-Proto` you get a `301` to `https://`,
which is `SECURE_SSL_REDIRECT` working as intended. The *"WSGI app sent body
bytes on a HEAD request"* warning is harmless.

## 6. nginx

This nginx also serves other sites, so this step only **adds** one vhost. Do not
remove `sites-enabled/default` or edit any other site's config.

Certbot needs a working port-80 vhost before the certificate exists, and the
sample file references certificate paths that are not yet present. So install
it in two passes.

**First** — copy the sample:

```bash
sudo cp deploy/nginx/notebooks.conf \
        /etc/nginx/sites-available/notes.example.com
```

Then, in that copy only:

- **Comment out the entire `server { listen 443 …}` block** for now.
- **Debian 12 only:** its nginx (1.22) predates the `http2 on;` directive
  (1.25.1+) and `nginx -t` rejects it. Delete that line and put `http2` on the
  listen lines instead. Check with `nginx -v`; Debian 13 (1.26) needs no change.

  ```nginx
  listen 443 ssl http2;
  listen [::]:443 ssl http2;
  ```

The sample declares `upstream notebooks_app` and the `notebooks_login`
rate-limit zone (which throttles `/accounts/login/`); both names are global
across every vhost, so make sure no other site already uses them (this should
print nothing before the symlink exists):

```bash
grep -rE 'notebooks_app|notebooks_login' /etc/nginx/sites-enabled/ /etc/nginx/conf.d/
```

Enable it:

```bash
sudo ln -s ../sites-available/notes.example.com \
           /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

A failed `nginx -t` blocks only the reload — the other sites keep serving on
the previous config.

**Then** get the certificate. Use `certonly` so certbot obtains the certificate
without rewriting the hand-written vhost (plain `certbot --nginx` edits it):

```bash
sudo apt install -y certbot python3-certbot-nginx   # skip if already installed
sudo certbot certonly --nginx -d notes.example.com
```

**Finally** check that the two certbot-managed files the TLS block includes
exist (they will if other sites on this server already use certbot's nginx
plugin):

```bash
ls /etc/letsencrypt/options-ssl-nginx.conf /etc/letsencrypt/ssl-dhparams.pem
```

Then uncomment the TLS block, and `sudo nginx -t && sudo systemctl reload
nginx`.

Certbot installs a systemd timer for renewal. Verify it:

```bash
sudo systemctl list-timers | grep certbot
sudo certbot renew --dry-run
```

## 7. Turn on HSTS — last, and only once TLS works

HSTS is deliberately off (`SECURE_HSTS_SECONDS=0`) until this point. Browsers
cache the directive for its **full duration**, so publishing it with a broken
or mis-issued certificate locks visitors out of the domain for that long, with
no way to override from the server side.

Load `https://notes.example.com` and confirm the certificate is valid.
Only then:

```ini
SECURE_HSTS_SECONDS=31536000
```

```bash
sudo systemctl restart notebooks
```

Start with a short duration (`3600`) if you want a cautious first pass.
`SECURE_HSTS_INCLUDE_SUBDOMAINS` and `SECURE_HSTS_PRELOAD` are left unset: on a
subdomain like `notes.` they add nothing useful, and preload only applies to an
apex domain.

---

## Search engine and AI crawler exclusion

Three layers, all shipped:

| Layer | Where |
| --- | --- |
| `robots.txt` with a named AI-crawler denylist | `templates/robots.txt`, served at `/robots.txt` |
| `<meta name="robots" content="noindex, …">` | `templates/base.html` |
| `X-Robots-Tag` response header | nginx, on every response |

Verify after deploying:

```bash
curl -s https://notes.example.com/robots.txt | head
curl -sI https://notes.example.com/accounts/login/ | grep -i x-robots-tag
```

**What this does and does not do.** Every note is behind `@login_required` and
encrypted at rest, so no crawler can reach note *content* regardless of these
settings — authentication is the actual control. What the three layers above
protect is the publicly reachable surface: the sign-in and registration pages.
They are advisory: compliant crawlers honour them, and anything ignoring
`robots.txt` was never going to get past the login anyway.

The AI crawlers are named individually rather than relying on `User-agent: *`
because several have historically treated a wildcard block as not applying to
them.

---

## Operations

### Updating

```bash
cd /home/deploy/web/notebooks
git pull
uv sync --frozen
uv run manage.py migrate
uv run manage.py collectstatic --noinput
sudo systemctl restart notebooks
```

Keep this order: `collectstatic` before the restart. Static URLs are versioned
by the collected copies' mtimes, read once per process, so a `collectstatic`
run after the restart keeps serving the old JS/CSS until the next restart --
and new views running against old `offline-notes.js` fail every save after the
first with a 409.

The installed copies under `/etc/systemd/system/` and
`/etc/nginx/sites-available/` are **not** updated by `git pull`. If a commit
changes anything in `deploy/`, diff it against the installed copy and merge by
hand, keeping the server-specific edits (`--workers`, the `http2` listen
syntax). After changing a unit file, `sudo systemctl daemon-reload`.

### Backups

The database holds every note as ciphertext. **A backup is only useful
alongside a copy of `NOTES_KEK`** — store the key separately, not in the same
archive.

Do **not** back up by copying `db.sqlite3` with `cp`. The database runs in WAL
mode, so recent commits may live in the `-wal` sidecar and a plain file copy
can capture a torn state. Use SQLite's own online backup, which is safe against
a running server:

```bash
sudo apt install -y sqlite3        # not installed by default on Debian
mkdir -p /home/deploy/backups

sqlite3 /home/deploy/web/notebooks/db.sqlite3 \
  "VACUUM INTO '/home/deploy/backups/notebooks-$(date +%F).sqlite3'"
```

`VACUUM INTO` writes a single consistent file with the WAL contents already
folded in, while the server keeps running.

To restore: stop the service, move the backup into place as `db.sqlite3`,
delete any stale `-wal`/`-shm` files, start the service.

### Rotating the encryption key

**Order matters and is not reversible.** The command needs the *old* key to
unwrap existing keys, so it must run *before* `.env` changes.

```bash
# 1. Generate the new key
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Back up the database first (see above)

# 3. Re-wrap every user's key, with .env still holding the OLD key
uv run manage.py rotate_kek '<new key>'

# 4. Only now edit NOTES_KEK in .env to the new value
# 5. Restart
sudo systemctl restart notebooks
```

The command runs in a single transaction and aborts without changing anything
if any key fails to unwrap. Editing `.env` first makes every wrapped key
permanently unreadable.

Note content itself is never re-encrypted — only the per-user keys are
re-wrapped — so this is fast regardless of how many notes exist.

### Logs

Application and gunicorn logs go to the journal; nginx keeps its own files.

```bash
journalctl -u notebooks -f
journalctl -u notebooks -p err --since today
sudo tail -f /var/log/nginx/notebooks.error.log
```

### Adding users

Self-registration is off by default. Create accounts with:

```bash
uv run manage.py createsuperuser
```

or through the admin. Each new user automatically gets their own encryption key
via a `post_save` signal — nothing extra to do.

To open public registration instead, set `ACCOUNT_SIGNUP_ENABLED=True` and
restart.

There is no password-reset flow (no mail is configured). To reset a password:

```bash
uv run manage.py changepassword <username>
```

---

## Troubleshooting

**Redirect loop / `ERR_TOO_MANY_REDIRECTS`**
nginx is missing `proxy_set_header X-Forwarded-Proto $scheme;`. Django's
`SECURE_PROXY_SSL_HEADER` reads that header to decide a request arrived over
HTTPS; without it `SECURE_SSL_REDIRECT` redirects forever. The two settings are
a matched pair.

**502 Bad Gateway**
The socket exists but nothing is serving it, or nginx cannot open it.

```bash
sudo systemctl status notebooks
journalctl -u notebooks -n 50
ls -l /run/notebooks/gunicorn.sock     # expect  srw-rw---- deploy www-data
```

If nginx's error log shows `(13: Permission denied)` and the socket is
`deploy deploy` instead of `deploy www-data`, the installed
`notebooks.service` still has a `RuntimeDirectory=` line (older copies of the
sample did). systemd re-owns everything under that directory to the service
user on start. Delete the `RuntimeDirectory*` lines, then:

```bash
sudo systemctl daemon-reload
sudo systemctl stop notebooks.service notebooks.socket
sudo rm -rf /run/notebooks
sudo systemctl start notebooks.socket
```

Test as nginx's user, not your own — the owner can always connect:

```bash
sudo -u www-data curl -I --unix-socket /run/notebooks/gunicorn.sock \
  -H 'Host: notes.example.com' -H 'X-Forwarded-Proto: https' \
  http://localhost/accounts/login/
```

If the service fails at startup with a mount or namespace error, check the
`ReadWritePaths=` line in the unit actually matches your install path —
`ProtectSystem=strict` makes everything else read-only.

**Pages load but are completely unstyled, `403` on `/static/…`**
nginx cannot traverse into the home directory. See
[step 2](#let-nginx-reach-the-static-files).

**Pages are styled, but recent design changes are missing**
`static/css/app.css` was not rebuilt and committed before deploying.

**`DisallowedHost` in the logs**
Add the domain to `ALLOWED_HOSTS` in `.env` and restart.

**CSRF failures on login or save**
`CSRF_TRUSTED_ORIGINS` must include the scheme:
`https://notes.example.com`, not the bare hostname.

**`django.core.exceptions.ImproperlyConfigured` at startup**
A required variable is missing from `.env`. `NOTES_KEK` is required always;
`DJANGO_SECRET_KEY` is required whenever `DEBUG=False`. Both refuse to fall
back to a default on purpose.

**`database is locked`**
Should not occur — SQLite runs in WAL mode with a 20s timeout. If it does, the
database is probably on a network filesystem (WAL needs local disk), or the
directory is not writable by the service user (WAL needs to create `-wal` and
`-shm` sidecars *next to* the database, so write access to the file alone is
not enough).

---

<sub>Deploying to a SELinux host (Fedora/RHEL) instead? The steps above are
otherwise the same, but you will additionally need to label `staticfiles/` with
`httpd_sys_content_t` and set the `httpd_can_network_connect` boolean. Not
covered here.</sub>
