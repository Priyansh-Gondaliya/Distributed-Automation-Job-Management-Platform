# Database Schema Reference

> Complete documentation of every SQLite table, column, index, relationship, helper function, and migration strategy used in `database.py`.

---

## Table of Contents

- [Overview](#overview)
- [Connection Management](#connection-management)
- [Schema Initialization & Migrations](#schema-initialization--migrations)
- [Table: `workers`](#table-workers)
- [Table: `scripts`](#table-scripts)
- [Table: `jobs`](#table-jobs)
- [Table: `users`](#table-users)
- [Table: `commands`](#table-commands)
- [Indexes](#indexes)
- [Foreign Key Relationships](#foreign-key-relationships)
- [Entity Relationship Diagram](#entity-relationship-diagram)
- [Helper Functions Reference](#helper-functions-reference)
  - [Utility Functions](#utility-functions)
  - [User Functions](#user-functions)
  - [Worker Functions](#worker-functions)
  - [Script Functions](#script-functions)
  - [Job Functions](#job-functions)
  - [Worker Config Functions](#worker-config-functions)
  - [Command Functions](#command-functions)
- [Data Flow Through the Database](#data-flow-through-the-database)

---

## Overview

The database layer is implemented in **`database.py`** (615 lines) and uses **SQLite** as the backing store. The file `automation.db` is created in the project root by default.

**Key design choices:**
- **Thread-local connections** — Each Flask request thread gets its own connection via `threading.local()`
- **WAL mode** — Write-Ahead Logging enables concurrent reads with writes
- **Context-managed cursors** — Auto-commit on success, auto-rollback on error
- **Foreign keys enabled** — Referential integrity enforced
- **UPSERT patterns** — `ON CONFLICT ... DO UPDATE` used for idempotent insertions

---

## Connection Management

### `get_connection() → sqlite3.Connection`

Returns a **thread-local** database connection. Creates one if it doesn't exist:

```python
def get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "connection") or _local.connection is None:
        conn = sqlite3.connect(
            config.DATABASE_PATH,
            check_same_thread=False,
            timeout=30,              # Wait up to 30s for locks
        )
        conn.row_factory = sqlite3.Row   # Access columns by name
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.connection = conn
    return _local.connection
```

**Why `check_same_thread=False`?** SQLite's default behavior raises an error if a connection is used from a different thread than the one that created it. Since we store connections per-thread in `_local`, this flag is safe — each thread always uses its own connection.

**Why `timeout=30`?** SQLite uses file-level locking. If one thread is writing, another thread trying to write must wait. A 30-second timeout prevents `OperationalError: database is locked` in most cases.

### `db_cursor()` — Context Manager

```python
@contextmanager
def db_cursor():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()       # Success → commit
    except Exception:
        conn.rollback()     # Error → rollback
        raise
    finally:
        cursor.close()
```

Every database operation uses this pattern:
```python
with db_cursor() as cur:
    cur.execute("SELECT ...")
    result = cur.fetchone()
```

### `_utc_now() → str`

Returns the current UTC timestamp as a string in `YYYY-MM-DD HH:MM:SS` format. Used for all `created_at`, `updated_at`, `last_seen`, `start_time`, and `end_time` columns.

### `row_to_dict(row) → dict | None`

Converts a `sqlite3.Row` object to a Python dictionary, or returns `None` if the row is `None`. Used by every query function to return standard dict results.

---

## Schema Initialization & Migrations

The `init_schema()` function creates all tables using `CREATE TABLE IF NOT EXISTS` and then runs a series of `ALTER TABLE ADD COLUMN` statements wrapped in `try/except`. This pattern allows:

1. **Clean installs** — All tables are created with the latest schema
2. **Upgrades** — Existing databases get new columns without dropping data

```python
# Example migration pattern:
try:
    cur.execute("ALTER TABLE workers ADD COLUMN state TEXT NOT NULL DEFAULT 'idle'")
except sqlite3.OperationalError:
    pass  # Column already exists — this is fine
```

**Columns added via migration:**
- `workers.state` — Worker idle/busy state (added post-v1)
- `workers.script_location` — Configurable script directory per worker
- `workers.env_details` — JSON string of environment variables
- `jobs.start_time` — When the job started executing
- `jobs.end_time` — When the job finished
- `jobs.duration` — Execution time in seconds (float)
- `jobs.total_images` — Metric extracted from script output
- `jobs.output_count` — Metric extracted from script output

---

## Table: `workers`

Tracks every registered worker machine.

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | — | Auto-incrementing unique ID |
| `worker_name` | `TEXT` | `NOT NULL UNIQUE` | — | Display name (dashboard is source of truth) |
| `ip_address` | `TEXT` | `UNIQUE` | — | Worker's IP address (identity key) |
| `status` | `TEXT` | `NOT NULL` | `'offline'` | `online` or `offline` |
| `state` | `TEXT` | `NOT NULL` | `'idle'` | `idle` or `busy` (only meaningful when online) |
| `script_location` | `TEXT` | — | `''` | Configurable script directory path |
| `env_details` | `TEXT` | — | `'{}'` | JSON string of environment variables to inject |
| `last_seen` | `TEXT` | — | `NULL` | UTC timestamp of last heartbeat |

**Uniqueness constraints:**
- `worker_name` — Each worker must have a unique name
- `ip_address` — Each IP address maps to exactly one worker

**Status transitions:**
- `offline` → `online`: Worker sends heartbeat or registers
- `online` → `offline`: `refresh_worker_statuses()` detects stale `last_seen`

---

## Table: `scripts`

Tracks automation scripts discovered on each worker.

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | — | Auto-incrementing unique ID |
| `worker_name` | `TEXT` | `NOT NULL` | — | Which worker owns this script |
| `script_name` | `TEXT` | `NOT NULL` | — | Filename (e.g., `scraper.py`) |
| `script_path` | `TEXT` | `NOT NULL` | — | Absolute local path on the worker machine |
| `created_at` | `TEXT` | `NOT NULL` | — | UTC timestamp of first registration |

**Uniqueness:** `UNIQUE(worker_name, script_name)` — A worker can't have two scripts with the same name.

**Foreign key:** `worker_name` → `workers(worker_name)` with `ON DELETE CASCADE`

---

## Table: `jobs`

The job queue and execution history.

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | — | Job ID (displayed as `#42`) |
| `worker_name` | `TEXT` | `NOT NULL` | — | Target worker for execution |
| `script_id` | `INTEGER` | `NOT NULL` | — | References `scripts.id` |
| `status` | `TEXT` | `NOT NULL` | `'pending'` | Job lifecycle state |
| `output` | `TEXT` | — | `''` | Captured stdout/stderr from script |
| `start_time` | `TEXT` | — | `NULL` | When execution began (set on claim) |
| `end_time` | `TEXT` | — | `NULL` | When execution finished |
| `duration` | `REAL` | — | `NULL` | Execution time in seconds |
| `total_images` | `INTEGER` | — | `NULL` | Metric parsed from output |
| `output_count` | `INTEGER` | — | `NULL` | Metric parsed from output |
| `created_at` | `TEXT` | `NOT NULL` | — | When the job was queued |
| `updated_at` | `TEXT` | `NOT NULL` | — | Last modification timestamp |

**Status values:**
| Status | Meaning |
|--------|---------|
| `pending` | Queued, waiting for worker to claim |
| `running` | Claimed by worker, script executing |
| `completed` | Script exited with code 0 |
| `error` | Script exited with non-zero code, or worker went offline |
| `stopped` | Cancelled by user via dashboard |

**Foreign key:** `script_id` → `scripts(id)` with `ON DELETE CASCADE`

---

## Table: `users`

Dashboard authentication accounts.

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | — | User ID |
| `username` | `TEXT` | `NOT NULL UNIQUE` | — | Login username |
| `password_hash` | `TEXT` | `NOT NULL` | — | Werkzeug password hash |
| `created_at` | `TEXT` | `NOT NULL` | — | Registration timestamp |

**Password hashing** uses Werkzeug's `generate_password_hash()` (pbkdf2:sha256 by default).

---

## Table: `commands`

Controller-to-worker command queue for remote operations.

| Column | Type | Constraints | Default | Description |
|--------|------|-------------|---------|-------------|
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | — | Command ID |
| `worker_name` | `TEXT` | `NOT NULL` | — | Target worker |
| `command` | `TEXT` | `NOT NULL` | — | Action type (e.g., `rename`, `write_file`) |
| `payload` | `TEXT` | — | `'{}'` | JSON-encoded parameters |
| `status` | `TEXT` | `NOT NULL` | `'pending'` | `pending`, `running`, `completed`, `error` |
| `output` | `TEXT` | — | `''` | Result message from worker |
| `created_at` | `TEXT` | `NOT NULL` | — | When command was created |
| `updated_at` | `TEXT` | `NOT NULL` | — | Last modification timestamp |

**Foreign key:** `worker_name` → `workers(worker_name)` with `ON DELETE CASCADE`

---

## Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_jobs_worker_status ON jobs(worker_name, status);
CREATE INDEX IF NOT EXISTS idx_scripts_worker ON scripts(worker_name);
```

| Index | Purpose |
|-------|---------|
| `idx_jobs_worker_status` | Speeds up `claim_pending_job()` — filters by worker + status |
| `idx_scripts_worker` | Speeds up `list_scripts(worker_name)` — filters by worker |

---

## Foreign Key Relationships

```
workers.worker_name  ←──  scripts.worker_name     (CASCADE delete)
workers.worker_name  ←──  commands.worker_name     (CASCADE delete)
scripts.id           ←──  jobs.script_id           (CASCADE delete)
```

**Cascade behavior:** Deleting a worker automatically deletes all its scripts, commands, and (via scripts) all related jobs.

> **Note:** `jobs.worker_name` does not have a formal foreign key to `workers`. This is because jobs reference both `worker_name` (for display) and `script_id` (for the script relationship).

---

## Entity Relationship Diagram

```
┌──────────────┐       1:N        ┌──────────────┐       1:N        ┌──────────────┐
│   workers    │ ───────────────→ │   scripts    │ ───────────────→ │    jobs      │
│              │                  │              │                  │              │
│ PK: id       │                  │ PK: id       │                  │ PK: id       │
│ UK: worker_  │                  │ FK: worker_  │                  │ FK: script_  │
│     name     │                  │     name     │                  │     id       │
│ UK: ip_      │                  │ UK: (worker_ │                  │              │
│     address  │                  │     name,    │                  │              │
│              │                  │     script_  │                  │              │
│              │                  │     name)    │                  │              │
└──────┬───────┘                  └──────────────┘                  └──────────────┘
       │
       │ 1:N
       ▼
┌──────────────┐
│   commands   │
│              │
│ PK: id       │
│ FK: worker_  │
│     name     │
└──────────────┘

┌──────────────┐
│    users     │  (standalone — no FK relationships)
│              │
│ PK: id       │
│ UK: username │
└──────────────┘
```

---

## Helper Functions Reference

### Utility Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `_utc_now()` | `→ str` | Returns current UTC time as `YYYY-MM-DD HH:MM:SS` |
| `get_connection()` | `→ sqlite3.Connection` | Thread-local connection with WAL + FK enabled |
| `db_cursor()` | Context manager | Yields cursor, auto-commits/rollbacks |
| `row_to_dict()` | `(row) → dict \| None` | Converts `sqlite3.Row` to dict |

### User Functions

| Function | Parameters | Returns | Description |
|----------|-----------|---------|-------------|
| `create_user` | `username, password_hash` | `dict \| None` | Creates user; returns `None` if username exists |
| `get_user_by_username` | `username` | `dict \| None` | Lookup user by username |
| `get_user_by_id` | `user_id` | `dict \| None` | Lookup user by ID |

### Worker Functions

| Function | Parameters | Returns | Description |
|----------|-----------|---------|-------------|
| `register_worker` | `worker_name, ip_address, state='idle'` | `dict` | Register or update worker; IP-based identity check |
| `touch_worker` | `worker_name, ip_address=None, state=None` | `None` | Update `last_seen` and optionally IP/state |
| `rename_worker` | `old_name, new_name` | `bool` | Rename across all tables; returns False if new name exists |
| `refresh_worker_statuses` | `offline_seconds` | `None` | Mark stale workers offline; cleanup zombie jobs |
| `list_workers` | — | `list[dict]` | All workers (triggers status refresh first) |
| `get_worker` | `worker_name` | `dict \| None` | Single worker by name |
| `get_worker_by_ip` | `ip_address` | `dict \| None` | Single worker by IP |

**`register_worker()` logic in detail:**

```
Input: worker_name="MY-PC", ip_address="192.168.50.42", state="idle"
  ↓
Check: SELECT worker_name FROM workers WHERE ip_address = "192.168.50.42"
  ├── Row found (e.g., worker_name="Production-3")
  │   → UPDATE status='online', state, last_seen WHERE ip_address=...
  │   → Return existing worker (with name "Production-3")
  │
  └── No row found
      → INSERT ... ON CONFLICT(worker_name) DO UPDATE ...
      → Return new or updated worker
```

**`refresh_worker_statuses()` — Zombie cleanup:**

This function runs before every `list_workers()` and `get_worker()` call. It:
1. Finds all workers where `last_seen` is older than `WORKER_OFFLINE_SECONDS` (default 30)
2. Marks them as `offline` with `state='idle'`
3. Sets any `running` jobs for those workers to `error` with the message `[Worker went offline unexpectedly]`

### Script Functions

| Function | Parameters | Returns | Description |
|----------|-----------|---------|-------------|
| `register_script` | `worker_name, script_name, script_path` | `dict` | Register or update script; auto-creates worker if needed |
| `list_scripts` | `worker_name=None` | `list[dict]` | All scripts, optionally filtered by worker |
| `get_script` | `script_id` | `dict \| None` | Single script by ID |
| `remove_scripts_not_in_list` | `worker_name, script_names` | `int` | Delete scripts not in the given list; returns count removed |

**`remove_scripts_not_in_list()`** is called during `/sync-scripts` to clean up scripts that have been deleted from the worker's filesystem. If the worker sends an empty list, all scripts for that worker are removed.

### Job Functions

| Function | Parameters | Returns | Description |
|----------|-----------|---------|-------------|
| `create_job` | `worker_name, script_id` | `dict` | Create pending job |
| `claim_pending_job` | `worker_name` | `dict \| None` | Atomically claim oldest pending job |
| `update_job` | `job_id, status, output='', duration=None, total_images=None, output_count=None` | `dict \| None` | Update job status and metadata |
| `list_jobs` | `limit=100, status=None` | `list[dict]` | List jobs with optional status filter |
| `get_job` | `job_id` | `dict \| None` | Single job by ID (includes script info via JOIN) |
| `retry_job` | `job_id` | `dict \| None` | Create new pending job for the same script |
| `stop_job` | `job_id` | `dict \| None` | Mark pending/running job as stopped |

**`claim_pending_job()` — Atomic claiming:**

```python
cur.execute("BEGIN IMMEDIATE")  # Lock database for atomic operation

# Step 1: Find oldest pending job
cur.execute("""
    SELECT j.*, s.script_name, s.script_path
    FROM jobs j JOIN scripts s ON s.id = j.script_id
    WHERE j.worker_name = ? AND j.status = 'pending'
    ORDER BY j.created_at ASC LIMIT 1
""", (worker_name,))

# Step 2: Atomically update to 'running'
cur.execute("""
    UPDATE jobs SET status = 'running', start_time = ?
    WHERE id = ? AND status = 'pending'
""", (now, job["id"]))

# Step 3: Return the claimed job with script details
```

The `BEGIN IMMEDIATE` ensures that no other thread can read the same pending job and claim it simultaneously.

### Worker Config Functions

| Function | Parameters | Returns | Description |
|----------|-----------|---------|-------------|
| `get_worker_config` | `ip_address` | `dict \| None` | Get `script_location` and `env_details` by IP |
| `update_worker_config` | `ip_address, script_location, env_details` | `None` | Update config fields by IP |

### Command Functions

| Function | Parameters | Returns | Description |
|----------|-----------|---------|-------------|
| `create_command` | `worker_name, command, payload='{}'` | `dict` | Queue a command for a worker |
| `claim_pending_command` | `worker_name` | `dict \| None` | Atomically claim oldest pending command |
| `update_command` | `cmd_id, status, output=''` | `dict \| None` | Update command result |

---

## Data Flow Through the Database

### Job Creation → Completion

```sql
-- 1. User clicks "Run" on dashboard
INSERT INTO jobs (worker_name, script_id, status, output, created_at, updated_at)
VALUES ('PC220', 5, 'pending', '', '2026-06-01 10:00:00', '2026-06-01 10:00:00');

-- 2. Worker claims the job
BEGIN IMMEDIATE;
SELECT j.*, s.* FROM jobs j JOIN scripts s ON s.id = j.script_id
WHERE j.worker_name = 'PC220' AND j.status = 'pending'
ORDER BY j.created_at ASC LIMIT 1;

UPDATE jobs SET status = 'running', updated_at = '...', start_time = '...'
WHERE id = 42 AND status = 'pending';
COMMIT;

-- 3. Worker reports completion
UPDATE jobs SET status = 'completed', output = '...', updated_at = '...',
  end_time = '...', duration = 12.5, total_images = 150, output_count = 42
WHERE id = 42;
```

### Worker Registration → Heartbeat

```sql
-- First registration
INSERT INTO workers (worker_name, ip_address, status, state, last_seen)
VALUES ('PC220', '192.168.50.42', 'online', 'idle', '2026-06-01 10:00:00')
ON CONFLICT(worker_name) DO UPDATE SET
  ip_address = '192.168.50.42', status = 'online', state = 'idle', last_seen = '...';

-- Subsequent heartbeats
UPDATE workers SET last_seen = '...', status = 'online'
WHERE worker_name = 'PC220';

-- Offline detection (runs on list_workers/get_worker)
UPDATE workers SET status = 'offline', state = 'idle'
WHERE status != 'offline' AND datetime(last_seen) < datetime('now', '-30 seconds');

-- Zombie job cleanup
UPDATE jobs SET status = 'error', output = output || '\n[Worker went offline unexpectedly]'
WHERE status = 'running' AND worker_name IN (...offline workers...);
```
