# PlanAble MVP

A Flask MVP for:
- private caller worksheet
- Google login protected search engine
- accessibility profiles with last verified + comments
- OBS browser-source widgets

## Run locally

```bash
cd planable_app
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add:

```env
SECRET_KEY=anything-random
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
ADMIN_EMAILS=your-google-email@example.com
```

Google OAuth redirect URI for local dev:

```text
http://127.0.0.1:5000/auth/google/callback
```

Then:

```bash
flask --app app init-db
flask --app app seed
flask --app app run
```

Open:

```text
http://127.0.0.1:5000
```

## Key pages

Public/search:
- `/` landing page
- `/search` protected results
- `/place/<slug>` protected detail page

Admin worksheet:
- `/dashboard`
- `/admin/place/new`
- `/admin/place/<id>/call`

OBS browser sources:
- `/obs/current-call`
- `/obs/progress`

## Notes

Search is intentionally login-gated to protect the early verified dataset.
Only emails listed in `ADMIN_EMAILS` can access the worksheet/admin pages.
