# System Architecture

> Deep-dive into the architectural design of the Distributed Python Automation Platform — how each component works, how they interact, and the reasoning behind design decisions.

---

## Table of Contents

- [High-Level Architecture](#high-level-architecture)
- [Component Breakdown](#component-breakdown)
  - [1. Controller (Flask App)](#1-controller-flask-app)
  - [2. Worker Agent](#2-worker-agent)
  - [3. SQLite Database](#3-sqlite-database)
- [Communication Protocol](#communication-protocol)
- [Data Flow: End-to-End Runtime](#data-flow-end-to-end-runtime)
- [Identity Management](#identity-management)
- [State Machine: Jobs](#state-machine-jobs)
- [State Machine: Workers](#state-machine-workers)
- [Command System Architecture](#command-system-architecture)
- [Thread Safety Model](#thread-safety-model)
- [File: app.py — Application Entry Point](#file-apppy--application-entry-point)
- [File: config.py — Configuration Management](#file-configpy--configuration-management)
- [File: init_db.py — Database Initialization Script](#file-init_dbpy--database-initialization-script)
- [File: worker.py (root) — Legacy Entry Point](#file-workerpy-root--legacy-entry-point)

---

## High-Level Architecture

The system follows a **hub-and-spoke** model:

```
                          ┌─────────────────────────────────────┐
                          │         CONTROLLER (Hub)            │
                          │                                     │
                          │  ┌──────────┐    ┌──────────────┐  │
                          │  │ Flask    │────│ SQLite DB    │  │
                          │  │ Web App  │    │ automation.db│  │
                          │  └────┬─────┘    └──────────────┘  │
                          │       │                             │
                          │  ┌────┴──────────────────────┐     │
                          │  │ Blueprints:               │     │
                          │  │  • api_routes (worker API) │     │
                          │  │  • web_routes (dashboard)  │     │
                          │  └───────────────────────────┘     │
                          └─────────────┬───────────────────────┘
                                        │
                              HTTP/JSON  │  (polling model)
                                        │
            ┌───────────────────────────┼───────────────────────────┐
            │                           │                           │
      ┌─────┴─────┐              ┌──────┴────┐              ┌──────┴────┐
      │ Worker 1  │              │ Worker 2  │              │ Worker N  │
      │ (Spoke)   │              │ (Spoke)   │              │ (Spoke)   │
      │           │              │           │              │           │
      │ Polls     │              │ Polls     │              │ Polls     │
      │ every 5s  │              │ every 5s  │              │ every 5s  │
      │           │              │           │              │           │
      │ Executes  │              │ Executes  │              │ Executes  │
      │ scripts   │              │ scripts   │              │ scripts   │
      │ locally   │              │ locally   │              │ locally   │
      └───────────┘              └───────────┘              └───────────┘
```

### Why Polling Instead of Push?

- **Simplicity** — No need for WebSockets, message queues, or persistent connections
- **Firewall-friendly** — Workers only need outbound HTTP; controller needs no access to workers
- **Resilience** — Workers reconnect automatically after network interruptions
- **No infrastructure** — No Redis, RabbitMQ, or Kafka required

---

## Component Breakdown

### 1. Controller (Flask App)

**Location:** `app.py`, `config.py`, `database.py`, `routes/`

The controller is responsible for:

| Responsibility | How |
|---------------|-----|
| Worker management | Tracks registration, heartbeats, online/offline status |
| Script registry | Maintains a database of scripts available on each worker |
| Job queue | Creates, assigns, and tracks automation jobs |
| Command dispatch | Queues commands (rename, file ops) for workers to pick up |
| Dashboard UI | Serves HTML pages for operators via Jinja2 templates |
| Authentication | User registration/login with hashed passwords |

**What the controller does NOT do:**
- ❌ Execute automation scripts
- ❌ Access worker file systems directly
- ❌ Push notifications to workers (pull-only)

#### `app.py` — Application Entry Point

```python
def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    database.init_schema()              # Create tables if needed
    app.register_blueprint(api_bp)      # Worker REST API
    app.register_blueprint(web_bp)      # Dashboard UI
    return app
```

**Key behaviors:**
- Uses the **application factory pattern** via `create_app()` for clean initialization
- Initializes the database schema on every startup (idempotent `CREATE TABLE IF NOT EXISTS`)
- Registers two Flask Blueprints: one for API routes, one for web routes
- Module-level `app = create_app()` makes the app available for WSGI servers
- Runs in **debug mode** with **threaded=True** when executed directly
- Binds to `0.0.0.0:7561` by default (accepts connections from all interfaces)

#### `config.py` — Configuration Management

This module centralizes all configurable settings with environment variable overrides:

| Setting | Env Var | Default | Purpose |
|---------|---------|---------|---------|
| `HOST` | `CONTROLLER_HOST` | `0.0.0.0` | Flask bind address |
| `PORT` | `CONTROLLER_PORT` | `7561` | Flask port number |
| `DATABASE_PATH` | `CONTROLLER_DB` | `./automation.db` | SQLite file path |
| `WORKER_OFFLINE_SECONDS` | `WORKER_OFFLINE_SECONDS` | `30` | Heartbeat timeout threshold |
| `SECRET_KEY` | `FLASK_SECRET_KEY` | `change-me-in-production` | Flask session encryption key |

**Design decision:** Using `os.environ.get()` with sensible defaults means zero configuration is needed for development, while production can be customized entirely through environment variables.

#### `init_db.py` — Database Initialization Script

A standalone utility for initializing the database without starting the full Flask application:

```python
def main() -> None:
    database.init_schema()
    print(f"Database initialized at: {config.DATABASE_PATH}")
```

**Usage:** `python init_db.py`

This is useful for:
- First-time setup before running the controller
- Schema migrations (new columns are added with `ALTER TABLE` fallbacks)
- Verifying database connectivity

#### `worker.py` (root) — Legacy Entry Point

```python
"""Legacy entry point — use worker_agent/worker.py or deploy to C:\\Automation."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).parent / "worker_agent" / "worker.py"), run_name="__main__")
```

This file exists for backward compatibility. It simply delegates to `worker_agent/worker.py` using `runpy.run_path()`. This allows developers to run `python worker.py` from the project root without knowing about the `worker_agent/` subdirectory.


### 2. Worker Agent

**Location:** `worker_agent/worker.py` (primary), `deploy/Automation/worker.py` (older version)

The worker is a standalone Python script designed to run on remote PCs. It:
- Requires only the `requests` library (no Flask dependency)
- Runs in a terminal window indefinitely
- Communicates with the controller via HTTP

> **See:** [worker_flow.md](worker_flow.md) for a deep-dive into the worker lifecycle.


### 3. SQLite Database

**Location:** `database.py` (DAL), `automation.db` (runtime file)

The database is the single source of truth for all system state:
- Worker registrations and heartbeats
- Script inventory
- Job queue and history
- User accounts
- Command queue

> **See:** [database_schema.md](database_schema.md) for the full schema documentation.

---

## Communication Protocol

All communication between controller and workers uses **HTTP/JSON** over the local network.

### Request Flow

```
Worker → Controller:
  POST /register-worker     {"worker_name": "PC220", "state": "idle"}
  POST /sync-scripts        {"worker_name": "PC220", "scripts": [...]}
  GET  /get-job/PC220       (poll for pending job)
  GET  /get-command/PC220   (poll for pending command)
  GET  /api/my-config       (fetch config by IP)
  POST /job-complete        {"job_id": 42, "output": "...", "duration": 12.5}
  POST /job-error           {"job_id": 42, "output": "Error: ...", "duration": 3.1}
  POST /job-stopped         {"job_id": 42, "output": "[Stopped by user]"}
  POST /command-complete    {"cmd_id": 7, "status": "completed", "output": "..."}
```

### Worker Identity Resolution

Every API call from a worker includes the worker's IP address (extracted from the HTTP connection). The controller uses a two-step identity resolution:

1. **Check IP first** — If a worker with this IP already exists in the database, use the database's `worker_name` (not the worker's self-reported name)
2. **Fall back to reported name** — Only used for new/unknown workers

This means the **dashboard is the source of truth** for worker names. If an operator renames a worker on the dashboard, the worker adopts the new name on its next config fetch.

---

## Data Flow: End-to-End Runtime

This is the complete lifecycle of a job from dashboard click to result display:

```
Step 1: User clicks "▶ Run on PC220" for script "scraper.py"
        ↓
Step 2: web_routes.run_script() creates a pending job in the database
        database.create_job("PC220", script_id=5)
        → INSERT INTO jobs (..., status='pending', ...)
        ↓
Step 3: Worker PC220 polls GET /get-job/PC220
        → database.claim_pending_job("PC220")
        → Atomically: SELECT oldest pending job → UPDATE status='running'
        → Returns {id: 42, script_path: "C:\Automation\scripts\scraper.py", ...}
        ↓
Step 4: Worker sets state to "busy" and executes the script
        → subprocess.Popen([sys.executable, script_path], ...)
        → Output redirected to C:\Automation\logs\job_42.log
        → Worker polls /job-status/42 every 2s to check for stop requests
        ↓
Step 5: Script finishes (exit code 0 = success, non-zero = error)
        → Worker reads log file contents
        → Extracts metrics (total_images, output_count) from output
        → POST /job-complete {job_id: 42, output: "...", duration: 12.5, total_images: 150}
        ↓
Step 6: Controller updates job in database
        → UPDATE jobs SET status='completed', output=..., end_time=..., duration=...
        ↓
Step 7: Dashboard auto-refreshes (8s interval)
        → fetch('/api/jobs?limit=50') shows job #42 as "completed"
        → User clicks "Log" to view output
```

---

## Identity Management

Worker identity is IP-based with dashboard-authoritative naming:

```
Worker boots → sends POST /register-worker { worker_name: "MY-PC" }
  ↓
Controller checks: Does IP 192.168.50.42 exist in workers table?
  ├── YES → Use the database's worker_name (e.g., "Production-PC-3")
  │         UPDATE status='online', state=..., last_seen=...
  │
  └── NO  → Insert new worker with reported name "MY-PC"
            INSERT INTO workers (worker_name, ip_address, status, ...)
```

**Worker rename flow:**
1. Operator clicks "Rename" on dashboard → `database.rename_worker()` updates all tables
2. A `rename` command is queued in the `commands` table
3. Worker polls `/get-command/<name>` → receives `{"command": "rename", "payload": {"new_name": "..."}}`
4. Worker updates its in-memory `WORKER_NAME`
5. Worker also fetches new name via `/api/my-config` on each poll cycle

---

## State Machine: Jobs

```
                    ┌─────────┐
                    │ pending │ ←── created by dashboard "Run" action
                    └────┬────┘
                         │ worker claims (GET /get-job)
                         ▼
                    ┌─────────┐
          ┌────────│ running │────────┐
          │        └────┬────┘        │
          │             │             │
    user clicks     exit == 0    exit != 0
      "Stop"          │             │
          │             ▼             ▼
          │        ┌──────────┐  ┌───────┐
          │        │completed │  │ error │
          │        └──────────┘  └───────┘
          ▼             │             │
     ┌─────────┐       │             │
     │ stopped │       └──────┬──────┘
     └─────────┘              │
          │              user clicks
          └──────────── "Retry" ────→ creates new "pending" job
```

**Terminal states:** `completed`, `error`, `stopped` — these can be retried.
**Zombie cleanup:** If a worker goes offline while a job is `running`, it's automatically changed to `error` with the message `[Worker went offline unexpectedly]`.

---

## State Machine: Workers

```
     ┌──────────┐     register/heartbeat     ┌────────┐
     │  (new)   │ ─────────────────────────→ │ online │
     └──────────┘                            └───┬────┘
                                                 │
                                            heartbeat ↻ every 10s
                                                 │
                                   ┌─────────────┴─────────────┐
                                   │                           │
                           last_seen < 30s ago          last_seen > 30s ago
                                   │                           │
                                   ▼                           ▼
                              ┌────────┐                 ┌─────────┐
                              │ online │                 │ offline │
                              └────────┘                 └─────────┘
```

**Worker states (when online):**
- `idle` — No job running, ready to accept work
- `busy` — Currently executing a job

---

## Command System Architecture

The command system enables the controller to push operations to workers:

```
Dashboard Action → database.create_command() → commands table (status='pending')
                                                      ↓
Worker polls GET /get-command/<name>              claim_pending_command()
                                                      ↓
Worker executes action locally               (rename, create_folder, 
                                              delete_folder, delete_file, 
                                              write_file)
                                                      ↓
Worker reports POST /command-complete          update_command(status, output)
```

**Supported commands:**

| Command | Payload | Action |
|---------|---------|--------|
| `rename` | `{"new_name": "..."}` | Update worker's in-memory name |
| `create_folder` | `{"target_path": "..."}` | `Path.mkdir(parents=True)` |
| `delete_folder` | `{"target_path": "..."}` | `shutil.rmtree()` |
| `delete_file` | `{"target_path": "..."}` | `Path.unlink()` |
| `write_file` | `{"target_path": "...", "file_content_b64": "..."}` | Decode base64 → write bytes |

---

## Thread Safety Model

The controller runs Flask with `threaded=True`, meaning multiple requests can be served concurrently. The database module handles this through:

1. **Thread-local connections** — `threading.local()` ensures each thread gets its own SQLite connection
2. **WAL journal mode** — Enables concurrent reads with writes (`PRAGMA journal_mode = WAL`)
3. **Context manager** — `db_cursor()` automatically commits on success, rolls back on error
4. **`BEGIN IMMEDIATE`** — Used in `claim_pending_job()` and `claim_pending_command()` for atomic read-modify-write operations
5. **Foreign keys** — Enabled via `PRAGMA foreign_keys = ON` for referential integrity
6. **30-second timeout** — `sqlite3.connect(..., timeout=30)` prevents immediate lock failures

```python
@contextmanager
def db_cursor():
    conn = get_connection()    # thread-local
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()          # auto-commit on success
    except Exception:
        conn.rollback()        # auto-rollback on error
        raise
    finally:
        cursor.close()
```

This pattern ensures that every database operation is wrapped in a transaction boundary, preventing partial writes.
