"""
Lightweight worker agent — runs on each automation PC.

Deploy to: C:\\Automation\\worker.py
Scripts folder: C:\\Automation\\scripts\\

Polls the controller every 5 seconds, auto-registers worker and scripts,
executes jobs locally, and reports results via HTTP.
"""
from __future__ import annotations

import json
import time
import os
import sys
import subprocess
import threading
import socket
import re
import shutil
import signal
import ctypes
from pathlib import Path
from urllib.parse import urljoin
from typing import Optional
import requests

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Configuration (edit for your environment) ---
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://192.168.50.49:7561")
WORKER_NAME = os.environ.get("WORKER_NAME", socket.gethostname())
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "1"))
# 0 / unset = unlimited (current behavior). Set e.g. 2 to cap parallel Chromes on one PC.
try:
    MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "0"))
except ValueError:
    MAX_CONCURRENT_JOBS = 0
# Pending jobs older than this (seconds) are expired by reconcile (default 2h).
try:
    STALE_PENDING_SECONDS = int(os.environ.get("STALE_PENDING_SECONDS", "7200"))
except ValueError:
    STALE_PENDING_SECONDS = 7200

# Default layout on worker PC
AUTOMATION_ROOT = Path(os.environ.get("AUTOMATION_ROOT", r"C:\Automation"))
# Directory where worker looks for scripts
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", AUTOMATION_ROOT / "scripts"))
# Directory where logs are stored
LOGS_DIR = AUTOMATION_ROOT / "logs"
# Directory where scraper outputs are stored (for future analytics)
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", AUTOMATION_ROOT / "results"))
# Drop System Scheduler .INI backups here for on-demand schedule import (not under SCRIPTS_DIR)
SCHEDULE_IMPORT_DIR = Path(
    os.environ.get("SCHEDULE_IMPORT_DIR", str(AUTOMATION_ROOT / "dfms_schedule_import"))
)
# Default allowed extensions for scripts to sync to database
SCRIPT_EXTENSIONS = {".py", ".bat", ".cmd", ".txt", ".json", ".csv", ".log", ".md", ".yml", ".yaml"}

# Persisted local state so restarts restore the last synced path immediately
LOCAL_STATE_FILE = AUTOMATION_ROOT / "worker_local_state.json"
TREE_SNAPSHOT_FILE = AUTOMATION_ROOT / "worker_tree_snapshot.json"
# If more than this many folders changed while offline, fall back to full sync
INCREMENTAL_DIRTY_FOLDER_LIMIT = 1500
# How many folders to send per HTTP partial-sync request
PARTIAL_SYNC_HTTP_BATCH = 40
# Max folders processed per watcher debounce flush (rest stay queued)
WATCHER_FOLDERS_PER_FLUSH = 120
# If a single move dirties more nested folders than this, do one full tree sync
MOVED_TREE_FULL_SYNC_THRESHOLD = 250


def log(msg: str) -> None:
    print(f"[{WORKER_NAME}] {msg}", flush=True)


# Held for process lifetime so a second worker.py exits instead of doubling heartbeats/jobs.
_INSTANCE_LOCK = None


def acquire_worker_instance_lock() -> bool:
    """Return False if another worker instance is already running on this PC."""
    global _INSTANCE_LOCK
    if os.name == "nt":
        # Named mutex — OS-backed, no racey PID files
        k32 = ctypes.windll.kernel32
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (WORKER_NAME or "worker"))[:64]
        k32.SetLastError(0)
        handle = k32.CreateMutexW(None, False, f"Local\\DFMS_Worker_{safe}")
        if not handle:
            return True
        already = k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        if already:
            k32.CloseHandle(handle)
            return False
        _INSTANCE_LOCK = handle
        return True
    # POSIX
    import fcntl

    AUTOMATION_ROOT.mkdir(parents=True, exist_ok=True)
    fh = open(AUTOMATION_ROOT / "worker.instance.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _INSTANCE_LOCK = fh
    return True


def _norm_path_str(p: str | Path) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(p).strip().rstrip("\\/")))
    except Exception:
        return str(p).strip().lower()


_local_state_lock = threading.Lock()


def load_local_state() -> dict:
    try:
        if LOCAL_STATE_FILE.exists():
            text = LOCAL_STATE_FILE.read_text(encoding="utf-8").strip()
            if not text:
                return {}
            return json.loads(text)
    except Exception as e:
        log(f"Could not read local state: {e}")
    return {}


def save_local_state(**fields) -> None:
    """Merge-and-persist local state. Thread-safe; uses a unique temp file to avoid WinError 32."""
    try:
        with _local_state_lock:
            AUTOMATION_ROOT.mkdir(parents=True, exist_ok=True)
            state = load_local_state()
            state.update({k: v for k, v in fields.items() if v is not None})
            state["updated_at"] = time.time()
            # Unique temp name: heartbeat/config/sync threads must not share one .tmp
            tmp = LOCAL_STATE_FILE.with_name(
                f"{LOCAL_STATE_FILE.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
                os.replace(str(tmp), str(LOCAL_STATE_FILE))
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as e:
        log(f"Could not save local state: {e}")


def load_tree_snapshot() -> dict:
    """
    Snapshot format:
    {
      "script_location": "...",
      "entries": { "rel/path": {"t": "file"|"folder", "m": mtime, "s": size|0} }
    }
    """
    try:
        if TREE_SNAPSHOT_FILE.exists():
            text = TREE_SNAPSHOT_FILE.read_text(encoding="utf-8").strip()
            if not text:
                return {}
            return json.loads(text)
    except Exception as e:
        log(f"Could not read tree snapshot: {e}")
    return {}


_last_snapshot_log_at = 0.0
_last_snapshot_persist_at = 0.0
SNAPSHOT_PERSIST_MIN_INTERVAL = 60.0  # seconds between disk writes for live patches


def save_tree_snapshot(script_location: str, entries: dict, quiet: bool = False) -> None:
    global _last_snapshot_log_at
    try:
        AUTOMATION_ROOT.mkdir(parents=True, exist_ok=True)
        payload = {
            "script_location": script_location,
            "entries": entries,
            "saved_at": time.time(),
            "count": len(entries),
        }
        tmp = TREE_SNAPSHOT_FILE.with_name(
            f"{TREE_SNAPSHOT_FILE.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(str(tmp), str(TREE_SNAPSHOT_FILE))
        finally:
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass
        now = time.time()
        if not quiet and (now - _last_snapshot_log_at) > 10:
            log(f"Saved local tree snapshot ({len(entries)} entries)")
            _last_snapshot_log_at = now
    except Exception as e:
        log(f"Could not save tree snapshot: {e}")


def clear_tree_snapshot() -> None:
    global _snapshot_cache, _snapshot_cache_dirty
    try:
        if TREE_SNAPSHOT_FILE.exists():
            TREE_SNAPSHOT_FILE.unlink()
    except Exception:
        pass
    _snapshot_cache = None
    _snapshot_cache_dirty = False


def api_post(path: str, payload: dict, timeout: float | int = 60) -> requests.Response:
    url = f"{CONTROLLER_URL.rstrip('/')}{path}"
    return requests.post(url, json=payload, timeout=timeout)


def api_get(path: str, timeout: float | int = 60) -> requests.Response:
    url = f"{CONTROLLER_URL.rstrip('/')}{path}"
    return requests.get(url, timeout=timeout)


def post_command_complete(cmd_id: int, status: str, output: str) -> bool:
    """Post command result with retries — editor hangs if this never lands."""
    payload = {"cmd_id": cmd_id, "status": status, "output": output}
    last_err = None
    for attempt in range(1, 4):
        try:
            # Large base64 file bodies need a longer window than normal API calls
            resp = api_post("/command-complete", payload, timeout=120)
            if resp.status_code == 200:
                return True
            last_err = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = exc
        log(f"command-complete #{cmd_id} attempt {attempt} failed: {last_err}")
        time.sleep(min(2 * attempt, 5))
    return False


_tree_sync_bg_lock = threading.Lock()
_tree_sync_cancel = threading.Event()
_tree_sync_seq = 0


def start_background_tree_sync(reason: str = "", *, replace: bool = False) -> bool:
    """
    Run full tree + script sync off the command loop so read_file/write_file
    are not blocked for minutes on large workers (e.g. Ayush 300k+ files).
    replace=True cancels an in-flight scan (needed when the config path changes
    from a huge tree to a small folder) without blocking the command loop.
    """
    global _tree_sync_seq
    if not replace and _tree_sync_bg_lock.locked():
        log(f"Background tree sync already running — skipped ({reason})")
        return False
    _tree_sync_seq += 1
    seq = _tree_sync_seq
    if replace:
        _tree_sync_cancel.set()

    def _run():
        with _tree_sync_bg_lock:
            if seq != _tree_sync_seq:
                return
            _tree_sync_cancel.clear()
            try:
                log(f"Background tree sync started ({reason})")
                # Register scripts first so File Explorer Schedule buttons get
                # script_id quickly after path/config change (tree upload can take minutes).
                try:
                    sync_scripts()
                except Exception as se:
                    log(f"Background sync_scripts (pre-tree) failed: {se}")
                if seq != _tree_sync_seq or _tree_sync_cancel.is_set():
                    log(f"Background tree sync cancelled before tree ({reason})")
                    return
                ok = sync_file_tree()
                if seq != _tree_sync_seq or _tree_sync_cancel.is_set():
                    log(f"Background tree sync cancelled ({reason})")
                    return
                try:
                    sync_scripts()
                except Exception as se:
                    log(f"Background sync_scripts failed: {se}")
                log(f"Background tree sync finished ok={ok} ({reason})")
            except Exception as e:
                log(f"Background tree sync crashed: {e}")

    threading.Thread(target=_run, daemon=True, name="bg-tree-sync").start()
    return True


# --- State tracking ---
worker_state = "idle"
state_lock = threading.Lock()

# Track the currently running processes for reliable stop and state
_running_procs: dict[int, subprocess.Popen] = {}
_procs_lock = threading.Lock()


def get_state() -> str:
    with _procs_lock:
        if len(_running_procs) > 0:
            return "busy"
    with state_lock:
        return worker_state


def _add_running_proc(job_id: int, proc: subprocess.Popen) -> None:
    with _procs_lock:
        _running_procs[job_id] = proc


def _remove_running_proc(job_id: int) -> None:
    with _procs_lock:
        if job_id in _running_procs:
            del _running_procs[job_id]


# --- Process tree killing ---
_WIN_CTRL_C_EXIT = 3221225786  # STATUS_CONTROL_C_EXIT


def _pid_alive(pid: int) -> bool:
    """True if the OS process still exists (works even when Popen.poll() is stuck)."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
    if not handle:
        return int(k32.GetLastError()) == ERROR_ACCESS_DENIED
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and int(code.value) == STILL_ACTIVE


def _read_inner_pid(log_path) -> Optional[int]:
    try:
        raw = Path(str(log_path) + ".pid").read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def kill_process_tree(pid: int, extra_pids: list | None = None) -> None:
    """
    Kill a process and ALL its children on Windows using taskkill /T /F.
    extra_pids is used to hit the inner script PID before the conhost wrapper
    so the visible console closes immediately.
    """
    seen: list[int] = []
    for raw in list(extra_pids or []) + [pid]:
        try:
            p = int(raw)
        except (TypeError, ValueError):
            continue
        if p > 0 and p not in seen:
            seen.append(p)
    for p in seen:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(p)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=4,
                )
                log(f"Killed process tree for PID {p}")
            else:
                os.killpg(os.getpgid(p), signal.SIGKILL)
                log(f"Killed process group for PID {p}")
        except Exception as exc:
            log(f"Failed to kill process tree PID {p}: {exc}")


def _wait_proc_exit(proc, timeout: float = 1.5) -> bool:
    """Wait briefly for Popen to exit; do not block the job thread for many seconds."""
    if proc is None:
        return True
    deadline = time.time() + max(0.2, float(timeout))
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    try:
        proc.kill()
    except Exception:
        pass
    return proc.poll() is not None


def suspend_process(pid: int) -> bool:
    """Suspend (freeze) a process using Windows NtSuspendProcess."""
    try:
        if os.name != "nt":
            os.kill(pid, signal.SIGSTOP)
            log(f"Sent SIGSTOP to PID {pid}")
            return True
        PROCESS_SUSPEND_RESUME = 0x0800
        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            log(f"Cannot open process PID {pid} for suspend")
            return False
        result = ntdll.NtSuspendProcess(handle)
        kernel32.CloseHandle(handle)
        if result == 0:
            log(f"Suspended process PID {pid}")
            return True
        else:
            log(f"NtSuspendProcess failed for PID {pid}, status={result}")
            return False
    except Exception as exc:
        log(f"Failed to suspend PID {pid}: {exc}")
        return False


def resume_process(pid: int) -> bool:
    """Resume (unfreeze) a suspended process using Windows NtResumeProcess."""
    try:
        if os.name != "nt":
            os.kill(pid, signal.SIGCONT)
            log(f"Sent SIGCONT to PID {pid}")
            return True
        PROCESS_SUSPEND_RESUME = 0x0800
        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            log(f"Cannot open process PID {pid} for resume")
            return False
        result = ntdll.NtResumeProcess(handle)
        kernel32.CloseHandle(handle)
        if result == 0:
            log(f"Resumed process PID {pid}")
            return True
        else:
            log(f"NtResumeProcess failed for PID {pid}, status={result}")
            return False
    except Exception as exc:
        log(f"Failed to resume PID {pid}: {exc}")
        return False



def register_worker() -> bool:
    global WORKER_NAME
    try:
        resp = api_post("/register-worker", {"worker_name": WORKER_NAME, "state": get_state()})
        if resp.status_code != 200:
            log(f"Register worker failed: HTTP {resp.status_code}")
            return False
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("error"):
            log(f"Register worker failed from server: {data.get('error')}")
            return False

        # Adopt the name returned by the server, in case it was renamed in the dashboard
        server_worker = (data or {}).get("worker", {}) if isinstance(data, dict) else {}
        if server_worker.get("worker_name") and server_worker["worker_name"] != WORKER_NAME:
            WORKER_NAME = server_worker["worker_name"]
            log(f"Adopted new worker name from register response: {WORKER_NAME}")

        return True
    except Exception as exc:
        log(f"Register worker exception: {exc}")
        return False


def heartbeat_loop() -> None:
    """Keep the worker marked online. Config fetch stays on the main loop so a
    slow/hung config request cannot block heartbeats and flip the UI to offline."""
    while True:
        try:
            register_worker()
        except Exception as exc:
            log(f"Heartbeat register failed: {exc}")
        time.sleep(8)


def fetch_server_config() -> dict:
    """Fetch config from controller. Returns dict (may be empty)."""
    try:
        resp = api_get(f"/api/my-config?worker_name={WORKER_NAME}")
        if resp.status_code == 200:
            return resp.json() or {}
    except Exception as exc:
        log(f"Failed to fetch server config: {exc}")
    return {}


def bootstrap_scripts_dir() -> dict:
    """
    Restore the last synchronized script path before any scan/watcher starts.
    Preference order: controller script_location → local state → env/default.
    Returns server config dict (may be empty).
    """
    global SCRIPTS_DIR, WORKER_NAME

    server = fetch_server_config()
    local = load_local_state()

    if server.get("worker_name") and server["worker_name"] != WORKER_NAME:
        WORKER_NAME = server["worker_name"]
        log(f"Adopted worker name from controller: {WORKER_NAME}")

    chosen = None
    source = "default"
    if server.get("script_location"):
        chosen = Path(server["script_location"])
        source = "controller"
    elif local.get("script_location"):
        chosen = Path(local["script_location"])
        source = "local-state"

    if chosen:
        SCRIPTS_DIR = chosen
        log(f"Restored SCRIPTS_DIR from {source}: {SCRIPTS_DIR}")
    else:
        log(f"Using default SCRIPTS_DIR (first-time worker): {SCRIPTS_DIR}")

    save_local_state(
        script_location=str(SCRIPTS_DIR),
        worker_name=WORKER_NAME,
        controller_url=CONTROLLER_URL,
    )
    return server


def fetch_config() -> None:
    global SCRIPTS_DIR, WORKER_NAME, observer, dirty_full_tree, dirty_scripts, last_event_time, first_event_time
    try:
        data = fetch_server_config()
        if not data:
            return

        if data.get("worker_name") and data["worker_name"] != WORKER_NAME:
            WORKER_NAME = data["worker_name"]
            log(f"Adopted new worker name from controller: {WORKER_NAME}")
            save_local_state(worker_name=WORKER_NAME)

        if data.get("script_location"):
            new_dir = Path(data["script_location"])
            if _norm_path_str(SCRIPTS_DIR) != _norm_path_str(new_dir):
                SCRIPTS_DIR = new_dir
                log(f"Updated SCRIPTS_DIR from server to {SCRIPTS_DIR}")
                save_local_state(script_location=str(SCRIPTS_DIR), worker_name=WORKER_NAME)
                clear_tree_snapshot()

                if observer and observer.is_alive():
                    try:
                        observer.stop()
                        observer.join(timeout=2)
                    except Exception:
                        pass

                if not SCRIPTS_DIR.exists():
                    try:
                        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        log(f"Could not create new SCRIPTS_DIR: {e}")

                try:
                    observer = Observer()
                    observer.schedule(ScriptFolderWatcher(), str(SCRIPTS_DIR), recursive=True)
                    if OUTPUT_DIR.exists():
                        observer.schedule(OutputFolderWatcher(), str(OUTPUT_DIR), recursive=True)
                    observer.start()
                    log(f"Watcher rebound to {SCRIPTS_DIR}")
                except Exception as e:
                    log(f"Failed to rebind watcher: {e}")

                try:
                    cleanup_orphaned_temp_scripts()
                except Exception as e:
                    log(f"Orphan temp cleanup after path change: {e}")

                with watcher_lock:
                    dirty_full_tree = True
                    dirty_scripts = True
                    last_event_time = time.time()
                    first_event_time = last_event_time
            else:
                # Path unchanged — keep local state fresh, do not rescan
                save_local_state(script_location=str(SCRIPTS_DIR), worker_name=WORKER_NAME)
    except Exception as exc:
        log(f"Failed to fetch config: {exc}")


def scan_local_scripts() -> list[dict]:
    """Discover scripts recursively in the local scripts folder."""
    if not SCRIPTS_DIR.exists():
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Created scripts folder: {SCRIPTS_DIR}")

    found = []
    # Use rglob to recursively find files
    for path in SCRIPTS_DIR.rglob('*'):
        if path.is_file() and path.suffix.lower() in SCRIPT_EXTENSIONS:
            days = None
            if path.suffix.lower() == ".py":
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    # Match days = N with optional indent / spacing (same contract as controller)
                    match = re.search(
                        r"^\s*days\s*=\s*(\d+)",
                        content,
                        re.MULTILINE | re.IGNORECASE,
                    )
                    if match:
                        days = int(match.group(1))
                except Exception:
                    pass
            found.append(
                {
                    "script_name": path.name,
                    "script_path": str(path.resolve()),
                    "days": days
                }
            )
    return found


def sync_scripts() -> None:
    scripts_by_name: dict[str, dict] = {}
    for item in scan_local_scripts():
        name = (item.get("script_name") or "").strip()
        if name:
            scripts_by_name[name] = item

    # Refresh days for registered scripts that live outside SCRIPTS_DIR
    # (Desktop paths kept while worker root is e.g. C:\Automation\scripts).
    try:
        resp = api_get(f"/api/worker-scripts?worker_name={WORKER_NAME}")
        if resp.ok:
            for row in (resp.json() or {}).get("scripts") or []:
                name = (row.get("script_name") or "").strip()
                path_str = (row.get("script_path") or "").strip()
                if not name or not path_str:
                    continue
                p = Path(path_str)
                if not p.is_file() or p.suffix.lower() != ".py":
                    continue
                days = None
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    match = re.search(
                        r"^\s*days\s*=\s*(\d+)",
                        content,
                        re.MULTILINE | re.IGNORECASE,
                    )
                    if match:
                        days = int(match.group(1))
                except Exception:
                    days = None
                existing = scripts_by_name.get(name)
                if existing:
                    existing["days"] = days
                    if not existing.get("script_path"):
                        existing["script_path"] = str(p.resolve())
                else:
                    scripts_by_name[name] = {
                        "script_name": name,
                        "script_path": str(p.resolve()),
                        "days": days,
                    }
    except Exception as exc:
        log(f"worker-scripts days refresh skipped: {exc}")

    scripts = list(scripts_by_name.values())
    try:
        resp = api_post(
            "/sync-scripts",
            {"worker_name": WORKER_NAME, "scripts": scripts},
        )
        resp.raise_for_status()
        data = resp.json()
        log(f"Scripts synced: {data.get('registered', 0)} active, {data.get('removed', 0)} removed.")
    except requests.RequestException as exc:
        log(f"Script sync failed: {exc}")


def scan_full_tree():
    """Generator that yields file/folder dicts one at a time."""
    log("Starting scan_full_tree...")
    start_t = time.time()
    count = 0
    root = str(SCRIPTS_DIR)
    cancelled = False
    try:
        for root_dir, dirs, files in os.walk(root):
            if _tree_sync_cancel.is_set():
                cancelled = True
                break
            for d in dirs:
                path = os.path.join(root_dir, d)
                rel_path = os.path.relpath(path, root).replace("\\", "/")
                try:
                    yield {
                        "name": d,
                        "type": "folder",
                        "path": rel_path,
                        "mtime": os.path.getmtime(path)
                    }
                    count += 1
                except Exception:
                    pass
            if _tree_sync_cancel.is_set():
                cancelled = True
                break
            for i, f in enumerate(files):
                if i % 200 == 0 and _tree_sync_cancel.is_set():
                    cancelled = True
                    break
                path = os.path.join(root_dir, f)
                rel_path = os.path.relpath(path, root).replace("\\", "/")
                try:
                    yield {
                        "name": f,
                        "type": "file",
                        "path": rel_path,
                        "size": os.path.getsize(path),
                        "mtime": os.path.getmtime(path)
                    }
                    count += 1
                except Exception:
                    pass
            if cancelled:
                break
    except Exception as e:
        log(f"Tree scan error: {e}")

    elapsed = time.time() - start_t
    if cancelled or _tree_sync_cancel.is_set():
        log(f"scan_full_tree cancelled after {elapsed:.2f}s ({count} items).")
        return
    log(f"Finished scan_full_tree in {elapsed:.2f}s, found {count} items.")


BATCH_SIZE = 5000

def build_local_tree_index() -> dict:
    """Walk SCRIPTS_DIR and return snapshot entries dict (no network)."""
    entries: dict = {}
    if not SCRIPTS_DIR.exists():
        return entries
    try:
        for root_dir, dirs, files in os.walk(SCRIPTS_DIR):
            for d in dirs:
                path = os.path.join(root_dir, d)
                rel_path = os.path.relpath(path, SCRIPTS_DIR).replace("\\", "/")
                try:
                    entries[rel_path] = {"t": "folder", "m": os.path.getmtime(path), "s": 0}
                except Exception:
                    pass
            for f in files:
                if f.startswith("__temp_"):
                    continue
                path = os.path.join(root_dir, f)
                rel_path = os.path.relpath(path, SCRIPTS_DIR).replace("\\", "/")
                try:
                    st = os.stat(path)
                    entries[rel_path] = {"t": "file", "m": st.st_mtime, "s": st.st_size}
                except Exception:
                    pass
    except Exception as e:
        log(f"Local tree index error: {e}")
        raise
    return entries


def _parent_rel(path: str) -> str:
    if not path:
        return ""
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    return parent


def compute_dirty_folders(old_entries: dict, new_entries: dict) -> set[str]:
    """Return relative folder paths that need partial sync after offline changes."""
    dirty: set[str] = set()
    old_paths = set(old_entries.keys())
    new_paths = set(new_entries.keys())

    for path in old_paths - new_paths:
        dirty.add(_parent_rel(path))
        meta = old_entries.get(path) or {}
        if meta.get("t") == "folder":
            dirty.add(path)

    for path in new_paths - old_paths:
        dirty.add(_parent_rel(path))
        meta = new_entries.get(path) or {}
        if meta.get("t") == "folder":
            dirty.add(path)

    for path in old_paths & new_paths:
        o = old_entries[path]
        n = new_entries[path]
        if o.get("t") != n.get("t") or abs(float(o.get("m", 0)) - float(n.get("m", 0))) > 0.001 or int(o.get("s", 0)) != int(n.get("s", 0)):
            dirty.add(_parent_rel(path))
            if n.get("t") == "folder" or o.get("t") == "folder":
                dirty.add(path)

    # Normalize root marker
    normalized = set()
    for d in dirty:
        if d is None:
            continue
        if d in (".",):
            normalized.add("")
        else:
            normalized.add(d.replace("\\", "/").strip("/"))
    return normalized


def sync_file_tree() -> bool:
    """Full tree upload. Returns True on success."""
    pause_file_watcher("full tree sync")
    try:
        return _sync_file_tree_impl()
    finally:
        resume_file_watcher()


def _sync_file_tree_impl() -> bool:
    log("sync_file_tree starting batched upload...")
    sync_t0 = time.time()
    scan_root = str(SCRIPTS_DIR)
    batch = []
    batch_index = 0
    total_sent = 0
    snapshot_entries: dict = {}

    def _post_batch(items, index, is_last, **extra):
        payload = {
            "worker_name": WORKER_NAME,
            "tree": items,
            "batch_index": index,
            "is_last_batch": is_last,
            "tree_root": scan_root,
            "elapsed_s": time.time() - sync_t0,
        }
        payload.update(extra)
        return api_post("/api/sync-file-tree", payload)

    for item in scan_full_tree():
        if _tree_sync_cancel.is_set():
            log("sync_file_tree cancelled")
            return False
        batch.append(item)
        path = item.get("path") or ""
        if path:
            snapshot_entries[path] = {
                "t": item.get("type", "file"),
                "m": item.get("mtime", 0.0),
                "s": item.get("size", 0) or 0,
            }
        if len(batch) >= BATCH_SIZE:
            if _tree_sync_cancel.is_set():
                log("sync_file_tree cancelled")
                return False
            try:
                resp = _post_batch(
                    batch,
                    batch_index,
                    False,
                    items_so_far=total_sent + len(batch),
                )
                resp.raise_for_status()
                total_sent += len(batch)
                log(f"sync_file_tree batch {batch_index} sent ({total_sent} items so far)")
            except requests.RequestException as exc:
                log(f"File tree sync failed at batch {batch_index}: {exc}")
                return False
            except Exception as e:
                log(f"File tree sync failed with unexpected error at batch {batch_index}: {e}")
                return False
            batch = []
            batch_index += 1

    if _tree_sync_cancel.is_set():
        log("sync_file_tree cancelled before final batch")
        return False

    if batch or batch_index == 0:
        try:
            resp = _post_batch(
                batch,
                batch_index,
                True,
                total_items=total_sent + len(batch),
            )
            resp.raise_for_status()
            total_sent += len(batch)
        except requests.RequestException as exc:
            log(f"File tree sync failed at final batch: {exc}")
            return False
        except Exception as e:
            log(f"File tree sync failed with unexpected error at final batch: {e}")
            return False
    else:
        # Tree size was an exact multiple of BATCH_SIZE — send an empty completing batch
        try:
            resp = _post_batch([], batch_index, True, total_items=total_sent)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log(f"File tree sync failed at completing batch: {exc}")
            return False
        except Exception as e:
            log(f"File tree sync failed with unexpected error at completing batch: {e}")
            return False

    log(f"sync_file_tree completed. Total items sent: {total_sent}")
    _set_snapshot_entries(snapshot_entries, persist=True)
    save_local_state(script_location=scan_root, worker_name=WORKER_NAME, last_full_sync_at=time.time())
    return True


def incremental_sync_from_snapshot() -> bool:
    """
    Compare local disk to last snapshot and push only dirty folders.
    Returns True on success. Raises/returns False to trigger full sync fallback.
    """
    pause_file_watcher("incremental sync")
    try:
        return _incremental_sync_from_snapshot_impl()
    finally:
        resume_file_watcher()


def _incremental_sync_from_snapshot_impl() -> bool:
    snap = load_tree_snapshot()
    old_entries = snap.get("entries") or {}
    if not old_entries:
        log("No local snapshot entries — full sync required")
        return False

    if snap.get("script_location") and _norm_path_str(snap["script_location"]) != _norm_path_str(SCRIPTS_DIR):
        log("Snapshot path does not match current SCRIPTS_DIR — full sync required")
        return False

    log("Building local index for incremental sync...")
    start_t = time.time()
    new_entries = build_local_tree_index()
    log(f"Local index built in {time.time() - start_t:.2f}s ({len(new_entries)} entries)")

    dirty = compute_dirty_folders(old_entries, new_entries)
    if not dirty:
        log("Incremental sync: no offline changes detected")
        _set_snapshot_entries(new_entries, persist=True)
        return True

    if len(dirty) > INCREMENTAL_DIRTY_FOLDER_LIMIT:
        log(
            f"Incremental sync: {len(dirty)} dirty folders exceeds limit "
            f"({INCREMENTAL_DIRTY_FOLDER_LIMIT}) — full sync required"
        )
        return False

    log(f"Incremental sync: {len(dirty)} dirty folder(s) to push")
    # Parents before children; batched HTTP to avoid flooding the controller
    ordered = sorted(dirty, key=lambda p: (p.count("/"), p))
    try:
        uploaded = sync_folders_partial_batched(ordered)
        log(f"Incremental sync uploaded {uploaded}/{len(ordered)} changed folder(s)")
    except Exception as e:
        log(f"Incremental sync failed: {e}")
        return False

    _set_snapshot_entries(new_entries, persist=True)
    save_local_state(script_location=str(SCRIPTS_DIR), worker_name=WORKER_NAME, last_incremental_sync_at=time.time())
    log("Incremental sync completed successfully")
    return True


def needs_full_tree_sync(server_cfg: dict) -> tuple[bool, str]:
    """Decide whether startup must do a full tree upload."""
    if not SCRIPTS_DIR.exists():
        return True, "script path missing on disk"
    try:
        if not any(SCRIPTS_DIR.iterdir()) and int(server_cfg.get("tree_entry_count") or 0) == 0:
            # Empty folder and nothing on server — still do a quick full sync to clear/confirm
            return True, "empty first-time path"
    except Exception:
        return True, "script path not accessible"

    tree_count = int(server_cfg.get("tree_entry_count") or 0)
    if tree_count <= 0:
        return True, "controller has no tree data yet (first sync)"

    snap = load_tree_snapshot()
    if not snap.get("entries"):
        return True, "no local snapshot (first sync on this machine)"

    if snap.get("script_location") and _norm_path_str(snap["script_location"]) != _norm_path_str(SCRIPTS_DIR):
        return True, "saved path changed since last snapshot"

    return False, "incremental"

# --- Event-Driven Watcher ---
watcher_lock = threading.Lock()
dirty_folders = set()
dirty_scripts = False
dirty_full_tree = False
last_event_time = 0.0
first_event_time = 0.0
observer = None  # Global reference for recovery
_watcher_paused = False  # Ignore FS events during full/incremental scans

# --- Live job output tracking (image/pdf counts for reports) ---
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".jfif"}
PDF_EXTS = {".pdf"}
_job_output_lock = threading.Lock()
_active_job_id: Optional[int] = None
_job_output_files: set[str] = set()


def begin_job_output_tracking(job_id: int) -> None:
    """Start collecting output files written while a job runs (watchdog-assisted)."""
    global _active_job_id, _job_output_files
    with _job_output_lock:
        _active_job_id = int(job_id)
        _job_output_files = set()


def end_job_output_tracking() -> dict:
    """Stop tracking and return counts + folders from files observed during the job."""
    global _active_job_id, _job_output_files
    with _job_output_lock:
        paths = list(_job_output_files)
        _active_job_id = None
        _job_output_files = set()
    image_count = 0
    pdf_count = 0
    file_count = 0
    total_size = 0
    folders: set[str] = set()
    for p in paths:
        try:
            path = Path(p)
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            st = path.stat()
            file_count += 1
            total_size += st.st_size
            folders.add(str(path.parent))
            if ext in IMAGE_EXTS:
                image_count += 1
            elif ext in PDF_EXTS:
                pdf_count += 1
        except OSError:
            pass
    collapsed = _collapse_download_roots([Path(f) for f in folders]) if folders else []
    return {
        "image_count": image_count,
        "pdf_count": pdf_count,
        "file_count": file_count,
        "total_folder_size": total_size,
        "folder_path": ", ".join(str(d) for d in collapsed[:5]) if collapsed else "",
        "tracked_paths": paths,
    }


def _record_job_output_file(path: str) -> None:
    """Record a file create/modify for the active job (no-op when idle)."""
    if not path or _active_job_id is None:
        return
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_EXTS and ext not in PDF_EXTS and ext not in {".txt", ".log"}:
            return
        name = os.path.basename(path)
        if name.startswith("__temp_") or name.startswith("."):
            return
        with _job_output_lock:
            if _active_job_id is None:
                return
            _job_output_files.add(os.path.normpath(path))
    except Exception:
        pass


def pause_file_watcher(reason: str = "") -> None:
    """Pause flushing during heavy local scans. FS events still queue for after resume."""
    global _watcher_paused, dirty_full_tree, dirty_scripts, last_event_time, first_event_time
    _watcher_paused = True
    # Clear only the pre-scan backlog — the scan/full sync itself reconciles disk.
    # Events that arrive while paused are still recorded by ScriptFolderWatcher.
    with watcher_lock:
        dirty_folders.clear()
        dirty_full_tree = False
        dirty_scripts = False
        last_event_time = 0.0
        first_event_time = 0.0
    if reason:
        log(f"File watcher paused: {reason}")


def resume_file_watcher() -> None:
    """Resume flushing. Keep any dirty folders queued while the watcher was paused."""
    global _watcher_paused, last_event_time, first_event_time
    with watcher_lock:
        # Do not clear dirty_* — changes during the pause must still reach the dashboard.
        if dirty_folders or dirty_full_tree or dirty_scripts:
            now = time.time()
            last_event_time = now
            first_event_time = now
        else:
            last_event_time = 0.0
            first_event_time = 0.0
    _watcher_paused = False
    log("File watcher resumed")


class ScriptFolderWatcher(FileSystemEventHandler):
    def _add_dirty_folder(self, abs_folder_path: str):
        global last_event_time, first_event_time
        # Always queue (even while paused). Debounce loop skips flush until resume.
        try:
            rel_path = os.path.relpath(abs_folder_path, str(SCRIPTS_DIR)).replace("\\", "/")
            if rel_path == ".":
                rel_path = ""
            if rel_path.startswith(".."):
                return
            with watcher_lock:
                dirty_folders.add(rel_path)
                last_event_time = time.time()
                if first_event_time == 0.0:
                    first_event_time = last_event_time
        except ValueError:
            pass

    def _mark_dirty(self, path: str, is_directory: bool | None = None):
        global dirty_scripts, last_event_time, first_event_time
        try:
            if is_directory is None:
                is_directory = os.path.isdir(path)

            # Always dirty the parent so the parent listing gains/loses this entry
            self._add_dirty_folder(os.path.dirname(path))

            # Also dirty the folder itself so its children are scanned after create/move
            if is_directory:
                self._add_dirty_folder(path)
            else:
                # Live job reporting: count images/PDFs written under the scripts tree
                _record_job_output_file(path)

            ext = os.path.splitext(path)[1].lower()
            filename = os.path.basename(path)
            if ext in SCRIPT_EXTENSIONS and not filename.startswith("__temp_"):
                with watcher_lock:
                    dirty_scripts = True
                    last_event_time = time.time()
                    if first_event_time == 0.0:
                        first_event_time = last_event_time
        except Exception as e:
            log(f"Watcher error marking dirty: {e}")

    def on_created(self, event):
        self._mark_dirty(event.src_path, event.is_directory)

    def on_deleted(self, event):
        self._mark_dirty(event.src_path, event.is_directory)

    def on_modified(self, event):
        if event.is_directory:
            return  # Directory modified events are noisy; create/delete/move cover real changes
        self._mark_dirty(event.src_path, False)

    def on_moved(self, event):
        global dirty_full_tree, last_event_time, first_event_time
        self._mark_dirty(event.src_path, event.is_directory)
        self._mark_dirty(event.dest_path, event.is_directory)
        # Deep paste/rename: queue nested directories, or escalate to full sync if huge
        if event.is_directory:
            try:
                if os.path.exists(event.dest_path):
                    nested = []
                    for root, dirs, _ in os.walk(event.dest_path):
                        for d in dirs:
                            nested.append(os.path.join(root, d))
                            if len(nested) > MOVED_TREE_FULL_SYNC_THRESHOLD:
                                with watcher_lock:
                                    dirty_full_tree = True
                                    dirty_folders.clear()
                                    last_event_time = time.time()
                                    if first_event_time == 0.0:
                                        first_event_time = last_event_time
                                return
                    for abs_dir in nested:
                        self._add_dirty_folder(abs_dir)
            except Exception as e:
                log(f"Watcher error during on_moved walk: {e}")

class OutputFolderWatcher(FileSystemEventHandler):
    """Tracks scraper output file creates for job image/pdf reporting."""

    def on_created(self, event):
        if not event.is_directory:
            _record_job_output_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            _record_job_output_file(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            _record_job_output_file(getattr(event, "dest_path", None) or event.src_path)


def scan_folder_contents(rel_path: str) -> list:
    """Scandir one folder and return content dicts for partial sync."""
    target_dir = os.path.join(str(SCRIPTS_DIR), rel_path) if rel_path else str(SCRIPTS_DIR)
    contents = []
    if not (os.path.exists(target_dir) and os.path.isdir(target_dir)):
        return contents
    try:
        for entry in os.scandir(target_dir):
            try:
                entry_rel_path = os.path.relpath(entry.path, str(SCRIPTS_DIR)).replace("\\", "/")
                if entry.is_dir(follow_symlinks=False):
                    contents.append({
                        "name": entry.name,
                        "type": "folder",
                        "path": entry_rel_path,
                        "mtime": entry.stat(follow_symlinks=False).st_mtime,
                    })
                else:
                    if not entry.name.startswith("__temp_"):
                        st = entry.stat(follow_symlinks=False)
                        contents.append({
                            "name": entry.name,
                            "type": "file",
                            "path": entry_rel_path,
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        })
            except Exception:
                pass
    except Exception:
        pass
    return contents


def _folder_contents_match_snapshot(folder_path: str, contents: list) -> bool:
    """True if local snapshot already matches this folder listing (skip network)."""
    try:
        entries = _get_snapshot_entries()
        folder_path = (folder_path or "").replace("\\", "/").strip("/")
        snap_children = {
            p: meta for p, meta in entries.items() if _parent_rel(p) == folder_path
        }
        if len(snap_children) != len(contents):
            return False
        for item in contents:
            path = (item.get("path") or "").replace("\\", "/").strip("/")
            meta = snap_children.get(path)
            if not meta:
                return False
            if meta.get("t") != item.get("type", "file"):
                return False
            if abs(float(meta.get("m", 0)) - float(item.get("mtime", 0) or 0)) > 0.001:
                return False
            if int(meta.get("s", 0) or 0) != int(item.get("size", 0) or 0):
                return False
        return True
    except Exception:
        return False


def sync_folders_partial_batched(rel_paths: list[str]) -> int:
    """
    Scan dirty folders and push changed ones to the controller in HTTP batches.
    Returns number of folders that were actually uploaded.
    """
    if not rel_paths:
        return 0

    # Parents before children
    ordered = sorted(
        {(p or "").replace("\\", "/").strip("/") for p in rel_paths},
        key=lambda p: (p.count("/"), p),
    )

    to_upload = []
    skipped = 0
    for folder in ordered:
        contents = scan_folder_contents(folder)
        if _folder_contents_match_snapshot(folder, contents):
            skipped += 1
            continue
        to_upload.append({"folder_path": folder, "contents": contents})

    if skipped and not to_upload:
        return 0

    uploaded = 0
    for i in range(0, len(to_upload), PARTIAL_SYNC_HTTP_BATCH):
        chunk = to_upload[i : i + PARTIAL_SYNC_HTTP_BATCH]
        try:
            resp = api_post(
                "/api/sync-folder-partial",
                {"worker_name": WORKER_NAME, "folders": chunk},
            )
            resp.raise_for_status()
            for item in chunk:
                update_snapshot_folder(item["folder_path"], item["contents"], persist=False)
            uploaded += len(chunk)
        except Exception as e:
            log(f"Batched partial sync failed ({len(chunk)} folders): {e}")
            # Fall back to single-folder posts for this chunk so one bad folder
            # does not block the rest (preserves prior behavior).
            for item in chunk:
                try:
                    resp = api_post(
                        "/api/sync-folder-partial",
                        {
                            "worker_name": WORKER_NAME,
                            "folder_path": item["folder_path"],
                            "contents": item["contents"],
                        },
                    )
                    resp.raise_for_status()
                    update_snapshot_folder(item["folder_path"], item["contents"], persist=False)
                    uploaded += 1
                except Exception as e2:
                    log(f"Partial sync failed for {item['folder_path']}: {e2}")
    return uploaded


def sync_folder_partial(rel_path: str) -> None:
    """Scan one folder and push to controller (uses batched path)."""
    n = sync_folders_partial_batched([rel_path or ""])
    if n == 0:
        # Still update snapshot if scan matched (or folder missing) — keep local consistent
        contents = scan_folder_contents(rel_path or "")
        update_snapshot_folder(rel_path or "", contents, persist=False)


_snapshot_cache: dict | None = None
_snapshot_cache_dirty = False
_snapshot_lock = threading.Lock()


def _get_snapshot_entries() -> dict:
    global _snapshot_cache
    with _snapshot_lock:
        if _snapshot_cache is None:
            _snapshot_cache = (load_tree_snapshot().get("entries") or {})
        return _snapshot_cache


def _set_snapshot_entries(entries: dict, persist: bool = True) -> None:
    global _snapshot_cache, _snapshot_cache_dirty, _last_snapshot_persist_at
    with _snapshot_lock:
        _snapshot_cache = entries
        if persist:
            save_tree_snapshot(str(SCRIPTS_DIR), entries)
            _snapshot_cache_dirty = False
            _last_snapshot_persist_at = time.time()
        else:
            _snapshot_cache_dirty = True


def flush_snapshot_if_dirty(force: bool = False) -> None:
    """Persist in-memory snapshot to disk. Throttled unless force=True."""
    global _snapshot_cache_dirty, _last_snapshot_persist_at
    with _snapshot_lock:
        if not _snapshot_cache_dirty or _snapshot_cache is None:
            return
        now = time.time()
        if not force and (now - _last_snapshot_persist_at) < SNAPSHOT_PERSIST_MIN_INTERVAL:
            return
        save_tree_snapshot(str(SCRIPTS_DIR), _snapshot_cache, quiet=not force)
        _snapshot_cache_dirty = False
        _last_snapshot_persist_at = now


def update_snapshot_folder(folder_path: str, contents: list, persist: bool = False) -> None:
    """Patch local snapshot for one folder's direct children (keeps restart incremental).

    Preserves nested entries under folders that still exist. Only removes subtrees
    for children that disappeared or changed from folder→file. (Previously every
    resync wiped all descendants, e.g. 151k → ~37 after resync_folder on root.)
    """
    try:
        entries = dict(_get_snapshot_entries())
        folder_path = (folder_path or "").replace("\\", "/").strip("/")

        new_by_path = {}
        for item in contents:
            path = (item.get("path") or "").replace("\\", "/").strip("/")
            if not path:
                continue
            new_by_path[path] = {
                "t": item.get("type", "file"),
                "m": item.get("mtime", 0.0),
                "s": item.get("size", 0) or 0,
            }

        old_children = {
            p: meta for p, meta in entries.items() if _parent_rel(p) == folder_path
        }

        # Drop children that are gone (and their subtrees if they were folders)
        for path, meta in old_children.items():
            if path in new_by_path:
                continue
            was_folder = bool(meta and meta.get("t") == "folder")
            entries.pop(path, None)
            if was_folder:
                sub_prefix = path + "/"
                for child in [p for p in list(entries.keys()) if p.startswith(sub_prefix)]:
                    entries.pop(child, None)

        # Upsert direct children; keep nested data when a folder remains a folder
        for path, new_meta in new_by_path.items():
            old_meta = old_children.get(path)
            was_folder = bool(old_meta and old_meta.get("t") == "folder")
            is_folder = new_meta.get("t") == "folder"
            if was_folder and not is_folder:
                sub_prefix = path + "/"
                for child in [p for p in list(entries.keys()) if p.startswith(sub_prefix)]:
                    entries.pop(child, None)
            entries[path] = new_meta

        _set_snapshot_entries(entries, persist=persist)
    except Exception as e:
        log(f"Snapshot folder patch failed for '{folder_path}': {e}")

def watcher_debounce_loop():
    global last_event_time, first_event_time, observer, dirty_full_tree, dirty_scripts
    while True:
        time.sleep(0.1)  # Fast poll for the debounce check

        if _watcher_paused:
            continue
        
        # Watchdog Recovery
        if observer and not observer.is_alive():
            try:
                observer.join(timeout=1)
                from watchdog.observers import Observer
                observer = Observer()
                observer.schedule(ScriptFolderWatcher(), str(SCRIPTS_DIR), recursive=True)
                if OUTPUT_DIR.exists():
                    observer.schedule(OutputFolderWatcher(), str(OUTPUT_DIR), recursive=True)
                observer.start()
            except Exception as e:
                log(f"Failed to restart observer: {e}")
                time.sleep(5)
                continue
        
        folders_to_sync = []
        sync_all_scripts = False
        sync_all_tree = False
        
        with watcher_lock:
            if last_event_time == 0.0:
                continue
                
            now = time.time()
            # Wait until 0.5s have passed since LAST event, OR 2.0s max delay since FIRST event
            if (now - last_event_time < 0.5) and (now - first_event_time < 2.0):
                continue
                
            if dirty_full_tree:
                sync_all_tree = True
                dirty_full_tree = False
                dirty_folders.clear()
                last_event_time = 0.0
                first_event_time = 0.0
            elif dirty_folders:
                # Always flush in bounded chunks. Escalating to full sync here caused a
                # death spiral on large trees (100k+ entries): full sync pauses the
                # watcher for minutes, FS churn re-queues thousands of folders, and
                # live dashboard updates never catch up.
                all_dirty = list(dirty_folders)
                if len(all_dirty) > WATCHER_FOLDERS_PER_FLUSH:
                    folders_to_sync = all_dirty[:WATCHER_FOLDERS_PER_FLUSH]
                    dirty_folders.clear()
                    dirty_folders.update(all_dirty[WATCHER_FOLDERS_PER_FLUSH:])
                    # Keep timer warm so remaining folders flush soon
                    last_event_time = now
                    first_event_time = now
                else:
                    folders_to_sync = all_dirty
                    dirty_folders.clear()
                    last_event_time = 0.0
                    first_event_time = 0.0
            else:
                # scripts-only wakeups
                last_event_time = 0.0
                first_event_time = 0.0
                
            if dirty_scripts:
                sync_all_scripts = True
                dirty_scripts = False
                
        if sync_all_scripts:
            sync_scripts()
            
        if sync_all_tree:
            sync_file_tree()
        elif folders_to_sync:
            try:
                uploaded = sync_folders_partial_batched(folders_to_sync)
                if uploaded:
                    flush_snapshot_if_dirty(force=False)
            except Exception:
                pass


def report_pid(job_id: int, pid: int) -> None:
    """Report the OS process ID to the controller for tracking."""
    try:
        api_post("/job-update-pid", {"job_id": job_id, "pid": pid})
    except Exception as exc:
        log(f"Failed to report PID for job #{job_id}: {exc}")


_SUCCESS_FINISH_MARKERS = (
    "driver closed",
    "email sent",
    "successfully",
    "download complete",
    "scraping completed",
    "scraping finished",
    "job finished",
    "ending pagination",
    "next page not found",
    "no next button found",
    "no more pages",
    "waiting for background downloads",
)


def _rfind_any(text: str, markers) -> int:
    low = (text or "").lower()
    best = -1
    for m in markers:
        i = low.rfind(m)
        if i > best:
            best = i
    return best


def _strip_system_outcome_noise(output: str = "") -> str:
    kept = []
    for ln in (output or "").splitlines():
        s = ln.strip().lower()
        if s.startswith("[stopped]") and "without report" in s:
            continue
        if s == "[terminated on worker pc]":
            continue
        kept.append(ln)
    return "\n".join(kept)


def _ended_with_failure(output: str = "", exit_code=None) -> bool:
    """True only when the script's final outcome is failure (mid-run errors allowed)."""
    text = _strip_system_outcome_noise(output or "")
    low = text.lower()
    tb_i = low.rfind("traceback (most recent call last)")
    ok_i = _rfind_any(text, _SUCCESS_FINISH_MARKERS)
    if tb_i >= 0:
        return ok_i < tb_i
    tail = "\n".join([ln for ln in text.splitlines() if ln.strip()][-30:]).lower()
    if any(k in tail for k in ("script failed", "modulenotfounderror", "sessionnotcreated")) and not any(
        m in tail for m in _SUCCESS_FINISH_MARKERS
    ):
        return True
    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    interrupt = {_WIN_CTRL_C_EXIT, -1073741510, 3221225501, -1073741787, 130, -2}
    if code not in (0, None) and code not in interrupt:
        if ok_i >= 0 and any(m in tail for m in _SUCCESS_FINISH_MARKERS):
            return False
        if ok_i < 0:
            return True
    return False


def _ended_successfully(output: str = "", exit_code=None) -> bool:
    text = output or ""
    low = text.lower()
    if text.strip().lower().startswith("[stopped by user]") or "[stop requested from dashboard]" in low:
        return False
    if _ended_with_failure(text, exit_code):
        return False
    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 0:
        return True
    cleaned = _strip_system_outcome_noise(text)
    tb_i = cleaned.lower().rfind("traceback (most recent call last)")
    ok_i = _rfind_any(cleaned, _SUCCESS_FINISH_MARKERS)
    return ok_i >= 0 and ok_i > tb_i


def _looks_like_clean_finish(output: str = "") -> bool:
    """True when log looks like a normal scraper end (console may still auto-close)."""
    text = output or ""
    if text.strip().lower().startswith("[stopped by user]") or "[stop requested from dashboard]" in text.lower():
        return False
    if _ended_with_failure(text):
        return False
    return _rfind_any(_strip_system_outcome_noise(text), _SUCCESS_FINISH_MARKERS) >= 0


def _script_python_exe() -> str:
    """Console Python for child scripts (pythonw has no window and exits oddly)."""
    exe = sys.executable or "python"
    low = exe.lower()
    if low.endswith("pythonw.exe"):
        alt = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.isfile(alt):
            return alt
    return exe


def _resolve_job_script_path(script_path: str, script_name: str) -> str:
    """
    Map a controller script_path to a file on this worker PC.

    Jobs often carry absolute paths from another machine or an outdated DB row.
    When the path is missing locally, resolve under SCRIPTS_DIR by relative tail.
    """
    raw = (script_path or "").strip()
    name = (script_name or "").strip() or (os.path.basename(raw) if raw else "")
    if raw:
        norm = os.path.normpath(raw)
        if os.path.isfile(norm):
            return norm

    if not name:
        return os.path.normpath(raw) if raw else ""

    if raw:
        parts = [p for p in raw.replace("\\", "/").split("/") if p]
        for i in range(len(parts)):
            tail = "/".join(parts[i:])
            candidate = SCRIPTS_DIR / tail.replace("/", os.sep)
            if candidate.is_file():
                log(f"Resolved script via relative tail: {tail}")
                return str(candidate.resolve())

    direct = SCRIPTS_DIR / name
    if direct.is_file():
        if raw and _norm_path_str(str(direct)) != _norm_path_str(raw):
            log(f"Resolved script via SCRIPTS_DIR/{name} (controller path missing locally)")
        return str(direct.resolve())

    try:
        matches = [p for p in SCRIPTS_DIR.rglob(name) if p.is_file()]
        if len(matches) == 1:
            log(f"Resolved script via search: {matches[0]}")
            return str(matches[0].resolve())
        if len(matches) > 1:
            log(f"Multiple local files named {name}; using first match {matches[0]}")
            return str(matches[0].resolve())
    except OSError:
        pass

    return os.path.normpath(raw) if raw else name


def execute_script(script_path: str, job_id: int, days: int = 0) -> tuple[int, str, float]:
    """
    Execute script locally in the background.
    Output is redirected to a log file.
    Returns (exit_code, output, duration).
    """
    script_path = os.path.normpath(script_path)
    if not os.path.isfile(script_path):
        return 1, f"Script not found: {script_path}", 0.0

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"job_{job_id}.log"

    log(f"Launching script in new console: {script_path}")

    script_dir = os.path.dirname(script_path)

    start_time = time.time()
    try:
        # Determine command based on file extension
        py_exe = _script_python_exe()
        _, ext = os.path.splitext(script_path)
        if ext.lower() == ".py":
            cmd = [py_exe, "-u", script_path]
        elif ext.lower() in {".bat", ".cmd"}:
            # Use cmd.exe to run batch files on Windows
            cmd = ["cmd", "/c", script_path]
        else:
            cmd = [script_path]

        # CREATE_NEW_CONSOLE opens a visible child window on Windows.
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_CONSOLE

        # Prepare environment variables
        env = os.environ.copy()
        env["SCRIPT_DAYS"] = str(days)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["DFMS_JOB_ID"] = str(job_id)
        env["DFMS_SCRIPT_NAME"] = os.path.basename(script_path)
        env.pop("WT_SESSION", None)
        env.pop("WT_PROFILE_ID", None)

        # Script writes the log file directly (no stdout PIPE). A PIPE + readline
        # pump deadlocks scrapers that print \\r progress or lots of output:
        # print() blocks, the console freezes, downloads may continue, job stays
        # "running". Console is a tail of the log; Ctrl+C still kills the script.
        wrapper_code = (
            "import sys, subprocess, os, threading, ctypes, time\n"
            "INT = 3221225786\n"
            "log_path = sys.argv[1]\n"
            "f = open(log_path, 'wb', buffering=0)\n"
            "try:\n"
            "    jid = os.environ.get('DFMS_JOB_ID','')\n"
            "    sn = os.environ.get('DFMS_SCRIPT_NAME','')\n"
            "    f.write(('[Job started] id=%s script=%s\\n' % (jid, sn)).encode('utf-8','replace'))\n"
            "except Exception:\n"
            "    pass\n"
            "p = None\n"
            "def _kill():\n"
            "    global p\n"
            "    if p is None or p.poll() is not None:\n"
            "        return\n"
            "    try:\n"
            "        if os.name == 'nt':\n"
            "            subprocess.call(['taskkill','/F','/T','/PID',str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "        else:\n"
            "            p.kill()\n"
            "    except Exception:\n"
            "        pass\n"
            "def _handler(ctrl):\n"
            "    try:\n"
            "        f.write(b'[Terminated on worker PC]\\n')\n"
            "        f.flush()\n"
            "    except Exception:\n"
            "        pass\n"
            "    _kill()\n"
            "    os._exit(INT)\n"
            "    return 1\n"
            "if os.name == 'nt':\n"
            "    k32 = ctypes.windll.kernel32\n"
            "    CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)\n"
            "    _cb = CB(_handler)\n"
            "    k32.SetConsoleCtrlHandler(_cb, True)\n"
            "    globals()['_CTRL_CB'] = _cb\n"
            "    try:\n"
            "        h = k32.GetStdHandle(-11)\n"
            "        mode = ctypes.c_uint()\n"
            "        if k32.GetConsoleMode(h, ctypes.byref(mode)):\n"
            "            mode.value = (mode.value | 0x0080) & ~0x0040\n"
            "            k32.SetConsoleMode(h, mode)\n"
            "    except Exception:\n"
            "        pass\n"
            "try:\n"
            "    p = subprocess.Popen(sys.argv[2:], stdout=f, stderr=subprocess.STDOUT)\n"
            "    try:\n"
            "        open(log_path + '.pid', 'w', encoding='utf-8').write(str(p.pid))\n"
            "    except Exception:\n"
            "        pass\n"
            "except Exception as e:\n"
            "    msg = 'Failed to launch script: %s\\n' % e\n"
            "    sys.stdout.write(msg); sys.stdout.flush()\n"
            "    try:\n"
            "        f.write(msg.encode('utf-8', 'replace')); f.flush()\n"
            "    except Exception:\n"
            "        pass\n"
            "    sys.exit(1)\n"
            "def tail():\n"
            "    pos = 0\n"
            "    while True:\n"
            "        try:\n"
            "            with open(log_path, 'rb') as rf:\n"
            "                rf.seek(pos)\n"
            "                while True:\n"
            "                    chunk = rf.read(8192)\n"
            "                    if chunk:\n"
            "                        pos = rf.tell()\n"
            "                        try:\n"
            "                            sys.stdout.buffer.write(chunk)\n"
            "                            sys.stdout.buffer.flush()\n"
            "                        except Exception:\n"
            "                            try:\n"
            "                                sys.stdout.write(chunk.decode('utf-8', 'replace')); sys.stdout.flush()\n"
            "                            except Exception:\n"
            "                                pass\n"
            "                    elif p.poll() is not None:\n"
            "                        extra = rf.read(8192)\n"
            "                        if extra:\n"
            "                            pos = rf.tell()\n"
            "                            try:\n"
            "                                sys.stdout.buffer.write(extra); sys.stdout.buffer.flush()\n"
            "                            except Exception:\n"
            "                                pass\n"
            "                            continue\n"
            "                        return\n"
            "                    else:\n"
            "                        time.sleep(0.05)\n"
            "        except Exception:\n"
            "            if p is not None and p.poll() is not None:\n"
            "                return\n"
            "            time.sleep(0.1)\n"
            "t = threading.Thread(target=tail, daemon=True)\n"
            "t.start()\n"
            "rc = p.wait()\n"
            "try:\n"
            "    f.flush()\n"
            "except Exception:\n"
            "    pass\n"
            "t.join(timeout=3)\n"
            "sys.exit(rc if rc is not None else 1)\n"
        )

        wrapper_path = LOGS_DIR / f"job_{job_id}_wrap.py"
        wrapper_path.write_text(wrapper_code, encoding="utf-8")
        wrapper_cmd = [py_exe, "-u", str(wrapper_path), str(log_path)] + cmd
        # Force classic conhost on Windows 11 (default Terminal can flash-close
        # and leave Chrome/chromedriver running). The "--" is required so Win11
        # conhost treats the rest as the command, not as conhost flags.
        if os.name == "nt":
            conhost = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "conhost.exe")
            if os.path.isfile(conhost):
                wrapper_cmd = [conhost, "--"] + wrapper_cmd

        proc = subprocess.Popen(
            wrapper_cmd,
            creationflags=creation_flags,
            cwd=script_dir or str(SCRIPTS_DIR),
            env=env,
        )

        # Track the process globally for stop commands
        _add_running_proc(job_id, proc)

        # Report PID to controller immediately
        report_pid(job_id, proc.pid)
        log(f"Job #{job_id} PID: {proc.pid}")

        # Push the wrapper's "[Job started]" line once it appears (no fake progress).
        try:
            for _ in range(20):
                if log_path.exists() and log_path.stat().st_size > 0:
                    started = log_path.read_text(encoding="utf-8", errors="replace")
                    if started.strip():
                        api_post(
                            "/job-live-log",
                            {"job_id": job_id, "output": started[:4000]},
                            timeout=4,
                        )
                    break
                time.sleep(0.05)
        except Exception:
            pass

        last_log_size = 0
        last_log_post = 0.0
        inner_pid = None
        wrapper_gone_logged = False

        def _partial_log() -> str:
            if log_path.exists():
                return log_path.read_text(encoding="utf-8", errors="replace")
            return ""

        def _kill_job_procs():
            ipid = inner_pid or _read_inner_pid(log_path)
            kill_process_tree(proc.pid, extra_pids=[ipid] if ipid else None)
            _wait_proc_exit(proc, 1.2)
            _remove_running_proc(job_id)

        def _end_as_pc_terminated():
            _kill_job_procs()
            return _WIN_CTRL_C_EXIT, "[Terminated on worker PC]\n" + _partial_log(), time.time() - start_time

        def _script_still_running() -> bool:
            if proc.poll() is None:
                return True
            ipid = inner_pid or _read_inner_pid(log_path)
            return bool(ipid and _pid_alive(ipid))

        # Win11 conhost can close the outer window while python is still starting.
        for _ in range(40):
            inner_pid = _read_inner_pid(log_path)
            if inner_pid or proc.poll() is None:
                break
            time.sleep(0.05)

        while _script_still_running():
            try:
                if inner_pid is None:
                    inner_pid = _read_inner_pid(log_path)

                if proc.poll() is not None and inner_pid and _pid_alive(inner_pid) and not wrapper_gone_logged:
                    log(
                        f"Job #{job_id}: console host exited but script PID {inner_pid} "
                        "is still running — waiting on the script (not marking complete)."
                    )
                    wrapper_gone_logged = True

                # Dashboard stop first — never wait on a live-log POST to see it.
                status_resp = api_get(f"/job-status/{job_id}", timeout=3)
                if status_resp.status_code == 200:
                    job_status = status_resp.json().get("status")
                    if job_status == "stopped":
                        if inner_pid and not _pid_alive(inner_pid):
                            log(f"Job #{job_id}: dashboard stop on already-dead script; not a normal stop.")
                            return _end_as_pc_terminated()
                        log(f"Job #{job_id} cancelled by controller. Killing process tree PID {proc.pid}...")
                        _kill_job_procs()
                        return -1, "[Stopped by user]\n" + _partial_log(), time.time() - start_time
                    elif job_status == "paused":
                        if suspend_process(inner_pid or proc.pid):
                            api_post("/job-paused", {"job_id": job_id}, timeout=5)
                            log(f"Job #{job_id} paused. Waiting for resume...")
                            while True:
                                time.sleep(0.4)
                                try:
                                    sr = api_get(f"/job-status/{job_id}", timeout=3)
                                    if sr.status_code == 200:
                                        s = sr.json().get("status")
                                        if s == "running":
                                            resume_process(inner_pid or proc.pid)
                                            api_post("/job-resumed", {"job_id": job_id}, timeout=5)
                                            log(f"Job #{job_id} resumed.")
                                            break
                                        elif s == "stopped":
                                            log(f"Job #{job_id} stopped while paused. Killing...")
                                            resume_process(inner_pid or proc.pid)
                                            _kill_job_procs()
                                            return -1, "[Stopped by user]\n" + _partial_log(), time.time() - start_time
                                except Exception:
                                    pass

                # Script process ended: wait for wrapper to exit with the real code.
                # A short wait falsely marked normal finishes/crashes as "PC terminated".
                if inner_pid and not _pid_alive(inner_pid):
                    for _ in range(50):  # up to ~5s for wrapper teardown / chrome quit
                        time.sleep(0.1)
                        if proc.poll() is not None:
                            break
                    if proc.poll() is not None:
                        break
                    partial = _partial_log()
                    plow = (partial or "").lower()
                    if "traceback (most recent call last)" in plow:
                        log(f"Job #{job_id}: script PID {inner_pid} gone after Traceback — reporting error.")
                        _kill_job_procs()
                        return 1, partial, time.time() - start_time
                    if _looks_like_clean_finish(partial):
                        log(f"Job #{job_id}: script PID {inner_pid} gone after clean finish — reporting completed.")
                        _kill_job_procs()
                        return 0, partial, time.time() - start_time
                    log(f"Job #{job_id}: script PID {inner_pid} is gone (terminal closed or Ctrl+C).")
                    return _end_as_pc_terminated()

                # Live log: throttle so many jobs do not flood Flask/DB with full files.
                now_m = time.time()
                if log_path.exists() and (now_m - last_log_post) >= 4.0:
                    current_size = log_path.stat().st_size
                    if current_size > last_log_size:
                        with open(log_path, "r", encoding="utf-8", errors="replace") as f_live:
                            partial_output = f_live.read()
                        if len(partial_output) > 80_000:
                            partial_output = partial_output[-80_000:]
                        try:
                            api_post(
                                "/job-live-log",
                                {"job_id": job_id, "output": partial_output},
                                timeout=4,
                            )
                        except Exception:
                            pass
                        last_log_size = current_size
                        last_log_post = now_m
            except Exception:
                pass
            time.sleep(0.35)

        exit_code = proc.returncode
        if exit_code is None:
            exit_code = 1
    except Exception as exc:
        _remove_running_proc(job_id)
        return 1, str(exc), time.time() - start_time
    finally:
        _remove_running_proc(job_id)
        if 'proc' in locals() and proc.pid:
            # Force clean up any orphaned processes like browser instances
            ipid = inner_pid if 'inner_pid' in locals() else None
            if not ipid:
                ipid = _read_inner_pid(log_path)
            extras = [ipid] if ipid else None
            kill_process_tree(proc.pid, extra_pids=extras)
        try:
            Path(str(log_path) + ".pid").unlink()
        except Exception:
            pass
        try:
            wrap = LOGS_DIR / f"job_{job_id}_wrap.py"
            if wrap.exists():
                wrap.unlink()
        except Exception:
            pass

    duration = time.time() - start_time

    output = ""
    if log_path.exists():
        output = log_path.read_text(encoding="utf-8", errors="replace")
    else:
        output = "(no log file produced)"

    body = output
    if body.startswith("[Job started]"):
        body = "\n".join(body.splitlines()[1:]).strip()
    if duration < 2.0 and not body and "[Stopped by user]" not in (output or ""):
        output = (output or "") + (
            "\nScript console closed immediately. The worker could not keep the "
            "script process running on this PC. Confirm Python (python.exe, not pythonw) "
            "is installed and the script file exists on this machine."
        )
        if exit_code in (0, None):
            exit_code = 1

    # Wrapper exited from Ctrl+C / console close — do not label as dashboard stop.
    # Final outcome only: last-event failure → error; recovered mid-run errors → completed.
    interrupt_codes = {_WIN_CTRL_C_EXIT, -1073741510, 3221225501, -1073741787, 130, -2}
    low = (output or "").lower()
    pc_term = "[terminated on worker pc]" in low or "keyboardinterrupt" in low
    if _ended_with_failure(output or "", exit_code):
        if (output or "").startswith("[Terminated on worker PC]\n"):
            output = output[len("[Terminated on worker PC]\n"):]
        if exit_code in interrupt_codes or exit_code in (0, None):
            exit_code = 1
    elif (
        "[Stopped by user]" not in (output or "")
        and (exit_code in interrupt_codes or pc_term or exit_code == 0)
        and (_ended_successfully(output or "", exit_code) or _looks_like_clean_finish(output or ""))
    ):
        if (output or "").startswith("[Terminated on worker PC]\n"):
            output = output[len("[Terminated on worker PC]\n"):]
        exit_code = 0
    elif (
        "[Stopped by user]" not in (output or "")
        and (exit_code in interrupt_codes or pc_term)
        and not _ended_with_failure(output or "", exit_code)
    ):
        if not (output or "").startswith("[Terminated on worker PC]"):
            output = "[Terminated on worker PC]\n" + (output or "")
        if exit_code not in interrupt_codes:
            exit_code = _WIN_CTRL_C_EXIT

    return exit_code, output, duration


def poll_job() -> dict | None:
    try:
        # Optional concurrency cap — default 0 means unlimited (unchanged behavior).
        if MAX_CONCURRENT_JOBS > 0:
            with _procs_lock:
                running_n = len(_running_procs)
            if running_n >= MAX_CONCURRENT_JOBS:
                return None
        resp = api_get(f"/get-job/{WORKER_NAME}", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        if data and data.get("id"):
            log(f"Polled /get-job, received: {data}")
            return data
        else:
            return None
    except requests.RequestException as exc:
        log(f"Poll failed: {exc}")
        return None


def show_site_master_notification(title: str, body: str) -> str:
    """Native Windows toast via WinRT (windows-toasts) — no PowerShell."""
    title = (title or "Site Master").strip() or "Site Master"
    body = (body or "").strip() or "New message from Site Master"
    try:
        from windows_toasts import Toast, WindowsToaster

        toaster = WindowsToaster(title)
        toast = Toast()
        # First field is the toast headline; second is the detail body.
        toast.text_fields = [title, body]
        toaster.show_toast(toast)
        return f"Shown: {title}"
    except ImportError:
        msg = "windows-toasts not installed (pip install windows-toasts)"
        log(msg)
        return msg
    except Exception as exc:
        msg = f"Notification failed: {exc}"
        log(msg)
        return msg


def ensure_schedule_import_dir() -> Path:
    """Create the INI drop folder. Does not scan or read any files."""
    SCHEDULE_IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Schedule import folder ready: {SCHEDULE_IMPORT_DIR}")
    return SCHEDULE_IMPORT_DIR


def _schedule_import_root() -> Path:
    """Resolved import root — INI reads must stay under this path only."""
    return SCHEDULE_IMPORT_DIR.resolve()


def _is_under_schedule_import(path: Path) -> bool:
    try:
        root = _schedule_import_root()
        resolved = path.resolve()
        return resolved == root or str(resolved).startswith(str(root) + os.sep)
    except Exception:
        return False


def _parse_system_scheduler_ini(path: Path) -> dict | None:
    """
    Parse Softvoile System Scheduler task .INI ([EventInfo]).
    Returns None for Preferences.ini / non-task files.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        try:
            text = path.read_text(encoding="latin-1", errors="ignore")
        except Exception as e:
            return {"error": f"read failed: {e}", "ini_file": path.name}

    if "[EventInfo]" not in text and "[eventinfo]" not in text.lower():
        return None

    fields: dict[str, str] = {}
    in_event = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_event = line.lower() == "[eventinfo]"
            continue
        if not in_event or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            fields[key] = val

    if not fields.get("ProgramName") and not fields.get("TaskName"):
        return None

    program = (fields.get("ProgramName") or "").strip()
    task_name = (fields.get("TaskName") or "").strip() or Path(program).stem or path.stem

    try:
        hh = int(str(fields.get("TimeHH") or "0").strip() or "0")
        mm = int(str(fields.get("TimeMM") or "0").strip() or "0")
    except ValueError:
        hh, mm = 0, 0
    hh = max(0, min(23, hh))
    mm = max(0, min(59, mm))
    run_time = f"{hh:02d}:{mm:02d}"

    try:
        multi_days = int(str(fields.get("MultiDays") or "127").strip() or "127")
    except ValueError:
        multi_days = 127

    # Softvoile MultiDays: bit0=Sun … bit6=Sat
    day_bits = [
        (1, "Sun"),
        (2, "Mon"),
        (4, "Tue"),
        (8, "Wed"),
        (16, "Thu"),
        (32, "Fri"),
        (64, "Sat"),
    ]
    weekdays = [name for bit, name in day_bits if multi_days & bit]
    if not weekdays:
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    try:
        repeat_type = int(str(fields.get("RepeatType") or "3").strip() or "3")
    except ValueError:
        repeat_type = 3

    # Softvoile common: 0=Once, 1=Every minute, 2=Hourly, 3=Daily, 4=Weekly, 5=Monthly
    schedule_type = "daily"
    type_label = "Every Day / Week"
    interval_numeric = ""
    interval_unit = ""
    day_of_month = ""
    full_date = ""
    detail = ", ".join(weekdays) if len(weekdays) < 7 else "Every day"

    if repeat_type == 0:
        schedule_type = "once"
        type_label = "Once"
        start = (fields.get("StartDateTime") or fields.get("StartDate") or "").strip()
        # Accept YYYY-MM-DD or DD/MM/YYYY fragments if present
        m = re.search(r"(\d{4}-\d{2}-\d{2})", start)
        if not m:
            m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", start)
            if m:
                d, mo, y = m.group(1), m.group(2), m.group(3)
                full_date = f"{y}-{int(mo):02d}-{int(d):02d}"
        else:
            full_date = m.group(1)
        detail = full_date or "One-time (set date on import if needed)"
    elif repeat_type in (1, 2):
        schedule_type = "interval"
        type_label = "Interval"
        try:
            period = int(str(fields.get("Period") or fields.get("Every") or "1").strip() or "1")
        except ValueError:
            period = 1
        period = max(1, period)
        if repeat_type == 1:
            interval_numeric = str(period)
            interval_unit = "m"
            detail = f"Every {period} minute(s)"
        else:
            interval_numeric = str(period)
            interval_unit = "h"
            detail = f"Every {period} hour(s)"
    elif repeat_type == 5:
        schedule_type = "monthly"
        type_label = "Every Month"
        try:
            dom = int(str(fields.get("MonthDay") or fields.get("DayOfMonth") or "1").strip() or "1")
        except ValueError:
            dom = 1
        day_of_month = str(max(1, min(31, dom)))
        detail = f"Day {day_of_month} @ {run_time}"
    else:
        # Daily / Weekly → DFMS "Every Day / Week"
        schedule_type = "daily"
        type_label = "Every Day / Week"
        detail = ", ".join(weekdays) if len(weekdays) < 7 else "Every day"

    enabled_raw = (fields.get("Enabled") or "1").strip().lower()
    enabled = enabled_raw not in ("0", "false", "no", "off")

    path_exists = bool(program) and os.path.isfile(program)

    return {
        "ini_file": path.name,
        "task_name": task_name,
        "script_name": Path(program).name if program else task_name,
        "program_path": program,
        "path_exists": path_exists,
        "run_time": run_time,
        "schedule_type": schedule_type,
        "type_label": type_label,
        "detail": detail,
        "weekdays": weekdays,
        "interval_numeric": interval_numeric,
        "interval_unit": interval_unit,
        "day_of_month": day_of_month,
        "full_date": full_date,
        "enabled": enabled,
        "multi_days": multi_days,
        "repeat_type": repeat_type,
    }


def scan_schedule_import_folder() -> dict:
    """
    Read *.INI only under SCHEDULE_IMPORT_DIR. No other paths are scanned.
    Called only when the dashboard queues scan_schedule_imports.
    """
    try:
        root = ensure_schedule_import_dir().resolve()
    except Exception as e:
        return {"ok": False, "error": f"Cannot access import folder: {e}", "import_dir": str(SCHEDULE_IMPORT_DIR), "items": []}

    if not root.is_dir():
        return {"ok": False, "error": f"Import folder not found: {root}", "import_dir": str(root), "items": []}

    items = []
    skipped = 0
    errors = []
    try:
        candidates = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except Exception as e:
        return {"ok": False, "error": f"Cannot list import folder: {e}", "import_dir": str(root), "items": []}

    for entry in candidates:
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() != ".ini":
                continue
            if not _is_under_schedule_import(entry):
                continue
            if entry.name.lower() == "preferences.ini":
                skipped += 1
                continue
            parsed = _parse_system_scheduler_ini(entry)
            if parsed is None:
                skipped += 1
                continue
            if parsed.get("error") and not parsed.get("program_path"):
                errors.append(f"{entry.name}: {parsed['error']}")
                continue
            items.append(parsed)
        except Exception as e:
            errors.append(f"{entry.name}: {e}")

    log(f"scan_schedule_imports: {len(items)} task(s) from {root} (skipped={skipped})")
    return {
        "ok": True,
        "import_dir": str(root),
        "items": items,
        "skipped": skipped,
        "errors": errors,
    }


def poll_commands() -> bool:
    """Claim and run at most one pending command. Returns True if a command ran."""
    global WORKER_NAME, SCRIPTS_DIR, dirty_full_tree, dirty_scripts, last_event_time, first_event_time, observer
    try:
        resp = api_get(f"/get-command/{WORKER_NAME}", timeout=8)
        if resp.status_code != 200:
            return False
        cmd = resp.json()
        if not cmd or not cmd.get("id"):
            return False

        cmd_id = cmd["id"]
        action = cmd["command"]
        payload = json.loads(cmd.get("payload", "{}"))

        log(f"Received command #{cmd_id}: {action}")
        output = ""
        success = True

        try:
            def _safe_path(p: str) -> Path:
                target = (SCRIPTS_DIR / str(p).strip().lstrip("/\\")).resolve()
                if not str(target).startswith(str(SCRIPTS_DIR.resolve())):
                    raise ValueError(f"Path traversal detected: {p}")
                return target

            if action == "rename":
                new_name = payload.get("new_name")
                if new_name:
                    WORKER_NAME = new_name
                    output = f"Renamed to {new_name}"
            elif action == "create_folder":
                target = _safe_path(payload.get("target_path", ""))
                target.mkdir(parents=True, exist_ok=True)
                output = f"Created {target}"
            elif action == "delete_folder":
                import shutil
                target = _safe_path(payload.get("target_path", ""))
                if target.exists() and target.is_dir():
                    shutil.rmtree(target)
                output = f"Deleted {target}"
            elif action == "update_days":
                script_path = _safe_path(payload.get("script_path", ""))
                days = payload.get("days", 0)
                if script_path.exists() and script_path.is_file():
                    content = script_path.read_text(encoding="utf-8", errors="ignore")
                    pattern = r"^(\s*days\s*=\s*)(\d+)(.*)$"
                    new_content, count = re.subn(
                        pattern,
                        lambda m: f"{m.group(1)}{days}{m.group(3)}",
                        content,
                        flags=re.MULTILINE | re.IGNORECASE,
                    )
                    if count:
                        script_path.write_text(new_content, encoding="utf-8")
                        output = f"Updated days to {days} in {script_path}"
                    else:
                        output = f"days variable not found in {script_path}"
                else:
                    output = f"Script not found: {script_path}"
            elif action == "rename_folder":
                source = _safe_path(payload.get("source_path", ""))
                new_name = payload.get("new_name", "")
                if source.exists() and source.is_dir() and new_name:
                    target = source.parent / new_name
                    # Make sure target is also safe (parent must be safe)
                    if not str(target.resolve()).startswith(str(SCRIPTS_DIR.resolve())):
                         raise ValueError("Invalid target name")
                    source.rename(target)
                    output = f"Renamed {source.name} → {target.name}"
                else:
                    output = f"Invalid rename: source={source}, new_name={new_name}"
                    success = False
            elif action == "rename_file":
                source = _safe_path(payload.get("source_path", ""))
                new_name = payload.get("new_name", "")
                if source.exists() and source.is_file() and new_name:
                    target = source.parent / new_name
                    if not str(target.resolve()).startswith(str(SCRIPTS_DIR.resolve())):
                         raise ValueError("Invalid target name")
                    source.rename(target)
                    output = f"Renamed {source.name} → {target.name}"
                else:
                    output = f"Invalid rename: source={source}, new_name={new_name}"
                    success = False
            elif action == "delete_file":
                target = _safe_path(payload.get("target_path", ""))
                if target.exists() and target.is_file():
                    target.unlink()
                output = f"Deleted {target}"
            elif action == "write_file":
                import base64
                target = _safe_path(payload.get("target_path", ""))
                # Prefer file_content_b64; accept legacy file_content for older controllers
                b64_data = payload.get("file_content_b64") or payload.get("file_content") or ""
                data = base64.b64decode(b64_data)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                output = f"Wrote to {target}"
            elif action == "move_item":
                source = _safe_path(payload.get("source_path", ""))
                dest_parent_rel = (payload.get("dest_parent") or "").replace("\\", "/").strip("/")
                dest_name = source.name
                if dest_parent_rel:
                    dest_parent = _safe_path(dest_parent_rel)
                else:
                    dest_parent = SCRIPTS_DIR.resolve()
                dest_parent.mkdir(parents=True, exist_ok=True)
                dest = dest_parent / dest_name
                if not str(dest.resolve()).startswith(str(SCRIPTS_DIR.resolve())):
                    raise ValueError("Invalid move destination")
                if not source.exists():
                    output = f"Source not found: {source}"
                    success = False
                elif dest.exists():
                    output = f"Destination already exists: {dest}"
                    success = False
                else:
                    shutil.move(str(source), str(dest))
                    output = f"Moved {source} → {dest}"
            elif action == "read_file":
                import base64
                target = _safe_path(payload.get("target_path", ""))
                if target.exists() and target.is_file():
                    data = target.read_bytes()
                    output = base64.b64encode(data).decode("utf-8")
                else:
                    output = f"File not found: {target}"
                    success = False
            elif action == "resync_folder":
                folder = (payload.get("folder_path") or "").replace("\\", "/").strip("/")
                # Rescan current folder contents from disk and push to controller
                sync_folder_partial(folder)
                flush_snapshot_if_dirty(force=True)
                output = f"Resynced folder: {folder or '/'}"
            elif action == "reload_config":
                # Dashboard changed script_location — apply path immediately, then
                # sync the tree in a background thread so interactive commands
                # (read_file / write_file) are not blocked for many minutes.
                loc = (payload.get("script_location") or "").strip()
                if loc:
                    new_dir = Path(loc)
                    if _norm_path_str(SCRIPTS_DIR) != _norm_path_str(new_dir):
                        SCRIPTS_DIR = new_dir
                        log(f"reload_config: SCRIPTS_DIR → {SCRIPTS_DIR}")
                        save_local_state(script_location=str(SCRIPTS_DIR), worker_name=WORKER_NAME)
                        clear_tree_snapshot()
                        if observer and observer.is_alive():
                            try:
                                observer.stop()
                                observer.join(timeout=2)
                            except Exception:
                                pass
                        if not SCRIPTS_DIR.exists():
                            try:
                                SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
                            except Exception as e:
                                log(f"Could not create SCRIPTS_DIR: {e}")
                        try:
                            observer = Observer()
                            observer.schedule(ScriptFolderWatcher(), str(SCRIPTS_DIR), recursive=True)
                            if OUTPUT_DIR.exists():
                                observer.schedule(OutputFolderWatcher(), str(OUTPUT_DIR), recursive=True)
                            observer.start()
                        except Exception as e:
                            log(f"Failed to rebind watcher: {e}")
                    else:
                        # Same path — still refresh from controller then resync
                        fetch_config()
                else:
                    fetch_config()
                started = start_background_tree_sync("reload_config", replace=True)
                output = (
                    f"Config reloaded. SCRIPTS_DIR={SCRIPTS_DIR}. "
                    f"tree_sync={'started' if started else 'already_running'}"
                )
            elif action == "desktop_notify":
                title = (payload.get("title") or "Site Master").strip() or "Site Master"
                body = (payload.get("body") or "").strip() or "New message from Site Master"
                output = show_site_master_notification(title, body)
            elif action == "scan_schedule_imports":
                # On-demand only — never auto-scan this folder
                result = scan_schedule_import_folder()
                output = json.dumps(result, ensure_ascii=False)
                if not result.get("ok"):
                    success = False
            else:
                output = f"Unknown command: {action}"
                success = False

            # After mutating filesystem ops, push parent listing so dashboard updates quickly
            if success and action in (
                "create_folder", "delete_folder", "rename_folder",
                "delete_file", "rename_file", "write_file", "move_item",
            ):
                try:
                    if action == "move_item":
                        src = (payload.get("source_path") or "").replace("\\", "/").strip("/")
                        dest_parent = (payload.get("dest_parent") or "").replace("\\", "/").strip("/")
                        sync_folder_partial(_parent_rel(src))
                        sync_folder_partial(dest_parent)
                    else:
                        target_hint = (
                            payload.get("target_path")
                            or payload.get("source_path")
                            or ""
                        )
                        parent = _parent_rel((target_hint or "").replace("\\", "/").strip("/"))
                        sync_folder_partial(parent)
                    flush_snapshot_if_dirty(force=False)
                except Exception as sync_e:
                    log(f"Post-command folder sync skipped: {sync_e}")
        except Exception as e:
            output = f"Command failed: {e}"
            success = False

        if not post_command_complete(cmd_id, "completed" if success else "error", output):
            log(f"Failed to report command #{cmd_id} completion after retries")
        return True

    except Exception as exc:
        log(f"Command poll failed: {exc}")
        return False


def extract_metrics(output: str) -> dict:
    metrics = {}

    images_match = re.search(r'(?i)total images?[:=]\s*(\d+)', output)
    if images_match:
        metrics['total_images'] = int(images_match.group(1))
        metrics['image_count'] = int(images_match.group(1))

    # Common selenium / requests scraper log lines
    for pat in (
        r'(?i)(?:downloaded|saved|fetched)\s+(\d+)\s+images?',
        r'(?i)images?\s+(?:downloaded|saved|fetched)[:=]?\s*(\d+)',
        r'(?i)(?:total|count)\s+of\s+images?[:=]?\s*(\d+)',
    ):
        m = re.search(pat, output)
        if m:
            n = int(m.group(1))
            metrics['image_count'] = max(int(metrics.get('image_count') or 0), n)
            metrics['total_images'] = max(int(metrics.get('total_images') or 0), n)
            break

    pdf_match = re.search(r'(?i)(?:total\s+)?pdfs?[:=]\s*(\d+)|(?:downloaded|saved)\s+(\d+)\s+pdfs?', output)
    if pdf_match:
        n = int(pdf_match.group(1) or pdf_match.group(2) or 0)
        if n:
            metrics['pdf_count'] = n

    output_match = re.search(r'(?i)output count[:=]\s*(\d+)', output)
    if output_match:
        metrics['output_count'] = int(output_match.group(1))
        
    metrics['warning_count'] = len(re.findall(r'(?i)\bwarning\b', output))
    # Leave error_count to the server parse_execution_log — naive word matches
    # inflate counts (and disagree with View Error categories).
    metrics['error_count'] = 0

    folders = re.findall(r'(?i)(?:OUTPUT|REPORT)_FOLDER[:=]\s*([^\r\n]+)', output)
    if folders:
        metrics['explicit_folders'] = [f.strip() for f in folders]

    # Count image files only on download/save lines — not every .png URL or traceback path
    img_mentions = []
    pdf_mentions = []
    for line in (output or "").splitlines():
        if not re.search(r'(?i)\b(?:download(?:ed)?|saved|fetched|wrote|written)\b', line):
            continue
        img_mentions.extend(
            re.findall(
                r'(?i)([^\s\'\"<>]+\.(?:jpg|jpeg|png|webp|gif|bmp|tif|tiff|jfif))\b',
                line,
            )
        )
        pdf_mentions.extend(
            re.findall(r'(?i)([^\s\'\"<>]+\.pdf)\b', line)
        )
    if img_mentions:
        n = len({m.lower() for m in img_mentions})
        metrics["image_count"] = max(int(metrics.get("image_count") or 0), n)
        metrics["total_images"] = max(int(metrics.get("total_images") or 0), n)
    if pdf_mentions:
        n = len({m.lower() for m in pdf_mentions})
        metrics["pdf_count"] = max(int(metrics.get("pdf_count") or 0), n)
    dl_img = len(
        re.findall(
            r'(?i)(?:image downloaded|image saved|images? downloaded and saved)',
            output or "",
        )
    )
    if dl_img:
        metrics["image_count"] = max(int(metrics.get("image_count") or 0), dl_img)
        metrics["total_images"] = max(int(metrics.get("total_images") or 0), dl_img)
    pages = re.findall(r'(?i)\bpage\s*(\d+)\b', output or "")
    if pages:
        try:
            metrics["log_count"] = max(int(p) for p in pages)
        except ValueError:
            metrics["log_count"] = len(pages)

    # Also pick folder paths printed as "saved to ..." / "download folder ..."
    extra_folders = re.findall(
        r'(?i)(?:saved to|download(?:ed)?(?:\s+to)?|output(?:\s+dir(?:ectory)?)?|folder)[\s:=]+([A-Za-z]:\\[^\r\n\"\']+|/[^\r\n\"\']+)',
        output,
    )
    if extra_folders:
        cleaned = []
        for f in extra_folders:
            f = f.strip().strip('"').strip("'")
            # If a file path was captured, use its parent
            if f.lower().endswith(tuple(IMAGE_EXTS) + tuple(PDF_EXTS)):
                f = str(Path(f).parent)
            cleaned.append(f)
        metrics['explicit_folders'] = list(dict.fromkeys(
            (metrics.get('explicit_folders') or []) + cleaned
        ))

    # Extract detailed error info for the new reports UI
    failed_files = re.findall(r'(?i)(?:failed file|failed to download|failed|error file)[\s:=]+([^\r\n]+)', output)
    missing_files = re.findall(r'(?i)(?:missing file|not found|missing)[\s:=]+([^\r\n]+)', output)
    
    error_details = {}
    if failed_files or missing_files or metrics['error_count'] > 0:
        error_details = {
            "failed_files": [f.strip() for f in failed_files if f.strip()],
            "missing_files": [f.strip() for f in missing_files if f.strip()],
            "folder_summary": {},
            "error": output[-2000:] if len(output) > 2000 else output
        }
        metrics['error_details'] = error_details

    return metrics


def _collapse_download_roots(folders: list[Path]) -> list[Path]:
    """Prefer download*/<date> roots over leaf page/image folders.

    Target layout: ``.../download/<date>/`` (files directly under the date folder).
    Collapses ``.../download/<date>/<page>/<image>/...`` up to the date folder.
    Also accepts ``downloads`` / ``downloaded_images``.
    """
    if not folders:
        return []
    download_names = {"download", "downloads", "downloaded_images"}
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{8}$|^\d{2}-\d{2}-\d{4}$")
    collapsed: list[Path] = []
    seen = set()
    for folder in folders:
        try:
            parts = list(folder.parts)
            chosen = folder
            lower_parts = [p.lower() for p in parts]
            dl_idx = next((i for i, p in enumerate(lower_parts) if p in download_names), -1)
            if dl_idx >= 0:
                # download / <date> / ... → keep through date when present
                if dl_idx + 1 < len(parts) and date_re.match(parts[dl_idx + 1]):
                    candidate = Path(*parts[: dl_idx + 2])
                else:
                    candidate = Path(*parts[: dl_idx + 1])
                if candidate.exists():
                    chosen = candidate
                else:
                    chosen = candidate  # still prefer logical root for folder_path label
            else:
                # Strip trailing page-number / image leaf segments
                while len(parts) > 1:
                    last = parts[-1].lower()
                    if last in {"image", "images", "img", "page", "pages"} or last.isdigit():
                        parts = parts[:-1]
                        continue
                    break
                chosen = Path(*parts) if parts else folder
            try:
                key = str(chosen.resolve()).lower()
            except OSError:
                key = str(chosen).lower()
            if key not in seen:
                seen.add(key)
                collapsed.append(chosen)
        except Exception:
            key = str(folder).lower()
            if key not in seen:
                seen.add(key)
                collapsed.append(folder)
    return collapsed


def analyze_job_output(script_path: str, start_time: float, end_time: float, explicit_folders: list = None) -> dict:
    """Scan job output folders and count images/PDFs written for this run.

    Improvements vs older strict single-folder scan:
    - considers all folders touched in the job window (not only the newest)
    - collapses epaper page leaves up to downloaded_images/<date>
    - if a folder itself was created during the job, count its files even when
      individual file timestamps are slightly outside the window
    """
    metrics = {
        "folder_path": "",
        "image_count": 0,
        "pdf_count": 0,
        "file_count": 0,
        "log_count": 0,
        "total_folder_size": 0
    }

    TIME_PAD = 60.0  # allow FS flush / antivirus delay
    win_start = start_time - TIME_PAD
    win_end = end_time + TIME_PAD

    dirs_to_scan: list[Path] = []
    if explicit_folders:
        for f in explicit_folders:
            p = Path(f)
            if p.exists() and p.is_dir():
                dirs_to_scan.append(p)
            elif p.exists() and p.is_file():
                dirs_to_scan.append(p.parent)
        dirs_to_scan = _collapse_download_roots(dirs_to_scan)

    if not dirs_to_scan:
        script_dir = Path(script_path).parent
        search_dirs = [
            script_dir / "downloaded_images",
            OUTPUT_DIR,
            Path.home() / "Documents" / "PythonDocuments",
            Path.home() / "Documents" / "PythonDocumentscorrigendum",
        ]
        # Also walk sibling downloaded_images under the script tree root (common epaper layout)
        try:
            for child in script_dir.iterdir():
                if child.is_dir() and child.name.lower() == "downloaded_images":
                    search_dirs.append(child)
        except OSError:
            pass

        recent_folders = []
        for base_dir in search_dirs:
            if not base_dir.exists():
                continue
            try:
                # Prefer direct children + one level for speed; still catch date folders
                candidates = [base_dir]
                try:
                    candidates.extend([p for p in base_dir.iterdir() if p.is_dir()])
                    for child in list(candidates[1:]):
                        try:
                            candidates.extend([p for p in child.iterdir() if p.is_dir()][:30])
                        except OSError:
                            pass
                except OSError:
                    pass
                for item in candidates:
                    if not item.is_dir() or item.name.startswith("__") or ".git" in item.parts:
                        continue
                    try:
                        st = item.stat()
                        if win_start <= st.st_mtime <= win_end or win_start <= st.st_ctime <= win_end:
                            recent_folders.append((max(st.st_mtime, st.st_ctime), item))
                    except OSError:
                        pass
                # Deep search only under downloaded_images trees
                if "downloaded_images" in str(base_dir).lower() or base_dir.name.lower() == "downloaded_images":
                    for item in base_dir.rglob("*"):
                        if not item.is_dir() or item.name.startswith("__"):
                            continue
                        try:
                            st = item.stat()
                            if win_start <= st.st_mtime <= win_end or win_start <= st.st_ctime <= win_end:
                                recent_folders.append((max(st.st_mtime, st.st_ctime), item))
                        except OSError:
                            pass
            except OSError:
                pass

        if recent_folders:
            recent_folders.sort(key=lambda x: x[0], reverse=True)
            # Keep several recent folders (not just one leaf page)
            uniq = []
            seen = set()
            for _, folder in recent_folders:
                key = str(folder).lower()
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(folder)
                if len(uniq) >= 25:
                    break
            dirs_to_scan = _collapse_download_roots(uniq)

    if dirs_to_scan:
        dirs_to_scan = _collapse_download_roots(dirs_to_scan)
        metrics["folder_path"] = ", ".join(str(d) for d in dirs_to_scan[:5])

    counted_files: set[str] = set()

    def _in_window(ts: float) -> bool:
        return win_start <= ts <= win_end

    for target_folder in dirs_to_scan:
        try:
            try:
                folder_st = target_folder.stat()
                # Creation-time only: avoids counting yesterday's images when a parent
                # folder is merely touched (mtime) by a new page subfolder.
                folder_is_new = _in_window(folder_st.st_ctime)
            except OSError:
                folder_is_new = False

            for item in target_folder.rglob("*"):
                if not item.is_file():
                    continue
                key = str(item.resolve()).lower() if item.exists() else str(item).lower()
                if key in counted_files:
                    continue
                try:
                    st = item.stat()
                    # Count if file touched during job, OR this folder was created in-window
                    # (covers selenium writes whose file mtimes lag slightly).
                    if not (_in_window(st.st_mtime) or _in_window(st.st_ctime) or folder_is_new):
                        continue
                    counted_files.add(key)
                    metrics["file_count"] += 1
                    metrics["total_folder_size"] += st.st_size
                    ext = item.suffix.lower()
                    if ext in IMAGE_EXTS:
                        metrics["image_count"] += 1
                    elif ext in PDF_EXTS:
                        metrics["pdf_count"] += 1
                    elif ext in (".txt", ".log"):
                        metrics["log_count"] += 1
                except OSError:
                    pass
        except OSError:
            pass

    return metrics


def report_complete(job_id: int, output: str, duration: float, exit_code: int, metrics: dict) -> None:
    payload = {"job_id": job_id, "output": output, "duration": duration, "exit_code": exit_code}
    payload.update(metrics)
    api_post("/job-complete", payload)


def handle_job(job: dict) -> None:
    job_id = job["id"]
    script_path = job["script_path"]
    script_name = job.get("script_name", script_path)

    days = job.get("days")
    try:
        days_int = int(days) if days is not None and days != "" else None
    except (TypeError, ValueError):
        days_int = None

    execution_path = _resolve_job_script_path(script_path, script_name)
    temp_script_path = None
    # days=0 (common default / midnight reset): run original file — no temp copy
    if days_int is not None and days_int != 0:
        try:
            original_content = Path(execution_path).read_text(encoding="utf-8", errors="ignore")
            pattern = r"^(\s*days\s*=\s*)(\d+)(.*)$"
            new_content, count = re.subn(
                pattern,
                lambda m: f"{m.group(1)}{days_int}{m.group(3)}",
                original_content,
                flags=re.MULTILINE | re.IGNORECASE,
            )
            if count:
                p = Path(execution_path)
                temp_script_path = p.with_name(f"__temp_{job_id}_{p.name}")
                temp_script_path.write_text(new_content, encoding="utf-8")
                execution_path = str(temp_script_path)
                log(f"Pre-run days update: using temp script {temp_script_path.name} (days={days_int})")
            else:
                log(f"Pre-run days update skipped: no days = N line in {execution_path}")
        except Exception as e:
            log(f"Failed to create temp script for days update: {e}")
    elif days_int == 0:
        log(f"Job #{job_id}: days=0 — running original script (no temp file)")

    log(
        f"Job #{job_id} fetched: script_path={script_path}, "
        f"execution_path={execution_path}, script_name={script_name}, days={days_int}"
    )

    begin_job_output_tracking(job_id)
    tracked_metrics: dict = {}
    start_time = time.time()
    end_time = start_time
    exit_code, output, duration = 1, "", 0.0
    try:
        start_time = time.time()
        exit_code, output, duration = execute_script(execution_path, job_id, days_int)
        end_time = time.time()
    finally:
        tracked_metrics = end_job_output_tracking()
        if temp_script_path and temp_script_path.exists():
            try:
                temp_script_path.unlink()
                log(f"Cleaned up temp script {temp_script_path.name}")
            except Exception as e:
                log(f"Failed to clean up temp script: {e}")

    def _is_dashboard_stop(text: str) -> bool:
        t = text or ""
        return t.startswith("[Stopped by user]") or t.startswith("[Stop requested from dashboard]")

    def _is_pc_terminated(code, text: str) -> bool:
        low = (text or "").lower()
        if "[terminated on worker pc]" in low or "process ended without report" in low:
            return True
        if any(k in low for k in ("keyboardinterrupt", "ctrl+c")) and not _is_dashboard_stop(text or ""):
            return True
        try:
            c = int(code) if code is not None else None
        except (TypeError, ValueError):
            c = None
        return c in (3221225786, -1073741510, 3221225501, -1073741787, 130, -2)

    def _output_has_traceback(text: str) -> bool:
        return "traceback (most recent call last)" in (text or "").lower()

    def _is_manual_stop(code, text: str) -> bool:
        # Final crash → error. Successful finish (even with mid-run errors) → not stop.
        if _ended_with_failure(text or "", code):
            return False
        if _ended_successfully(text or "", code) or _looks_like_clean_finish(text or ""):
            return False
        if _output_has_traceback(text or "") and not _is_dashboard_stop(text or ""):
            # Mid-run traceback only — still allow PC-terminate if that was the final event
            pass
        return _is_dashboard_stop(text or "") or _is_pc_terminated(code, text)

    # Tell the dashboard immediately on stop/Ctrl+C — do not wait on FS scans.
    # Crash (Traceback) must report as error, not stopped — even if console died after.
    metrics: dict = dict(extract_metrics(output or "") or {})
    if tracked_metrics:
        for k in ("image_count", "pdf_count", "file_count", "folder_path"):
            if not metrics.get(k) and tracked_metrics.get(k):
                metrics[k] = tracked_metrics[k]
    reported_early = False
    try:
        if _is_dashboard_stop(output or ""):
            payload = {
                "job_id": job_id,
                "output": output if (output or "").startswith("[Stopped by user]") else ("[Stopped by user]\n" + (output or "")),
                "exit_code": exit_code,
                "duration": duration,
            }
            payload.update(metrics)
            api_post("/job-stopped", payload, timeout=15)
            reported_early = True
            log(f"Job #{job_id} stopped from dashboard (exit {exit_code}).")
        elif _is_manual_stop(exit_code, output):
            if not (output or "").startswith("[Terminated on worker PC]"):
                output = "[Terminated on worker PC]\n" + (output or "")
            lines = (output or "").strip().split("\n")
            error_text = "\n".join(lines[-30:]) if len(lines) > 30 else "\n".join(lines)
            metrics["error_details"] = {
                "error_title": "Terminated on worker PC",
                "error_type": "User Interrupted",
                "failed_files": [],
                "missing_files": [],
                "folder_summary": {},
                "error": error_text or "Script console was closed or the process was killed (Ctrl+C).",
            }
            payload = {
                "job_id": job_id,
                "output": output,
                "exit_code": exit_code,
                "duration": duration,
            }
            payload.update(metrics)
            api_post("/job-stopped", payload, timeout=15)
            reported_early = True
            log(f"Job #{job_id} terminated on worker PC (exit {exit_code}).")
    except Exception as exc:
        log(f"Early stop report failed for job #{job_id}: {exc}")

    try:
        log_image = int(metrics.get("image_count") or metrics.get("total_images") or 0)
        log_pdf = int(metrics.get("pdf_count") or 0)
        explicit = list(metrics.get("explicit_folders") or [])
        # Folders observed by watchdog during the run help FS scan find downloads
        if tracked_metrics.get("folder_path"):
            for f in str(tracked_metrics["folder_path"]).split(","):
                f = f.strip()
                if f and f not in explicit:
                    explicit.append(f)
        # Heavy rglob only for completed/error runs. Stop/Ctrl+C already reported.
        if not reported_early:
            fs_metrics = analyze_job_output(script_path, start_time, end_time, explicit or None)
            if "explicit_folders" in metrics:
                del metrics["explicit_folders"]
            metrics.update(fs_metrics)
            metrics["image_count"] = max(
                log_image,
                int(fs_metrics.get("image_count") or 0),
                int(tracked_metrics.get("image_count") or 0),
            )
            metrics["pdf_count"] = max(
                log_pdf,
                int(fs_metrics.get("pdf_count") or 0),
                int(tracked_metrics.get("pdf_count") or 0),
            )
            if not metrics.get("folder_path") and tracked_metrics.get("folder_path"):
                metrics["folder_path"] = tracked_metrics["folder_path"]
            metrics["file_count"] = max(
                int(metrics.get("file_count") or 0),
                int(tracked_metrics.get("file_count") or 0),
            )
    except Exception as exc:
        log(f"Metrics extraction failed for job #{job_id}: {exc}")
        if not metrics:
            metrics = dict(tracked_metrics or {})

    # Always report a terminal status so dashboard never stays stuck on "running"
    try:
        if reported_early:
            pass
        elif _ended_successfully(output or "", exit_code) or (
            exit_code == 0 and not _ended_with_failure(output or "", exit_code)
        ):
            report_complete(job_id, output, duration, 0 if _ended_successfully(output or "", exit_code) else exit_code, metrics)
            log(f"Job #{job_id} completed in {duration:.2f}s.")
        elif _is_dashboard_stop(output or ""):
            payload = {
                "job_id": job_id,
                "output": output if (output or "").startswith("[Stopped by user]") else ("[Stopped by user]\n" + (output or "")),
                "exit_code": exit_code,
                "duration": duration,
            }
            payload.update(metrics)
            api_post("/job-stopped", payload, timeout=15)
            log(f"Job #{job_id} stopped from dashboard (exit {exit_code}).")
        elif _is_manual_stop(exit_code, output):
            if not (output or "").startswith("[Terminated on worker PC]"):
                output = "[Terminated on worker PC]\n" + (output or "")
            lines = (output or "").strip().split("\n")
            error_text = "\n".join(lines[-30:]) if len(lines) > 30 else "\n".join(lines)
            metrics["error_details"] = {
                "error_title": "Terminated on worker PC",
                "error_type": "User Interrupted",
                "failed_files": [],
                "missing_files": [],
                "folder_summary": {},
                "error": error_text or "Script console was closed or the process was killed (Ctrl+C).",
            }
            payload = {
                "job_id": job_id,
                "output": output,
                "exit_code": exit_code,
                "duration": duration,
            }
            payload.update(metrics)
            api_post("/job-stopped", payload, timeout=15)
            log(f"Job #{job_id} terminated on worker PC (exit {exit_code}).")
        else:
            if "error_details" not in metrics:
                lines = (output or "").strip().split("\n")
                error_text = "\n".join(lines[-30:]) if len(lines) > 30 else "\n".join(lines)
                err_title = "Runtime Error"
                if "selenium.common.exceptions" in (output or ""):
                    err_title = "Selenium Error"
                elif "requests.exceptions" in (output or ""):
                    err_title = "Download Error"
                metrics["error_details"] = {
                    "error_title": err_title,
                    "error_type": err_title,
                    "failed_files": [],
                    "missing_files": [],
                    "folder_summary": {},
                    "error": error_text,
                }
            # Strip false terminate prefix if we reclassified a crash as error
            out_body = output or ""
            if out_body.startswith("[Terminated on worker PC]\n"):
                out_body = out_body[len("[Terminated on worker PC]\n"):]
            payload = {
                "job_id": job_id,
                "output": f"Exit code {exit_code}\n{out_body}",
                "duration": duration,
                "exit_code": exit_code if exit_code not in (0, None) else 1,
            }
            payload.update(metrics)
            api_post("/job-error", payload, timeout=15)
            log(f"Job #{job_id} failed (exit {exit_code}) in {duration:.2f}s.")
    except Exception as exc:
        log(f"Failed to report job #{job_id}: {exc}")
        try:
            # Last-resort so controller does not leave the job running forever
            if _is_dashboard_stop(output or ""):
                out = "[Stopped by user]\n" + (output or str(exc))
            elif _is_pc_terminated(exit_code, output or ""):
                out = output if (output or "").startswith("[Terminated on worker PC]") else ("[Terminated on worker PC]\n" + (output or str(exc)))
            else:
                out = f"Exit code {exit_code}\n{output or ''}\n[report failure: {exc}]"
            fallback = {
                "job_id": job_id,
                "output": out,
                "exit_code": exit_code,
                "duration": duration,
            }
            fallback.update(metrics or tracked_metrics or {})
            if _is_dashboard_stop(output or "") or _is_pc_terminated(exit_code, output or ""):
                api_post("/job-stopped", fallback)
            else:
                api_post("/job-error", fallback)
        except Exception as exc2:
            log(f"Critical: could not report job #{job_id} at all: {exc2}")


def update_script_days_in_file(script_path: str, days: int) -> str:
    """Update the `days` variable in the given script file.
    Returns a success message or an error description."""
    try:
        p = Path(script_path)
        if not p.is_file():
            return f"Error: script file not found: {script_path}"
        content = p.read_text(encoding="utf-8")
        pattern = r"^(\s*days\s*=\s*)(\d+)(.*)$"
        new_content, count = re.subn(
            pattern,
            lambda m: f"{m.group(1)}{days}{m.group(3)}",
            content,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        if count == 0:
            return "Error: days variable not found in script"
        p.write_text(new_content, encoding="utf-8")
        return "Success: days updated"
    except Exception as e:
        return f"Error: {e}"
        
        
def cleanup_orphaned_temp_scripts() -> int:
    """
    Delete leftover __temp_{job_id}_*.py copies left behind after worker crash / PC power-off.
    Keeps files for jobs that are still tracked as running (safe if called mid-session).
    Temps stay beside the original script (same folder) so download paths stay correct.
    """
    with _procs_lock:
        running_ids = set(_running_procs.keys())

    roots: set[Path] = set()
    try:
        if SCRIPTS_DIR:
            roots.add(Path(SCRIPTS_DIR))
    except Exception:
        pass

    # Also clean folders of registered scripts that live outside SCRIPTS_DIR
    try:
        resp = api_get(f"/api/worker-scripts?worker_name={WORKER_NAME}", timeout=20)
        if resp.ok:
            for row in (resp.json() or {}).get("scripts") or []:
                path_str = (row.get("script_path") or "").strip()
                if not path_str:
                    continue
                parent = Path(path_str).parent
                if parent.is_dir():
                    roots.add(parent)
    except Exception as exc:
        log(f"Orphan temp cleanup: could not list registered scripts ({exc})")

    deleted = 0
    seen: set[str] = set()
    for root in roots:
        try:
            # Full tree under SCRIPTS_DIR; only the folder itself for outside roots
            if SCRIPTS_DIR and Path(root).resolve() == Path(SCRIPTS_DIR).resolve():
                candidates = root.rglob("__temp_*")
            else:
                candidates = root.glob("__temp_*")
            for path in candidates:
                try:
                    if not path.is_file():
                        continue
                    key = str(path.resolve()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    name = path.name
                    m = re.match(r"^__temp_(\d+)_", name, re.IGNORECASE)
                    if m and int(m.group(1)) in running_ids:
                        continue
                    path.unlink()
                    deleted += 1
                    log(f"Removed orphan temp script: {path}")
                except Exception as e:
                    log(f"Failed to remove orphan temp {path}: {e}")
        except Exception as e:
            log(f"Orphan temp scan failed under {root}: {e}")

    if deleted:
        log(f"Orphan temp cleanup removed {deleted} file(s)")
    else:
        log("Orphan temp cleanup: no leftover __temp_* files")
    return deleted


def main() -> None:
    log(f"Worker starting — controller: {CONTROLLER_URL}")

    # 1) Register first (FK constraints for scripts/tree)
    if not register_worker():
        log("Warning: initial registration failed; will retry in background.")
    try:
        api_post("/register-worker", {"worker_name": WORKER_NAME, "state": "idle"})
    except Exception:
        pass

    # 2) Immediately restore last synchronized script path (controller → local state → default)
    server_cfg = bootstrap_scripts_dir()
    log(f"Scripts directory: {SCRIPTS_DIR}")

    if not SCRIPTS_DIR.exists():
        try:
            SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            log(f"Created scripts folder: {SCRIPTS_DIR}")
        except Exception as e:
            log(f"WARNING: could not create SCRIPTS_DIR: {e}")

    # Crash / power-off leftovers: remove __temp_* next to scripts before accepting jobs
    try:
        cleanup_orphaned_temp_scripts()
    except Exception as e:
        log(f"Orphan temp cleanup error: {e}")

    # Ensure output dir exists (watcher attached after startup sync)
    if not OUTPUT_DIR.exists():
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # Schedule-import drop folder only — never scanned until dashboard Import
    try:
        ensure_schedule_import_dir()
    except Exception as e:
        log(f"Could not create schedule import folder: {e}")

    # Heartbeat MUST start before the possibly multi-minute startup tree sync,
    # otherwise the dashboard flips the worker offline after WORKER_OFFLINE_SECONDS.
    threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()
    log("Background heartbeat thread started.")

    def _startup_sync_then_watch():
        """Full/incremental sync first, then attach FS watchers (avoids walk flood)."""
        global observer
        try:
            do_full, reason = needs_full_tree_sync(server_cfg)
            if do_full:
                log(f"Startup full sync required: {reason}")
                sync_scripts()
                if not sync_file_tree():
                    log("Startup full sync failed — will retry via watcher/heartbeat")
            else:
                log(f"Startup incremental sync: {reason}")
                try:
                    ok = incremental_sync_from_snapshot()
                    if not ok:
                        log("Incremental sync unavailable — falling back to full sync")
                        sync_scripts()
                        sync_file_tree()
                    else:
                        sync_scripts()
                except Exception as e:
                    log(f"Incremental sync error ({e}) — falling back to full reconciliation")
                    try:
                        sync_scripts()
                        sync_file_tree()
                    except Exception as e2:
                        log(f"Full sync fallback also failed: {e2}")
        except Exception as e:
            log(f"Startup sync crashed: {e}")

        try:
            observer = Observer()
            observer.schedule(ScriptFolderWatcher(), str(SCRIPTS_DIR), recursive=True)
            if OUTPUT_DIR.exists():
                observer.schedule(OutputFolderWatcher(), str(OUTPUT_DIR), recursive=True)
                log(f"Attached future-reporting watcher to output directory: {OUTPUT_DIR}")
            observer.start()
            log("Event-driven file watcher started.")
            threading.Thread(target=watcher_debounce_loop, daemon=True, name="watcher-debounce").start()
            log("Watcher debounce thread started.")
        except Exception as e:
            log(f"Failed to start file watcher: {e}")

    threading.Thread(target=_startup_sync_then_watch, daemon=True, name="startup-sync").start()
    log("Startup sync running in background (dashboard stays online).")

    # Start auto-resume watcher for paused jobs
    def auto_resume_watcher():
        """Periodically check for jobs paused > 10 minutes and auto-resume them."""
        while True:
            # Reconcile faster when jobs are active so zombie "running" clears sooner.
            with _procs_lock:
                busy = len(_running_procs) > 0
            time.sleep(15 if busy else 30)
            try:
                api_post("/auto-resume-stale", {})
            except Exception:
                pass
            try:
                with _procs_lock:
                    active_ids = list(_running_procs.keys())
                # Drop dead process handles so reconcile can clear stuck dashboard rows
                dead = []
                with _procs_lock:
                    for jid, proc in list(_running_procs.items()):
                        if proc.poll() is not None:
                            dead.append(jid)
                    for jid in dead:
                        _running_procs.pop(jid, None)
                    active_ids = list(_running_procs.keys())
                api_post(
                    "/reconcile-running-jobs",
                    {
                        "worker_name": WORKER_NAME,
                        "job_ids": active_ids,
                        "grace_seconds": 45,
                        "pending_max_age_seconds": STALE_PENDING_SECONDS,
                    },
                    timeout=15,
                )
            except Exception:
                pass

    threading.Thread(target=auto_resume_watcher, daemon=True, name="auto-resume").start()
    if MAX_CONCURRENT_JOBS > 0:
        log(f"MAX_CONCURRENT_JOBS={MAX_CONCURRENT_JOBS} (pending jobs wait for a free slot).")
    else:
        log("MAX_CONCURRENT_JOBS=0 (unlimited — default).")
    log("Auto-resume watcher thread started.")

    while True:
        fetch_config()

        try:
            # Drain a burst of quick commands (editor read/write) each tick so they
            # are not delayed by the 1s sleep when the queue is busy.
            for _ in range(8):
                if not poll_commands():
                    break
            job = poll_job()
            if job:
                threading.Thread(target=handle_job, args=(job,), daemon=False, name=f"job-{job['id']}").start()
        except Exception as e:
            log(f"Unexpected error in main loop: {e}")

        time.sleep(POLL_INTERVAL)


def _interrupt_all_running_jobs() -> None:
    """Kill active script consoles so handle_job can report stopped + counts."""
    with _procs_lock:
        items = list(_running_procs.items())
    for job_id, proc in items:
        try:
            if proc and proc.poll() is None:
                kill_process_tree(proc.pid)
        except Exception as exc:
            log(f"Failed to interrupt job #{job_id}: {exc}")
    deadline = time.time() + 12
    while time.time() < deadline:
        with _procs_lock:
            if not _running_procs:
                break
        time.sleep(0.25)


if __name__ == "__main__":
    if not acquire_worker_instance_lock():
        log("Worker already running — not starting a second instance.")
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        try:
            _interrupt_all_running_jobs()
        except Exception:
            pass
        try:
            flush_snapshot_if_dirty(force=True)
        except Exception:
            pass
        log("Worker stopped.")
        sys.exit(0)
