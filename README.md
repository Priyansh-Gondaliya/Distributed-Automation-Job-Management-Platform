# AutoControl — Distributed File & Script Management

> Flask **controller** + lightweight Windows **worker agents** for scheduling, running, and monitoring automation scripts across LAN PCs — from one web dashboard.

The controller **never executes** automation scripts. Workers poll for jobs/commands and run everything locally.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Key Design Principles](#key-design-principles)
- [Project Layout](#project-layout)
- [Quick Start](#quick-start)
  - [Controller](#controller)
  - [Worker PC](#worker-pc)
  - [Local dual start](#local-dual-start)
- [Dashboard Features](#dashboard-features)
- [REST API Summary](#rest-api-summary)
- [Database](#database)
- [Environment Variables](#environment-variables)
- [Python Packages](#python-packages)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Docs & Tests](#docs--tests)

---

## Overview

| Piece | Role |
|--------|------|
| **Controller** | Flask app (`run.py`) + PostgreSQL. Web UI + REST API for workers. |
| **Workers** | `worker_agent/worker.py` on each PC. Poll jobs/commands, run scripts, sync file trees, show toasts. |

Typical uses: scrapers, batch jobs, remote file explore/edit, schedule folders, reports, and Schedule Tracking chat + desktop notify.

---

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │           Controller PC             │
                    │  python run.py                      │
                    │  ├── app/  (Flask package)          │
                    │  │    blueprints/api + web          │
                    │  │    services/ (scheduler, chat…)  │
                    │  │    templates/ + static/          │
                    │  └── PostgreSQL (tbl_dfms_*)        │
                    │  Port: 7561 (default)               │
                    └──────────────────┬──────────────────┘
                                       │ HTTP JSON
              poll register / jobs / commands / file sync
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
 ┌──────────────┐              ┌──────────────┐              ┌──────────────┐
 │ Worker PC 1  │              │ Worker PC 2  │              │ Worker PC N  │
 │ worker.py    │              │ worker.py    │              │ worker.py    │
 │ scripts\…    │              │ scripts\…    │              │ scripts\…    │
 └──────────────┘              └──────────────┘              └──────────────┘
```

**Main flow**
1. Worker registers / heartbeats (`POST /register-worker`)
2. Syncs scripts & file tree
3. Polls `GET /get-job/<worker>` and `GET /get-command/<worker>` (~1s)
4. Runs scripts locally; reports `POST /job-complete` (or error/stopped)
5. Commands cover file ops, config reload, and `desktop_notify` (Site Master toasts)

---

## Key Design Principles

| Principle | Description |
|-----------|-------------|
| No shared folders required | Workers use local paths only |
| Controller never runs scripts | Queue + monitor only |
| IP-based worker identity | One live agent per IP; dashboard names are source of truth |
| Auto script discovery | Worker scans configured scripts folder |
| Heartbeat status | Offline after ~30s without heartbeat |
| Command queue | Dashboard pushes rename / FS / notify commands; worker pulls them |

---

## Project Layout

```text
Distributed file management/
├── run.py                 # ★ Start controller: python run.py
├── app.py                 # Thin alias (also starts via run.app)
├── start_local.bat        # Starts controller + a local test worker
├── requirements.txt
├── .env / .env.example    # Secrets & DB (do not commit real passwords)
├── README.md
│
├── app/                   # ★ Flask application package
│   ├── __init__.py        # create_app()
│   ├── config.py          # Host, Postgres, chat API, paths
│   ├── database.py        # DAL (tbl_dfms_*)
│   ├── db_compat.py       # Postgres helpers / SQL adapt
│   ├── blueprints/
│   │   ├── api/routes.py  # Worker REST + admin APIs
│   │   └── web/routes.py  # Dashboard pages
│   ├── services/
│   │   ├── scheduler.py         # Background due-schedule runner
│   │   ├── schedule_folders.py  # Multi-script folder runs
│   │   └── chat_notify.py       # Schedule Tracking → Chat API
│   ├── templates/         # Jinja2 HTML
│   └── static/            # css / js / img
│
├── worker_agent/
│   └── worker.py          # ★ Deploy this to worker PCs (keep outside app/)
├── scripts/
│   └── init_db.py         # Schema seed / readiness check
├── tests/                 # Smoke / load / unit helpers
├── postgres/              # Schema review SQL + smoke scripts
├── docs/                  # Historical notes & reports
├── uploads/               # Legacy upload / artifact area
├── scratch/               # One-off tools (not runtime)
└── other pc worker/       # Optional deploy copy (not source of truth)
```

> `__init__.py` marks packages. `__pycache__/` is auto-generated bytecode — ignore it / don’t commit it.

---

## Quick Start

### Controller

1. **Python 3.10+** in the project root.

2. **venv + deps**
   ```powershell
   python -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure `.env`** (copy from `.env.example`)
   ```env
   PGHOST=192.168.50.18
   PGPORT=5432
   PGDATABASE=sitewisedata
   PGUSER=crawling
   PGPASSWORD=your-password-here
   PGSCHEMA=public
   FLASK_SECRET_KEY=change-me-in-production
   CHAT_BOT_TOKEN=          # optional Schedule Tracking chat
   ```

4. **Ensure Postgres tables exist** (company schema / `postgres/` review SQL), then:
   ```powershell
   python scripts\init_db.py
   ```

5. **Start**
   ```powershell
   python run.py
   ```
   Dashboard: `http://<controller-ip>:7561/`  
   Register / login, then allow **TCP 7561** from worker PCs.

### Worker PC

1. **Deploy agent**
   ```powershell
   mkdir C:\Automation -Force
   copy worker_agent\worker.py C:\Automation\worker.py
   mkdir C:\Automation\scripts -Force
   ```

2. **Deps on the worker**
   ```powershell
   pip install requests watchdog windows-toasts
   ```
   (`windows-toasts` = Site Master desktop notifications, no PowerShell.)

3. **Point at controller**
   ```powershell
   set CONTROLLER_URL=http://192.168.50.89:7561
   set WORKER_NAME=Priyansh
   ```

4. **Run**
   ```powershell
   python C:\Automation\worker.py
   ```

### Local dual start

From the repo (controller + test worker windows):

```powershell
.\start_local.bat
```

---

## Dashboard Features

| Area | What you get |
|------|----------------|
| **Home / Workers** | Online/offline, busy/idle, open detail |
| **Worker detail** | File Explorer (live FS sync), config path, jobs |
| **File IDE** | Remote read/write editor + change ledger / diffs |
| **Scheduler** | Schedules, folders, job history, view-other-user |
| **Reports** | Execution history, Schedule Tracking (admin), watchlist |
| **Permissions / Users** | PC grants, schedule grants, user admin |
| **Global History** | Audit / file history |
| **Chat + toast** | Tracking notes → Chat API + worker **Site Master** toast |

---

## REST API Summary

Worker-facing (no session cookie; LAN trust model):

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/register-worker` | Register / heartbeat |
| `POST` | `/sync-scripts` | Bulk script sync |
| `GET` | `/get-job/<worker_name>` | Poll job |
| `POST` | `/job-complete` | Success / result |
| `POST` | `/job-error` | Failure |
| `POST` | `/job-stopped` | Stopped |
| `GET` | `/get-command/<worker_name>` | Poll command |
| `POST` | `/command-complete` | Command result |
| `GET` | `/api/my-config` | Worker config by IP |
| `POST` | `/api/sync-file-tree` / `/api/sync-folder-partial` | File Explorer sync |

Dashboard JSON examples: `/api/schedules/list`, `/api/workers`, `/api/jobs`, Schedule Tracking chat/status, watchlist, folder APIs.

---

## Database

PostgreSQL tables use physical names like `tbl_dfms_*` (logical names in code via `db_compat`).

| Area | Examples |
|------|----------|
| Core | workers, scripts, jobs, users, commands |
| Access | user_pc_access, script/schedule access, scheduler view |
| Files | worker_file_tree, file_history |
| Scheduler | schedules, schedule_folders / items / runs |
| Reports | scraper / job report aggregates |

Schema review helpers: `postgres/`.

---

## Environment Variables

### Controller (`.env` or OS env)

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTROLLER_HOST` | `0.0.0.0` | Bind address |
| `CONTROLLER_PORT` | `7561` | HTTP port |
| `FLASK_SECRET_KEY` | `change-me-in-production` | Session secret |
| `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` / `PGSCHEMA` | (see `app/config.py`) | Postgres |
| `WORKER_OFFLINE_SECONDS` | `30` | Heartbeat timeout |
| `WORKER_ROOT` | `./uploads` | Legacy artifact dir |
| `CHAT_API_BASE` | internal Chat API URL | Schedule Tracking |
| `CHAT_BOT_TOKEN` | empty | Bot token (required for chat send) |
| `CHAT_SCHEDULES_CHANNEL` | `73` | Channel id |
| `CHAT_ALSO_DM_USER` | `1` | Also DM assigned user |

### Worker

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTROLLER_URL` | `http://192.168.50.89:7561` | Controller base URL |
| `WORKER_NAME` | hostname | Display / queue name |
| `POLL_INTERVAL` | `1` | Seconds between polls |
| `AUTOMATION_ROOT` | `C:\Automation` | Root |
| `SCRIPTS_DIR` | `…\scripts` | Script scan folder |
| `OUTPUT_DIR` | `…\results` | Optional outputs |
| `MAX_CONCURRENT_JOBS` | `0` | `0` = unlimited |

---

## Python Packages

**Controller** (`requirements.txt`):

- Flask, Werkzeug, requests  
- psycopg2-binary  
- watchdog  
- windows-toasts (optional on controller; **required on workers** for toasts)

**Worker minimum:** `requests`, `watchdog`, `windows-toasts`

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Worker offline | Firewall / `CONTROLLER_URL` / controller running / same LAN |
| Script missing | File under worker scripts dir; wait for sync; check extensions `.py` `.bat` `.cmd` |
| Job stuck pending | Worker online; name matches; check worker console |
| Import errors after layout change | Run from project root: `python run.py` (package is `app`) |
| No Site Master toast | `pip install windows-toasts` on worker; restart worker; Focus Assist off |
| Chat send fails | Set `CHAT_BOT_TOKEN` in `.env` |
| DB connection fail | Check `.env` Postgres values and network to `PGHOST` |
| Static/CSS 404 after move | Hard-refresh browser; assets live under `app/static/` |

---

## Security

Designed for a **trusted LAN**. Do not expose to the public internet without:

- HTTPS (reverse proxy)
- Strong `FLASK_SECRET_KEY`
- Worker API auth / network controls
- Restricted who can use File Explorer / commands
- Never commit real `.env` passwords or chat tokens

---

## Docs & Tests

| Path | Purpose |
|------|---------|
| `docs/last_update/` | Historical design / QA notes |
| `postgres/` | Schema review SQL + smoke scripts |
| `tests/` | Load / scheduler / report helpers |
| `scripts/init_db.py` | Init / readiness |

```powershell
python postgres\smoke_test.py
python postgres\smoke_dashboard.py
```

---

## Adding Scripts

1. Put `.py` / `.bat` / `.cmd` in the worker’s scripts folder (or set path in Worker Detail).  
2. Worker syncs → script appears on dashboard.  
3. **Run** or attach to a **Schedule** / **Folder**.

---

*Start the controller with `python run.py`. Deploy only `worker_agent/worker.py` to worker PCs.*
#   D i s t r i b u t e d - A u t o m a t i o n - J o b - M a n a g e m e n t - P l a t f o r m  
 