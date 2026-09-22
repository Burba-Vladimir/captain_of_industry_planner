# Captain of Industry — Production Planner

### 🔗 Live: **[coi-planner.com](https://coi-planner.com/)** — free, no install, no account required

A web-based production planner for [Captain of Industry](https://www.captain-of-industry.com/).

Design a production chain as a graph, save it as a **reusable block**, and drop that block inside
a bigger one. Publish what you build — or start from a complex another player has already shared
and fork it. Beyond the calculator there is a settlement-needs planner (how much food, medicine and
goods a population of a given size actually consumes) and an automatic farm layout that handles
crop rotation.

![CoI Production Planner](web_public/static/og-preview.png)

**Reference pages** (no JavaScript needed, English and Russian):
[all resources](https://coi-planner.com/items) ·
[all buildings](https://coi-planner.com/buildings) ·
[settlement needs](https://coi-planner.com/population)

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
- **Settlement needs** — how much food, medicine, goods and utilities a population of a given size consumes, down to buildings, workers and upkeep.
- **Farm layout** — crops distributed across plantations automatically: fewest fields, least wasted surplus, with crop rotation respected.
- **Reference pages** — server-rendered pages for every resource and building (`/items`, `/item/<slug>`, `/buildings`, `/building/<slug>`): what makes it, what consumes it, at what rate. Work without JavaScript.
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
├── seo.py              # server-rendered reference pages · robots.txt · sitemap.xml
├── monitoring.py       # daily snapshots · health checks · admin dashboard data
├── db.py               # psycopg2 connection helper
├── requirements.txt    # runtime deps
├── requirements-dev.txt# test/lint deps
├── i18n/               # en.json · ru.json
├── sql/
│   ├── schema.sql      # full DDL — single file for a fresh database
│   └── migration_v*.sql# incremental upgrades for existing databases
├── templates/          # index.html · complex_editor.html · population.html
│                       # about.html · privacy.html · seo_*.html · sitemap.xml
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
| `ADMIN_EMAILS` | Comma-separated addresses allowed into `/admin/stats`; also the recipients of monitoring alerts. Empty disables the dashboard entirely |
| `MONITOR_*` | Alert thresholds — see the Monitoring section and `.env.example` |
| `SITE_URL` | Public origin, e.g. `https://coi-planner.com`. Used for absolute URLs in `sitemap.xml` and canonical tags — **set it in production**, or the sitemap will point at the internal host over `http` and search engines will ignore it |

See `.env.example` for the annotated, complete list.

---

## CLI commands

```bash
flask stats                                     # one-off traffic summary to the console
flask collect-stats [--day YYYY-MM-DD]          # write a daily snapshot into daily_stats
flask collect-stats --backfill 60               # reconstruct the last 60 days from created_at
flask cleanup-guests [--months N] [--dry-run]   # remove stale guest accounts (default 6 months)
flask cleanup-ghosts [--dry-run]                # purge orphaned ghost complexes
flask send-test-email you@example.com           # verify SMTP configuration
```

---

## Monitoring

A daily snapshot of activity and system health is written to `daily_stats`, and
`/admin/stats` renders it as a private dashboard.

**Why snapshots and not live queries.** Most user metrics could be recomputed from
`created_at` — but `cleanup-guests` deletes guests older than six months and takes the
traffic history with them. Database size, free disk and "active in the last 24h"
(`last_seen_at` holds one current value per user, not a history) cannot be reconstructed
at all.

### Setup

1. Apply the migration — `psql -d coi_public -f sql/migration_v10_monitoring.sql`
   (already included in `schema.sql` for fresh databases).
2. Set `ADMIN_EMAILS` in `.env` to the address you log in with. Without it the
   dashboard does not exist for anyone and alert emails have nowhere to go.
3. Seed some history: `flask collect-stats --backfill 60`.
4. Schedule the daily run.

```bash
# crontab -e — every day at 00:20 UTC, for the day that just ended
20 0 * * *  cd /app && /app/.venv/bin/flask collect-stats >> /var/log/coi_stats.log 2>&1
```

Or as a systemd timer, if the rest of the deployment already uses systemd:

```ini
# /etc/systemd/system/coi-stats.service
[Unit]
Description=CoI Planner daily stats
After=network.target postgresql.service

[Service]
Type=oneshot
WorkingDirectory=/app
EnvironmentFile=/app/.env
ExecStart=/app/.venv/bin/flask collect-stats

# /etc/systemd/system/coi-stats.timer
[Unit]
Description=Run CoI Planner stats daily

[Timer]
OnCalendar=*-*-* 00:20:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` matters: if the server was down at 00:20, the run happens on the next
boot instead of being skipped.

### Access

`/admin/stats` and `/admin/stats.json` are restricted to `ADMIN_EMAILS` and return **404**
to everyone else — a 403 would confirm the route exists to anyone probing for it. The page
carries `noindex` and is excluded from `sitemap.xml`.

### Alerts

After each collection the snapshot is compared against the previous two weeks. A single
email goes to `ADMIN_EMAILS` listing everything that fired:

| Check | Fires when |
|-------|-----------|
| `disk_low` | free disk below `MONITOR_DISK_MIN_RATIO` (default 15%; critical under 5%) |
| `db_growth` | database grew more than `MONITOR_DB_GROWTH_RATIO` in a day (default 25%) |
| `no_traffic` | zero new visitors on a day that normally has some — usually means the site is down |
| `traffic_drop` | new visitors below `MONITOR_TRAFFIC_DROP_RATIO` of the weekly median, and only once the median passes `MONITOR_TRAFFIC_MIN_BASELINE` |
| `collector_gap` | a gap in the snapshot history, detected on the next successful run |

`UNIQUE (day, kind)` on `monitor_alerts` means the same check never emails twice for the
same day, however often the collector runs.

> **Limitation worth knowing.** These checks run *inside* the collector, so a host that is
> fully down sends nothing — there is no one left to send it. The dashboard shows a red
> banner once the newest snapshot is over 36 hours old, but catching an outage while it is
> happening needs an external pinger (UptimeRobot, Healthchecks.io or similar) hitting the
> site on a schedule.

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
