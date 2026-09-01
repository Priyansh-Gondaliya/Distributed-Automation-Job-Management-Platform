# Worker Agent — Lifecycle, Execution, and Command Handling

> In-depth documentation of the worker agent: how it starts, polls, executes scripts, handles jobs, processes commands, and communicates with the controller.

---

## Table of Contents

- [Overview](#overview)
- [Worker Variants](#worker-variants)
- [Configuration](#configuration)
- [Startup Sequence](#startup-sequence)
- [Main Loop](#main-loop)
- [Heartbeat Thread](#heartbeat-thread)
- [Config Fetching](#config-fetching)
- [Script Discovery & Sync](#script-discovery--sync)
- [Job Polling](#job-polling)
- [Script Execution Engine](#script-execution-engine)
- [Stop Detection (In-Flight Cancellation)](#stop-detection-in-flight-cancellation)
- [Metric Extraction](#metric-extraction)
- [Result Reporting](#result-reporting)
- [Command Polling & Execution](#command-polling--execution)
- [State Management](#state-management)
- [Error Handling & Resilience](#error-handling--resilience)
- [Deploy Version vs Development Version](#deploy-version-vs-development-version)
- [File: worker_agent/worker.py — Complete Function Reference](#file-worker_agentworkerpy--complete-function-reference)

---

## Overview

The worker agent is a **standalone Python script** that runs on each automation PC. It has exactly **one dependency** (`requests`) and does not require Flask or the controller's database code.

**Core responsibilities:**
1. Register with the controller and maintain a heartbeat
2. Scan local filesystem for automation scripts
3. Poll the controller for assigned jobs
4. Execute scripts locally via `subprocess`
5. Report results (output, duration, metrics) back to the controller
6. Process commands from the controller (rename, file operations)

---

## Worker Variants

The project contains three versions of the worker:

| File | Purpose | Status |
|------|---------|--------|
| `worker_agent/worker.py` | **Primary** — Full-featured worker with threading, commands, config fetch, metrics, stop detection | ✅ Active (373 lines) |
| `deploy/Automation/worker.py` | **Deploy** — Older simpler version, opens CMD windows, no command support | ⚠️ Outdated (159 lines) |
| `worker.py` (root) | **Legacy shim** — Simply delegates to `worker_agent/worker.py` via `runpy` | ✅ Convenience wrapper |

**Recommendation:** Always use `worker_agent/worker.py` (or the root `worker.py` which delegates to it). The `deploy/Automation/worker.py` is an older version that lacks many features.

### Key Differences: `worker_agent/worker.py` vs `deploy/Automation/worker.py`

| Feature | `worker_agent/worker.py` | `deploy/Automation/worker.py` |
|---------|--------------------------|-------------------------------|
| Background heartbeat thread | ✅ Yes (every 10s) | ❌ No (heartbeat in main loop) |
| Command system | ✅ Yes (rename, file ops) | ❌ No |
| Config fetching | ✅ Yes (script_location, env, name) | ❌ No |
| Silent execution | ✅ `CREATE_NO_WINDOW` | ❌ Opens CMD window (`start /wait`) |
| Stop detection | ✅ Polls `/job-status` during execution | ❌ No |
| Metric extraction | ✅ `total_images`, `output_count` | ❌ No |
| Duration tracking | ✅ Yes | ❌ No |
| State tracking | ✅ Yes (idle/busy with thread lock) | ❌ No |

---

## Configuration

Configuration is set via environment variables with sensible defaults:

```python
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://192.168.50.89:7561")
WORKER_NAME    = os.environ.get("WORKER_NAME", socket.gethostname())
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "5"))
AUTOMATION_ROOT = Path(os.environ.get("AUTOMATION_ROOT", r"C:\Automation"))
SCRIPTS_DIR    = Path(os.environ.get("SCRIPTS_DIR", AUTOMATION_ROOT / "scripts"))
```

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTROLLER_URL` | `http://192.168.50.89:7561` | Base URL of the Flask controller |
| `WORKER_NAME` | Machine hostname | Display name for this worker |
| `POLL_INTERVAL` | `5` seconds | How often to check for new jobs |
| `AUTOMATION_ROOT` | `C:\Automation` | Base directory for the worker installation |
| `SCRIPTS_DIR` | `C:\Automation\scripts` | Where to scan for automation scripts |

**Derived paths:**
- `LOGS_DIR` = `AUTOMATION_ROOT / "logs"` — Job output logs stored here
- Script extensions: `.py`, `.bat`, `.cmd`

> **Note:** `SCRIPTS_DIR` can also be dynamically overridden by the controller via the `/api/my-config` endpoint (see [Config Fetching](#config-fetching)).

---

## Startup Sequence

```
main()
  │
  ├── 1. Log startup info (controller URL, scripts directory)
  │
  ├── 2. register_worker()
  │      POST /register-worker {"worker_name": "MY-PC", "state": "idle"}
  │      └── If fails: log warning, continue (will retry in heartbeat loop)
  │
  ├── 3. sync_scripts()
  │      scan_local_scripts() → POST /sync-scripts {"worker_name": "...", "scripts": [...]}
  │      └── Registers all local .py/.bat/.cmd files with controller
  │
  ├── 4. Start heartbeat thread (daemon)
  │      └── heartbeat_loop() runs register_worker() every 10 seconds
  │
  └── 5. Enter main polling loop
         └── while True: fetch_config → poll_commands → poll_job → sleep
```

---

## Main Loop

The main loop runs indefinitely with a `POLL_INTERVAL` (default 5 seconds) delay:

```python
while True:
    fetch_config()                    # Step 1: Get config updates from controller

    if time.time() - last_sync > 60:  # Step 2: Re-sync scripts every 60 seconds
        sync_scripts()
        last_sync = time.time()

    try:
        poll_commands()               # Step 3: Check for and execute commands
        job = poll_job()              # Step 4: Check for a pending job
        if job:
            set_state("busy")        # Step 5: Mark as busy
            handle_job(job)           # Step 6: Execute the job
            set_state("idle")        # Step 7: Mark as idle
    except Exception as e:
        log(f"Unexpected error: {e}")
    finally:
        set_state("idle")            # Safety net: always return to idle

    time.sleep(POLL_INTERVAL)         # Step 8: Wait before next iteration
```

**Key behaviors:**
- Scripts are re-scanned every 60 seconds (handles dynamically added/removed scripts)
- Commands are processed before jobs (ensures renames happen before job polling)
- The `finally` block ensures state always returns to `idle` even if an exception occurs
- The `try/except` prevents any single error from crashing the worker

---

## Heartbeat Thread

A separate daemon thread sends heartbeats to keep the worker marked as "online":

```python
def heartbeat_loop() -> None:
    while True:
        register_worker()    # POST /register-worker with current state
        time.sleep(10)       # Every 10 seconds
```

**Why a separate thread?** If the main loop is busy executing a long-running script, the heartbeat still needs to fire. Without this, a 30-minute script would cause the controller to mark the worker as offline after 30 seconds.

**Thread safety:** The heartbeat thread accesses `worker_state` through a `threading.Lock`:

```python
worker_state = "idle"
state_lock = threading.Lock()

def set_state(new_state):
    global worker_state
    with state_lock:
        worker_state = new_state

def get_state():
    with state_lock:
        return worker_state
```

---

## Config Fetching

Every iteration of the main loop fetches configuration from the controller:

```python
def fetch_config() -> None:
    resp = api_get("/api/my-config")
    data = resp.json()
```

**What gets fetched and applied:**

| Field | Effect |
|-------|--------|
| `script_location` | Updates `SCRIPTS_DIR` to scan a different directory |
| `worker_name` | Updates `WORKER_NAME` if the dashboard renamed this worker |
| `env_details` | Parses JSON, sets environment variables via `os.environ` |

**Flow:**
1. Worker sends `GET /api/my-config`
2. Controller identifies worker by IP address (from HTTP connection)
3. Returns `script_location`, `env_details`, and `worker_name`
4. Worker applies changes in-memory

**Example `env_details`:**
```json
{"CHROME_DRIVER_PATH": "C:\\chromedriver.exe", "OUTPUT_DIR": "D:\\Results"}
```
These become available as `os.environ["CHROME_DRIVER_PATH"]` etc. in the worker process (and any scripts it launches).

---

## Script Discovery & Sync

### Local Scanning

```python
def scan_local_scripts() -> list[dict]:
    if not SCRIPTS_DIR.exists():
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)  # Auto-create if missing

    found = []
    # Uses recursive rglob to find all scripts inside subdirectories
    for path in SCRIPTS_DIR.rglob('*'):
        if path.is_file() and path.suffix.lower() in SCRIPT_EXTENSIONS:
            # ... checks for days configuration ...
            found.append({
                "script_name": path.name,
                "script_path": str(path.resolve()),
                "days": days
            })
    return found
```

**Behavior:**
- Scans `SCRIPTS_DIR` completely for all `.py`, `.bat`, or `.cmd` files.
- Used to establish the initial state of the scripts directory on worker startup.

### Event-Driven Watcher

To eliminate the high CPU cost of continuously running recursive scans (both `rglob()` for scripts and `os.walk()` for the file tree), the worker uses an event-driven file watcher powered by the `watchdog` library.

```python
# Initialization in main()
observer = Observer()
observer.schedule(ScriptFolderWatcher(), str(SCRIPTS_DIR), recursive=True)
observer.start()
```

The watcher listens for filesystem events:
1. `on_created`
2. `on_deleted`
3. `on_modified`
4. `on_moved`

When an event occurs, the worker:
1. Marks the affected **folder path** as dirty.
2. If the changed file is a `.py`, `.bat`, or `.cmd` script, marks the scripts flag as dirty.
3. Resets a 2-second debounce timer in a background thread.

### Syncing with Controller

Once the 2-second debounce timer expires, the background thread fires minimal, localized updates:

**For File Explorer Tree (`/api/sync-folder-partial`):**
Instead of sending the full tree using `os.walk()`, the worker performs a single, shallow `os.scandir()` only on the folders that changed. It sends this partial list to the controller, which surgically updates `tree.json` without wiping the unchanged folders.

**For Executable Scripts (`/sync-scripts`):**
If a script file was changed, the worker calls `sync_scripts()`. The controller `/sync-scripts` endpoint:
1. Registers all scripts in the list (upsert)
2. **Deletes** any scripts in the database that are NOT in the list
3. This reliably handles scripts that were manually deleted or renamed.

**Sync frequency:** Exactly once on startup, and then purely event-driven based on `watchdog` events. Continuous polling has been permanently removed.

---

## Job Polling

```python
def poll_job() -> dict | None:
    resp = api_get(f"/get-job/{WORKER_NAME}")
    data = resp.json()
    if data and data.get("id"):
        return data    # {id, worker_name, script_id, script_name, script_path, status}
    return None
```

**What happens on the controller side:**
- `claim_pending_job()` uses `BEGIN IMMEDIATE` for atomic claiming
- Selects the oldest pending job for this worker
- Atomically updates its status to `running`
- Returns the job with script details

**If no job is available:** Returns empty JSON `{}`, worker skips and sleeps.

---

## Script Execution Engine

The `execute_script()` function is the core of the worker. It runs scripts locally using `subprocess.Popen`:

```python
def execute_script(script_path: str, job_id: int) -> tuple[int, str, float]:
```

**Parameters:**
- `script_path` — Absolute path to the script file
- `job_id` — Used for naming the log file

**Returns:** `(exit_code, output_text, duration_seconds)`

### Execution Steps

```
1. Normalize script path (os.path.normpath)
2. Check if file exists → return error if not
3. Create logs directory: C:\Automation\logs\
4. Open log file: C:\Automation\logs\job_42.log
5. Determine command:
   - .py  → [sys.executable, script_path]    (uses same Python interpreter)
   - .bat/.cmd → ["cmd", "/c", script_path]  (Windows command shell)
   - other → [script_path]                    (direct execution)
6. Launch subprocess:
   - stdout → log file
   - stderr → merged into stdout (STDOUT)
   - CREATE_NO_WINDOW flag on Windows (silent execution)
7. Poll loop every 2 seconds:
   - Check if process is still running
   - Check /job-status/{job_id} for stop requests
8. Read log file contents as output
9. Return (exit_code, output, duration)
```

### Key Details

**Silent execution:** `subprocess.CREATE_NO_WINDOW` (Windows) prevents a CMD window from appearing. This is different from the deploy version which intentionally opens a visible CMD window.

**Output capture:** Output is written to a log file, not captured via `subprocess.PIPE`. This allows:
- Viewing partial output while the job is running
- Surviving worker crashes (log file persists)
- No risk of deadlocks from full pipe buffers

---

## Stop Detection (In-Flight Cancellation)

During script execution, the worker polls the controller every 2 seconds to check if the job has been stopped:

```python
while proc.poll() is None:
    time.sleep(2)
    status_resp = api_get(f"/job-status/{job_id}")
    if status_resp.json().get("status") == "stopped":
        proc.terminate()           # Send SIGTERM (graceful)
        try:
            proc.wait(timeout=3)   # Wait 3 seconds
        except subprocess.TimeoutExpired:
            proc.kill()            # Force kill if still running
        return 1, "[Stopped by user]\n" + log_content, duration
```

**Stop flow:**
1. User clicks "Stop" on dashboard → `database.stop_job()` → status = `stopped`
2. Worker polls `/job-status/{job_id}` → sees `stopped`
3. Worker calls `proc.terminate()` (graceful shutdown)
4. If process doesn't exit within 3 seconds → `proc.kill()` (force)
5. Worker returns with `[Stopped by user]` prefix in output

---

## Metric Extraction

After a job completes successfully, the worker extracts metrics from the script's output:

```python
def extract_metrics(output: str) -> dict:
    metrics = {}
    
    # Match: "total images: 150" or "Total Image= 42"
    images_match = re.search(r'(?i)total images?[:=]\s*(\d+)', output)
    if images_match:
        metrics['total_images'] = int(images_match.group(1))
    
    # Match: "output count: 42" or "Output Count=10"
    output_match = re.search(r'(?i)output count[:=]\s*(\d+)', output)
    if output_match:
        metrics['output_count'] = int(output_match.group(1))
    
    return metrics
```

**How it works:** The worker uses regex to search for specific patterns in the script's stdout:
- `total images: 150` → `{"total_images": 150}`
- `Output Count= 42` → `{"output_count": 42}`

These metrics are sent to the controller and stored in the `jobs` table.

**Convention:** Automation scripts should print these lines to stdout if they want to report metrics:
```python
print(f"total images: {count}")
print(f"output count: {processed}")
```

---

## Result Reporting

### Successful Completion

```python
def report_complete(job_id: int, output: str, duration: float, metrics: dict) -> None:
    payload = {"job_id": job_id, "output": output, "duration": duration}
    payload.update(metrics)    # Add total_images, output_count if found
    api_post("/job-complete", payload)
```

### Error

```python
def report_error(job_id: int, output: str, duration: float) -> None:
    api_post("/job-error", {"job_id": job_id, "output": output, "duration": duration})
```

### User-Stopped

```python
api_post("/job-stopped", {"job_id": job_id, "output": output})
```

### `handle_job()` — Complete Job Handler

```python
def handle_job(job: dict) -> None:
    job_id = job["id"]
    script_path = job["script_path"]
    
    exit_code, output, duration = execute_script(script_path, job_id)
    
    if exit_code == 0:
        metrics = extract_metrics(output)
        report_complete(job_id, output, duration, metrics)
    elif output.startswith("[Stopped by user]"):
        api_post("/job-stopped", {"job_id": job_id, "output": output})
    else:
        report_error(job_id, f"Exit code {exit_code}\n{output}", duration)
```

---

## Command Polling & Execution

The `poll_commands()` function checks for and executes controller-issued commands:

```python
def poll_commands() -> None:
    resp = api_get(f"/get-command/{WORKER_NAME}")
    cmd = resp.json()
    if not cmd or not cmd.get("id"):
        return
    
    cmd_id = cmd["id"]
    action = cmd["command"]
    payload = json.loads(cmd.get("payload", "{}"))
```

### Supported Commands

| Command | Action | Example Payload |
|---------|--------|-----------------|
| `rename` | Update in-memory `WORKER_NAME` | `{"new_name": "Production-PC-3"}` |
| `create_folder` | `Path(target).mkdir(parents=True, exist_ok=True)` | `{"target_path": "C:\\Data\\Results"}` |
| `delete_folder` | `shutil.rmtree(target)` | `{"target_path": "C:\\Automation\\old_scripts"}` |
| `delete_file` | `Path(target).unlink()` | `{"target_path": "C:\\Automation\\scripts\\old.py"}` |
| `write_file` | Decode base64 → write bytes | `{"target_path": "...", "file_content_b64": "SGVsbG8="}` |

### Command Execution Flow

```
1. GET /get-command/PC220 → {id: 7, command: "create_folder", payload: "{\"target_path\": \"...\"}"}
2. Parse payload JSON
3. Execute action:
   - create_folder → Path(target_path).mkdir(parents=True, exist_ok=True)
4. POST /command-complete {cmd_id: 7, status: "completed", output: "Created C:\Data\Results"}
```

If any exception occurs during execution:
```python
except Exception as e:
    output = f"Command failed: {e}"
    success = False
```
The command is reported as `error` with the exception message.

---

## State Management

Worker state is tracked via a global variable protected by a threading lock:

```
         ┌──────┐
         │ idle │ ← Default state, reported in heartbeats
         └──┬───┘
            │ job = poll_job()  (job found)
            ▼
         ┌──────┐
         │ busy │ ← Reported in heartbeats while job executes
         └──┬───┘
            │ handle_job() returns
            ▼
         ┌──────┐
         │ idle │ ← Always returns to idle (even on error)
         └──────┘
```

The state is sent to the controller as part of:
- `register_worker()` → `POST /register-worker {"state": "idle/busy"}`
- `heartbeat_loop()` → sends current state every 10 seconds

The dashboard displays this state next to the worker's online badge.

---

## Error Handling & Resilience

The worker is designed to continue running even when individual operations fail:

| Failure | Behavior |
|---------|----------|
| Controller unreachable | Log warning, retry on next iteration |
| Script file missing | Report `error` to controller with "Script not found" message |
| Script crashes | Report `error` with exit code and output |
| Config fetch fails | Log warning, use previous config |
| Command execution fails | Report `error` to controller with exception message |
| Unexpected exception in main loop | Catch, log, continue next iteration |
| `KeyboardInterrupt` | Clean exit with `sys.exit(0)` |

**Network resilience:**
- All HTTP requests use `timeout=15` to avoid hanging
- All API calls are wrapped in `try/except requests.RequestException`
- Failed heartbeats don't crash the worker
- The heartbeat thread and main loop operate independently

---

## Deploy Version vs Development Version

### `deploy/Automation/worker.py` (159 lines)

This is the older, simpler version intended to be copied to `C:\Automation\worker.py` on worker machines.

**Key differences from the primary worker:**

1. **No heartbeat thread** — Heartbeat happens in the main loop (blocks during job execution)
2. **CMD window execution** — Creates a `.bat` wrapper that opens a visible CMD window:
   ```python
   def create_run_wrapper(script_path, job_id):
       content = f"""@echo off
   title Automation Job #{job_id}
   python "{script_path}" > "{log_path}" 2>&1
   pause
   exit /b %ERRORLEVEL%
   """
   ```
3. **No command system** — Cannot process rename/file commands
4. **No config fetching** — Cannot dynamically update scripts dir or environment
5. **No stop detection** — Cannot cancel running jobs
6. **No metrics** — Doesn't extract `total_images` / `output_count`
7. **No duration tracking** — Doesn't measure execution time
8. **No state tracking** — No idle/busy reporting

---

## File: `worker_agent/worker.py` — Complete Function Reference

| Function | Lines | Parameters | Returns | Description |
|----------|-------|-----------|---------|-------------|
| `log()` | 37-38 | `msg: str` | `None` | Print with `[WORKER_NAME]` prefix |
| `api_post()` | 41-43 | `path, payload` | `Response` | POST JSON to controller |
| `api_get()` | 46-48 | `path` | `Response` | GET from controller |
| `set_state()` | 54-57 | `new_state: str` | `None` | Thread-safe state update |
| `get_state()` | 59-61 | — | `str` | Thread-safe state read |
| `register_worker()` | 63-70 | — | `bool` | Register with controller |
| `heartbeat_loop()` | 72-75 | — | `None` | Infinite heartbeat every 10s |
| `fetch_config()` | 78-103 | — | `None` | Fetch and apply config from controller |
| `scan_local_scripts()` | 106-121 | — | `list[dict]` | Find local .py/.bat/.cmd files |
| `sync_scripts()` | 124-135 | — | `None` | Register scripts with controller |
| `execute_script()` | 138-200 | `script_path, job_id` | `(int, str, float)` | Run script, return (exit, output, duration) |
| `poll_job()` | 203-215 | — | `dict \| None` | Check for pending job |
| `poll_commands()` | 217-277 | — | `None` | Check for and execute commands |
| `extract_metrics()` | 280-291 | `output: str` | `dict` | Parse metrics from output text |
| `report_complete()` | 294-297 | `job_id, output, duration, metrics` | `None` | Report success to controller |
| `report_error()` | 300-301 | `job_id, output, duration` | `None` | Report failure to controller |
| `handle_job()` | 304-327 | `job: dict` | `None` | Execute job and report result |
| `main()` | 330-364 | — | `None` | Startup + main polling loop |

### Global Variables

| Variable | Type | Description |
|----------|------|-------------|
| `CONTROLLER_URL` | `str` | Controller base URL |
| `WORKER_NAME` | `str` | Mutable — can be changed by rename commands |
| `POLL_INTERVAL` | `int` | Seconds between polls |
| `AUTOMATION_ROOT` | `Path` | Base installation directory |
| `SCRIPTS_DIR` | `Path` | Mutable — can be changed by config fetch |
| `LOGS_DIR` | `Path` | Where job logs are written |
| `SCRIPT_EXTENSIONS` | `set` | `{".py", ".bat", ".cmd"}` |
| `worker_state` | `str` | `"idle"` or `"busy"` (protected by `state_lock`) |
| `state_lock` | `threading.Lock` | Mutex for `worker_state` |
