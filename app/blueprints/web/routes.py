"""
Dashboard UI routes — display, job management, permissions, and scheduler.
Controller-side authorization is the single source of truth.
"""
import json
import base64
from collections import defaultdict
from urllib.parse import urlparse, urljoin

from flask import Blueprint, flash, redirect, render_template, request, url_for, session, g, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from app import database

web_bp = Blueprint("web", __name__)

def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")


# --- Permission history details (History → Details column) -----------------

def _perm_on(v) -> bool:
    return v is True or v == 1 or v == "1"


def _perm_flags(*pairs) -> str:
    labels = [label for label, on in pairs if on]
    return ", ".join(labels) if labels else "none"


def _perm_clip(parts: list[str], limit: int = 8) -> str:
    if not parts:
        return ""
    if len(parts) <= limit:
        return "; ".join(parts)
    return "; ".join(parts[:limit]) + f"; … +{len(parts) - limit} more"


def _perm_worker_flags_from_row(a: dict, *, paths: str | None = None, exts: str | None = None) -> str:
    flags = _perm_flags(
        ("All Files", _perm_on(a.get("can_access_all_files"))),
        ("Run Script", _perm_on(a.get("can_run"))),
        ("Create Folder", _perm_on(a.get("can_create_folder"))),
        ("Rename Folder", _perm_on(a.get("can_rename_folder"))),
        ("Delete Folder", _perm_on(a.get("can_delete_folder"))),
        ("Upload File", _perm_on(a.get("can_create_file"))),
        ("Rename File", _perm_on(a.get("can_rename_file"))),
        ("Edit File", _perm_on(a.get("can_edit_file"))),
        ("Update File", _perm_on(a.get("can_update_file"))),
        ("Delete File", _perm_on(a.get("can_delete_file"))),
    )
    extras = []
    p = (a.get("allowed_paths") if paths is None else paths) or ""
    e = (a.get("allowed_extensions") if exts is None else exts) or ""
    p = str(p).strip()
    e = str(e).strip()
    if p:
        extras.append(f"paths={p}")
    if e:
        extras.append(f"exts={e}")
    return f"{flags} ({', '.join(extras)})" if extras else flags


def _perm_summarize_own(
    items: list[tuple[bool, str, str]],
    *,
    kind: str,
    user_name: str,
    for_remove: bool = False,
) -> list[str]:
    """Own-worker items → one summary chip; other users' items stay listed."""
    own_counts: dict[str, int] = defaultdict(int)
    other: list[str] = []
    for is_own, flags, label in items:
        if is_own:
            own_counts[flags or "none"] += 1
        else:
            other.append(label if for_remove else f"{label} [{flags}]")
    out: list[str] = []
    for flags, n in sorted(own_counts.items(), key=lambda x: (-x[1], x[0])):
        if for_remove:
            out.append(f"All {kind} on {user_name}'s workers ({n})")
        else:
            out.append(f"All {kind} on {user_name}'s workers ({n}) [{flags}]")
    out.extend(other)
    return out


def _perm_diff_simple(prev: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Diff id→flags maps into display chips (added / removed / changed)."""
    added, removed, changed = [], [], []
    for key, val in new.items():
        if key not in prev:
            added.append(f"{key} [{val}]")
        elif prev[key] != val:
            changed.append(f"{key} [{prev[key]} → {val}]")
    for key in prev:
        if key not in new:
            removed.append(key)
    return added, removed, changed


def _perm_diff_labeled(
    prev: dict[str, str],
    new: dict[str, str],
    meta_prev: dict[str, tuple[bool, str]],
    meta_new: dict[str, tuple[bool, str]],
    *,
    kind: str,
    user_name: str,
) -> tuple[list[str], list[str], list[str]]:
    """Diff with display labels + own-worker summarization."""
    add_items, rem_items, changed = [], [], []
    for key, val in new.items():
        is_own, label = meta_new.get(key, (False, key))
        if key not in prev:
            add_items.append((is_own, val, label))
        elif prev[key] != val:
            changed.append(f"{label} [{prev[key]} → {val}]")
    for key, val in prev.items():
        if key not in new:
            is_own, label = meta_prev.get(key, (False, key))
            rem_items.append((is_own, val, label))
    added = _perm_summarize_own(add_items, kind=kind, user_name=user_name, for_remove=False)
    removed = _perm_summarize_own(rem_items, kind=kind, user_name=user_name, for_remove=True)
    return added, removed, changed


def _perm_history_details(
    *,
    target_user_id: int,
    target_name: str,
    workers_added: list[str],
    workers_removed: list[str],
    workers_changed: list[str],
    scripts_added: list[str],
    scripts_removed: list[str],
    scripts_changed: list[str],
    schedules_added: list[str],
    schedules_removed: list[str],
    schedules_changed: list[str],
    folders_added: list[str],
    folders_removed: list[str],
    folders_changed: list[str],
    views_added: list[str],
    views_removed: list[str],
    days_change: str | None,
) -> str:
    """Compact delta-only permission log for History Details.

    Rules:
    - Only added / removed / changed lines (never the full grant snapshot)
    - Skip empty sections
    - Own-worker scripts/schedules already summarized by callers
    """
    lines = [f"Target: {target_name} (#{target_user_id})"]
    sections = [
        ("Workers added", workers_added),
        ("Workers removed", workers_removed),
        ("Workers changed", workers_changed),
        ("Scripts added", scripts_added),
        ("Scripts removed", scripts_removed),
        ("Scripts changed", scripts_changed),
        ("Schedules added", schedules_added),
        ("Schedules removed", schedules_removed),
        ("Schedules changed", schedules_changed),
        ("Folders added", folders_added),
        ("Folders removed", folders_removed),
        ("Folders changed", folders_changed),
        ("Scheduler view added", views_added),
        ("Scheduler view removed", views_removed),
    ]
    for title, parts in sections:
        clipped = _perm_clip(parts)
        if not clipped:
            continue
        lines.append(f"{title} ({len(parts)}): {clipped}")
    if days_change:
        lines.append(f"Can set days: {days_change}")
    if len(lines) == 1:
        lines.append("No permission changes")
    return "\n".join(lines)


def _perm_join_workers(names: list[str], *, limit: int = 4) -> str:
    """Compact worker label for history_log.worker_name."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return ", ".join(cleaned)
    return ", ".join(cleaned[:limit]) + f" +{len(cleaned) - limit}"


def _perm_workers_from_delta_maps(
    prev: dict[str, str],
    new: dict[str, str],
) -> list[str]:
    out: list[str] = []
    for key in new:
        if key not in prev or prev[key] != new[key]:
            out.append(key)
    for key in prev:
        if key not in new:
            out.append(key)
    return out


def _perm_workers_from_labeled_meta(
    prev: dict[str, str],
    new: dict[str, str],
    meta_prev: dict[str, tuple[bool, str]],
    meta_new: dict[str, tuple[bool, str]],
) -> list[str]:
    """Pull worker names from script/schedule labels like 'Worker/script'."""
    out: list[str] = []
    keys = set(prev) | set(new)
    for key in keys:
        if prev.get(key) == new.get(key) and key in prev and key in new:
            continue
        _is_own, label = meta_new.get(key) or meta_prev.get(key) or (False, "")
        label = (label or "").strip()
        if "/" in label and not label.startswith("All "):
            out.append(label.split("/", 1)[0].strip())
    return out


def _perm_workers_from_details(details: str) -> str:
    """Best-effort worker names from permission history details text."""
    import re

    text = details or ""
    names: list[str] = []

    # Workers added/removed/changed: "Name [flags]" or bare "Name"
    for m in re.finditer(
        r"^Workers (?:added|removed|changed)\s*(?:\(\d+\))?\s*:\s*(.+)$",
        text,
        re.I | re.M,
    ):
        for part in (m.group(1) or "").split(";"):
            chip = part.strip()
            if not chip or chip.startswith("…") or chip.startswith("..."):
                continue
            names.append(chip.split(" [", 1)[0].strip())

    # Scripts: "Worker/script [flags]" (skip "All scripts on …" summaries)
    for m in re.finditer(
        r"^Scripts (?:added|removed|changed)\s*(?:\(\d+\))?\s*:\s*(.+)$",
        text,
        re.I | re.M,
    ):
        for part in (m.group(1) or "").split(";"):
            chip = part.strip()
            if not chip or chip.lower().startswith("all scripts"):
                continue
            base = chip.split(" [", 1)[0].strip()
            if "/" in base:
                names.append(base.split("/", 1)[0].strip())

    # Legacy / revoke lines
    for m in re.finditer(r"PC access to\s+([^\s,;]+)", text, re.I):
        names.append(m.group(1).strip().strip("'\""))
    for m in re.finditer(r"worker\s+([^\s,;/]+)", text, re.I):
        names.append(m.group(1).strip().strip("'\""))

    return _perm_join_workers(names)


def _relative_request_target() -> str:
    """Current path + query as a relative URL (never absolute). Used for ?next=."""
    full = request.full_path or request.path or "/"
    if full.endswith("?"):
        full = full[:-1]
    return full or "/"


def _safe_next_url(raw: str | None, default: str | None = None) -> str:
    """
    Return a safe same-origin relative path for post-login redirects.
    Accepts relative paths (/reports) or absolute same-host URLs
    (http://host/reports) and normalizes them to a path like /reports.
    Rejects open redirects, auth-page loops, and unsafe schemes.
    """
    fallback = default if default is not None else url_for("web.dashboard")
    if not raw:
        return fallback

    candidate = str(raw).strip()
    if not candidate:
        return fallback

    lower = candidate.lower()
    if lower.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return fallback
    # Protocol-relative URLs are an open-redirect risk
    if candidate.startswith("//"):
        return fallback

    # Resolve against this request so absolute same-host URLs become comparable
    try:
        joined = urljoin(request.host_url, candidate)
        parsed = urlparse(joined)
        host_parsed = urlparse(request.host_url)
    except Exception:
        return fallback

    if parsed.scheme not in ("http", "https"):
        return fallback
    if parsed.netloc.lower() != host_parsed.netloc.lower():
        return fallback

    path = parsed.path or "/"
    if not path.startswith("/"):
        return fallback
    # Disallow backslash tricks
    if "\\" in path or "\\" in candidate:
        return fallback

    if parsed.query:
        path = f"{path}?{parsed.query}"

    path_only = (parsed.path or "/").rstrip("/") or "/"
    # Avoid bounce loops onto auth endpoints
    if path_only in ("/login", "/register", "/logout"):
        return fallback

    return path


def _next_for_form(raw: str | None = None) -> str:
    """Relative next value for hidden form fields / query strings (empty if none/invalid)."""
    if raw is None:
        raw = request.args.get("next") or request.form.get("next")
    if not raw or not str(raw).strip():
        return ""
    safe = _safe_next_url(str(raw).strip(), default="")
    # Empty default means invalid → ""
    return safe if safe else ""


# ============================================================
# Auth decorators
# ============================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None or g.get("current_user") is None:
            session.pop("user_id", None)
            return redirect(url_for("web.login", next=_relative_request_target()))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None or g.get("current_user") is None:
            session.pop("user_id", None)
            return redirect(url_for("web.login", next=_relative_request_target()))
        if not database.is_admin(session["user_id"]):
            database.log_action(session["user_id"], "unauthorized_access", f"Attempted to access admin route: {request.path}", _client_ip())
            flash("Admin access required.", "error")
            return redirect(url_for("web.dashboard"))
        return f(*args, **kwargs)
    return decorated_function


@web_bp.before_request
def load_current_user():
    """Inject current_user into g and templates. Clear session if account was disabled."""
    g.current_user = None
    uid = session.get("user_id")
    if uid:
        user = database.get_user_by_id(uid)
        if user and int(user.get("is_disabled") or 0) == 1:
            session.pop("user_id", None)
            g.current_user = None
            return
        g.current_user = user


@web_bp.context_processor
def inject_user():
    return {"current_user": g.get("current_user")}


# ============================================================
# Auth routes
# ============================================================

@web_bp.route("/register", methods=["GET", "POST"])
def register():
    # Already signed in → home (do not show register form)
    if session.get("user_id") and g.get("current_user"):
        return redirect(_safe_next_url(request.args.get("next") or request.form.get("next")))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = _next_for_form(request.form.get("next") or request.args.get("next"))
        if not username or not password:
            flash("Username and password required.", "error")
            return redirect(url_for("web.register", next=next_url) if next_url else url_for("web.register"))

        client_ip = _client_ip()
        hashed = generate_password_hash(password)
        user = database.create_user(username, hashed, client_ip)
        if user:
            session["user_id"] = user["id"]
            database.update_user_login(user["id"], client_ip)
            database.log_action(user["id"], "user_registered", f"Registered new account: {username}", client_ip)
            flash("Registration successful.", "success")
            return redirect(_safe_next_url(next_url or None))
        else:
            flash("Username already exists.", "error")
            return redirect(url_for("web.register", next=next_url) if next_url else url_for("web.register"))

    return render_template("register.html", next_url=_next_for_form())


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    # Already signed in → requested page or dashboard
    if session.get("user_id") and g.get("current_user"):
        return redirect(_safe_next_url(request.args.get("next") or request.form.get("next")))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        client_ip = _client_ip()
        next_url = _next_for_form(request.form.get("next") or request.args.get("next"))

        user = database.get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            if int(user.get("is_disabled") or 0) == 1:
                database.log_action(user["id"], "failed_login", f"Login blocked — account disabled: {username}", client_ip)
                flash("This account has been disabled. Contact an administrator.", "error")
                return redirect(url_for("web.login", next=next_url) if next_url else url_for("web.login"))

            # Admin can bypass IP check
            if user["role"] != "admin" and user.get("registered_ip") and user["registered_ip"] != client_ip:
                database.log_action(user["id"], "unauthorized_access", f"Login attempt from unregistered IP: {client_ip} (registered: {user['registered_ip']})", client_ip)
                flash("Access Denied: You must login from your registered IP address.", "error")
                return redirect(url_for("web.login", next=next_url) if next_url else url_for("web.login"))

            session["user_id"] = user["id"]
            database.update_user_login(user["id"], client_ip)
            # Retroactively link any worker whose IP matches this user's registered IP
            if user.get("registered_ip"):
                try:
                    database.associate_user_with_workers_by_ip(user["id"], user["registered_ip"])
                except Exception as e:
                    # Login must succeed even if PC auto-link fails
                    print(f"login associate_user_with_workers_by_ip failed: {e}")
            database.log_action(user["id"], "user_login", "User logged in", client_ip)
            flash("Logged in successfully.", "success")
            return redirect(_safe_next_url(next_url or None))
        else:
            database.log_action(None, "failed_login", f"Failed login attempt for username: {username}", client_ip)
            flash("Invalid username or password.", "error")
            return redirect(url_for("web.login", next=next_url) if next_url else url_for("web.login"))

    return render_template("login.html", next_url=_next_for_form())

@web_bp.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid:
        database.log_action(uid, "user_logout", "User logged out", _client_ip())
    session.pop("user_id", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("web.login"))


# ============================================================
# Dashboard
# ============================================================

@web_bp.route("/")
@login_required
def dashboard():
    """Main dashboard — permission-filtered for non-admin users.

    Scripts & access lists only File Explorer–starred scripts for this user
    (keeps homepage light; full catalog remains on worker detail / explorer).
    """
    uid = session["user_id"]
    user = g.current_user
    status_filter = (request.args.get("status") or "").strip()

    workers = database.list_accessible_workers(uid)
    job_counts = database.get_job_counts(uid, [w["worker_name"] for w in workers])

    # Starred-only scripts (not the full worker catalog)
    scripts_by_worker = defaultdict(list)
    starred_scripts = database.list_starred_scripts_for_dashboard(uid)
    for script in starred_scripts:
        scripts_by_worker[script["worker_name"]].append(script)

    if user and user["role"] == "admin":
        jobs = database.list_jobs(limit=50, status=status_filter or None)
    else:
        accessible_worker_names = {w["worker_name"] for w in workers}
        all_jobs = database.list_jobs(limit=100, status=status_filter or None)
        jobs = [j for j in all_jobs if j["worker_name"] in accessible_worker_names][:50]

    online_count = sum(1 for w in workers if w["status"] == "online")

    return render_template(
        "dashboard.html",
        workers=workers,
        jobs=jobs,
        scripts_by_worker=scripts_by_worker,
        starred_script_count=len(starred_scripts),
        status_filter=status_filter,
        job_counts=job_counts,
        online_count=online_count,
    )

@web_bp.route("/workers")
@login_required
def workers_page():
    """Worker list page — permission-filtered for non-admin users."""
    uid = session["user_id"]
    status_filter = (request.args.get("status") or "").strip()
    search_query = (request.args.get("search") or "").strip().lower()

    all_workers = database.list_accessible_workers(uid)
    
    filtered_workers = []
    for w in all_workers:
        if status_filter and w["status"] != status_filter:
            continue
        if search_query:
            name = (w.get("worker_name") or "").lower()
            ip = (w.get("ip_address") or "").lower()
            if search_query not in name and search_query not in ip:
                continue
        filtered_workers.append(w)

    # Online first, then offline; stable name order within each group
    filtered_workers.sort(
        key=lambda w: (
            0 if (w.get("status") or "") == "online" else 1,
            (w.get("worker_name") or "").lower(),
        )
    )
            
    online_count = sum(1 for w in all_workers if w["status"] == "online")

    return render_template(
        "workers.html",
        workers=filtered_workers,
        total_workers=len(all_workers),
        online_count=online_count,
        status_filter=status_filter,
        search_query=request.args.get("search", "")
    )


# ============================================================
# Worker detail
# ============================================================

@web_bp.route("/worker/<ip_address>")
@login_required
def worker_detail(ip_address):
    """Detailed view for a specific worker — checks PC access."""
    uid = session["user_id"]
    worker = database.get_worker_by_ip(ip_address)
    if not worker:
        flash("Worker not found.", "error")
        return redirect(url_for("web.dashboard"))

    if not database.check_pc_access(uid, worker["worker_name"]):
        database.log_action(uid, "unauthorized_access", f"Attempted to access worker {worker['worker_name']}", _client_ip(), worker_name=worker['worker_name'])
        flash("You don't have access to this worker.", "error")
        return redirect(url_for("web.dashboard"))

    user = g.current_user
    if user and user["role"] == "admin":
        scripts = database.list_scripts(worker["worker_name"])
    else:
        scripts = database.list_accessible_scripts(uid, worker["worker_name"])

    jobs = database.list_jobs_for_worker(worker["worker_name"], limit=100)

    return render_template(
        "worker_detail.html",
        worker=worker,
        scripts=scripts,
        jobs=jobs,
    )

@web_bp.route("/rename-worker", methods=["POST"])
@login_required
def rename_worker():
    """Rename a worker across all tables and queue a rename command."""
    uid = session["user_id"]
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()
    
    if not old_name or not new_name:
        flash("Both current and new worker names are required.", "error")
        return redirect(url_for("web.dashboard"))

    # Admin-only (matches Worker Details UI)
    if not database.is_admin(uid):
        flash("Only admins can rename workers.", "error")
        return redirect(url_for("web.dashboard"))
        
    if not database.check_pc_access(uid, old_name):
        flash("You do not have permission to rename this worker.", "error")
        return redirect(url_for("web.dashboard"))
        
    # Queue the command so the worker updates its local state
    payload = json.dumps({"new_name": new_name})
    database.create_command(old_name, "rename", payload)
    
    # Update all db tables
    database.rename_worker(old_name, new_name)
    
    database.log_action(uid, "worker_renamed", f"Renamed worker from {old_name} to {new_name}", _client_ip())
    flash(f"Worker '{old_name}' renamed to '{new_name}'.", "success")
    
    worker = database.get_worker(new_name)
    if worker:
        return redirect(url_for("web.worker_detail", ip_address=worker["ip_address"]))
    return redirect(url_for("web.dashboard"))



# ============================================================
# Script / Job actions
# ============================================================

@web_bp.route("/run-script", methods=["POST"])
@login_required
def run_script():
    """Queue a job — checks script run permission.

    Accepts either script_id, or worker_name + file_path (File Explorer), which
    finds or registers the script so duplicate basenames still run.
    """
    uid = session["user_id"]
    script_id = request.form.get("script_id", type=int)
    worker_name = (request.form.get("worker_name") or "").strip()
    file_path = (request.form.get("file_path") or "").strip().replace("\\", "/").lstrip("/")

    if not script_id and worker_name and file_path:
        # PC / admin run gate before creating a scripts row
        if not database.is_admin(uid):
            perms = database.get_pc_access_details(uid, worker_name) or {}
            fe = database.build_frontend_pc_perms(uid, worker_name, perms)
            if not fe.get("can_run"):
                flash("You don't have permission to run scripts on this worker.", "error")
                next_url = request.form.get("next") or url_for("web.dashboard")
                return redirect(next_url)
            root = database.get_worker_script_location(worker_name)
            if not database.path_allowed_by_perms(file_path, perms, root, is_folder=False):
                flash("Path not allowed.", "error")
                next_url = request.form.get("next") or url_for("web.dashboard")
                return redirect(next_url)
        script = database.ensure_script_for_worker_file_path(worker_name, file_path)
        if not script:
            flash("Could not resolve script for that file.", "error")
            next_url = request.form.get("next") or url_for("web.dashboard")
            return redirect(next_url)
        script_id = script["id"]

    if not script_id:
        flash("Invalid script.", "error")
        return redirect(url_for("web.dashboard"))

    script = database.get_script(script_id)
    if not script:
        flash("Script not found.", "error")
        return redirect(url_for("web.dashboard"))

    if not database.check_script_access(uid, script_id, "run"):
        database.log_action(uid, "unauthorized_access", f"Attempted to run script {script['script_name']}", _client_ip(), worker_name=script["worker_name"])
        flash("You don't have permission to run this script.", "error")
        return redirect(url_for("web.dashboard"))

    worker = database.get_worker(script["worker_name"])
    if not worker or worker["status"] != "online":
        flash(
            f"Worker '{script['worker_name']}' is offline. Job queued anyway.",
            "warning",
        )

    # Reports re-run: keep schedule_id so the new job appears in Scheduler Job History
    # (get_scheduler_jobs only lists jobs with schedule_id). Dashboard / File Explorer
    # omit these fields and behave as before.
    schedule_id = request.form.get("schedule_id", type=int)
    source_job_id = request.form.get("source_job_id", type=int)
    if not schedule_id and source_job_id:
        src = database.get_job(source_job_id)
        if src and src.get("script_id") == script_id and src.get("schedule_id"):
            schedule_id = src.get("schedule_id")
    if schedule_id:
        sch = database.get_schedule(schedule_id)
        if (
            not sch
            or sch.get("is_deleted")
            or int(sch.get("script_id") or 0) != int(script_id)
        ):
            schedule_id = None

    job = database.create_job(script["worker_name"], script_id, schedule_id=schedule_id)
    database.log_action(uid, "job_run", f"Queued job #{job['id']} for {script['worker_name']} / {script['script_name']} ({script.get('script_path', '')})", _client_ip(), worker_name=script["worker_name"])
    flash(f"Job #{job['id']} queued for {script['worker_name']} / {script['script_name']}.", "success")
    next_url = request.form.get("next") or url_for("web.dashboard")
    return redirect(next_url)



@web_bp.route("/retry-job/<int:job_id>", methods=["POST"])
@login_required
def retry_job(job_id):
    uid = session["user_id"]
    existing = database.get_job(job_id)
    if existing and not database.check_script_access(uid, existing["script_id"], "run"):
        database.log_action(uid, "unauthorized_access", f"Attempted to retry job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("You don't have permission to retry this job.", "error")
        next_url = request.form.get("next") or url_for("web.dashboard")
        return redirect(next_url)

    job = database.retry_job(job_id)
    if not job:
        flash("Cannot retry this job.", "error")
    else:
        database.log_action(uid, "job_run", f"Retried job #{job_id} as new job #{job['id']}", _client_ip(), worker_name=job["worker_name"])
        flash(f"Retry queued as job #{job['id']}.", "success")
    next_url = request.form.get("next") or url_for("web.dashboard")
    return redirect(next_url)


@web_bp.route("/stop-job/<int:job_id>", methods=["POST"])
@login_required
def stop_job(job_id):
    uid = session["user_id"]
    existing = database.get_job(job_id)
    if existing and not database.check_script_access(uid, existing["script_id"], "run"):
        database.log_action(uid, "unauthorized_access", f"Attempted to stop job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("You don't have permission to stop this job.", "error")
        next_url = request.form.get("next") or url_for("web.dashboard")
        return redirect(next_url)

    job = database.stop_job(job_id)
    if not job:
        flash("Cannot stop this job.", "error")
    else:
        database.log_action(uid, "job_stopped", f"Stopped job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("Job stop signal sent.", "success")
    next_url = request.form.get("next") or url_for("web.dashboard")
    return redirect(next_url)


@web_bp.route("/pause-job/<int:job_id>", methods=["POST"])
@login_required
def pause_job(job_id):
    uid = session["user_id"]
    existing = database.get_job(job_id)
    if existing and not database.check_script_access(uid, existing["script_id"], "run"):
        database.log_action(uid, "unauthorized_access", f"Attempted to pause job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("You don't have permission to pause this job.", "error")
        next_url = request.form.get("next") or url_for("web.dashboard")
        return redirect(next_url)

    job = database.pause_job(job_id)
    if not job:
        flash("Cannot pause this job.", "error")
    else:
        database.log_action(uid, "job_paused", f"Paused job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("Job pause signal sent.", "success")
    next_url = request.form.get("next") or url_for("web.dashboard")
    return redirect(next_url)


@web_bp.route("/resume-job/<int:job_id>", methods=["POST"])
@login_required
def resume_job(job_id):
    uid = session["user_id"]
    existing = database.get_job(job_id)
    if existing and not database.check_script_access(uid, existing["script_id"], "run"):
        database.log_action(uid, "unauthorized_access", f"Attempted to resume job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("You don't have permission to resume this job.", "error")
        next_url = request.form.get("next") or url_for("web.dashboard")
        return redirect(next_url)

    job = database.resume_job(job_id)
    if not job:
        flash("Cannot resume this job.", "error")
    else:
        database.log_action(uid, "job_resumed", f"Resumed job #{job_id}", _client_ip(), worker_name=job["worker_name"])
        flash("Job resumed.", "success")
    next_url = request.form.get("next") or url_for("web.dashboard")
    return redirect(next_url)


# ============================================================
# Admin Management (Permissions, Users, History)
# ============================================================

@web_bp.route("/permissions", methods=["GET", "POST"])
@admin_required
def permissions():
    users = database.get_all_users()
    workers = database.list_workers()
    schedules = database.list_schedules_for_permissions()

    pc_access = database.get_all_pc_access()
    script_access = database.get_all_script_access()
    schedule_access = database.get_all_schedule_access()
    schedule_access_assign = database.get_schedule_access_for_assign()
    scheduler_view_access = []
    try:
        scheduler_view_access = database.get_all_scheduler_view_access()
    except Exception:
        pass
    folder_access = []
    folders_catalog = []
    try:
        from app.services import schedule_folders as sf
        folder_access = sf.get_all_folder_access()
        folders_catalog = [
            [f["id"], f.get("username") or "", f.get("name") or ""]
            for f in sf.list_folders_for_permissions()
        ]
    except Exception:
        pass
    
    if request.method == "POST":
        action = request.form.get("action")
        target_user_id = request.form.get("user_id", type=int)
        
        if action == "assign_permissions":
            worker_names = request.form.getlist("workers")
            script_ids = request.form.getlist("scripts")
            schedule_ids = request.form.getlist("schedules")

            target_user = database.get_user_by_id(target_user_id) if target_user_id else None
            target_name = (target_user or {}).get("username") or f"user #{target_user_id}"
            owned_workers = set(database.list_owned_worker_names(int(target_user_id)) or [])
            tid = int(target_user_id)

            # Snapshot previous grants (before revoke) so history logs only deltas.
            # Use list_pc_access_worker_names + details — get_all_pc_access() excludes
            # owner self-grants, which falsely marks own workers as "added" every save.
            prev_workers: dict[str, str] = {}
            for wn in database.list_pc_access_worker_names(tid):
                a = database.get_pc_access_details(tid, wn)
                if not a:
                    continue
                prev_workers[wn] = _perm_worker_flags_from_row(a)

            prev_scripts: dict[str, str] = {}
            prev_script_meta: dict[str, tuple[bool, str]] = {}
            for a in script_access or []:
                if int(a.get("user_id") or 0) != tid:
                    continue
                sid = str(int(a.get("script_id") or 0))
                if sid == "0":
                    continue
                flags = _perm_flags(
                    ("Run", _perm_on(a.get("can_run"))),
                    ("Update", _perm_on(a.get("can_update"))),
                    ("Delete", _perm_on(a.get("can_delete"))),
                )
                if flags == "none":
                    flags = "View"
                wn = a.get("worker_name") or ""
                label = f"{wn or '?'}/{a.get('script_name') or sid}"
                prev_scripts[sid] = flags
                prev_script_meta[sid] = (bool(wn) and wn in owned_workers, label)

            prev_schedules: dict[str, str] = {}
            prev_schedule_meta: dict[str, tuple[bool, str]] = {}
            prev_schedule_workers: dict[str, str] = {}
            for a in schedule_access_assign or []:
                if int(a.get("user_id") or 0) != tid:
                    continue
                sch_id = str(int(a.get("schedule_id") or 0))
                if sch_id == "0":
                    continue
                flags = _perm_flags(
                    ("Enable/Disable", _perm_on(a.get("can_enable")) or _perm_on(a.get("can_disable"))),
                    ("Run", _perm_on(a.get("can_run"))),
                    ("Edit", _perm_on(a.get("can_edit"))),
                    ("Duplicate", _perm_on(a.get("can_duplicate"))),
                    ("Delete", _perm_on(a.get("can_delete"))),
                )
                wn = a.get("worker_name") or ""
                label = f"#{sch_id} {a.get('script_name') or '?'} @ {a.get('run_time') or '?'}"
                prev_schedules[sch_id] = flags
                prev_schedule_meta[sch_id] = (bool(wn) and wn in owned_workers, label)
                if wn:
                    prev_schedule_workers[sch_id] = wn

            prev_folders: dict[str, str] = {}
            prev_folder_names: dict[str, str] = {}
            for a in folder_access or []:
                if int(a.get("user_id") or 0) != tid:
                    continue
                fid = str(int(a.get("folder_id") or 0))
                if fid == "0":
                    continue
                prev_folders[fid] = _perm_flags(
                    ("Edit", _perm_on(a.get("can_edit"))),
                    ("Enable/Disable", _perm_on(a.get("can_enable")) or _perm_on(a.get("can_disable"))),
                    ("Run", _perm_on(a.get("can_run"))),
                    ("Delete", _perm_on(a.get("can_delete"))),
                )
                prev_folder_names[fid] = a.get("folder_name") or f"folder #{fid}"

            prev_views: dict[str, str] = {}
            for a in scheduler_view_access or []:
                if int(a.get("viewer_user_id") or 0) != tid:
                    continue
                oid = str(int(a.get("target_user_id") or 0))
                if oid == "0":
                    continue
                prev_views[oid] = a.get("target_username") or f"#{oid}"

            prev_can_set_days = bool(int((target_user or {}).get("can_set_days") or 0) == 1)

            # Revoke only workers that were removed (keeps assignment periods continuous for kept workers)
            existing_workers = database.list_pc_access_worker_names(target_user_id)
            new_workers = set(worker_names)
            for wn in existing_workers - new_workers:
                database.revoke_pc_access(target_user_id, wn)

            database.revoke_all_script_access(target_user_id)
            database.revoke_all_schedule_access(target_user_id)
            try:
                from app.services import schedule_folders as sf
                sf.revoke_all_folder_access(target_user_id)
            except Exception:
                pass
            database.revoke_all_scheduler_view_access(target_user_id)

            new_workers_map: dict[str, str] = {}
            for wn in worker_names:
                paths = request.form.get(f"worker_{wn}_paths", "")
                exts = request.form.get(f"worker_{wn}_exts", "")
                c_folder = 1 if request.form.get(f"worker_{wn}_create_folder") else 0
                r_folder = 1 if request.form.get(f"worker_{wn}_rename_folder") else 0
                u_file = 1 if request.form.get(f"worker_{wn}_update_file") else 0
                c_file = 1 if request.form.get(f"worker_{wn}_create_file") else 0
                d_file = 1 if request.form.get(f"worker_{wn}_delete_file") else 0
                d_folder = 1 if request.form.get(f"worker_{wn}_delete_folder") else 0
                r_file = 1 if request.form.get(f"worker_{wn}_rename_file") else 0
                e_file = 1 if request.form.get(f"worker_{wn}_edit_file") else 0
                access_all = 1 if request.form.get(f"worker_{wn}_access_all_files") else 0
                can_run = 1 if request.form.get(f"worker_{wn}_run") else 0
                # Auto-grant create_folder when delete_folder is granted (folder recreate dependency)
                if d_folder and not c_folder:
                    c_folder = 1
                database.grant_pc_access(target_user_id, wn, session["user_id"], paths, exts, c_folder, r_folder, u_file, c_file, d_file, d_folder, r_file, e_file, access_all, can_run)
                new_workers_map[wn] = _perm_worker_flags_from_row(
                    {
                        "can_access_all_files": access_all,
                        "can_run": can_run,
                        "can_create_folder": c_folder,
                        "can_rename_folder": r_folder,
                        "can_delete_folder": d_folder,
                        "can_create_file": c_file,
                        "can_rename_file": r_file,
                        "can_edit_file": e_file,
                        "can_update_file": u_file,
                        "can_delete_file": d_file,
                    },
                    paths=paths,
                    exts=exts,
                )

            new_scripts: dict[str, str] = {}
            new_script_meta: dict[str, tuple[bool, str]] = {}
            for sid in script_ids:
                can_run = bool(request.form.get(f"script_{sid}_run"))
                can_update = bool(request.form.get(f"script_{sid}_update"))
                can_delete = bool(request.form.get(f"script_{sid}_delete"))
                if can_update and not can_run:
                    can_run = True
                database.grant_script_access(target_user_id, int(sid), session["user_id"], can_run=can_run, can_update=can_update, can_delete=can_delete)

                script_info = database.get_script(int(sid))
                flags = _perm_flags(
                    ("Run", can_run),
                    ("Update", can_update),
                    ("Delete", can_delete),
                )
                if flags == "none":
                    flags = "View"
                worker_name = (script_info or {}).get("worker_name") or ""
                if script_info:
                    label = f"{worker_name or '?'}/{script_info.get('script_name') or sid}"
                else:
                    label = f"script #{sid}"
                sid_key = str(int(sid))
                new_scripts[sid_key] = flags
                new_script_meta[sid_key] = (bool(worker_name) and worker_name in owned_workers, label)

            new_schedules: dict[str, str] = {}
            new_schedule_meta: dict[str, tuple[bool, str]] = {}
            new_schedule_workers: dict[str, str] = {}
            for sch_id in schedule_ids:
                can_delete = 1 if request.form.get(f"schedule_{sch_id}_delete") else 0
                can_enable = 1 if request.form.get(f"schedule_{sch_id}_enable") else 0
                can_disable = can_enable
                can_run = 1 if request.form.get(f"schedule_{sch_id}_run") else 0
                can_duplicate = 1 if request.form.get(f"schedule_{sch_id}_duplicate") else 0
                can_edit = 1 if request.form.get(f"schedule_{sch_id}_edit") else 0
                database.grant_schedule_access(int(sch_id), target_user_id, session["user_id"], can_delete, can_enable, can_disable, can_run, can_duplicate, can_edit)
                flags = _perm_flags(
                    ("Enable/Disable", can_enable),
                    ("Run", can_run),
                    ("Edit", can_edit),
                    ("Duplicate", can_duplicate),
                    ("Delete", can_delete),
                )
                sch = database.get_schedule(int(sch_id))
                worker_name = (sch or {}).get("worker_name") or ""
                label = (
                    f"#{sch_id} {(sch or {}).get('script_name') or '?'} @ {(sch or {}).get('run_time') or '?'}"
                    if sch else f"#{sch_id}"
                )
                sch_key = str(int(sch_id))
                new_schedules[sch_key] = flags
                new_schedule_meta[sch_key] = (bool(worker_name) and worker_name in owned_workers, label)
                if worker_name:
                    new_schedule_workers[sch_key] = worker_name

            new_folders: dict[str, str] = {}
            new_folder_names: dict[str, str] = {}
            try:
                from app.services import schedule_folders as sf
                folder_ids = request.form.getlist("folders")
                for fid in folder_ids:
                    fid_i = int(fid)
                    can_edit = 1 if request.form.get(f"folder_{fid}_edit") else 0
                    can_enable = 1 if request.form.get(f"folder_{fid}_enable") else 0
                    can_delete_f = 1 if request.form.get(f"folder_{fid}_delete") else 0
                    can_run_f = 1 if request.form.get(f"folder_{fid}_run") else 0
                    sf.grant_folder_access(
                        fid_i,
                        target_user_id,
                        session["user_id"],
                        can_delete=can_delete_f,
                        can_enable=can_enable,
                        can_disable=can_enable,
                        can_run=can_run_f,
                        can_edit=can_edit,
                        can_manage=can_edit,
                    )
                    flags = _perm_flags(
                        ("Edit", can_edit),
                        ("Enable/Disable", can_enable),
                        ("Run", can_run_f),
                        ("Delete", can_delete_f),
                    )
                    folder = sf.get_folder(fid_i)
                    fname = (folder or {}).get("name") or f"folder #{fid_i}"
                    fid_key = str(fid_i)
                    new_folders[fid_key] = flags
                    new_folder_names[fid_key] = fname
            except Exception as exc:
                print(f"folder permission grant failed: {exc}", flush=True)

            new_views: dict[str, str] = {}
            for other_id in request.form.getlist("scheduler_view_users"):
                try:
                    oid = int(other_id)
                except (TypeError, ValueError):
                    continue
                if oid == tid:
                    continue
                if database.is_admin(oid):
                    continue
                database.grant_scheduler_view_access(target_user_id, oid, session["user_id"])
                other = database.get_user_by_id(oid)
                new_views[str(oid)] = (other or {}).get("username") or f"#{oid}"

            can_set_days_val = None
            if not database.is_admin(target_user_id):
                can_set_days_val = bool(request.form.get("can_set_days"))
                database.set_user_can_set_days(target_user_id, can_set_days_val)

            # Delta-only history Details (added / removed / changed).
            w_add, w_rem, w_chg = _perm_diff_simple(prev_workers, new_workers_map)
            s_add, s_rem, s_chg = _perm_diff_labeled(
                prev_scripts, new_scripts, prev_script_meta, new_script_meta,
                kind="scripts", user_name=target_name,
            )
            sch_add, sch_rem, sch_chg = _perm_diff_labeled(
                prev_schedules, new_schedules, prev_schedule_meta, new_schedule_meta,
                kind="schedules", user_name=target_name,
            )
            f_add = [
                f"{new_folder_names.get(k, k)} [{new_folders[k]}]"
                for k in new_folders if k not in prev_folders
            ]
            f_rem = [prev_folder_names.get(k, k) for k in prev_folders if k not in new_folders]
            f_chg = [
                f"{new_folder_names.get(k) or prev_folder_names.get(k) or k} [{prev_folders[k]} → {new_folders[k]}]"
                for k in new_folders if k in prev_folders and prev_folders[k] != new_folders[k]
            ]
            v_add = [new_views[k] for k in new_views if k not in prev_views]
            v_rem = [prev_views[k] for k in prev_views if k not in new_views]
            days_change = None
            if can_set_days_val is not None and bool(can_set_days_val) != prev_can_set_days:
                days_change = (
                    f"{'yes' if prev_can_set_days else 'no'} → {'yes' if can_set_days_val else 'no'}"
                )

            details = _perm_history_details(
                target_user_id=tid,
                target_name=target_name,
                workers_added=w_add,
                workers_removed=w_rem,
                workers_changed=w_chg,
                scripts_added=s_add,
                scripts_removed=s_rem,
                scripts_changed=s_chg,
                schedules_added=sch_add,
                schedules_removed=sch_rem,
                schedules_changed=sch_chg,
                folders_added=f_add,
                folders_removed=f_rem,
                folders_changed=f_chg,
                views_added=v_add,
                views_removed=v_rem,
                days_change=days_change,
            )
            workers_for_log = _perm_join_workers(
                _perm_workers_from_delta_maps(prev_workers, new_workers_map)
                + _perm_workers_from_labeled_meta(
                    prev_scripts, new_scripts, prev_script_meta, new_script_meta
                )
                + [
                    (new_schedule_workers.get(k) or prev_schedule_workers.get(k) or "")
                    for k in (set(prev_schedules) | set(new_schedules))
                    if prev_schedules.get(k) != new_schedules.get(k)
                ]
            )
            if not workers_for_log:
                workers_for_log = _perm_workers_from_details(details) or None
            database.log_action(
                session["user_id"],
                "permissions_updated",
                details,
                _client_ip(),
                worker_name=workers_for_log,
            )
            flash("Permissions updated.", "success")
            return redirect(url_for("web.permissions"))

    admin_ids = {
        int(u["id"]) for u in (users or [])
        if (u.get("role") or "") == "admin"
    }

    def _non_admin_grants(rows, *id_keys):
        out = []
        for row in rows or []:
            skip = False
            for key in id_keys:
                try:
                    if int(row.get(key) or 0) in admin_ids:
                        skip = True
                        break
                except (TypeError, ValueError):
                    pass
            if not skip:
                out.append(row)
        return out

    pc_access = _non_admin_grants(pc_access, "user_id")
    script_access = _non_admin_grants(script_access, "user_id")
    schedule_access = _non_admin_grants(schedule_access, "user_id")
    schedule_access_assign = _non_admin_grants(schedule_access_assign, "user_id")
    folder_access = _non_admin_grants(folder_access, "user_id")
    scheduler_view_access = _non_admin_grants(
        scheduler_view_access, "viewer_user_id", "target_user_id"
    )

    return render_template(
        "permissions.html",
        users=users,
        workers=workers,
        scripts_catalog=[],  # loaded async via /api/permissions/catalog
        schedules_catalog=[
            [
                s["id"],
                s.get("username") or "",
                s.get("script_name") or "",
                s.get("run_time") or "",
                s.get("user_id") or 0,
                s.get("worker_owner_id") or 0,
            ]
            for s in schedules
        ],
        pc_access=pc_access,
        script_access=script_access,
        schedule_access=schedule_access,
        schedule_access_assign=schedule_access_assign,
        folder_access=folder_access,
        folders_catalog=folders_catalog,
        scheduler_view_access=scheduler_view_access,
        users_can_set_days={
            str(int(u["id"])): 1 if int(u.get("can_set_days") or 0) == 1 else 0
            for u in (users or [])
        },
    )


@web_bp.route("/api/permissions/catalog")
@admin_required
def permissions_catalog():
    """Async lightweight script catalog for the Permissions page (keeps initial HTML small)."""
    scripts = database.list_scripts_for_permissions()
    return jsonify({
        "scripts": [
            [s["id"], s["worker_name"], s["script_name"], s.get("username") or ""]
            for s in scripts
        ],
    })

@web_bp.route("/revoke-pc-access", methods=["POST"])
@admin_required
def revoke_pc_access():
    user_id = request.form.get("user_id", type=int)
    worker_name = request.form.get("worker_name", "").strip()
    if user_id and worker_name:
        database.revoke_pc_access(user_id, worker_name)
        database.log_action(session["user_id"], "permission_revoked", f"Revoked PC access to {worker_name} for user #{user_id}", _client_ip(), worker_name=worker_name)
        flash(f"Revoked PC access to '{worker_name}'.", "success")
    return redirect(url_for("web.permissions"))


@web_bp.route("/revoke-script-access", methods=["POST"])
@admin_required
def revoke_script_access():
    user_id = request.form.get("user_id", type=int)
    script_id = request.form.get("script_id", type=int)
    if user_id and script_id:
        database.revoke_script_access(user_id, script_id)
        script = database.get_script(script_id)
        wn = (script or {}).get("worker_name") or None
        database.log_action(
            session["user_id"],
            "permission_revoked",
            f"Revoked script #{script_id} access for user #{user_id}",
            _client_ip(),
            worker_name=wn,
        )
        flash("Script access revoked.", "success")
    return redirect(url_for("web.permissions"))


@web_bp.route("/revoke-schedule-access", methods=["POST"])
@admin_required
def revoke_schedule_access():
    user_id = request.form.get("user_id", type=int)
    schedule_id = request.form.get("schedule_id", type=int)
    if user_id and schedule_id:
        database.revoke_schedule_access(schedule_id, user_id)
        sch = database.get_schedule(schedule_id)
        wn = (sch or {}).get("worker_name") or None
        database.log_action(
            session["user_id"],
            "permission_revoked",
            f"Revoked schedule #{schedule_id} access for user #{user_id}",
            _client_ip(),
            worker_name=wn,
        )
        flash(f"Schedule #{schedule_id} access revoked.", "success")
    return redirect(url_for("web.permissions"))


# ============================================================
# Scheduler
# ============================================================

@web_bp.route("/scheduler")
@login_required
def scheduler():
    """Scheduler page — defaults to the current user's data; admins can pick All users."""
    uid = session["user_id"]
    user = g.current_user
    raw_view = (request.args.get("view_user_id") or "").strip()
    view_all = False
    view_user_id = uid
    if raw_view.lower() == "all":
        if user and user["role"] == "admin":
            view_all = True
            view_user_id = None
        else:
            view_user_id = uid
    elif raw_view:
        try:
            view_user_id = int(raw_view)
        except (TypeError, ValueError):
            view_user_id = uid
        if view_user_id and not database.can_view_scheduler_user(uid, view_user_id):
            view_user_id = uid
    schedules = database.list_schedules_for_viewer(
        uid, view_user_id, exclude_folder_members=True
    )

    # For the create form
    if user and user["role"] == "admin":
        scripts = database.list_scripts()
    else:
        scripts = database.list_accessible_scripts(uid)
        
    all_workers = database.list_workers()
    online_workers = {w["worker_name"] for w in all_workers if w["status"] == "online"}
    scripts = [s for s in scripts if s["worker_name"] in online_workers]

    users = []
    if user and user["role"] == "admin":
        users = database.get_all_users()

    selected_script = request.args.get("selected_script", type=int)
    worker_filter = request.args.get("job_worker", "").strip()
    status_filter = request.args.get("job_status", "").strip()
    job_search = request.args.get("job_search", "").strip()
    job_page = request.args.get("job_page", 1, type=int)
    job_limit = request.args.get("job_limit", 10, type=int)
    
    offset = (job_page - 1) * job_limit
    
    if view_all:
        user_id_param = None if (user and user["role"] == "admin") else uid
    else:
        user_id_param = view_user_id or uid
    
    scheduler_jobs, job_total = database.get_scheduler_jobs(
        worker_name=worker_filter if worker_filter else None,
        status=status_filter if status_filter else None,
        search=job_search if job_search else None,
        limit=job_limit,
        offset=offset,
        user_id=user_id_param
    )
    
    # For the dropdown
    workers = database.list_accessible_workers(uid)
    scheduler_view_targets = []
    try:
        if user and user["role"] == "admin":
            scheduler_view_targets = [
                u for u in (users or database.get_all_users()) if int(u["id"]) != int(uid)
            ]
        else:
            scheduler_view_targets = database.list_scheduler_view_targets(uid)
    except Exception:
        scheduler_view_targets = []

    return render_template(
        "scheduler.html",
        schedules=schedules,
        scripts=scripts,
        users=users,
        selected_script=selected_script,
        scheduler_jobs=scheduler_jobs,
        job_total=job_total,
        job_page=job_page,
        job_limit=job_limit,
        worker_filter=worker_filter,
        status_filter=status_filter,
        job_search=job_search,
        workers=workers,
        scheduler_view_targets=scheduler_view_targets,
        view_user_id=view_user_id,
        view_all=view_all,
        can_set_days=database.user_can_set_days(uid),
    )


@web_bp.route("/scheduler/create", methods=["POST"])
@login_required
def create_schedule():
    uid = session["user_id"]
    schedule_id = request.form.get("schedule_id", type=int)
    script_ids = request.form.getlist("script_ids", type=int)
    script_ids = list(set(script_ids))  # Deduplicate to prevent double-creation
    run_time = request.form.get("run_time", "").strip()
    # Only treat days as provided when the field is actually submitted (hidden/disabled omits it)
    days_provided = "days" in request.form
    days = request.form.get("days", type=int) if days_provided else None
    if not database.user_can_set_days(uid):
        days_provided = False
        days = None
    schedule_type = request.form.get("frequency", "daily")
    folder_id = request.form.get("folder_id", type=int)
    # Folder members run only via sequential "Run Folder" — no custom schedule time
    is_folder_member_create = bool(folder_id) and not schedule_id
    is_folder_member_edit = False
    if schedule_id and not folder_id:
        try:
            from app.services import schedule_folders as sf
            is_folder_member_edit = sf.get_schedule_folder_id(schedule_id) is not None
        except Exception:
            is_folder_member_edit = False
    skip_schedule_timing = is_folder_member_create or is_folder_member_edit

    # Check for explicit date fields
    full_date = request.form.get("full_date", "").strip()
    day_of_month = request.form.get("day_of_month", "").strip()

    # Check for advanced config
    interval_numeric = request.form.get("interval_numeric", "").strip()
    interval_unit = request.form.get("interval_unit", "").strip()
    weekdays = request.form.getlist("weekdays")
    interval_use_window = request.form.get("interval_use_window") == "1"
    interval_window_start = request.form.get("interval_window_start", "").strip()
    interval_window_end = request.form.get("interval_window_end", "").strip()

    import re

    def _respond(message: str, category: str = "success", **extra):
        """Flash+redirect for normal posts; JSON for XHR (scheduler drawer)."""
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "ok": category == "success",
                "message": message,
                "category": category,
                **extra,
            })
        flash(message, category)
        return redirect(url_for("web.scheduler"))

    if skip_schedule_timing:
        # Placeholder only — excluded from due-schedule ticks while in a folder
        schedule_type = "daily"
        run_time = "00:00"
        schedule_config = {
            "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "folder_member": True,
        }
        config_json = json.dumps(schedule_config)
    else:
        schedule_config = {}
        if schedule_type == "daily":
            if not weekdays:
                return _respond("Select at least one day of the week.", "error")
            schedule_config["weekdays"] = weekdays
        if schedule_type == "interval":
            if not interval_numeric or not interval_unit:
                return _respond("Interval value and unit are required.", "error")
            try:
                if int(interval_numeric) < 1:
                    return _respond("Interval must be at least 1.", "error")
            except ValueError:
                return _respond("Invalid interval value.", "error")
            schedule_config["interval_val"] = f"{interval_numeric}{interval_unit}"
            if interval_use_window:
                hm = re.compile(r"^\d{1,2}:\d{2}$")

                def _norm_hm(t: str) -> str | None:
                    if not hm.match(t or ""):
                        return None
                    h, m = t.split(":")
                    h_i, m_i = int(h), int(m)
                    if h_i > 23 or m_i > 59:
                        return None
                    return f"{h_i:02d}:{m_i:02d}"

                start_n = _norm_hm(interval_window_start)
                end_n = _norm_hm(interval_window_end)
                if not start_n or not end_n:
                    return _respond("Valid From/To times are required for the interval time range.", "error")
                schedule_config["window_start"] = start_n
                schedule_config["window_end"] = end_n

        config_json = json.dumps(schedule_config) if schedule_config else None

        # Normalize run_time based on schedule_type
        if schedule_type == "once":
            if full_date:
                run_time = f"{full_date} {run_time}" if " " not in run_time else run_time
            elif " " not in run_time:
                return _respond("Date is required for one-time schedules.", "error")
            # Validate final once format
            once_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}$")
            if not once_re.match(run_time or ""):
                return _respond("One-time schedules require a valid date and time (YYYY-MM-DD HH:MM).", "error")
        elif schedule_type == "interval":
            run_time = "00:00"
        elif schedule_type == "monthly":
            if day_of_month:
                run_time = f"{str(day_of_month).zfill(2)}:{run_time}"
            elif run_time.count(":") != 2:
                return _respond("Day of month is required for monthly schedules.", "error")
        elif ":" not in run_time:
            run_time = "00:00"

    if schedule_id:
        sch = database.get_schedule(schedule_id)
        if not sch:
            return _respond("Schedule not found.", "error")

        # Admin, owner, or granted Edit permission
        if not database.user_can_edit_schedule(uid, schedule_id):
            database.log_action(
                uid,
                "unauthorized_access",
                f"Attempted to update schedule #{schedule_id}",
                _client_ip(),
                worker_name=sch.get("worker_name"),
            )
            return _respond("You don't have permission to update this schedule.", "error")

        script_id = script_ids[0] if script_ids else sch["script_id"]
        worker_name = sch["worker_name"]
        if script_id != sch["script_id"]:
            if not database.check_script_access(uid, script_id, "run"):
                s = database.get_script(script_id)
                database.log_action(
                    uid,
                    "unauthorized_access",
                    f"Attempted to retarget schedule #{schedule_id} to script {script_id}",
                    _client_ip(),
                    worker_name=(s or {}).get("worker_name") or sch.get("worker_name"),
                )
                return _respond("You don't have permission to schedule the selected script.", "error")
            s = database.get_script(script_id)
            if s:
                worker_name = s["worker_name"]
            else:
                return _respond("Selected script not found.", "error")

        # Preserve days when the days field was not submitted (hidden/disabled)
        if not days_provided:
            days = sch.get("days")
        else:
            # Only store override when the (possibly new) script has days = N
            target = database.get_script(script_id)
            if not target or target.get("days") is None:
                days = None

        # Preserve timing when editing a folder member (no schedule settings UI)
        if skip_schedule_timing:
            run_time = sch.get("run_time") or "00:00"
            schedule_type = sch.get("schedule_type") or "daily"
            config_json = sch.get("schedule_config")
            if isinstance(config_json, dict):
                config_json = json.dumps(config_json)

        database.update_schedule_full(
            schedule_id, script_id, worker_name, run_time, days, config_json, schedule_type
        )
        database.log_action(
            uid,
            "schedule_updated",
            f"Updated schedule #{schedule_id} for script ID {script_id}",
            _client_ip(),
            worker_name=worker_name,
        )
        return _respond("Schedule updated.", "success", schedule_id=schedule_id)

    if not script_ids:
        return _respond("At least one script is required.", "error")
    if not run_time and not is_folder_member_create:
        return _respond("At least one script and run time are required.", "error")

    created_count = 0
    created_ids = []
    for script_id in script_ids:
        script = database.get_script(script_id)
        if not script:
            continue

        if not database.check_script_access(uid, script_id, "run"):
            database.log_action(
                uid,
                "unauthorized_access",
                f"Attempted to schedule script {script['script_name']}",
                _client_ip(),
                worker_name=script["worker_name"],
            )
            continue

        sch = database.create_schedule(
            script_id,
            uid,
            script["worker_name"],
            run_time,
            days if (days is not None and script.get("days") is not None) else None,
            config_json,
            schedule_type,
        )
        database.log_action(
            uid,
            "schedule_created",
            f"Created schedule #{sch['id']} for {script['script_name']} ({script.get('script_path', '')})",
            _client_ip(),
            worker_name=script["worker_name"],
        )
        created_count += 1
        created_ids.append(sch["id"])

    if folder_id and created_ids:
        try:
            from app.services import schedule_folders as sf
            if sf.get_folder(folder_id) and (
                database.is_admin(uid) or sf.user_can_folder(uid, folder_id, "can_manage", False)
                or sf.user_can_folder(uid, folder_id, "can_edit", False)
            ):
                sf.add_schedules_to_folder(folder_id, created_ids)
        except Exception as exc:
            print(f"add schedules to folder failed: {exc}", flush=True)

    type_label = {
        "once": "once",
        "daily": "daily",
        "monthly": "monthly",
        "interval": "interval",
    }.get(schedule_type, schedule_type)

    if created_count > 0:
        if folder_id:
            return _respond(
                f"{created_count} script(s) added to folder (run one-by-one via Run Folder).",
                "success",
                created_count=created_count,
                folder_id=folder_id,
                schedule_ids=created_ids,
            )
        return _respond(
            f"{created_count} schedule(s) created ({type_label}) for {run_time}.",
            "success",
            created_count=created_count,
            folder_id=folder_id,
            schedule_ids=created_ids,
        )
    return _respond("No schedules were created due to errors or permissions.", "error")


@web_bp.route("/schedule/<int:schedule_id>/update", methods=["POST"])
@login_required
def update_schedule(schedule_id):
    uid = session["user_id"]
    sch = database.get_schedule(schedule_id)
    if not sch:
        flash("Schedule not found.", "error")
        return redirect(url_for("web.scheduler"))

    # Admin, owner, or granted Edit permission
    if not database.user_can_edit_schedule(uid, schedule_id):
        database.log_action(uid, "unauthorized_access", f"Attempted to update schedule #{schedule_id}", _client_ip(), worker_name=sch["worker_name"])
        flash("You don't have permission to update this schedule.", "error")
        return redirect(url_for("web.scheduler"))

    run_time = request.form.get("run_time", "").strip() or None
    enabled = request.form.get("enabled")
    if enabled is not None:
        enabled = int(enabled)

    database.update_schedule(schedule_id, run_time=run_time, enabled=enabled)
    database.log_action(uid, "schedule_updated", f"Updated schedule #{schedule_id}", _client_ip(), worker_name=sch["worker_name"])
    flash("Schedule updated.", "success")
    return redirect(url_for("web.scheduler"))


@web_bp.route("/schedule/<int:schedule_id>/delete", methods=["POST"])
@login_required
def delete_schedule(schedule_id):
    uid = session["user_id"]
    schedules = {s["id"]: s for s in database.list_schedules(uid)}
    sch = schedules.get(schedule_id)
    
    if not sch:
        flash("Schedule not found or you lack permission.", "error")
        return redirect(url_for("web.scheduler"))

    if not database.is_admin(uid) and not sch.get("can_delete"):
        database.log_action(uid, "unauthorized_access", f"Attempted to delete schedule #{schedule_id}", _client_ip(), worker_name=sch["worker_name"])
        flash("You don't have permission to delete this schedule.", "error")
        return redirect(url_for("web.scheduler"))

    database.delete_schedule(schedule_id)
    database.log_action(uid, "schedule_deleted", f"Deleted schedule #{schedule_id}", _client_ip(), worker_name=sch["worker_name"])
    flash("Schedule deleted.", "success")
    return redirect(url_for("web.scheduler"))


@web_bp.route("/bulk-update-schedules", methods=["POST"])
@login_required
def bulk_update_schedules():
    action = request.form.get("action")
    schedule_ids = request.form.getlist("schedules") or request.form.getlist("schedule_ids")
    uid = session["user_id"]
    folder_id = request.form.get("folder_id", type=int)

    def _respond(message: str, category: str = "success", **extra):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "ok": category == "success",
                "message": message,
                "category": category,
                **extra,
            })
        flash(message, category)
        return redirect(url_for("web.scheduler"))

    if action in ["enable", "disable", "delete", "update_time", "update_days"] and not schedule_ids:
        # Check if it was actually an inline edit
        sch_id_str = request.form.get("inline_schedule_id")
        if sch_id_str:
            sch_id = int(sch_id_str)
            run_time = request.form.get("inline_time")
            days_val = request.form.get("inline_days", type=int)
            
            schedules = {s["id"]: s for s in database.list_schedules(uid, exclude_folder_members=False)}
            sch = schedules.get(sch_id)
            if sch and (database.is_admin(uid) or sch.get("can_edit") or sch["user_id"] == uid):
                database.update_schedule(sch_id, run_time=run_time)
                if days_val is not None and database.user_can_set_days(uid):
                    database.update_schedule_days(sch_id, days_val)
                database.log_action(uid, "schedule_updated", f"Updated inline schedule #{sch_id}", _client_ip(), worker_name=sch["worker_name"])
                return _respond("Schedule updated.", "success")
            return _respond("You don't have permission to update this schedule.", "error")

    if action == "update_days" and not database.user_can_set_days(uid):
        return _respond("You don't have permission to set days.", "error")

    if not schedule_ids:
        return _respond("No schedules selected.", "error")

    success_count = 0
    # Include folder members so Folders tab can reuse the same schedule actions
    schedules_map = {s["id"]: s for s in database.list_schedules(uid, exclude_folder_members=False)}
    duplicated_ids = []

    from app.services import schedule_folders as sf

    for sch_id_str in schedule_ids:
        sch_id = int(sch_id_str)
        sch = schedules_map.get(sch_id)
        if not sch:
            # Fallback: allow folder-scoped lookup for members not in map
            sch = database.get_schedule(sch_id)
            if not sch or int(sch.get("is_deleted") or 0):
                continue
            # Minimal permission flags for owner/admin
            if database.is_admin(uid) or int(sch.get("user_id") or 0) == uid:
                sch = {**sch, "can_enable": 1, "can_disable": 1, "can_run": 1,
                       "can_edit": 1, "can_delete": 1, "can_duplicate": 1}
            else:
                continue

        is_owner = int(sch.get("user_id") or 0) == int(uid)
        is_admin_user = database.is_admin(uid)

        if action == "enable":
            if is_admin_user or is_owner or sch.get("can_enable"):
                database.update_schedule(sch_id, enabled=1)
                try:
                    fid = sf.get_schedule_folder_id(sch_id) or folder_id
                    if fid:
                        with database.db_cursor() as cur:
                            cur.execute(
                                "UPDATE schedule_folder_items SET enabled = 1 WHERE folder_id = ? AND schedule_id = ?",
                                (fid, sch_id),
                            )
                except Exception:
                    pass
                success_count += 1
        elif action == "disable":
            if is_admin_user or is_owner or sch.get("can_disable") or sch.get("can_enable"):
                database.update_schedule(sch_id, enabled=0)
                try:
                    fid = sf.get_schedule_folder_id(sch_id) or folder_id
                    if fid:
                        with database.db_cursor() as cur:
                            cur.execute(
                                "UPDATE schedule_folder_items SET enabled = 0 WHERE folder_id = ? AND schedule_id = ?",
                                (fid, sch_id),
                            )
                except Exception:
                    pass
                success_count += 1
        elif action == "delete":
            if is_admin_user or is_owner or sch.get("can_delete"):
                database.delete_schedule(sch_id)
                try:
                    fid = sf.get_schedule_folder_id(sch_id)
                    if fid:
                        sf.remove_schedules_from_folder(fid, [sch_id])
                except Exception:
                    pass
                success_count += 1
        elif action == "duplicate":
            if is_admin_user or is_owner or sch.get("can_duplicate"):
                new_sch = database.duplicate_schedule(uid, sch_id)
                if new_sch and new_sch.get("id"):
                    duplicated_ids.append(int(new_sch["id"]))
                    if folder_id:
                        try:
                            sf.add_schedules_to_folder(folder_id, [int(new_sch["id"])])
                        except Exception:
                            pass
                    success_count += 1
        elif action in ("update_time", "update_days"):
            if is_admin_user or is_owner or sch.get("can_edit"):
                if action == "update_time":
                    bulk_time = request.form.get("bulk_time", "").strip() or None
                    if bulk_time:
                        database.update_schedule(sch_id, run_time=bulk_time)
                        success_count += 1
                elif action == "update_days":
                    bulk_days = request.form.get("bulk_days", type=int)
                    if bulk_days is not None and bulk_days >= 0:
                        # Only when script has a days variable (same rule as UI input)
                        if sch.get("script_days") is not None or (
                            database.get_script(sch["script_id"]) or {}
                        ).get("days") is not None:
                            database.update_schedule_days(sch_id, bulk_days)
                            success_count += 1
        elif action == "run_now":
            if is_admin_user or is_owner or sch.get("can_run"):
                database.create_job(sch["worker_name"], sch["script_id"], schedule_id=sch_id)
                database.mark_schedule_run(sch_id, "Manually triggered via Run Now")
                success_count += 1
        elif action == "stop_running":
            if is_admin_user or is_owner or sch.get("can_run"):
                job_id = database.get_active_job_for_schedule(sch_id)
                if job_id:
                    database.stop_job(job_id)
                    success_count += 1
        elif action == "pause_running":
            if is_admin_user or is_owner or sch.get("can_run"):
                job_id = database.get_active_job_for_schedule(sch_id)
                if job_id:
                    database.pause_job(job_id)
                    success_count += 1
        elif action == "resume_paused":
            if is_admin_user or is_owner or sch.get("can_run"):
                job_id = database.get_active_job_for_schedule(sch_id)
                if job_id:
                    database.resume_job(job_id)
                    success_count += 1

    if success_count > 0:
        database.log_action(uid, "bulk_schedules_updated", f"Bulk {action} applied to {success_count} schedules", _client_ip())
        return _respond(
            f"Successfully applied '{action}' to {success_count} schedules.",
            "success",
            success_count=success_count,
            duplicated_ids=duplicated_ids,
        )
    return _respond("No changes made. Check permissions.", "warning", success_count=0)


@web_bp.route("/profile/update", methods=["POST"])
@login_required
def update_profile():
    uid = session["user_id"]
    password = request.form.get("password", "").strip()
    username = request.form.get("username", "").strip()
    
    if not username:
        flash("Username cannot be empty.", "error")
        return redirect(request.referrer or url_for("web.dashboard"))
        
    try:
        with database.db_cursor() as cur:
            if password:
                pw_hash = generate_password_hash(password)
                cur.execute("UPDATE users SET username = ?, password_hash = ? WHERE id = ?", (username, pw_hash, uid))
                database.log_action(uid, "profile_updated", f"Updated username and password", _client_ip())
                flash("Profile updated successfully.", "success")
            else:
                cur.execute("UPDATE users SET username = ? WHERE id = ?", (username, uid))
                database.log_action(uid, "profile_updated", f"Updated username", _client_ip())
                flash("Profile updated successfully.", "success")
            
            if session.get("username") != username:
                session["username"] = username
    except Exception as e:
        flash("Failed to update profile. Username might already be taken.", "error")

    return redirect(request.referrer or url_for("web.dashboard"))


# ============================================================
# User management (admin only)
# ============================================================

@web_bp.route("/users")
@admin_required
def users():
    all_users = database.get_all_users()
    return render_template("users.html", users=all_users)


@web_bp.route("/users/create", methods=["POST"])
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    registered_ip = request.form.get("registered_ip", "").strip() or "0.0.0.0"
    role = request.form.get("role", "user").strip()

    if not username or not password :
        flash("Username, password, is required.", "error")
        return redirect(url_for("web.users"))

    pw_hash = generate_password_hash(password)
    user = database.create_user(username, pw_hash, registered_ip, role=role)

    if user:
        database.log_action(session["user_id"], "user_created", f"Admin created user {username}", _client_ip())
        flash(f"User '{username}' created successfully.", "success")
    else:
        flash("Username already exists.", "error")
    
    return redirect(url_for("web.users"))

@web_bp.route("/users/<int:user_id>/edit", methods=["POST"])
@admin_required
def edit_user(user_id):
    target = database.get_user_by_id(user_id)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("web.users"))

    username = request.form.get("username", "").strip()
    registered_ip = request.form.get("registered_ip", "").strip() or "0.0.0.0"
    role = request.form.get("role", "user").strip()
    password = request.form.get("password", "").strip()
    is_disabled = 1 if request.form.get("is_disabled") else 0

    if not username:
        flash("Username is required.", "error")
        return redirect(url_for("web.users"))

    # Cannot demote any admin to user (promote user→admin is allowed)
    if (target.get("role") or "") == "admin" and role != "admin":
        flash("Admin accounts cannot be changed back to User.", "error")
        return redirect(url_for("web.users"))

    # Cannot disable yourself or the primary admin account
    if is_disabled and int(user_id) == int(session["user_id"]):
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("web.users"))
    if is_disabled and (target.get("username") or "") == "admin":
        flash("The primary admin account cannot be disabled.", "error")
        return redirect(url_for("web.users"))

    pw_hash = generate_password_hash(password) if password else None

    if database.update_user_full(user_id, username, registered_ip, role, pw_hash, is_disabled=is_disabled):
        database.log_action(
            session["user_id"],
            "user_updated",
            f"Admin updated user #{user_id} (role={role}, disabled={is_disabled})",
            _client_ip(),
        )
        flash(f"User '{username}' updated successfully.", "success")
    else:
        flash("Failed to update user. Username may already exist.", "error")

    return redirect(url_for("web.users"))


@web_bp.route("/users/<int:user_id>/toggle-disabled", methods=["POST"])
@admin_required
def toggle_user_disabled(user_id):
    target = database.get_user_by_id(user_id)
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("web.users"))

    if int(user_id) == int(session["user_id"]):
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("web.users"))
    if (target.get("username") or "") == "admin":
        flash("The primary admin account cannot be disabled.", "error")
        return redirect(url_for("web.users"))

    new_disabled = 0 if int(target.get("is_disabled") or 0) == 1 else 1
    ok = database.update_user_full(
        user_id,
        target["username"],
        target.get("registered_ip") or "0.0.0.0",
        target.get("role") or "user",
        is_disabled=new_disabled,
    )
    if ok:
        label = "disabled" if new_disabled else "enabled"
        database.log_action(
            session["user_id"],
            "user_updated",
            f"Admin {label} user #{user_id}",
            _client_ip(),
        )
        flash(f"User '{target['username']}' {label}.", "success")
    else:
        flash("Failed to update user status.", "error")
    return redirect(url_for("web.users"))


@web_bp.route("/history")
@login_required
def history():
    uid = session["user_id"]
    is_admin_user = database.is_admin(uid)
    hist_limit = 3000 if is_admin_user else 1000
    if is_admin_user:
        logs = database.get_history(limit=hist_limit)
        f_history = database.get_file_history(limit=hist_limit)
        job_history = database.list_jobs(limit=hist_limit)
        history_users = database.get_all_users()
    else:
        # Own actions + worker history within current/past assignment windows
        logs = database.get_history(limit=hist_limit, user_id=uid)
        f_history = database.get_file_history(limit=hist_limit, user_id=uid)
        job_history = database.list_jobs(limit=hist_limit, user_id=uid)
        history_users = []

    # Filter and categorize logs — never drop a row
    system_logs = []
    execution_logs = []
    permission_logs = []
    all_logs = []

    system_actions = {
        "user_login", "user_logout", "failed_login", "user_registered",
        "user_deleted", "user_updated", "profile_updated", "unauthorized_access",
        "user_created",
    }
    execution_actions = {
        "job_run", "job_stopped", "job_paused", "job_resumed",
        "schedule_created", "schedule_updated", "schedule_deleted",
        "bulk_schedules_updated", "script_days_updated", "script_uploaded",
        "folder_created", "folder_updated", "folder_deleted",
        "folder_toggled", "folder_run", "folder_stopped",
    }
    permission_actions = {
        "permissions_updated", "ownership_transfer", "worker_renamed",
        "permission_granted", "permission_revoked", "worker_config_updated",
    }

    for log in logs:
        action = log.get("action") or ""
        # Ignore file_ actions as they are in file_history
        if action.startswith("file_"):
            continue
        all_logs.append(log)
        if action in system_actions:
            system_logs.append(log)
        elif action in execution_actions:
            execution_logs.append(log)
        elif action in permission_actions:
            permission_logs.append(log)
        else:
            system_logs.append(log)

    # Attach "granted to" user for Permissions tab (who received the access).
    import re
    _target_re = re.compile(r"Target:\s*(.+?)\s*\(#(\d+)\)", re.I)
    _legacy_perm_re = re.compile(r"Updated permissions for user\s*#(\d+)", re.I)
    _users_by_id = {}
    for u in (history_users or []):
        try:
            _users_by_id[int(u["id"])] = u
        except (TypeError, ValueError, KeyError):
            pass
    for log in permission_logs:
        details = log.get("details") or ""
        granted_name = None
        granted_id = None
        m = _target_re.search(details)
        if m:
            granted_name = (m.group(1) or "").strip()
            try:
                granted_id = int(m.group(2))
            except (TypeError, ValueError):
                granted_id = None
        else:
            m2 = _legacy_perm_re.search(details)
            if m2:
                try:
                    granted_id = int(m2.group(1))
                except (TypeError, ValueError):
                    granted_id = None
                if granted_id:
                    u = _users_by_id.get(granted_id)
                    if not u:
                        u = database.get_user_by_id(granted_id)
                        if u:
                            _users_by_id[granted_id] = u
                    granted_name = (u or {}).get("username") or f"#{granted_id}"
        if granted_id and not granted_name:
            u = _users_by_id.get(granted_id) or database.get_user_by_id(granted_id)
            granted_name = (u or {}).get("username") or f"#{granted_id}"
        log["granted_to_user_id"] = granted_id
        log["granted_to_username"] = granted_name or ""

    # Backfill blank Worker column from details (older permissions_updated rows).
    for log in all_logs:
        if (log.get("worker_name") or "").strip():
            continue
        inferred = _perm_workers_from_details(log.get("details") or "")
        if inferred:
            log["worker_name"] = inferred

    def _uniq_sorted(values):
        seen = set()
        out = []
        for v in values:
            s = (v or "").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        out.sort(key=str.lower)
        return out

    filter_actions_all = _uniq_sorted([r.get("action") for r in all_logs])
    filter_actions_system = _uniq_sorted([r.get("action") for r in system_logs])
    filter_actions_execution = _uniq_sorted([r.get("action") for r in execution_logs])
    filter_actions_permission = _uniq_sorted([r.get("action") for r in permission_logs])
    filter_actions_file = _uniq_sorted([r.get("action") for r in f_history])
    filter_job_statuses = _uniq_sorted([r.get("status") for r in job_history])
    filter_job_workers = _uniq_sorted([r.get("worker_name") for r in job_history])
    filter_file_workers = _uniq_sorted([r.get("worker_name") for r in f_history])
    filter_exec_workers = _uniq_sorted([r.get("worker_name") for r in execution_logs])
    filter_perm_workers = _uniq_sorted([r.get("worker_name") for r in permission_logs])
    filter_all_workers = _uniq_sorted([r.get("worker_name") for r in all_logs])

    return render_template(
        "history.html",
        all_logs=all_logs,
        system_logs=system_logs,
        execution_logs=execution_logs,
        permission_logs=permission_logs,
        file_history=f_history,
        job_history=job_history,
        history_users=history_users,
        is_admin_user=is_admin_user,
        filter_all_workers=filter_all_workers,
        filter_exec_workers=filter_exec_workers,
        filter_perm_workers=filter_perm_workers,
        filter_job_workers=filter_job_workers,
        filter_file_workers=filter_file_workers,
        filter_actions_all=filter_actions_all,
        filter_actions_system=filter_actions_system,
        filter_actions_execution=filter_actions_execution,
        filter_actions_permission=filter_actions_permission,
        filter_actions_file=filter_actions_file,
        filter_job_statuses=filter_job_statuses,
    )

# ============================================================
# Remote File Editor & History
# ============================================================

@web_bp.route("/editor")
@login_required
def editor():
    uid = session["user_id"]
    user = g.current_user

    if user and user["role"] == "admin":
        all_scripts = database.list_scripts()
        workers = database.list_workers()
    else:
        all_scripts = database.list_accessible_scripts(uid)
        workers = database.list_accessible_workers(uid)
        
    # Only show files for active workers
    active_workers = {w["worker_name"] for w in workers if w["status"] == "online"}
    scripts = [s for s in all_scripts if s["worker_name"] in active_workers]
    accessible_workers = list(active_workers)
        
    init_worker = request.args.get("worker_name", "")
    init_path = request.args.get("file_path", "")
    
    # Get file history (scoped by assignment periods for non-admins)
    if user and user["role"] == "admin":
        f_history = database.get_file_history(limit=500)
    else:
        f_history = database.get_file_history(limit=500, user_id=uid)

    return render_template("editor.html", scripts=scripts, init_worker=init_worker, init_path=init_path, file_history=f_history, accessible_workers=accessible_workers)

@web_bp.route("/api/editor/read", methods=["POST"])
@login_required
def editor_read():
    uid = session["user_id"]
    data = request.get_json() or {}
    script_id = data.get("script_id")
    
    if not script_id:
        return jsonify({"error": "script_id required"}), 400
        
    script = database.get_script(script_id)
    if not script:
        return jsonify({"error": "Script not found"}), 404
        
    # Check permissions - require at least view (run/update/delete all implicitly allow view)
    user = g.current_user
    if user["role"] != "admin":
        # Check user_script_access
        with database.db_cursor() as cur:
            cur.execute("SELECT * FROM user_script_access WHERE user_id = ? AND script_id = ?", (uid, script_id))
            if not cur.fetchone() and script["owner_id"] != uid:
                database.log_action(uid, "unauthorized_access", f"Attempted to read script #{script_id}", _client_ip())
                return jsonify({"error": "Permission denied"}), 403

    payload = {"target_path": script["script_path"]}
    cmd = database.create_command(script["worker_name"], "read_file", json.dumps(payload))
    database.log_file_action(uid, script["worker_name"], script["script_path"], "File viewed")

    # Edit File PC permission only — script owner/update must not bypass view-only
    can_edit = database.user_can_edit_pc_file(
        uid, script["worker_name"], script.get("script_path") or ""
    )
    
    return jsonify({"cmd_id": cmd["id"], "can_edit": bool(can_edit)})

@web_bp.route("/api/editor/read_path", methods=["POST"])
@login_required
def editor_read_path():
    uid = session["user_id"]
    data = request.get_json() or {}
    worker_name = data.get("worker_name")
    file_path = data.get("file_path")
    
    if not worker_name or not file_path:
        return jsonify({"error": "worker_name and file_path required"}), 400
        
    perms = database.get_pc_access_details(uid, worker_name)
    # View allowed with PC access; Edit File only needed to modify
    err = database.check_pc_file_view(
        uid, worker_name, file_path,
        is_folder=False, check_ext=True, perms=perms,
    )
    if err:
        return jsonify({"error": "Permission denied"}), 403

    can_edit = database.user_can_edit_pc_file(uid, worker_name, file_path, perms=perms)

    import json
    payload = {"target_path": file_path.lstrip("/")}
    cmd = database.create_command(worker_name, "read_file", json.dumps(payload))
    database.log_file_action(uid, worker_name, file_path, "File viewed in Editor")
    return jsonify({"cmd_id": cmd["id"], "can_edit": bool(can_edit)})

@web_bp.route("/api/editor/save_path", methods=["POST"])
@login_required
def editor_save_path():
    uid = session["user_id"]
    data = request.get_json() or {}
    worker_name = data.get("worker_name")
    file_path = data.get("file_path")
    content = data.get("content", "")
    old_content = data.get("old_content", "")
    
    if not worker_name or not file_path:
        return jsonify({"error": "worker_name and file_path required"}), 400
        
    perms = database.get_pc_access_details(uid, worker_name)
    err = database.check_pc_file_operation(
        uid, worker_name, "can_edit_file", file_path,
        is_folder=False, check_ext=True, perms=perms,
    )
    if err:
        return jsonify({"error": "Permission denied — Edit File required to save"}), 403

    b64_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    payload = {"target_path": file_path.lstrip("/"), "file_content_b64": b64_content}
    cmd = database.create_command(worker_name, "write_file", json.dumps(payload))
    database.log_file_action(uid, worker_name, file_path, "File edited", old_content, content)
    return jsonify({"cmd_id": cmd["id"]})

@web_bp.route("/api/editor/save", methods=["POST"])
@login_required
def editor_save():
    uid = session["user_id"]
    data = request.get_json() or {}
    script_id = data.get("script_id")
    content = data.get("content", "")
    old_content = data.get("old_content", "")
    
    if not script_id:
        return jsonify({"error": "script_id required"}), 400
        
    script = database.get_script(script_id)
    if not script:
        return jsonify({"error": "Script not found"}), 404
        
    # Edit File PC permission only — owner/script-update must not bypass
    if not database.user_can_edit_pc_file(
        uid, script["worker_name"], script.get("script_path") or ""
    ):
        database.log_action(uid, "unauthorized_access", f"Attempted to update script #{script_id}", _client_ip())
        return jsonify({"error": "Permission denied — Edit File required to save"}), 403

    # Save to history
    database.log_file_action(uid, script["worker_name"], script["script_path"], "File edited", old_content, content)

    payload = {
        "target_path": script["script_path"],
        "file_content_b64": base64.b64encode(content.encode("utf-8")).decode("utf-8")
    }
    cmd = database.create_command(script["worker_name"], "write_file", json.dumps(payload))
    
    return jsonify({"cmd_id": cmd["id"]})

@web_bp.route("/api/editor/poll/<int:cmd_id>")
@login_required
def editor_poll(cmd_id):
    # Does not do strict permission checking, but cmd_id is hard to guess
    cmd = database.get_command(cmd_id)
    if not cmd:
        return jsonify({"error": "Command not found"}), 404
        
    if cmd["status"] == "completed":
        output = cmd["output"]
        try:
            # Output is base64
            content = base64.b64decode(output).decode("utf-8")
            return jsonify({"status": "completed", "content": content})
        except Exception as e:
            return jsonify({"status": "completed", "content": output}) # Was not base64?
            
    elif cmd["status"] == "error":
        return jsonify({"status": "error", "error": cmd["output"]})
        
    return jsonify({"status": "pending"})

@web_bp.route("/diff/<int:history_id>")
@login_required
def file_diff(history_id):
    uid = session["user_id"]
    user = g.current_user
    # Preserve origin so Back returns to Editor or History (not always History).
    back_src = (request.args.get("back") or "").strip().lower()
    if back_src == "editor":
        back_url = url_for("web.editor") + "#file-change-ledger"
        back_label = "Editor"
    else:
        back_src = "history"
        back_url = url_for("web.history") + "#file-history"
        back_label = "History"

    record = database.get_file_history_by_id(history_id)
    if not record:
        flash("History record not found.", "error")
        return redirect(back_url.split("#", 1)[0])

    if user["role"] != "admin":
        if not database.user_can_view_history_at(
            uid,
            record.get("worker_name"),
            record.get("created_at"),
            event_user_id=record.get("user_id"),
        ):
            flash("Permission denied.", "error")
            return redirect(back_url.split("#", 1)[0])

    old_text = record["old_content"] or ""
    new_text = record["new_content"] or ""

    import difflib
    old_lines = old_text.splitlines(keepends=False)
    new_lines = new_text.splitlines(keepends=False)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    old_view = []
    new_view = []

    additions = 0
    deletions = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for line in old_lines[i1:i2]:
                old_view.append({"type": "unchanged", "text": line})
                new_view.append({"type": "unchanged", "text": line})
        elif tag == 'replace':
            for line in old_lines[i1:i2]:
                old_view.append({"type": "removed", "text": line})
                deletions += 1
            for line in new_lines[j1:j2]:
                new_view.append({"type": "added", "text": line})
                additions += 1
        elif tag == 'delete':
            for line in old_lines[i1:i2]:
                old_view.append({"type": "removed", "text": line})
                deletions += 1
        elif tag == 'insert':
            for line in new_lines[j1:j2]:
                new_view.append({"type": "added", "text": line})
                additions += 1

    return render_template(
        "file_diff.html",
        record=record,
        old_view=old_view,
        new_view=new_view,
        additions=additions,
        deletions=deletions,
        back_url=back_url,
        back_label=back_label,
        back_src=back_src,
    )

# ============================================================
# Reports & Analytics
# ============================================================

@web_bp.route("/reports")
@login_required
def reports():
    """Reports / Analytics Dashboard"""
    uid = session["user_id"]
    user = g.current_user

    if user and user["role"] == "admin":
        scripts = database.list_scripts()
        workers = database.list_workers()
    else:
        scripts = database.list_accessible_scripts(uid)
        workers = database.list_accessible_workers(uid)

    worker_filter = request.args.get("job_worker", "").strip()
    script_filter = request.args.get("job_script", type=int)
    status_filter = request.args.get("job_status", "").strip()
    folder_filter = request.args.get("folder_path", "").strip()
    search = request.args.get("search", "").strip()
    has_errors_raw = (request.args.get("has_errors") or "").strip().lower()
    has_errors = True if has_errors_raw in ("1", "true", "yes") else None
    processed_lt = request.args.get("processed_lt", type=int)
    if processed_lt is not None and processed_lt <= 0:
        processed_lt = None
    
    time_range = request.args.get("time_range", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    
    from datetime import datetime, timedelta
    now = datetime.now()
    if time_range == "today":
        date_from = now.strftime("%Y-%m-%d")
        date_to = date_from
    elif time_range == "yesterday":
        date_from = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_to = date_from
    elif time_range == "week":
        date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")
    elif time_range == "month":
        date_from = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")

    # Keep custom range logically ordered (start <= end)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    limit = request.args.get("limit", 10, type=int)
    if limit not in (10, 25, 50, 100):
        limit = 10
    offset = (page - 1) * limit

    reports_kwargs = dict(
        worker_name=worker_filter,
        script_id=script_filter,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=limit,
        offset=offset,
        user_id=uid if user["role"] != "admin" else None,
        folder_path=folder_filter or None,
        has_errors=has_errors,
        processed_lt=processed_lt,
    )

    reports_data, total_count = database.get_paginated_reports(**reports_kwargs)
    
    total_pages = (total_count + limit - 1) // limit if total_count else 0
    if total_pages and page > total_pages:
        page = total_pages
        offset = (page - 1) * limit
        reports_kwargs["offset"] = offset
        reports_data, total_count = database.get_paginated_reports(**reports_kwargs)

    summary = database.get_report_summary_cards(
        worker_name=worker_filter,
        script_id=script_filter,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        user_id=uid if user["role"] != "admin" else None,
        folder_path=folder_filter or None,
        search=search,
        has_errors=has_errors,
        processed_lt=processed_lt,
    )

    # Get high-performance analytics from the DB
    error_analytics = database.get_report_analytics(
        worker_name=worker_filter,
        script_id=script_filter,
        date_from=date_from,
        date_to=date_to,
        user_id=uid if user["role"] != "admin" else None,
        folder_path=folder_filter or None,
    )

    # Fetch user's file watchlist
    watchlist_files = database.get_user_watchlist(uid)

    return render_template(
        "reports.html",
        reports=reports_data,
        summary=summary,
        error_analytics=error_analytics,
        watchlist_files=watchlist_files,
        scripts=scripts,
        workers=workers,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        limit=limit,
        worker_filter=worker_filter,
        script_filter=script_filter,
        status_filter=status_filter,
        folder_filter=folder_filter,
        date_from=date_from,
        date_to=date_to,
        time_range=time_range,
        search=search,
        has_errors_filter=has_errors_raw if has_errors else "",
        processed_lt_filter=processed_lt or "",
    )


@web_bp.route("/schedule_tracking")
@login_required
def schedule_tracking():
    """Legacy URL: Schedule Tracking lives in /reports#schedule-tracking (no separate template)."""
    return redirect(url_for("web.reports", _anchor="schedule-tracking"))
