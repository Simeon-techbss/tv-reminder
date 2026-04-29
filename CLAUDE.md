# TV Reminder

A Flask web app that tracks TV shows and sends weekly email reminders. Live at **https://tv-reminder.vercel.app** — deployed on Vercel (serverless), auto-deploys from GitHub main.

## Architecture

- **Frontend**: Single-page app in `templates/index.html` — plain HTML/JS, no build step
- **Backend**: Flask app in `app.py` — all API routes
- **Vercel entry point**: `api/index.py` — wraps `app.py` for serverless
- **Cron job**: `/api/cron/daily` runs at 07:00 UTC daily (defined in `vercel.json`) — fetches TVMaze schedule, upserts episode cache, fans out reminder emails per user
- **Email logic**: `tv_reminder.py` — handles TVMaze fetching, email formatting, and sending; imported by `app.py`
- **Database**: Neon PostgreSQL (serverless Postgres). Connection via `DATABASE_URL` env var

## Key files

| File | Purpose |
|------|---------|
| `app.py` | Flask app — all API routes |
| `api/index.py` | Vercel serverless entry point |
| `tv_reminder.py` | TVMaze schedule fetching, email formatting, SMTP send |
| `lib/db.py` | All DB queries (psycopg2) |
| `lib/auth.py` | bcrypt password hashing, JWT helpers, `require_auth`/`require_admin` decorators |
| `vercel.json` | Routing rewrites + cron schedule |

## Key features

- **Show tracking**: users add shows by name; TVMaze metadata cached in DB on first add
- **Episode cache**: cron job pre-fetches schedule and stores in `episode_cache` table — no live TVMaze calls during requests
- **UK platform lookup**: JustWatch GraphQL (no API key needed) — looks up where a show streams in the UK, stored per show in DB
- **Email reminders**: per-user fanout — each user gets reminders for their own tracked shows only
- **Auth**: JWT in httpOnly cookie (30-day expiry), bcrypt passwords, first registered user is admin
- **Admin panel**: stats, manual cache refresh, bulk platform refresh

## Environment variables (set in Vercel dashboard)

- `DATABASE_URL` — Neon Postgres connection string
- `JWT_SECRET` — any long random string
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD` — Gmail SMTP
- `EMAIL_FROM` — sender address
- `CRON_SECRET` — bearer token to authenticate the cron endpoint
- `DASHBOARD_URL` — defaults to `https://tv-reminder.vercel.app`

## Deploying

Push to GitHub main — Vercel auto-deploys. No build step.

```bash
cd /home/sherbert/tv-reminder
git add -p
git commit -m "your message"
git push
```

## Auth model

- First registered user is automatically admin
- Passwords: bcrypt rounds=12
- Sessions: JWT in `tv_token` httpOnly cookie
- `require_auth` / `require_admin` decorators on protected routes

## Development setup

**Repo**: `git@github.com:Simeon-techbss/tv-reminder.git`

Simeon codes from either the Pi or the Mac — check which you're on.

### From the Pi
- Code lives at `/home/sherbert/tv-reminder`
- Push via SSH — key at `~/.ssh/id_ed25519_github`, already configured in `~/.ssh/config`
- `git push` works directly, no extra setup needed

### From the Mac
- Repo is not cloned locally — clone it first:
  ```bash
  git clone https://github.com/Simeon-techbss/tv-reminder.git
  ```
- GitHub credentials stored in Mac keychain (account: `Simeon-techbss`)
- macOS will prompt for login password the first time to unlock keychain — that's normal and safe
