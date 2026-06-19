# Captain of Industry — Production Planner

A free, web-based production planner for [Captain of Industry](https://www.captain-of-industry.com/).
Design production complexes, calculate resource flows automatically, and share blueprints
with the community.

![CoI Production Planner](web_public/static/og-preview.png)

---

## Features

- **Browse** every in-game recipe and building — workers, power, computing, maintenance, I/O — with full-text and structured search (`in:`, `out:`, `name:`, `by:`, `tag:`, `#`, with `& | ()`).
- **Visual complex editor** — drag-and-drop nodes on a canvas, auto-connect matching resources, and let the planner balance counts for you.
- **LCM (НОК) ratios** — snap connected machines to whole-number ratios across fan-in *and* fan-out topologies.
- **Idle / external ports** — mark a port as externally supplied or throttle a node to satisfy demand only.
- **Share & discover** — publish complexes (UUID slugs), browse the community tab, like, fork, and bookmark others' builds.
- **Ghost complexes** — shared builds stay viewable (frozen) even after the original is deleted or hidden.
- **Hashtags** — tag complexes and search by tag in both your library and the community.
- **Accounts** — automatic guest sessions, one-time email codes, optional Google OAuth and Steam OpenID. Guest work merges into your account on first login.
- **i18n** — full English / Russian UI plus translated game content.
- **Dark / light theme**, persisted locally.

## Tech stack

| Layer    | Choice |
|----------|--------|
| Backend  | Flask 3, Python 3.12 |
| Database | PostgreSQL 14 (psycopg2) |
| Frontend | Alpine.js + Tailwind CSS (no build step) |
| Auth     | Authlib (OAuth), Flask-Login, one-time email codes |
| Server   | Gunicorn behind Nginx (production) |

---

## Project layout

```
web_public/
├── app.py              # Flask app, routes, API (dev port 5001)
├── auth.py             # guest UUID · email code · Google OAuth · Steam OpenID
├── db.py               # psycopg2 connection helper
├── requirements.txt    # runtime deps
├── requirements-dev.txt# test/lint deps
├── i18n/               # en.json · ru.json
├── sql/
│   ├── schema.sql      # full DDL — single file for a fresh database
│   └── migration_v*.sql# incremental upgrades for existing databases
├── templates/          # index.html · complex_editor.html · privacy.html · about.html
├── static/
│   ├── icons/          # resource & building PNGs
│   └── og-preview.png  # Open Graph card
└── tests/              # pytest unit/API + tests/e2e (Playwright smoke)
```

---

## Local development

### 1. Requirements
- Python 3.12+
- PostgreSQL 14+

### 2. Install
```bash
cd web_public
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Create the database
```bash
createdb coi_public
psql -d coi_public -f sql/schema.sql
```
`schema.sql` is a complete, up-to-date DDL — it is all you need for a new database.
The `migration_v*.sql` files are only for upgrading databases created from an older schema.

> Game data (recipes, buildings, resource flows) is loaded separately into the database;
> the schema defines the tables that hold it.

### 4. Configure
```bash
cp .env.example .env
```
Edit `.env` and set at least:
- `DATABASE_URL` — your PostgreSQL connection string
- `SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`

Everything else is optional for local use:
- **Email** — leave `SMTP_HOST` empty and login codes print to the console (dev mode).
- **OAuth** — leave Google/Steam keys blank to disable those buttons.

### 5. Run
```bash
python app.py
```
Open http://localhost:5001

---

## Configuration reference

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string |
| `SECRET_KEY` | Flask session signing key (**required**) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth (optional) |
| `STEAM_API_KEY` | Steam OpenID (optional) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM` | Email codes; if `SMTP_HOST` is unset, codes are logged to the console |
| `MAX_COMPLEXES_PER_USER` | Per-account complex cap; `0` = unlimited |
| `RATELIMIT_STORAGE_URI` | Flask-Limiter backend (default in-memory; use Redis in production) |
| `TRUST_PROXY` | Set to `1` only when running behind Nginx, so Flask trusts `X-Forwarded-*` (HTTPS in OAuth redirects and `og:url`) |

See `.env.example` for the annotated, complete list.

---

## CLI commands

```bash
flask cleanup-guests [--months N] [--dry-run]   # remove stale guest accounts (default 6 months)
flask cleanup-ghosts [--dry-run]                # purge orphaned ghost complexes
flask send-test-email you@example.com           # verify SMTP configuration
```

---

## Tests

```bash
pip install -r requirements-dev.txt

pytest                       # unit + API tests
pytest tests/e2e/ -v         # Playwright smoke tests (needs a running server)
pytest tests/e2e/ -v --base-url http://localhost:5001
```

CI runs the unit/API suite on every push (GitHub Actions).

---

## Production deployment (outline)

```
Nginx (TLS via Let's Encrypt) → Gunicorn → Flask app
                                  PostgreSQL (local or managed)
```

1. Install Python 3.12, PostgreSQL, Nginx, Certbot.
2. `pip install -r requirements.txt`, apply `sql/schema.sql`.
3. Create `.env` with a strong `SECRET_KEY`, `DATABASE_URL`, SMTP, and `TRUST_PROXY=1`.
4. Run `gunicorn -w 2 -b 127.0.0.1:8000 app:app` (behind a systemd unit).
5. Configure Nginx as a reverse proxy with a long cache for `/static/icons/`, and obtain a certificate with Certbot.

---

## License

This is a fan-made tool and is not affiliated with or endorsed by MaFi Games.
Captain of Industry and related assets are property of their respective owners.
