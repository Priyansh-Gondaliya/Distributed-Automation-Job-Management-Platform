# REST API Reference

> Complete documentation of every HTTP endpoint in the automation controller, with request/response formats, examples, and behavioral notes.

---

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Worker Agent Endpoints](#worker-agent-endpoints)
  - [POST /register-worker](#post-register-worker)
  - [POST /register-script](#post-register-script)
  - [POST /sync-scripts](#post-sync-scripts)
  - [GET /get-job/\<worker_name\>](#get-get-jobworker_name)
  - [GET /job-status/\<job_id\>](#get-job-statusjob_id)
  - [POST /job-complete](#post-job-complete)
  - [POST /job-error](#post-job-error)
  - [POST /job-stopped](#post-job-stopped)
  - [GET /get-command/\<worker_name\>](#get-get-commandworker_name)
  - [POST /command-complete](#post-command-complete)
- [Dashboard JSON Endpoints](#dashboard-json-endpoints)
  - [GET /api/workers](#get-apiworkers)
  - [GET /api/scripts](#get-apiscripts)
  - [GET /api/jobs](#get-apijobs)
  - [GET/POST /api/worker-config/\<ip_address\>](#getpost-apiworker-configip_address)
  - [GET /api/my-config](#get-apimy-config)
- [Dashboard Web Routes](#dashboard-web-routes)
- [IP Resolution Logic](#ip-resolution-logic)
- [File: routes/api_routes.py — Function Reference](#file-routesapi_routespy--function-reference)
- [File: routes/web_routes.py — Function Reference](#file-routesweb_routespy--function-reference)
- [File: routes/\_\_init\_\_.py](#file-routes__init__py)

---

## Overview

The API is organized into two Flask Blueprints defined in the `routes/` package:

| Blueprint | Prefix | Purpose |
|-----------|--------|---------|
| `api_bp` | (none) | REST API for worker agents + JSON endpoints for dashboard |
| `web_bp` | (none) | HTML dashboard routes with session-based authentication |

All API endpoints accept and return **JSON**. There is no API key or token authentication — the API is designed for trusted LAN use.

---

## Authentication

### Web Routes (Dashboard)

Dashboard routes use **session-based authentication** via Flask's `session` mechanism:

- `session["user_id"]` is set on successful login
- The `@login_required` decorator redirects unauthenticated users to `/login`
- Passwords are hashed using Werkzeug's `generate_password_hash()` (pbkdf2:sha256)

### API Routes (Workers)

Worker API endpoints have **no authentication**. Any machine on the network can register as a worker or report job results.

> ⚠️ **Security warning:** In production, add API key or token-based authentication to worker endpoints.

---

## Worker Agent Endpoints

These endpoints are called by the worker agent script (`worker_agent/worker.py`).

---

### POST /register-worker

Register or refresh a worker (heartbeat). Called on startup and every 10 seconds.

**Request:**
```json
{
    "worker_name": "PC220",
    "state": "idle"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `worker_name` | string | ✅ | Worker display name (may be overridden by IP lookup) |
| `state` | string | ❌ | `"idle"` or `"busy"` (default: `"idle"`) |

**Response (200):**
```json
{
    "status": "ok",
    "worker": {
        "id": 1,
        "worker_name": "PC220",
        "ip_address": "192.168.50.42",
        "status": "online",
        "state": "idle",
        "script_location": "C:\\Automation\\scripts",
        "env_details": "{}",
        "last_seen": "2026-06-01 10:00:00"
    }
}
```

**Error (400):**
```json
{"error": "worker_name is required"}
```

**Behavior:**
1. Extracts client IP from `X-Forwarded-For` header or `request.remote_addr`
2. Resolves worker name via IP lookup (dashboard name takes priority)
3. Calls `database.register_worker()` which checks IP first, then name

---

### POST /register-script

Register a single script discovered on a worker machine.

**Request:**
```json
{
    "worker_name": "PC220",
    "script_name": "test.py",
    "script_path": "C:\\Automation\\scripts\\test.py"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `worker_name` | string | ✅ | Worker that owns this script |
| `script_name` | string | ✅ | Script filename |
| `script_path` | string | ✅ | Absolute path on the worker machine |

**Response (200):**
```json
{
    "status": "ok",
    "script": {
        "id": 5,
        "worker_name": "PC220",
        "script_name": "test.py",
        "script_path": "C:\\Automation\\scripts\\test.py",
        "created_at": "2026-06-01 10:00:00"
    }
}
```

**Behavior:**
- Uses UPSERT — if the script already exists, updates the path
- Also touches the worker heartbeat

---

### POST /sync-scripts

Bulk sync the script list from a worker. Registers new scripts and removes scripts that no longer exist on the worker's filesystem.

**Request:**
```json
{
    "worker_name": "PC220",
    "scripts": [
        {"script_name": "test.py", "script_path": "C:\\Automation\\scripts\\test.py"},
        {"script_name": "scraper.py", "script_path": "C:\\Automation\\scripts\\scraper.py"}
    ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `worker_name` | string | ✅ | Worker name |
| `scripts` | array | ✅ | List of `{script_name, script_path}` objects |

**Response (200):**
```json
{
    "status": "ok",
    "registered": 2,
    "removed": 1
}
```

**Behavior:**
1. Registers/updates each script in the list
2. Deletes any scripts in the database for this worker that are NOT in the list
3. If `scripts` is empty, all scripts for this worker are removed

---

### GET /get-job/\<worker_name\>

Worker polls for the next pending job. The job is atomically claimed (status changes from `pending` to `running`).

**Request:** `GET /get-job/PC220`

**Response (200 — job available):**
```json
{
    "id": 42,
    "worker_name": "PC220",
    "script_id": 5,
    "script_name": "test.py",
    "script_path": "C:\\Automation\\scripts\\test.py",
    "status": "running"
}
```

**Response (200 — no job):**
```json
{}
```

**Behavior:**
1. Resolves worker name by IP
2. Touches worker heartbeat
3. Calls `database.claim_pending_job()` (atomic with `BEGIN IMMEDIATE`)
4. Returns oldest pending job or empty object

---

### GET /job-status/\<job_id\>

Check the current status of a specific job. Used by the worker during execution to detect stop requests.

**Request:** `GET /job-status/42`

**Response (200):**
```json
{"status": "running"}
```

**Response (404):**
```json
{"error": "not found"}
```

---

### POST /job-complete

Mark a job as successfully completed with output and metrics.

**Request:**
```json
{
    "job_id": 42,
    "output": "test.py started\n  step 1/3\n  step 2/3\n  step 3/3\ntest.py finished\ntotal images: 150",
    "duration": 12.5,
    "total_images": 150,
    "output_count": 42
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | int | ✅ | Job ID to update |
| `output` | string | ❌ | Script stdout/stderr |
| `duration` | float | ❌ | Execution time in seconds |
| `total_images` | int | ❌ | Metric extracted from output |
| `output_count` | int | ❌ | Metric extracted from output |

**Response (200):**
```json
{
    "status": "ok",
    "job": { ... full job object ... }
}
```

---

### POST /job-error

Mark a job as failed with error output.

**Request:**
```json
{
    "job_id": 42,
    "output": "Exit code 1\nTraceback: ...",
    "duration": 3.1
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | int | ✅ | Job ID |
| `output` | string | ❌ | Error output |
| `duration` | float | ❌ | Execution time before failure |

**Response (200):**
```json
{
    "status": "ok",
    "job": { ... full job object ... }
}
```

---

### POST /job-stopped

Mark a job as stopped (cancelled by user).

**Request:**
```json
{
    "job_id": 42,
    "output": "[Stopped by user]\npartial output..."
}
```

**Response (200):**
```json
{
    "status": "ok",
    "job": { ... full job object ... }
}
```

---

### GET /get-command/\<worker_name\>

Worker polls for pending commands from the controller.

**Request:** `GET /get-command/PC220`

**Response (200 — command available):**
```json
{
    "id": 7,
    "worker_name": "PC220",
    "command": "rename",
    "payload": "{\"new_name\": \"Production-3\"}",
    "status": "running",
    "output": "",
    "created_at": "2026-06-01 10:00:00",
    "updated_at": "2026-06-01 10:00:01"
}
```

**Response (200 — no command):**
```json
{}
```

---

### POST /command-complete

Report the result of a command execution.

**Request:**
```json
{
    "cmd_id": 7,
    "status": "completed",
    "output": "Renamed to Production-3"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cmd_id` | int | ✅ | Command ID |
| `status` | string | ❌ | `"completed"` or `"error"` (default: `"completed"`) |
| `output` | string | ❌ | Result message |

**Response (200):**
```json
{"status": "ok"}
```

---

## Dashboard JSON Endpoints

These endpoints are used by the dashboard's JavaScript for live refresh and also serve as a read-only API.

---

### GET /api/workers

Returns a JSON list of all registered workers.

**Response:**
```json
{
    "workers": [
        {
            "id": 1,
            "worker_name": "PC220",
            "ip_address": "192.168.50.42",
            "status": "online",
            "state": "idle",
            "script_location": "",
            "env_details": "{}",
            "last_seen": "2026-06-01 10:00:00"
        }
    ]
}
```

**Note:** This endpoint triggers `refresh_worker_statuses()` to update offline status before returning.

---

### GET /api/scripts

Returns a JSON list of all scripts, optionally filtered by worker.

**Parameters:**
- `?worker=PC220` — Filter by worker name (optional)

**Response:**
```json
{
    "scripts": [
        {
            "id": 5,
            "worker_name": "PC220",
            "script_name": "test.py",
            "script_path": "C:\\Automation\\scripts\\test.py",
            "created_at": "2026-06-01 10:00:00"
        }
    ]
}
```

---

### GET /api/jobs

Returns a JSON list of recent jobs.

**Parameters:**
- `?status=running` — Filter by status (optional)
- `?limit=50` — Max results (default 100, max 500)

**Response:**
```json
{
    "jobs": [
        {
            "id": 42,
            "worker_name": "PC220",
            "script_id": 5,
            "script_name": "test.py",
            "script_path": "C:\\Automation\\scripts\\test.py",
            "status": "completed",
            "output": "...",
            "start_time": "2026-06-01 10:00:00",
            "end_time": "2026-06-01 10:00:12",
            "duration": 12.5,
            "total_images": 150,
            "output_count": 42,
            "created_at": "2026-06-01 09:59:55",
            "updated_at": "2026-06-01 10:00:12"
        }
    ]
}
```

---

### GET/POST /api/worker-config/\<ip_address\>

Get or update worker configuration by IP address.

**GET Response:**
```json
{
    "script_location": "C:\\Automation\\scripts",
    "env_details": "{\"VAR\": \"value\"}"
}
```

**POST (form data):**
- `script_location` — New script directory path
- `env_details` — JSON string of environment variables

**POST Response:** Redirects to dashboard with flash message.

---

### GET /api/my-config

Worker uses this to fetch its own configuration based on its IP address.

**Response (200):**
```json
{
    "script_location": "C:\\Automation\\scripts",
    "env_details": "{\"CHROME_PATH\": \"C:\\\\chromedriver.exe\"}",
    "worker_name": "Production-PC-3"
}
```

The `worker_name` field is included so the worker can adopt a renamed identity.

**Response (404):** `{}` — Worker not yet registered.

---

## Dashboard Web Routes

These routes serve HTML pages and handle form submissions. All are protected by `@login_required` (except `/login`, `/register`).

| Method | Route | Function | Description |
|--------|-------|----------|-------------|
| `GET/POST` | `/register` | `register()` | User registration page |
| `GET/POST` | `/login` | `login()` | User login page |
| `GET` | `/logout` | `logout()` | Log out, redirect to login |
| `GET` | `/` | `dashboard()` | Main dashboard (workers, scripts, jobs) |
| `GET` | `/worker/<ip_address>` | `worker_detail()` | Per-worker detail page |
| `POST` | `/run-script` | `run_script()` | Queue a job for a script |
| `POST` | `/retry-job/<job_id>` | `retry_job()` | Re-queue a completed/error/stopped job |
| `POST` | `/stop-job/<job_id>` | `stop_job()` | Stop a pending/running job |
| `POST` | `/upload-script` | `upload_script()` | Upload script file to controller |
| `POST` | `/rename-worker` | `rename_worker()` | Rename worker across all tables |
| `POST` | `/manage-files` | `manage_files()` | Queue file/folder operations on worker |

### Route Details

#### `GET /` — Dashboard

The main dashboard page. Displays:
- All registered workers as cards
- Scripts grouped by worker with "Run" buttons
- Job history table with filter and log viewing

**Template:** `dashboard.html`
**Query params:** `?status=running` to filter jobs

**Data passed to template:**
```python
render_template("dashboard.html",
    workers=database.list_workers(),            # All workers
    jobs=database.list_jobs(limit=50, status=...), # Recent jobs
    scripts_by_worker=defaultdict(list),        # Scripts grouped by worker name
    status_filter="running",                    # Current filter
)
```

#### `POST /run-script` — Queue a Job

Creates a new `pending` job for the specified script.

**Form fields:**
- `script_id` (int) — Script to execute
- `next` (optional) — URL to redirect back to

**Behavior:**
1. Validates script exists
2. Checks if worker is online (warns if offline but queues anyway)
3. Calls `database.create_job(worker_name, script_id)`
4. Redirects with flash message

#### `POST /upload-script` — Upload to Controller

Saves a script file to the controller's `uploads/` directory.

**Form fields (multipart):**
- `worker_name` — Target worker name (used as subdirectory)
- `script_file` — The `.py` file to upload

**Storage path:** `uploads/<worker_name>/<filename>`

> **Important:** Uploading does NOT deploy the script to the worker. The file must be manually copied to the worker's scripts directory.

#### `POST /rename-worker` — Rename Worker

Renames a worker across all database tables and queues a rename command.

**Form fields:**
- `old_name` — Current worker name
- `new_name` — Desired new name

**Behavior:**
1. `database.rename_worker()` updates `workers`, `scripts`, `jobs`, `commands` tables
2. Queues a `rename` command for the worker to pick up
3. Worker adopts new name on next command poll

#### `POST /manage-files` — Remote File Operations

Queues file/folder operations as commands for the worker.

**Form fields (multipart):**
- `worker_name` — Target worker
- `action` — `create_folder`, `delete_folder`, `delete_file`, `upload_file`, or `update_file`
- `target_path` — Absolute path on the worker machine
- `file` (optional) — File to upload (for `upload_file` / `update_file` actions)

**For file uploads:** The file content is base64-encoded and stored in the command payload.

---

## IP Resolution Logic

Every API endpoint that receives a `worker_name` from a worker runs it through IP resolution:

```python
def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")

def _resolve_worker_name(provided_name: str, ip_address: str) -> str:
    if ip_address and ip_address != "unknown":
        worker = database.get_worker_by_ip(ip_address)
        if worker:
            return worker["worker_name"]   # Dashboard's name wins
    return provided_name                    # Fall back to worker's reported name
```

**Why?** This ensures that if a worker is renamed on the dashboard, all subsequent API calls from that worker use the new name — even before the worker receives the rename command.

---

## File: `routes/api_routes.py` — Function Reference

| Function | Route | Method | Description |
|----------|-------|--------|-------------|
| `_client_ip()` | — | — | Extract client IP from headers |
| `_resolve_worker_name()` | — | — | IP-based name resolution |
| `register_worker()` | `/register-worker` | POST | Worker registration/heartbeat |
| `register_script()` | `/register-script` | POST | Single script registration |
| `sync_scripts()` | `/sync-scripts` | POST | Bulk script sync |
| `get_job()` | `/get-job/<name>` | GET | Claim pending job |
| `get_job_status()` | `/job-status/<id>` | GET | Check job status |
| `get_command()` | `/get-command/<name>` | GET | Claim pending command |
| `command_complete()` | `/command-complete` | POST | Report command result |
| `job_complete()` | `/job-complete` | POST | Report job success |
| `job_error()` | `/job-error` | POST | Report job failure |
| `job_stopped()` | `/job-stopped` | POST | Report job stopped |
| `api_workers()` | `/api/workers` | GET | List all workers (JSON) |
| `api_scripts()` | `/api/scripts` | GET | List scripts (JSON) |
| `api_jobs()` | `/api/jobs` | GET | List jobs (JSON) |
| `worker_config()` | `/api/worker-config/<ip>` | GET/POST | Worker config CRUD |
| `my_config()` | `/api/my-config` | GET | Worker self-config by IP |

---

## File: `routes/web_routes.py` — Function Reference

| Function | Route | Method | Auth | Description |
|----------|-------|--------|------|-------------|
| `login_required()` | — | — | — | Decorator: redirect to login if no session |
| `register()` | `/register` | GET/POST | ❌ | User registration |
| `login()` | `/login` | GET/POST | ❌ | User login |
| `logout()` | `/logout` | GET | ❌ | Clear session |
| `dashboard()` | `/` | GET | ✅ | Main dashboard |
| `worker_detail()` | `/worker/<ip>` | GET | ✅ | Per-worker detail page |
| `run_script()` | `/run-script` | POST | ✅ | Queue a job |
| `retry_job()` | `/retry-job/<id>` | POST | ✅ | Re-queue a job |
| `stop_job()` | `/stop-job/<id>` | POST | ✅ | Stop a job |
| `upload_script()` | `/upload-script` | POST | ✅ | Upload script file |
| `rename_worker()` | `/rename-worker` | POST | ✅ | Rename a worker |
| `manage_files()` | `/manage-files` | POST | ✅ | File/folder operations |

**Constants:**
- `UPLOAD_FOLDER` — Resolved to `<project_root>/uploads/`

---

## File: `routes/__init__.py`

Contains only a docstring:
```python
"""Flask route blueprints for the automation controller."""
```

This file makes `routes/` a Python package, allowing imports like `from routes.api_routes import api_bp`.
