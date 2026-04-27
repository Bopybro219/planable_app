# PlanAble

PlanAble is a Flask app for:

- Google-login-gated venue search
- accessibility profiles with verification metadata
- admin calling workflows
- OBS browser-source widgets
- early Stripe-based plan and API-pack checkout flows

## Local development

```bash
cd planable_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Update `.env` with at least:

```env
SECRET_KEY=replace-with-a-long-random-secret
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
ADMIN_EMAILS=your-google-email@example.com
APP_ENV=development
```

Database configuration:

```env
# Leave unset for local SQLite fallback
# DATABASE_URL=
```

PostgreSQL example:

```env
DATABASE_URL=postgresql+psycopg2://planira_user:password@localhost:5432/planira
```

Local Google OAuth callback:

```text
http://127.0.0.1:5000/auth/google/callback
```

Initialize and run:

```bash
flask --app app init-db
flask --app app seed
flask --app app check-config
flask --app app run
```

SQLite fallback notes:

- If `DATABASE_URL` is unset, the app falls back to `sqlite:///planable.db` for local development only.
- Local SQLite startup may still auto-create tables in development/test mode for convenience.
- Production and PostgreSQL environments should use Flask-Migrate instead of `db.create_all()`.

## Migrations

Initialize Alembic once per clone:

```bash
flask --app app db init
```

Create and apply migrations:

```bash
flask --app app db migrate -m "initial postgres schema"
flask --app app db upgrade
```

Backup warning:

- Do not delete the existing SQLite database before validating PostgreSQL.
- Take a copy of the SQLite file before running any manual migration or import steps.

### SQLite backup

Create a timestamped backup before any recovery or import work:

```bash
mkdir -p instance/backups
cp instance/planable.db "instance/backups/planable-$(date +%Y%m%d-%H%M%S).db"
```

### Stamp an existing schema safely

If the SQLite database already has the current tables and columns, but `alembic_version` is empty or missing the current revision, stamp it instead of recreating tables:

```bash
flask --app app db heads
flask --app app db stamp 60890b0d6a4b
```

Check the stamped revision:

```bash
sqlite3 instance/planable.db "SELECT version_num FROM alembic_version;"
```

### Venue import from `wetherspoons_locations.json`

Dry-run is the default and makes no database changes:

```bash
python import.py
```

Dry-run a different file:

```bash
python import.py --json-file custom_venues.json
```

Apply the import only when you are ready:

```bash
python import.py --apply
```

What the importer does:

- reads `wetherspoons_locations.json`
- inserts `Place` rows only
- inserts `AccessibilityProfile` rows only when the JSON actually includes accessibility fields
- skips duplicates using `name + parsed address1 + postcode`
- prints before/after counts
- leaves `User`, `APIKey`, and `AuditLog` untouched
- rolls back the transaction on errors

## Production checklist

Set these before deploying:

```env
APP_ENV=production
SECRET_KEY=strong-random-secret
DATABASE_URL=postgresql+psycopg2://planira_user:password@db-host:5432/planira
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
ADMIN_EMAILS=admin@example.com
SESSION_COOKIE_SECURE=true
SERVER_NAME=your-domain.com
TRUSTED_HOSTS=your-domain.com,www.your-domain.com
PROXY_FIX_COUNT=1
LOG_LEVEL=INFO
```

Optional Stripe settings:

```env
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PAID_CONSUMER=price_...
STRIPE_PRICE_API_20=price_...
STRIPE_PRICE_API_50=price_...
STRIPE_PRICE_API_100=price_...
```

Recommended production startup:

```bash
flask --app app db upgrade
flask --app app check-config
gunicorn --workers 3 --bind 0.0.0.0:8000 wsgi:application
```

## Tests

Run the test suite with:

```bash
pytest
```

## Production hardening included

- CSRF protection on app forms
- safer post-login redirect handling
- hardened session cookie defaults
- proxy-aware request handling via `PROXY_FIX_COUNT`
- security headers on responses
- upload/request size cap via `MAX_CONTENT_LENGTH`
- `/health` endpoint for uptime checks
- `flask --app app check-config` for deploy validation
- `wsgi.py` entrypoint for Gunicorn

## Main routes

Public:

- `/`
- `/plans`
- `/health`

Authenticated:

- `/search`
- `/place/<slug>`
- `/account`

Admin:

- `/dashboard`
- `/admin/place/new`
- `/admin/place/<id>/call`

OBS widgets:

- `/obs/current-call`
- `/obs/progress`
