"""
REST API routes for worker agents.
Controller never executes automation scripts — workers only.
"""
from flask import Blueprint, jsonify, request, session
from typing import Optional

from app import database

api_bp = Blueprint("api", __name__)


def _client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")


def _audit(user_id, action: str, details: str, worker_name=None) -> None:
    try:
        database.log_action(user_id, action, details, _client_ip(), worker_name=worker_name)
    except Exception as exc:
        print(f"audit log failed ({action}): {exc}", flush=True)


def _resolve_worker_name(provided_name: str, ip_address: str) -> str:
    if provided_name and provided_name.startswith("stress_worker_"):
        return provided_name
    if ip_address and ip_address != "unknown":
        worker = database.get_worker_by_ip(ip_address)
        if worker:
            return worker["worker_name"]
    return provided_name


def _queue_site_master_notify(
    *,
    owner_user_id: Optional[int],
    related_workers: list[str],
    body: str,
) -> list[str]:
    """Enqueue a Windows toast on the assigned user's worker PC(s). Soft-fail."""
    import json

    targets: set[str] = set()
    if owner_user_id:
        try:
            targets.update(database.list_owned_worker_names(int(owner_user_id)))
        except Exception:
            pass
    for name in related_workers or []:
        n = (name or "").strip()
        if n:
            targets.add(n)
    if not targets:
        return []

    payload = json.dumps({
        "title": "Site Master",
        "body": body or "",
    })
    notified: list[str] = []
    for worker_name in sorted(targets):
        try:
            database.create_command(worker_name, "desktop_notify", payload)
            notified.append(worker_name)
        except Exception as exc:
            print(f"desktop_notify queue failed for {worker_name}: {exc}", flush=True)
    return notified


_RUNNABLE_EXTS = {".py", ".pyw", ".bat", ".cmd"}


def _maybe_register_runnable_script(worker_name: str, rel_path: str) -> None:
    """Register runnable uploads immediately so Run/Schedule get a script_id without waiting for sync_scripts."""
    import os
    rel_path = (rel_path or "").replace("\\", "/").strip("/")
    if not rel_path:
        return
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in _RUNNABLE_EXTS:
        return
    name = os.path.basename(rel_path)
    worker = database.get_worker(worker_name) or {}
    root = (worker.get("script_location") or "").rstrip("\\/")
    abs_path = os.path.normpath(os.path.join(root, rel_path.replace("/", os.sep))) if root else rel_path
    database.register_script(worker_name, name, abs_path)


@api_bp.route("/register-worker", methods=["POST"])
def register_worker():
    """
    Register or refresh a worker (heartbeat).
    Body JSON: { "worker_name": "PC220", "state": "idle" }
    """
    data = request.get_json(silent=True) or {}
    worker_name = (data.get("worker_name") or "").strip()
    state = (data.get("state") or "idle").strip()
    if not worker_name:
        return jsonify({"error": "worker_name is required"}), 400
    ip = _client_ip()
    worker_name = _resolve_worker_name(worker_name, ip)

    worker = database.register_worker(worker_name, ip, state)
    return jsonify({"status": "ok", "worker": worker})


@api_bp.route("/sync-scripts", methods=["POST"])
def sync_scripts():
    """
    Sync script list from worker; removes scripts deleted locally.
    Body JSON: {
        "worker_name": "PC220",
        "scripts": [{"script_name": "...", "script_path": "...", "days": 0}, ...]
    }
    """
    data = request.get_json(silent=True) or {}
    worker_name = (data.get("worker_name") or "").strip()
    scripts = data.get("scripts") or []

    if not worker_name:
        return jsonify({"error": "worker_name is required"}), 400
    ip = _client_ip()
    worker_name = _resolve_worker_name(worker_name, ip)

    database.touch_worker(worker_name, ip)
    
    # Use the bulk registration method to process potentially 10k+ scripts efficiently
    registered_count = database.register_scripts_bulk(worker_name, scripts)

    # Extract names for the removal step
    names = [s.get("script_name").strip() for s in scripts if s.get("script_name") and s.get("script_name").strip()]
    removed = database.remove_scripts_not_in_list(worker_name, names)
    
    return jsonify({"status": "ok", "registered": registered_count, "removed": removed})


@api_bp.route("/api/worker-scripts", methods=["GET"])
def api_worker_scripts():
    """Worker-facing: list registered script paths so days can be refreshed outside SCRIPTS_DIR."""
    worker_name = (request.args.get("worker_name") or "").strip()
    if not worker_name:
        return jsonify({"error": "worker_name is required"}), 400
    ip = _client_ip()
    worker_name = _resolve_worker_name(worker_name, ip)
    database.touch_worker(worker_name, ip)
    scripts = database.list_worker_script_paths(worker_name)
    return jsonify({"scripts": scripts})


@api_bp.route("/get-job/<worker_name>", methods=["GET"])
def get_job(worker_name):
    """
    Worker polls for the next pending job.
    Returns job payload or empty object.
    """
    worker_name = worker_name.strip()
    if not worker_name:
        return jsonify({}), 400
    ip = _client_ip()
    worker_name = _resolve_worker_name(worker_name, ip)

    database.touch_worker(worker_name, ip)
    job = database.claim_pending_job(worker_name)
    if not job:
        return jsonify({})

    return jsonify(
        {
            "id": job["id"],
            "worker_name": job["worker_name"],
            "script_id": job["script_id"],
            "script_name": job["script_name"],
            "script_path": job["script_path"],
            "status": job["status"],
            "days": job.get("days"),
        }
    )


@api_bp.route("/job-status/<int:job_id>", methods=["GET"])
def get_job_status(job_id):
    job = database.get_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    # Include output so the UI can poll this endpoint for live logs
    return jsonify({"status": job["status"], "output": job.get("output", ""), "duration": job.get("duration")})


@api_bp.route("/job-status-batch", methods=["POST"])
def get_job_status_batch():
    """
    Batch live status for dashboard/worker-detail polling.
    Same fields as /job-status/<id>; one DB round-trip for many jobs.
    Body: {"job_ids": [1,2,3]}
    """
    data = request.get_json(silent=True) or {}
    raw = data.get("job_ids") or data.get("ids") or []
    if not isinstance(raw, list):
        return jsonify({"error": "job_ids must be a list"}), 400
    rows = database.get_jobs_status_batch(raw)
    jobs = {
        str(r["id"]): {
            "status": r.get("status"),
            "output": r.get("output") or "",
            "duration": r.get("duration"),
        }
        for r in rows
    }
    return jsonify({"status": "ok", "jobs": jobs})


@api_bp.route("/job-live-log", methods=["POST"])
def job_live_log():
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    output = data.get("output")
    if job_id and output is not None:
        database.update_job_output(int(job_id), output)
    return jsonify({"status": "ok"})


@api_bp.route("/job-update-pid", methods=["POST"])
def job_update_pid():
    """Worker reports the OS process ID when a job starts executing."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    pid = data.get("pid")

    if not job_id or pid is None:
        return jsonify({"error": "job_id and pid required"}), 400

    job = database.update_job_pid(int(job_id), int(pid))
    if not job:
        return jsonify({"error": "job not found"}), 404

    return jsonify({"status": "ok"})


@api_bp.route("/get-command/<worker_name>", methods=["GET"])
def get_command(worker_name):
    worker_name = worker_name.strip()
    if not worker_name:
        return jsonify({}), 400

    ip = _client_ip()
    worker_name = _resolve_worker_name(worker_name, ip)

    database.touch_worker(worker_name, ip)
    cmd = database.claim_pending_command(worker_name)
    if not cmd:
        return jsonify({})
    return jsonify(cmd)


@api_bp.route("/command-complete", methods=["POST"])
def command_complete():
    data = request.get_json(silent=True) or {}
    cmd_id = data.get("cmd_id")
    status = data.get("status", "completed")
    output = data.get("output", "")

    if not cmd_id:
        return jsonify({"error": "cmd_id required"}), 400

    cmd = database.update_command(int(cmd_id), status, output=str(output))
    if not cmd:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "ok"})


import re
def _categorize_error(exc_type: str, message: str) -> str:
    """Map an exception name + message to a user-facing category."""
    text = (exc_type + " " + message).lower()

    # 1. Element Not Found
    if any(k in text for k in ("nosuchelement", "staleelementreference", "element not found", "not interactable", "unable to locate element")):
        return "Element Not Found"

    # 2. Selenium Driver Error
    if any(k in text for k in ("webdriver", "sessionnotcreated", "selenium", "chromedriver", "geckodriver", "invalid session")):
        return "Selenium Driver Error"

    # 3. Downloading Error
    if any(k in text for k in ("download", "failed file", "failed to download", "download error", "status code: 404", "status code: 403", "status code: 500")):
        return "Downloading Error"

    # 4. Pagination Error (real failures only — not normal end-of-run "next page not found")
    if any(k in text for k in ("pagination failed", "page load failed", "failed to go to next page")):
        return "Pagination Error"

    # 5. Library / Import Error
    if any(k in text for k in ("importerror", "modulenotfounderror", "no module named")):
        return "Library / Import Error"

    # 6. Version Mismatch Error
    if any(k in text for k in ("version mismatch", "chromedriver version", "this version of chromedriver")):
        return "Version Mismatch Error"

    # 7. Database Error
    if any(k in text for k in ("operationalerror", "integrityerror", "database", "psycopg2", "postgres", "pyodbc", "sql server", "sqldriverconnect", "login failed for user", "nonetype' object has no attribute 'close")):
        if any(k in text for k in ("insert", "into")):
            return "Data Insert Error"
        return "Database Error"

    # 8. Data Insert Error
    if any(k in text for k in ("insert failed", "failed to insert")):
        return "Data Insert Error"

    # 9. Timeout Error
    if any(k in text for k in ("timeout", "timed out", "timeouterror", "readtimeout")):
        return "Timeout Error"

    # 10. Network / HTTP Error
    if any(k in text for k in ("httperror", "http error", "connectionerror", "urlerror", "requests.exceptions", "bad gateway", "connection refused")):
        return "Network / HTTP Error"

    # 11. File System Error
    if any(k in text for k in ("filenotfounderror", "permissionerror", "oserror", "isadirectoryerror", "notadirectoryerror", "ioerror", "file not found", "edition not found", "login not found", "error reading", "cannot read", "failed to read")):
        return "File System Error"

    # 12. User Interrupted (Ctrl+C / Ctrl+Z / console close / Stop)
    if any(k in text for k in (
        "keyboardinterrupt", "eoferror", "ctrl+c", "ctrl + c", "ctrl+z", "ctrl + z",
        "stopped by user", "terminated on worker pc", "interrupted by user", "stop requested",
    )):
        return "User Interrupted"

    # 13. Email / Notification
    if any(k in text for k in ("failed to send email", "smtplib", "smtp", "email notification", "recipient address rejected")):
        return "Email / Notification Error"

    # 14. Python Script Error
    if any(k in text for k in ("indexerror", "attributeerror", "typeerror", "valueerror", "keyerror", "nameerror", "syntaxerror", "indentationerror", "runtimeerror", "assertionerror")):
        return "Python Script Error"

    # 15. Script business failure
    if any(k in text for k in ("script failed",)):
        return "Script Runtime Error"

    return "Unknown Error"


def _is_benign_log_line(line: str) -> bool:
    """Normal scraper end/progress lines that must not inflate error_count."""
    low = (line or "").strip().lower()
    if not low:
        return True
    benign = (
        "next page not found",
        "no next button found",
        "ending pagination",
        "no more pages",
        "could not capture image url",
        "edition loaded, waiting for page load",
        "waiting for background downloads",
        "driver closed",
        "[job started]",
        "image downloaded",
        "image saved",
        "file downloaded",
        "set page load timeout",
        "implicitly_wait",
        "page load strategy",
    )
    return any(b in low for b in benign)


def _is_false_positive_error_mention(line: str) -> bool:
    """Lines that contain 'error' / 'timeout' but are not real failures."""
    low = (line or "").strip().lower()
    if not low:
        return True
    if re.search(r"\b(?:no|zero|without)\s+errors?\b", low):
        return True
    if re.search(r"\berrors?\s*[:=]\s*0\b", low) or re.search(r"\b0\s+errors?\b", low):
        return True
    fps = (
        "errorhandler",
        "onerror",
        "error_count",
        "error_details",
        "error_type",
        "ignore error",
        "suppress error",
        "logging.error",
        "logger.error",
        "except exception",
        "except error",
        "catch error",
    )
    return any(fp in low for fp in fps)


def _line_looks_like_real_error(line: str) -> bool:
    """Strict keyword match — bare 'error'/'timeout' alone must not count."""
    stripped = (line or "").strip()
    if not stripped or _is_benign_log_line(stripped) or _is_false_positive_error_mention(stripped):
        return False
    low = stripped.lower()
    if low.startswith("traceback (most recent call last)"):
        return False
    # Soft "error processing element" → warning path handled by caller
    if "error processing element" in low and "traceback" not in low:
        return False
    strong = (
        r"\berror\s*:",
        r"\bexception\s*:",
        r"failed to download",
        r"failed to send",
        r"error downloading",
        r"download_pdf failed",
        r"error in download_pdf",
        r"sessionnotcreated",
        r"not interactable",
        r"script failed",
        r"edition not found",
        r"login not found",
        r"status code:\s*[45]\d\d",
        r"\b[\w.]*(?:Error|Exception)\b\s*:",
        r"timed?\s*out",
        r"timeouterror",
        r"readtimeout",
    )
    if any(re.search(p, low) for p in strong):
        return True
    # Allow "failed …" download/send phrasing without requiring colon
    if "failed" in low and any(k in low for k in ("download", "send", "insert", "open", "read", "write", "login")):
        return True
    return False


def _last_exception_is_user_interrupt(output: str = "") -> bool:
    """True when the final exception in the log is KeyboardInterrupt / EOFError."""
    text = output or ""
    low = text.lower()
    if any(k in low for k in (
        "[stopped by user]", "[terminated on worker pc]", "[stop requested from dashboard]",
    )):
        return True
    # Prefer the last Python exception / interrupt line
    matches = list(re.finditer(
        r"(?m)^([\w.]+(?:Error|Exception|Interrupt|Warning))\b",
        text,
    ))
    if matches:
        last = matches[-1].group(1).rsplit(".", 1)[-1].lower()
        if last in ("keyboardinterrupt", "eoferror"):
            return True
    return bool(re.search(r"(?i)\b(?:keyboardinterrupt|eoferror)\b", text[-1200:]))


def parse_execution_log(output: str) -> dict:
    """Parse raw script output into structured error details."""
    if not output:
        return {}

    details = {
        "errors": [],
        "failed_files": [],
        "missing_files": [],
        "warnings": [],
    }

    def _append_tb_error(tb: str) -> None:
        lines = [ln for ln in tb.strip().split("\n") if ln.strip()]
        last_line = lines[-1] if lines else ""
        err_type = "Exception"
        err_msg = last_line
        # Prefer a real exception line if present (truncated logs often end mid-frame)
        for ln in reversed(lines):
            s = ln.strip()
            if re.match(r"^[\w.]*(?:Error|Exception|Warning|Interrupt)\b", s) or (
                ":" in s and re.search(r"(Error|Exception|Warning|Interrupt)\b", s.split(":", 1)[0])
            ):
                last_line = s
                break
        if ":" in last_line:
            parts = last_line.split(":", 1)
            err_type = parts[0].strip()
            err_msg = parts[1].strip()
        elif last_line.lower().startswith("traceback") or last_line.lower().startswith("[stopped]"):
            # Truncated traceback — categorize from body (chromedriver / selenium / etc.)
            err_type = "Exception"
            body_lines = [ln for ln in lines if not ln.strip().lower().startswith("[stopped]")]
            err_msg = " ".join(body_lines[-5:])[:500] if body_lines else last_line[:500]

        src_file = ""
        line_no = ""
        file_matches = re.findall(r'File "([^"]+)", line (\d+)', tb)
        if file_matches:
            def is_lib(fpath):
                low = fpath.lower()
                if "site-packages" in low or "\\lib\\" in low or "/lib/" in low:
                    return True
                lib_files = ("errorhandler.py", "webdriver.py", "connectionpool.py", "app.py", "utils.py", "socket.py", "__init__.py")
                for lf in lib_files:
                    if low.endswith(lf) or low.endswith("/" + lf) or low.endswith("\\" + lf):
                        return True
                return False

            user_files = [m for m in file_matches if not is_lib(m[0])]
            if user_files:
                src_file = user_files[-1][0]
                line_no = user_files[-1][1]
            else:
                src_file = file_matches[-1][0]
                line_no = file_matches[-1][1]

        details["errors"].append({
            "error_type": _categorize_error(err_type, err_msg + " " + tb[:800]),
            "error_title": err_type if not err_type.lower().startswith("traceback") else _categorize_error(err_type, err_msg + " " + tb[:800]),
            "error_message": err_msg[:500],
            "source_file": src_file,
            "line_number": line_no,
            "traceback": tb.strip()[:8000],
        })
        # Normalize interrupt exception titles for display
        last = details["errors"][-1]
        if (err_type or "").strip().rsplit(".", 1)[-1] in ("KeyboardInterrupt", "EOFError") or (
            "keyboardinterrupt" in (err_msg or "").lower()
        ):
            last["error_type"] = "User Interrupted"
            last["error_title"] = "User Interrupted"

    tb_blocks = re.findall(
        r'(Traceback \(most recent call last\):[\s\S]*?(?:\n[\w.]*(?:Error|Exception|Warning|Interrupt)[^\n]*))',
        output
    )
    for tb in tb_blocks:
        _append_tb_error(tb)

    # Truncated live-log traceback (no final Exception line yet)
    if not details["errors"] and "Traceback (most recent call last):" in output:
        m = re.search(r"Traceback \(most recent call last\):[\s\S]{0,8000}", output)
        if m:
            _append_tb_error(m.group(0))

    # Keyword scan: fills errors when no TB — strict patterns only (avoid false positives)
    for line in output.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _is_benign_log_line(stripped):
            low = stripped.lower()
            if any(k in low for k in ("next page not found", "no next button found", "ending pagination", "could not capture image url")):
                details["warnings"].append(stripped[:500])
            continue
        low = stripped.lower()
        if low.startswith("traceback (most recent call last)"):
            continue  # already handled as TB block
        if "error processing element" in low and "traceback" not in low:
            details["warnings"].append(stripped[:500])
            continue
        if not _line_looks_like_real_error(stripped):
            continue
        has_tb_err = any((e.get("traceback") or "").startswith("Traceback") for e in details["errors"])
        if has_tb_err and not any(
            k in low for k in ("error reading", "failed to download", "failed to send", "status code:", "error downloading")
        ):
            continue
        if len(details["errors"]) < 20:
            cat = _categorize_error("", stripped)
            # Interrupt lines are categorized separately — still record for stopped flows
            details["errors"].append({
                "error_type": cat,
                "error_title": cat,
                "error_message": stripped[:500],
                "source_file": "",
                "line_number": "",
                "traceback": stripped[:2000],
            })

    for m in re.finditer(r'(?i)(?:failed file|failed to download|error file)[:\s=]+([^\r\n]+)', output):
        details["failed_files"].append(m.group(1).strip())
    for m in re.finditer(r'(?i)(?:missing file|file not found)[:\s=]+([^\r\n]+)', output):
        details["missing_files"].append(m.group(1).strip())

    for m in re.finditer(r'(?i)(?:^|\n)\s*(?:warning|warn)[:\s]+([^\r\n]+)', output):
        details["warnings"].append(m.group(1).strip())
        if len(details["warnings"]) >= 20:
            break

    log_failed_downloads = len(re.findall(
        r'(?i)(error downloading|download_pdf failed|error in download_pdf|failed to download|failed to download image)',
        output,
    ))
    details["download_metrics"] = {"log_failed_downloads": log_failed_downloads}

    details["warnings"] = list(dict.fromkeys(details["warnings"]))[:20]
    details["failed_files"] = list(dict.fromkeys(details["failed_files"]))[:50]
    details["missing_files"] = list(dict.fromkeys(details["missing_files"]))[:50]

    if (
        not details["errors"]
        and not details["failed_files"]
        and not details["missing_files"]
        and not details["warnings"]
        and log_failed_downloads == 0
    ):
        return {}

    if details["errors"]:
        details["primary_error_category"] = details["errors"][0].get("error_type") or "Unknown Error"
    elif log_failed_downloads:
        details["primary_error_category"] = "Downloading Error"
    else:
        details["primary_error_category"] = _categorize_error("", output[:500] if output else "")

    details["error_count"] = max(len(details["errors"]), log_failed_downloads)
    details["warning_count"] = len(details["warnings"])
    return details


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
    """Drop controller/worker footer lines so final-outcome detection uses script log."""
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
    """
    True only when the script's *final* outcome is a failure.
    Mid-run errors/tracebacks that recover before exit do not count as failed.
    Manual stop (Ctrl+C / Ctrl+Z / KeyboardInterrupt) is not a script failure.
    """
    text = _strip_system_outcome_noise(output or "")
    low = text.lower()
    if _last_exception_is_user_interrupt(text):
        return False
    tb_i = low.rfind("traceback (most recent call last)")
    ok_i = _rfind_any(text, _SUCCESS_FINISH_MARKERS)

    # Last traceback wins unless a success marker appears after it (recovered run).
    if tb_i >= 0:
        return ok_i < tb_i

    tail = "\n".join([ln for ln in text.splitlines() if ln.strip()][-30:]).lower()
    if any(k in tail for k in (
        "script failed",
        "modulenotfounderror",
        "sessionnotcreated",
    )) and not any(m in tail for m in _SUCCESS_FINISH_MARKERS):
        return True

    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    interrupt = {3221225786, -1073741510, 3221225501, -1073741787, 130, -2}
    if code not in (0, None) and code not in interrupt:
        # Non-zero exit: failed unless the log clearly finished successfully at the end
        if ok_i >= 0 and any(m in tail for m in _SUCCESS_FINISH_MARKERS):
            return False
        if ok_i < 0:
            return True
    return False


def _ended_successfully(output: str = "", exit_code=None) -> bool:
    """True when the run finished successfully (mid-run errors allowed)."""
    text = output or ""
    low = text.lower()
    if text.strip().lower().startswith("[stopped by user]") or "[stop requested from dashboard]" in low:
        return False
    if _last_exception_is_user_interrupt(text):
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
    """Script finished normally at the end (console may still auto-close)."""
    if _last_exception_is_user_interrupt(output or ""):
        return False
    return _ended_successfully(output, exit_code=0) or (
        not _ended_with_failure(output)
        and _rfind_any(_strip_system_outcome_noise(output or ""), _SUCCESS_FINISH_MARKERS) >= 0
        and not (output or "").strip().lower().startswith("[stopped by user]")
        and "[stop requested from dashboard]" not in (output or "").lower()
    )


def _is_user_interrupted(output: str = "", exit_code=None, error_details=None) -> bool:
    """True when the job was cancelled locally (Ctrl+C / Ctrl+Z / console close / Stop)."""
    text = (output or "")
    low = text.lower()

    # Explicit stop / interrupt markers always win (even when a traceback is present)
    if text.startswith("[Stopped by user]") or "[stopped by user]" in low[:120]:
        return True
    if text.startswith("[Terminated on worker PC]") or "[stop requested from dashboard]" in low:
        return True
    if _last_exception_is_user_interrupt(text):
        return True
    if any(k in low for k in (
        "keyboardinterrupt", "ctrl+c", "ctrl + c", "ctrl+z", "ctrl + z",
        "interrupted by user", "stopped by user", "^c", "^z", "eoferror",
    )):
        if not _ended_successfully(text, exit_code) and not _looks_like_clean_finish(text):
            return True

    # Finished successfully (even with mid-run errors) → not stop
    if _ended_successfully(text, exit_code) or _looks_like_clean_finish(text):
        return False
    # Final crash → error, not stop
    if _ended_with_failure(text, exit_code):
        return False

    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in (3221225786, -1073741510, 3221225501, -1073741787, 130, -2):
        return True
    if isinstance(error_details, dict):
        title = str(error_details.get("error_title") or error_details.get("error_type") or "").lower()
        primary = str(error_details.get("primary_error_category") or "").lower()
        blob = title + " " + primary
        if any(k in blob for k in ("ctrl+c", "ctrl+z", "interrupted", "user interrupted", "stopped", "terminated on worker")):
            return True
        for err in error_details.get("errors") or []:
            et = str((err or {}).get("error_type") or (err or {}).get("error_title") or "").lower()
            if "user interrupted" in et or "keyboardinterrupt" in et:
                return True
    return False


def _is_download_related_error(err: dict) -> bool:
    blob = " ".join(str((err or {}).get(k) or "") for k in (
        "error_type", "error_title", "error_message", "traceback",
    )).lower()
    return any(k in blob for k in (
        "download", "failed file", "status code:", "image url", "pdf",
    ))


def _normalize_report_errors_for_status(data: dict, status: str, output: str = "") -> dict:
    """
    Align error_count / error_details with final job status for the reports table.
    - completed: do not treat recovered mid-run issues as Errors
    - stopped: frame as User Interrupted; Errors column = 0
    - error: keep real failures only
    """
    import json
    out = dict(data or {})
    details = out.get("error_details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except Exception:
            details = {}
    if not isinstance(details, dict):
        details = {}

    failed_dl = int(out.get("failed_downloads") or 0)
    if not failed_dl:
        failed_dl = int((details.get("download_metrics") or {}).get("log_failed_downloads") or 0)

    errors = list(details.get("errors") or [])
    status_l = (status or "").lower()

    if status_l == "completed":
        kept = [e for e in errors if _is_download_related_error(e)]
        recovered = [e for e in errors if not _is_download_related_error(e)]
        details["errors"] = kept
        if recovered:
            details["recovered_errors"] = recovered[:20]
        details["error_count"] = max(len(kept), failed_dl)
        out["error_count"] = details["error_count"]
        if kept or failed_dl or details.get("failed_files") or details.get("missing_files"):
            if failed_dl or kept:
                details["primary_error_category"] = "Downloading Error"
            out["error_details"] = details
        else:
            # Successful finish — hide recovered mid-run noise from Details
            out["error_count"] = 0
            if details.get("warnings"):
                out["error_details"] = {
                    "errors": [],
                    "warnings": details.get("warnings") or [],
                    "failed_files": [],
                    "missing_files": [],
                    "warning_count": len(details.get("warnings") or []),
                    "error_count": 0,
                }
            else:
                out["error_details"] = None
        return out

    if status_l == "stopped":
        interrupt_errs = []
        other = []
        for e in errors:
            blob = " ".join(str((e or {}).get(k) or "") for k in (
                "error_type", "error_title", "error_message", "traceback",
            )).lower()
            if "user interrupted" in blob or "keyboardinterrupt" in blob or "eoferror" in blob:
                ne = dict(e or {})
                ne["error_type"] = "User Interrupted"
                ne["error_title"] = "User Interrupted"
                interrupt_errs.append(ne)
            else:
                other.append(e)
        if not interrupt_errs:
            interrupt_errs = [{
                "error_type": "User Interrupted",
                "error_title": "User Interrupted",
                "error_message": "Job stopped manually (Stop / Ctrl+C / Ctrl+Z / console close).",
                "source_file": "",
                "line_number": "",
                "traceback": "",
            }]
        details["errors"] = interrupt_errs
        if other:
            details["mid_run_notes"] = other[:10]
        details["primary_error_category"] = "User Interrupted"
        details["error_type"] = "User Interrupted"
        details["error_title"] = "User Interrupted"
        details["error_count"] = 0
        out["error_count"] = 0
        out["error_details"] = details
        return out

    if status_l == "error":
        # Drop pure interrupt rows if somehow mixed in
        kept = []
        for e in errors:
            blob = " ".join(str((e or {}).get(k) or "") for k in (
                "error_type", "error_title", "error_message", "traceback",
            )).lower()
            if "user interrupted" in blob or "keyboardinterrupt" in blob:
                continue
            kept.append(e)
        details["errors"] = kept or errors
        details["error_count"] = max(len(details["errors"]), failed_dl)
        out["error_count"] = details["error_count"]
        if details.get("errors"):
            details["primary_error_category"] = (
                details["errors"][0].get("error_type") or details.get("primary_error_category") or "Unknown Error"
            )
        out["error_details"] = details
        return out

    return out


def _apply_error_stats(data: dict, output: str) -> tuple:
    """
    Merge worker payload with parse_execution_log so reports show correct
    error_count / warning_count / categorized error_details.
    Prefer structured parse over naive worker word-counts.
    Returns (data, parsed_log). Does not change job status.
    """
    out = dict(data or {})
    parsed = parse_execution_log(output or out.get("output") or "")
    failed_dl = int((parsed.get("download_metrics") or {}).get("log_failed_downloads") or 0)
    parsed_err_n = int(parsed.get("error_count") or len(parsed.get("errors") or []))
    parsed_warn_n = int(parsed.get("warning_count") or len(parsed.get("warnings") or []))
    if parsed:
        # Do not keep inflated worker regex counts (matches "error" in benign lines)
        out["error_count"] = max(parsed_err_n, failed_dl)
        out["warning_count"] = parsed_warn_n if parsed_warn_n else int(out.get("warning_count") or 0)
        if isinstance(out.get("error_details"), dict) and not parsed.get("errors") and not failed_dl:
            merged = dict(out["error_details"])
            merged.setdefault("warnings", parsed.get("warnings") or [])
            merged.setdefault("failed_files", parsed.get("failed_files") or [])
            merged.setdefault("missing_files", parsed.get("missing_files") or [])
            out["error_details"] = merged
        else:
            out["error_details"] = parsed
            if parsed.get("primary_error_category"):
                out["error_details"].setdefault("error_type", parsed["primary_error_category"])
                out["error_details"].setdefault("error_title", parsed["primary_error_category"])
    else:
        out["error_count"] = int(out.get("error_count") or 0)
        out["warning_count"] = int(out.get("warning_count") or 0)
    return out, parsed


def _error_details_json(details):
    import json
    if not details:
        return None
    if isinstance(details, (dict, list)):
        return json.dumps(details)
    return str(details)


def _insert_job_report(full_job: dict, data: dict, status: str, duration=None, failed_downloads: int = 0) -> None:
    """Write scraper_reports row using already-merged counts/details."""
    database.insert_scraper_report(
        worker_name=full_job["worker_name"],
        script_name=full_job.get("script_name", "Unknown"),
        script_id=full_job["script_id"],
        job_id=full_job["id"],
        folder_path=data.get("folder_path", ""),
        status=status,
        start_time=full_job.get("start_time", ""),
        end_time=full_job.get("end_time", ""),
        duration=duration if duration is not None else (data.get("duration") or 0.0),
        image_count=data.get("image_count", 0),
        pdf_count=data.get("pdf_count", 0),
        file_count=data.get("file_count", 0),
        log_count=data.get("log_count", 0),
        warning_count=data.get("warning_count", 0),
        error_count=data.get("error_count", 0),
        total_folder_size=data.get("total_folder_size", 0),
        failed_downloads=failed_downloads,
        error_details=_error_details_json(data.get("error_details")),
    )


def _merge_report_counts(data: dict, output: str = "") -> dict:
    """Prefer worker-supplied counts; fill zeros from the job log."""
    text = output or (data or {}).get("output") or ""
    log_counts = database.counts_from_job_log(text)
    out = dict(data or {})
    has_img_dl = database.log_has_image_download_evidence(text)
    for key in ("pdf_count", "log_count"):
        out[key] = max(int(out.get(key) or 0), int(log_counts.get(key) or 0))
    worker_img = int(out.get("image_count") or 0)
    log_img = int(log_counts.get("image_count") or 0) if has_img_dl else 0
    if has_img_dl:
        out["image_count"] = max(worker_img, log_img)
    else:
        # Do not treat .png/.jpg mentions in the log as downloads
        out["image_count"] = worker_img if worker_img else 0
    out["file_count"] = max(
        int(out.get("file_count") or 0),
        int(out.get("image_count") or 0) + int(out.get("pdf_count") or 0),
        int(log_counts.get("file_count") or 0) if has_img_dl else 0,
    )
    if not out.get("file_count"):
        out["file_count"] = int(out.get("image_count") or 0) + int(out.get("pdf_count") or 0)
    # Surface thin-run signal in total_images when worker omitted it (exit code unchanged).
    if not out.get("total_images") and out.get("image_count"):
        out["total_images"] = int(out["image_count"])
    return out


@api_bp.route("/watchlist/toggle", methods=["POST"])
def toggle_watchlist():
    """Toggle a file/folder in the user's watchlist (worker_name + path)."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    file_path = (data.get("file_path") or "").strip()
    worker_name = (data.get("worker_name") or data.get("worker") or "").strip()

    if not file_path:
        return jsonify({"error": "file_path is required"}), 400

    user_id = session["user_id"]
    added = database.toggle_file_watchlist(user_id, file_path, worker_name=worker_name)

    return jsonify({
        "status": "ok",
        "watchlisted": added,
        "worker_name": worker_name,
        "file_path": file_path.replace("\\", "/").strip().strip("/"),
        "message": f"{'Added to' if added else 'Removed from'} watchlist",
    })

@api_bp.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    """Fetch the enriched watchlist for the current user."""
    from flask import session
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    user_id = session["user_id"]
    worker_name = (request.args.get("worker") or request.args.get("job_worker") or "").strip() or None
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    try:
        items = database.get_enriched_watchlist(
            user_id,
            worker_name=worker_name,
            date_from=date_from,
            date_to=date_to,
        )
        failing = sum(1 for i in items if i.get("status") == "failing")
        return jsonify({
            "status": "ok",
            "items": items,
            "counts": {
                "total": len(items),
                "failing": failing,
                "healthy": len(items) - failing,
            },
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@api_bp.route("/api/watchlist/scheduler-folders", methods=["GET"])
def watchlist_scheduler_folders():
    """Folders derived from scheduler script paths for the watchlist picker."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    worker_name = (request.args.get("worker_name") or request.args.get("worker") or "").strip()
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400

    user_id = session["user_id"]
    is_admin = database.is_admin(user_id)
    perms = database.get_pc_access_details(user_id, worker_name) or {}
    if not perms and not is_admin:
        worker_rec = database.get_worker(worker_name)
        if worker_rec and worker_rec.get("owner_id") == user_id:
            database.ensure_ip_matched_pc_access(user_id, worker_name, granted_by=user_id)
            perms = database.get_pc_access_details(user_id, worker_name) or {"can_run": 1}
        else:
            return jsonify({"error": "no access"}), 403

    worker_root = database.get_worker_script_location(worker_name)
    folders = database.list_scheduler_script_folders(user_id, worker_name, is_admin_user=is_admin)
    if not is_admin:
        folders = [
            f for f in folders
            if database.path_allowed_by_perms(
                f.get("path") or "", perms, worker_root, is_folder=True
            )
        ]
    search = (request.args.get("search") or "").strip().lower()
    if search:
        folders = [
            f for f in folders
            if search in (f.get("path") or "").lower() or search in (f.get("name") or "").lower()
        ]
    return jsonify({"status": "ok", "folders": folders})


@api_bp.route("/api/admin/schedule-tracking", methods=["GET"])
def admin_schedule_tracking():
    """Admin-only: users as containers with Folder Scheduler + individual scripts."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if not database.is_admin(session["user_id"]):
        return jsonify({"error": "Admin only"}), 403

    def _int_arg(name: str) -> Optional[int]:
        raw = (request.args.get(name) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    data = database.list_admin_schedule_tracking(
        user_id=_int_arg("user_id"),
        worker_name=(request.args.get("worker") or "").strip() or None,
        status=(request.args.get("status") or "").strip() or None,
        search=(request.args.get("search") or "").strip() or None,
        folder_name=(request.args.get("folder_name") or "").strip() or None,
        date_from=(request.args.get("date_from") or "").strip() or None,
        date_to=(request.args.get("date_to") or "").strip() or None,
    )
    return jsonify({"status": "ok", **data})


@api_bp.route("/api/admin/schedule-tracking/status", methods=["POST"])
def admin_schedule_tracking_status():
    """Admin-only: manually set Schedule Tracking status on a script or folder."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if not database.is_admin(session["user_id"]):
        return jsonify({"error": "Admin only"}), 403

    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or request.form.get("kind") or "").strip()
    raw_id = payload.get("id") if payload.get("id") is not None else request.form.get("id")
    status = payload.get("status") if "status" in payload else request.form.get("status")
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError):
        return jsonify({"error": "id is required"}), 400

    result = database.set_admin_tracking_status(kind, item_id, status)
    if not result.get("ok"):
        return jsonify({"error": result.get("error") or "Update failed"}), 400

    try:
        database.log_action(
            session["user_id"],
            "schedule_tracking_status",
            f"{kind} #{item_id} → {result.get('tracking_status') or 'auto'}",
        )
    except Exception:
        pass
    return jsonify({"status": "ok", **result})


@api_bp.route("/api/admin/schedule-tracking/chat", methods=["POST"])
def admin_schedule_tracking_chat():
    """Admin-only: send a Schedule Tracking action note to the Chat API."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if not database.is_admin(session["user_id"]):
        return jsonify({"error": "Admin only"}), 403

    from app.services import chat_notify
    from app.services import schedule_folders as sf

    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "").strip().lower()
    try:
        schedule_id = int(payload.get("schedule_id") or 0)
    except (TypeError, ValueError):
        schedule_id = 0
    try:
        folder_id = int(payload.get("folder_id") or payload.get("id") or 0)
    except (TypeError, ValueError):
        folder_id = 0
    if kind == "folder" or (not schedule_id and folder_id):
        kind = "folder"
    else:
        kind = "script"
        folder_id = 0
    if kind == "script" and not schedule_id:
        return jsonify({"error": "schedule_id is required"}), 400
    if kind == "folder" and not folder_id:
        return jsonify({"error": "folder_id is required"}), 400

    action = chat_notify.normalize_action(payload.get("action"))
    if not action:
        return jsonify({"error": "Invalid action"}), 400

    note = str(payload.get("message") or payload.get("text") or "").strip()
    sender = database.get_user_by_id(session["user_id"]) or {}
    sender_name = sender.get("username") or "admin"

    if kind == "folder":
        folder = sf.get_folder(folder_id)
        if not folder:
            return jsonify({"error": "Folder not found"}), 404
        owner = database.get_user_by_id(int(folder.get("user_id") or 0)) or {}
        assigned = (owner.get("username") or "").strip()
        items = sf.list_folder_items(folder_id, is_admin=True)
        workers = sorted({
            (it.get("worker_name") or "").strip()
            for it in items
            if (it.get("worker_name") or "").strip()
        })
        text = chat_notify.build_folder_message(
            action=action,
            note=note,
            folder_name=folder.get("name") or "",
            folder_id=int(folder["id"]),
            script_count=len(items),
            worker_name=", ".join(workers),
            assigned_user=assigned,
            sender=sender_name,
        )
        sent = chat_notify.send_schedule_chat(text=text, assigned_user=assigned)
        if not sent.get("ok"):
            return jsonify({"error": sent.get("error") or "Chat send failed"}), 502
        tracking = chat_notify.ACTION_TO_TRACKING.get(action)
        status_result = None
        if tracking:
            status_result = database.set_admin_tracking_status("folder", folder_id, tracking)
            try:
                database.log_action(
                    session["user_id"],
                    "schedule_tracking_chat",
                    f"folder #{folder_id} {action} → chat channel {sent.get('channel')}",
                )
            except Exception:
                pass
        notify_body = chat_notify.build_desktop_notify_body(
            action=action,
            note=note,
            subject=f"Folder: {folder.get('name') or folder_id}",
            assigned_user=assigned,
            sender=sender_name,
        )
        notified = _queue_site_master_notify(
            owner_user_id=int(folder.get("user_id") or 0) or None,
            related_workers=workers,
            body=notify_body,
        )
        return jsonify({
            "status": "ok",
            "kind": "folder",
            "action": action,
            "tracking_status": (status_result or {}).get("tracking_status") or tracking,
            "channel_ok": sent.get("channel_ok"),
            "dm_ok": sent.get("dm_ok"),
            "notified_workers": notified,
        })

    sch = database.get_schedule(schedule_id)
    if not sch or int(sch.get("is_deleted") or 0):
        return jsonify({"error": "Schedule not found"}), 404

    assigned = (sch.get("username") or "").strip()
    text = chat_notify.build_message(
        action=action,
        note=note,
        script_name=sch.get("script_name") or "",
        schedule_id=int(sch["id"]),
        worker_name=sch.get("worker_name") or "",
        assigned_user=assigned,
        sender=sender_name,
    )
    sent = chat_notify.send_schedule_chat(text=text, assigned_user=assigned)
    if not sent.get("ok"):
        return jsonify({"error": sent.get("error") or "Chat send failed"}), 502

    tracking = chat_notify.ACTION_TO_TRACKING.get(action)
    status_result = None
    if tracking:
        status_result = database.set_admin_tracking_status("script", schedule_id, tracking)
        try:
            database.log_action(
                session["user_id"],
                "schedule_tracking_chat",
                f"#{schedule_id} {action} → chat channel {sent.get('channel')}",
                worker_name=sch.get("worker_name"),
            )
        except Exception:
            pass

    notify_body = chat_notify.build_desktop_notify_body(
        action=action,
        note=note,
        subject=f"Script: {sch.get('script_name') or schedule_id}",
        assigned_user=assigned,
        sender=sender_name,
    )
    notified = _queue_site_master_notify(
        owner_user_id=int(sch.get("user_id") or 0) or None,
        related_workers=[(sch.get("worker_name") or "").strip()],
        body=notify_body,
    )

    return jsonify({
        "status": "ok",
        "kind": "script",
        "action": action,
        "tracking_status": (status_result or {}).get("tracking_status") or tracking,
        "channel_ok": sent.get("channel_ok"),
        "dm_ok": sent.get("dm_ok"),
        "notified_workers": notified,
    })


@api_bp.route("/job-complete", methods=["POST"])
def job_complete():
    """Mark job as completed with output."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    output = data.get("output", "")
    duration = data.get("duration")
    exit_code = data.get("exit_code")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    data = _merge_report_counts(data, output)
    data, parsed_log = _apply_error_stats(data, output)
    total_images = data.get("total_images") or data.get("image_count")
    output_count = data.get("output_count") or data.get("file_count")
    failed_downloads = int((parsed_log.get("download_metrics") or {}).get("log_failed_downloads") or 0)

    # Final outcome only: mid-run errors still counted, but status follows the end.
    out_s = str(output)
    if _is_user_interrupted(out_s, exit_code, data.get("error_details")):
        status = "stopped"
    elif _ended_with_failure(out_s, exit_code):
        status = "error"
        if exit_code in (0, None):
            exit_code = 1
    else:
        status = "completed"
        if exit_code not in (0, None):
            try:
                if int(exit_code) in (3221225786, -1073741510, 3221225501, -1073741787, 130, -2):
                    exit_code = 0
            except (TypeError, ValueError):
                pass

    data["failed_downloads"] = failed_downloads
    data = _normalize_report_errors_for_status(data, status, out_s)

    job = database.update_job(
        int(job_id), status,
        output=out_s, duration=duration,
        total_images=total_images, output_count=output_count,
        exit_code=exit_code,
    )
    if not job:
        return jsonify({"error": "job not found"}), 404

    full_job = database.get_job(int(job_id))
    if full_job:
        _insert_job_report(full_job, data, status, duration=duration or 0.0, failed_downloads=failed_downloads)

    return jsonify({"status": "ok", "job": job})


@api_bp.route("/job-error", methods=["POST"])
def job_error():
    """Mark job as failed with error output."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    output = data.get("output", "")
    duration = data.get("duration")
    exit_code = data.get("exit_code")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    data = _merge_report_counts(data, output)
    data, parsed_log = _apply_error_stats(data, output)
    failed_downloads = int((parsed_log.get("download_metrics") or {}).get("log_failed_downloads") or 0)

    # Manual stop on PC (Ctrl+C / Ctrl+Z / console close) must not stay as running/error
    out_s = str(output)
    if _is_user_interrupted(out_s, exit_code, data.get("error_details")):
        status = "stopped"
    elif _ended_successfully(out_s, exit_code) or (
        not _ended_with_failure(out_s, exit_code) and _looks_like_clean_finish(out_s)
    ):
        status = "completed"
        if exit_code not in (0, None):
            exit_code = 0
    else:
        status = "error"

    data["failed_downloads"] = failed_downloads
    data = _normalize_report_errors_for_status(data, status, out_s)

    job = database.update_job(
        int(job_id), status,
        output=out_s, duration=duration,
        exit_code=exit_code,
    )
    if not job:
        return jsonify({"error": "job not found"}), 404

    full_job = database.get_job(int(job_id))
    if full_job:
        _insert_job_report(full_job, data, status, duration=duration or 0.0, failed_downloads=failed_downloads)

    return jsonify({"status": "ok", "job": job})


@api_bp.route("/job-stopped", methods=["POST"])
def job_stopped():
    """Mark job as stopped (canceled by user)."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    output = data.get("output", "")
    exit_code = data.get("exit_code")
    duration = data.get("duration")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    data = _merge_report_counts(data, output)
    data, parsed_log = _apply_error_stats(data, output)
    failed_downloads = int((parsed_log.get("download_metrics") or {}).get("log_failed_downloads") or 0)

    out_s = str(output)
    # Interrupt / Stop must win over traceback-as-failure (KeyboardInterrupt ends with a TB)
    if out_s.strip().lower().startswith("[stopped by user]") or "[stop requested from dashboard]" in out_s.lower():
        status = "stopped"
    elif _is_user_interrupted(out_s, exit_code, data.get("error_details")):
        status = "stopped"
    elif _ended_with_failure(out_s, exit_code):
        status = "error"
        if exit_code in (0, None):
            exit_code = 1
    elif _ended_successfully(out_s, exit_code) or _looks_like_clean_finish(out_s):
        status = "completed"
        exit_code = 0 if exit_code in (None, 3221225786, -1073741510, 3221225501, -1073741787, 130, -2) else exit_code
    else:
        # Unknown auto-close without clean markers — keep stopped (prior behavior)
        status = "stopped"

    data["failed_downloads"] = failed_downloads
    data = _normalize_report_errors_for_status(data, status, out_s)

    job = database.update_job(
        int(job_id), status,
        output=out_s, exit_code=exit_code, duration=duration,
    )
    if not job:
        return jsonify({"error": "job not found"}), 404

    full_job = database.get_job(int(job_id))
    if full_job:
        _insert_job_report(
            full_job, data, status,
            duration=duration if duration is not None else (data.get("duration") or 0.0),
            failed_downloads=failed_downloads,
        )

    return jsonify({"status": "ok", "job": job})

@api_bp.route("/job-paused", methods=["POST"])
def job_paused():
    """Worker confirms it has suspended the process."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    job = database.get_job(int(job_id))
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"status": "ok"})


@api_bp.route("/job-resumed", methods=["POST"])
def job_resumed():
    """Worker confirms it has resumed the process."""
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    job = database.get_job(int(job_id))
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"status": "ok"})


@api_bp.route("/auto-resume-stale", methods=["POST"])
def auto_resume_stale():
    """Auto-resume jobs that have been paused for more than 10 minutes."""
    stale_jobs = database.get_stale_paused_jobs(timeout_minutes=10)
    resumed = 0
    for job in stale_jobs:
        database.resume_job(job["id"])
        resumed += 1
    return jsonify({"status": "ok", "resumed": resumed})


@api_bp.route("/reconcile-running-jobs", methods=["POST"])
def reconcile_running_jobs():
    """
    Worker reports job IDs it is actively executing.
    Any other 'running' jobs for that worker (past grace) are marked stopped.
    Also expires very old pending jobs (offline worker queues) so they do not stick forever.
    Fixes dashboard stuck on running after manual Ctrl+C / console close.
    """
    data = request.get_json(silent=True) or {}
    worker_name = (data.get("worker_name") or "").strip()
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400
    raw_ids = data.get("job_ids") or data.get("active_job_ids") or []
    try:
        job_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "job_ids must be integers"}), 400
    grace = data.get("grace_seconds")
    try:
        grace_i = int(grace) if grace is not None else 45
    except (TypeError, ValueError):
        grace_i = 45
    stopped = database.reconcile_orphaned_running_jobs(
        worker_name, job_ids, grace_seconds=max(15, grace_i)
    )
    # Default 2h — pending for offline workers (e.g. Ayush) no longer stick forever.
    pending_age = data.get("pending_max_age_seconds")
    try:
        pending_age_i = int(pending_age) if pending_age is not None else 7200
    except (TypeError, ValueError):
        pending_age_i = 7200
    expired = database.reconcile_stale_pending_jobs(
        max_age_seconds=max(300, pending_age_i),
        worker_name=None,
    )
    return jsonify({
        "status": "ok",
        "stopped": stopped,
        "stopped_count": len(stopped),
        "expired_pending": expired,
        "expired_pending_count": len(expired),
    })


@api_bp.route("/api/workers", methods=["GET"])
def api_workers():
    """JSON list of workers (dashboard live refresh)."""
    return jsonify({"workers": database.list_workers()})


@api_bp.route("/api/stats", methods=["GET"])
def api_stats():
    """Aggregate counts for dashboard stats cards."""
    workers = database.list_workers()
    online = sum(1 for w in workers if w["status"] == "online")
    job_counts = database.get_job_counts()
    return jsonify({
        "workers_total": len(workers),
        "workers_online": online,
        "workers_offline": len(workers) - online,
        "jobs": job_counts,
    })


# --- File Explorer API ---

@api_bp.route("/api/sync-file-tree", methods=["POST"])
def sync_file_tree():
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name")
    
    db_worker = database.get_worker_by_ip(request.remote_addr)
    if db_worker:
        worker_name = db_worker["worker_name"]
    tree = data.get("tree", [])
    batch_index = data.get("batch_index", 0)
    is_last_batch = data.get("is_last_batch", True)
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400

    tree_root = (data.get("tree_root") or data.get("script_location") or "").strip()
    if tree_root:
        configured = database.get_worker_script_location(worker_name)
        if database._norm_fs_path(tree_root).rstrip("/").lower() != database._norm_fs_path(configured).rstrip("/").lower():
            return jsonify({"status": "ok", "batch_index": batch_index, "ignored": True, "reason": "tree_root_mismatch"})

    sync_state = database.get_worker_tree_sync_state(worker_name)
    expected_batch = int(sync_state.get("next_batch") or 0)
    # Path-change resets next_batch to 0. Ignore leftover batches from a previous scan.
    if int(batch_index or 0) != expected_batch:
        return jsonify({"status": "ok", "batch_index": batch_index, "ignored": True, "reason": "stale_batch"})

    # On the first batch, clear the old tree
    if batch_index == 0:
        database.clear_worker_file_tree(worker_name)
        if sync_state.get("status") != "syncing":
            database.mark_worker_tree_sync_started(worker_name, reset=False)
        database.mark_worker_tree_sync_uploading(worker_name)

    # Bulk insert this batch
    database.bulk_insert_worker_file_tree(worker_name, tree)

    items_so_far = data.get("total_items")
    if items_so_far is None:
        items_so_far = data.get("items_so_far")
    try:
        items_so_far = int(items_so_far) if items_so_far is not None else None
    except (TypeError, ValueError):
        items_so_far = None
    if items_so_far is None:
        state = database.get_worker_tree_sync_state(worker_name)
        items_so_far = int(state.get("item_count") or 0) + len(tree)
    database.mark_worker_tree_sync_progress(worker_name, items_so_far)

    if is_last_batch:
        elapsed_s = data.get("elapsed_s")
        try:
            elapsed_s = float(elapsed_s) if elapsed_s is not None else None
        except (TypeError, ValueError):
            elapsed_s = None
        database.mark_worker_tree_sync_finished(worker_name, items_so_far, elapsed_s)

    database.bump_worker_tree_sync_batch(worker_name, batch_index)

    return jsonify({"status": "ok", "batch_index": batch_index, "inserted": len(tree)})

@api_bp.route("/api/sync-folder-partial", methods=["POST"])
def sync_folder_partial():
    """
    Partial folder sync from worker.
    Backward compatible:
      - single: {worker_name, folder_path, contents}
      - batch:  {worker_name, folders: [{folder_path, contents}, ...]}
    """
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name")

    db_worker = database.get_worker_by_ip(request.remote_addr)
    if db_worker:
        worker_name = db_worker["worker_name"]

    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400

    folders = data.get("folders")
    if isinstance(folders, list) and folders:
        database.sync_folders_partial_batch_db(worker_name, folders)
        return jsonify({"status": "ok", "folders": len(folders)})

    folder_path = data.get("folder_path", "")
    contents = data.get("contents", [])
    database.sync_folder_partial_db(worker_name, folder_path, contents)
    return jsonify({"status": "ok"})

@api_bp.route("/files/list", methods=["GET"])
def files_list():
    worker_name = request.args.get("worker_name", "").strip()
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400
    search = request.args.get("search", "").strip().lower()
    file_type = request.args.get("type", "").strip().lower()
    base_path = request.args.get("base_path", "").strip()

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    perms = database.get_pc_access_details(user_id, worker_name) or {}
    is_admin = database.is_admin(user_id)
    if not perms and not is_admin:
        # IP-matched owner may have ownership before PC row is created — ensure default Run grant
        worker_rec = database.get_worker(worker_name)
        if worker_rec and worker_rec.get("owner_id") == user_id:
            database.ensure_ip_matched_pc_access(user_id, worker_name, granted_by=user_id)
            perms = database.get_pc_access_details(user_id, worker_name) or {"can_run": 1}
        else:
            return jsonify({"error": "no access"}), 403

    frontend_perms = database.build_frontend_pc_perms(user_id, worker_name, perms)
    access_all = frontend_perms["can_access_all_files"]
    pc_can_run = frontend_perms.get("can_run", False)

    worker_rec = database.get_worker(worker_name)
    worker_root = (worker_rec.get("script_location") if worker_rec else "C:\\Automation\\scripts") or "C:\\Automation\\scripts"
    worker_root = worker_root.replace("\\", "/")

    # Lightweight id/path only — explorer only needs script_id for Run mapping
    accessible_scripts = database.list_script_paths_for_explorer(user_id, worker_name, is_admin_user=is_admin)

    # Pre-compute script path lookup (case-insensitive). Prefer longer/more specific keys
    # so folder/a.py and folder/b/a.py don't collide on basename-only matches incorrectly.
    script_path_map = {}
    for s in accessible_scripts:
        s_path = s["script_path"].replace("\\", "/")
        can_run = 1 if (is_admin or pc_can_run) else 0
        info = {"id": s["id"], "can_run": can_run}
        keys = [s_path]
        parts = s_path.split("/")
        for i in range(len(parts)):
            keys.append("/".join(parts[i:]))
        if worker_root:
            root = worker_root.rstrip("/")
            if s_path.lower().startswith(root.lower() + "/"):
                keys.append(s_path[len(root) + 1 :])
        for key in keys:
            if not key:
                continue
            k = key.lower()
            # Keep first mapping for a key; longer relative paths are added as their own keys
            if k not in script_path_map:
                script_path_map[k] = info

    import os
    starred_paths = database.get_starred_files(user_id, worker_name)
    starred_set = set([p.replace("\\", "/") for p in starred_paths])

    if base_path and base_path != "/":
        parent_path = base_path.strip("/").replace("\\", "/")
    else:
        parent_path = ""

    starred_only = request.args.get("starred", "").lower() == "true"
    recursive = request.args.get("recursive", "").lower() in ("1", "true", "yes")

    def _norm_tree_path(p: str) -> str:
        return (p or "").replace("\\", "/").strip().strip("/").lower()

    starred_norm = {_norm_tree_path(p) for p in starred_set}

    def _file_matches_type(name: str, ft: str) -> bool:
        ft = (ft or "").strip().lstrip(".").lower()
        if not ft:
            return True
        if ft == "folder":
            return False
        n = (name or "").lower()
        return n.endswith("." + ft)

    if starred_only:
        all_files = []
        if starred_set:
            all_files = database.get_worker_file_tree_by_paths(worker_name, list(starred_set))
            if search:
                all_files = [
                    f for f in all_files
                    if search in (f.get("name") or "").lower()
                    or search in (f.get("path") or "").replace("\\", "/").lower()
                ]
            if file_type:
                if file_type == "folder":
                    all_files = [f for f in all_files if f["type"] == "folder"]
                else:
                    all_files = [
                        f for f in all_files
                        if f["type"] == "file" and _file_matches_type(f.get("name") or "", file_type)
                    ]
    elif recursive and file_type == "folder" and not search:
        # Full folder tree (watchlist picker) — additive; default list stays one level
        all_files = database.list_all_worker_folders(worker_name)
    elif search:
        # Global search across the whole tree
        all_files = database.get_worker_file_tree_folder(
            worker_name,
            parent_path=None,
            search=search,
            file_type=file_type
        )
    else:
        all_files = database.get_worker_file_tree_folder(
            worker_name,
            parent_path=parent_path,
            search="",
            file_type=file_type
        )

    total_files = None
    total_size = None
    include_stats = request.args.get("include_stats", "").lower() in ("1", "true", "yes")
    need_empty_check = (not all_files and not search and not file_type and not starred_only)
    tree_sync = database.get_worker_tree_sync_state(worker_name)
    tree_loading = tree_sync.get("status") in ("syncing", "uploading")

    if include_stats or need_empty_check:
        if tree_loading:
            entry_count = int(tree_sync.get("item_count") or 0)
            total_files = entry_count
            total_size = 0
        else:
            total_files, total_size, entry_count = database.get_worker_file_tree_stats(worker_name)
        if need_empty_check and entry_count == 0 and tree_sync.get("status") != "complete":
            return jsonify({
                "error": "Worker file tree not synced yet. Waiting for worker to scan files...",
                "worker_root": worker_root,
                "permissions": frontend_perms,
                "total_files": 0,
                "total_size": 0,
                "tree_sync": tree_sync,
            })

    filtered_files = []
    # Folder browse only — avoid mass register on search/starred/recursive picks
    lazy_ensure_scripts = (
        not search and not starred_only and not recursive and (pc_can_run or is_admin)
    )
    for f in all_files:
        path = f["path"].replace("\\", "/")
        f["path"] = path
        is_folder = f["type"] == "folder"
        if not is_admin and not access_all:
            if not database.path_allowed_by_perms(path, perms, worker_root, is_folder=is_folder):
                continue
            if not is_folder and not database.extension_allowed_by_perms(path, perms):
                continue
        if is_folder:
            filtered_files.append(f)
        else:
            info = script_path_map.get(path.lower())
            if info:
                f["script_id"] = info["id"]
                f["can_run"] = bool(info["can_run"])
            elif pc_can_run or is_admin:
                # Runnable types can still be run via ensure-on-run (basename collisions / lag)
                name_l = (f.get("name") or "").lower()
                if name_l.endswith((".py", ".pyw", ".bat", ".cmd")):
                    f["can_run"] = True
                    # After path change, tree rows appear before sync_scripts — register
                    # visible .py now so Schedule gets a script_id without waiting.
                    if lazy_ensure_scripts and name_l.endswith((".py", ".pyw")):
                        try:
                            ensured = database.ensure_script_for_worker_file_path(worker_name, path)
                            if ensured and ensured.get("id") is not None:
                                f["script_id"] = int(ensured["id"])
                                script_path_map[path.lower()] = {
                                    "id": int(ensured["id"]),
                                    "can_run": 1,
                                }
                        except Exception:
                            pass
            filtered_files.append(f)

    for f in filtered_files:
        if f["type"] == "file":
            f["is_starred"] = _norm_tree_path(f["path"]) in starred_norm
        elif starred_only:
            f["is_starred"] = False

    resp = jsonify({
        "status": "ok",
        "files": filtered_files,
        "permissions": frontend_perms,
        "worker_root": worker_root,
        "current_path": parent_path,
        "total_files": total_files,
        "total_size": total_size,
        "tree_sync": tree_sync,
        "filters": {
            "search": search or "",
            "type": file_type or "",
            "starred": starred_only,
        },
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@api_bp.route("/files/ensure-script", methods=["POST"])
def files_ensure_script():
    """Ensure a File Explorer .py path is registered so Schedule can open with script_id."""
    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    data = request.get_json(silent=True) or {}
    worker_name = (data.get("worker_name") or "").strip()
    file_path = (data.get("file_path") or "").replace("\\", "/").strip().strip("/")
    if not worker_name or not file_path:
        return jsonify({"error": "worker_name and file_path required"}), 400

    name_l = file_path.rsplit("/", 1)[-1].lower()
    if not name_l.endswith((".py", ".pyw")):
        return jsonify({"error": "only Python scripts can be scheduled"}), 400

    is_admin = database.is_admin(user_id)
    perms = database.get_pc_access_details(user_id, worker_name) or {}
    if not perms and not is_admin:
        worker_rec = database.get_worker(worker_name)
        if worker_rec and worker_rec.get("owner_id") == user_id:
            database.ensure_ip_matched_pc_access(user_id, worker_name, granted_by=user_id)
            perms = database.get_pc_access_details(user_id, worker_name) or {"can_run": 1}
        else:
            return jsonify({"error": "no access"}), 403

    frontend_perms = database.build_frontend_pc_perms(user_id, worker_name, perms)
    if not is_admin and not frontend_perms.get("can_run"):
        return jsonify({"error": "Run Script permission required to schedule"}), 403

    root = database.get_worker_script_location(worker_name)
    if not is_admin:
        if not database.path_allowed_by_perms(file_path, perms, root, is_folder=False):
            return jsonify({"error": "path not allowed"}), 403
        if not database.extension_allowed_by_perms(file_path, perms):
            return jsonify({"error": "extension not allowed"}), 403

    script = database.ensure_script_for_worker_file_path(worker_name, file_path)
    if not script or script.get("id") is None:
        return jsonify({"error": "could not register script"}), 500
    return jsonify({
        "status": "ok",
        "script_id": int(script["id"]),
        "script_name": script.get("script_name") or name_l,
    })


@api_bp.route("/files/star", methods=["POST"])
def toggle_star():
    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    
    data = request.get_json() or {}
    worker_name = data.get("worker_name", "").strip()
    file_path = data.get("file_path", "").strip()
    if not worker_name or not file_path:
        return jsonify({"error": "worker_name and file_path required"}), 400

    perms = database.get_pc_access_details(user_id, worker_name)
    if not database.is_admin(user_id) and not perms:
        return jsonify({"error": "permission denied"}), 403
    # Star requires visibility of the path (path + extension), not a mutate flag
    if not database.is_admin(user_id):
        root = database.get_worker_script_location(worker_name)
        if not database.path_allowed_by_perms(file_path, perms, root, is_folder=False):
            return jsonify({"error": "path not allowed"}), 403
        if not database.extension_allowed_by_perms(file_path, perms):
            return jsonify({"error": "extension not allowed"}), 403
        
    is_starred = database.toggle_starred_file(user_id, worker_name, file_path)
    return jsonify({"status": "ok", "starred": is_starred})

@api_bp.route("/files/create_folder", methods=["POST"])
def files_create_folder():
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    parent_path = data.get("parent_path", "/").strip()
    folder_name = data.get("folder_name", "").strip()
    if not all([worker_name, folder_name]):
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import os, json
    target_path = os.path.join(parent_path, folder_name).replace("\\", "/").lstrip("/")
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_create_folder", target_path,
        is_folder=True, check_ext=False,
    )
    if err:
        return jsonify({"error": err}), 403
    payload = {"target_path": target_path}
    database.create_command(worker_name, "create_folder", json.dumps(payload))
    # Optimistic DB update so explorer reflects the change immediately
    try:
        database.upsert_worker_file_tree_entry(worker_name, target_path, entry_type="folder")
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, target_path, "create_folder")
    return jsonify({"status": "ok"})

@api_bp.route("/files/rename_folder", methods=["POST"])
def files_rename_folder():
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    folder_path = data.get("folder_path", "").strip()
    new_name = data.get("new_name", "").strip()
    if not all([worker_name, folder_path, new_name]):
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import json, posixpath
    source = folder_path.lstrip("/")
    parent = posixpath.dirname(source)
    new_path = f"{parent}/{new_name}".strip("/") if parent else new_name
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_rename_folder", source, new_path,
        is_folder=True, check_ext=False,
    )
    if err:
        return jsonify({"error": err}), 403
    payload = {"source_path": source, "new_name": new_name}
    database.create_command(worker_name, "rename_folder", json.dumps(payload))
    try:
        database.rename_worker_file_tree_entry(worker_name, source, new_path, is_folder=True)
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, new_name, "rename_folder", old_content=folder_path)
    return jsonify({"status": "ok"})

@api_bp.route("/files/delete_folder", methods=["POST"])
def files_delete_folder():
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    folder_path = data.get("folder_path", "").strip()
    if not all([worker_name, folder_path]):
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import json
    path = folder_path.lstrip("/")
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_delete_folder", path,
        is_folder=True, check_ext=False,
    )
    if err:
        return jsonify({"error": err}), 403
    payload = {"target_path": path}
    database.create_command(worker_name, "delete_folder", json.dumps(payload))
    try:
        database.delete_worker_file_tree_paths(worker_name, [path], [path + "/"])
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, folder_path, "delete_folder")
    return jsonify({"status": "ok"})

@api_bp.route("/files/upload", methods=["POST"])
def files_upload():
    worker_name = request.form.get("worker_name", "").strip()
    target_path = request.form.get("target_path", "").strip()
    file = request.files.get("file")
    if not all([worker_name, target_path, file]):
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import base64, json, os, time as _time
    rel_path = target_path.replace("\\", "/").lstrip("/")
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_create_file", rel_path,
        is_folder=False, check_ext=True,
    )
    if err:
        return jsonify({"error": err}), 403
    raw = file.read()
    b64_content = base64.b64encode(raw).decode("utf-8")
    payload = {"target_path": rel_path, "file_content_b64": b64_content}
    database.create_command(worker_name, "write_file", json.dumps(payload))
    # Optimistic tree row so explorer shows the file immediately
    try:
        database.upsert_worker_file_tree_entry(
            worker_name,
            rel_path,
            entry_type="file",
            size=len(raw),
            mtime=_time.time(),
            name=os.path.basename(rel_path),
        )
        _maybe_register_runnable_script(worker_name, rel_path)
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, target_path, "upload_file")
    return jsonify({"status": "ok"})

@api_bp.route("/files/update", methods=["POST"])
def files_update():
    worker_name = request.form.get("worker_name", "").strip()
    target_path = request.form.get("target_path", "").strip()
    file = request.files.get("file")
    if not all([worker_name, target_path, file]):
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import base64, json, os, time as _time
    rel_path = target_path.replace("\\", "/").lstrip("/")
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_update_file", rel_path,
        is_folder=False, check_ext=True,
    )
    if err:
        return jsonify({"error": err}), 403
    raw = file.read()
    b64_content = base64.b64encode(raw).decode("utf-8")
    payload = {"target_path": rel_path, "file_content_b64": b64_content}
    database.create_command(worker_name, "write_file", json.dumps(payload))
    try:
        database.upsert_worker_file_tree_entry(
            worker_name,
            rel_path,
            entry_type="file",
            size=len(raw),
            mtime=_time.time(),
            name=os.path.basename(rel_path),
        )
        _maybe_register_runnable_script(worker_name, rel_path)
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, target_path, "update_file")
    return jsonify({"status": "ok"})

@api_bp.route("/files/delete", methods=["POST"])
def files_delete():
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    file_path = data.get("file_path", "").strip()
    if not all([worker_name, file_path]):
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import json
    path = file_path.lstrip("/")
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_delete_file", path,
        is_folder=False, check_ext=True,
    )
    if err:
        return jsonify({"error": err}), 403
    payload = {"target_path": path}
    database.create_command(worker_name, "delete_file", json.dumps(payload))
    try:
        database.delete_worker_file_tree_paths(worker_name, [path], [])
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, file_path, "delete_file")
    return jsonify({"status": "ok"})

@api_bp.route("/files/rename_file", methods=["POST"])
def files_rename_file():
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    source_path = data.get("source_path", "").strip()
    new_name = data.get("new_name", "").strip()
    if not all([worker_name, source_path, new_name]):
        return jsonify({"error": "missing parameters"}), 400
    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    import json, posixpath
    source = source_path.lstrip("/")
    parent = posixpath.dirname(source)
    new_path = f"{parent}/{new_name}".strip("/") if parent else new_name
    err = database.check_pc_file_operation(
        user_id, worker_name, "can_rename_file", source, new_path,
        is_folder=False, check_ext=True,
    )
    if err:
        return jsonify({"error": err}), 403
    payload = {"source_path": source, "new_name": new_name}
    database.create_command(worker_name, "rename_file", json.dumps(payload))
    try:
        database.rename_worker_file_tree_entry(worker_name, source, new_path, is_folder=False)
        _maybe_register_runnable_script(worker_name, new_path)
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, new_name, "rename_file", old_content=source_path)
    return jsonify({"status": "ok"})

@api_bp.route("/files/refresh", methods=["POST"])
def files_refresh():
    """Ask the worker to rescan the current folder from disk and push updates."""
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    folder_path = (data.get("folder_path") or "").strip().lstrip("/")
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    perms = database.get_pc_access_details(user_id, worker_name)
    if not database.is_admin(user_id) and not perms:
        return jsonify({"error": "permission denied"}), 403
    if folder_path and not database.is_admin(user_id):
        root = database.get_worker_script_location(worker_name)
        if not database.path_allowed_by_perms(folder_path, perms, root, is_folder=True):
            return jsonify({"error": "path not allowed"}), 403

    import json
    database.create_command(
        worker_name,
        "resync_folder",
        json.dumps({"folder_path": folder_path}),
    )
    return jsonify({"status": "ok"})


@api_bp.route("/files/move", methods=["POST"])
def files_move():
    """Move a file or folder to another parent path (cut/paste)."""
    data = request.get_json(silent=True) or {}
    worker_name = data.get("worker_name", "").strip()
    source_path = (data.get("source_path") or "").replace("\\", "/").strip("/")
    dest_parent = (data.get("dest_parent") or "").replace("\\", "/").strip("/")
    is_folder = bool(data.get("is_folder"))
    if not worker_name or not source_path:
        return jsonify({"error": "missing parameters"}), 400

    from flask import session
    import json, os, posixpath
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    name = posixpath.basename(source_path)
    dest_path = f"{dest_parent}/{name}".strip("/") if dest_parent else name
    if dest_path == source_path or dest_path.startswith(source_path + "/"):
        return jsonify({"error": "invalid destination"}), 400

    flag = "can_rename_folder" if is_folder else "can_rename_file"
    err = database.check_pc_file_operation(
        user_id, worker_name, flag, source_path, dest_path,
        is_folder=is_folder, check_ext=not is_folder,
    )
    if err:
        return jsonify({"error": err}), 403
    # Destination parent must also be an allowed folder when path limits apply
    if dest_parent:
        err_parent = database.check_pc_file_operation(
            user_id, worker_name, flag, dest_parent,
            is_folder=True, check_ext=False,
        )
        if err_parent:
            return jsonify({"error": err_parent}), 403

    payload = {
        "source_path": source_path,
        "dest_parent": dest_parent,
        "dest_path": dest_path,
        "is_folder": is_folder,
    }
    database.create_command(worker_name, "move_item", json.dumps(payload))
    try:
        database.rename_worker_file_tree_entry(worker_name, source_path, dest_path, is_folder=is_folder)
        if not is_folder:
            _maybe_register_runnable_script(worker_name, dest_path)
    except Exception:
        pass
    database.log_file_action(user_id, worker_name, dest_path, "move", old_content=source_path)
    return jsonify({"status": "ok", "dest_path": dest_path})


@api_bp.route("/files/types", methods=["GET"])
def files_types():
    worker_name = request.args.get("worker_name", "").strip()
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400

    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401
    perms = database.get_pc_access_details(user_id, worker_name)
    if not database.is_admin(user_id) and not perms:
        return jsonify({"error": "permission denied"}), 403

    # Only extensions the explorer meaningfully supports (run / edit / config)
    supported = {
        ".py", ".pyw", ".bat", ".cmd", ".ps1", ".sh",
        ".txt", ".log", ".md", ".json", ".yaml", ".yml", ".xml",
        ".html", ".htm", ".css", ".js", ".csv", ".ini", ".cfg", ".sql",
    }

    import os
    with database.db_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT name FROM worker_file_tree WHERE worker_name = ? AND type = 'file'",
            (worker_name,)
        )
        names = [row[0] for row in cur.fetchall()]
    extensions = set()
    for name in names:
        ext = os.path.splitext(name)[1].lower()
        if ext in supported:
            extensions.add(ext)

    if not database.is_admin(user_id) and perms and not database.pc_has_access_all_files(perms):
        allowed = perms.get("allowed_extensions", "")
        if allowed:
            allowed_set = {e.strip().lower() for e in allowed.split(',') if e.strip()}
            if allowed_set and not any(a in ("*", ".*", "all") for a in allowed_set):
                # Normalize to leading-dot form
                allowed_norm = {a if a.startswith(".") else f".{a}" for a in allowed_set}
                extensions = extensions.intersection(allowed_norm)

    # Prefer runnable types first in the dropdown
    priority = [".py", ".pyw", ".bat", ".cmd", ".ps1", ".sh"]
    ordered = [e for e in priority if e in extensions]
    ordered.extend(sorted(e for e in extensions if e not in priority))
    # Keep a Folder option for hierarchy-only browsing
    types = ["folder"] + ordered
    return jsonify({"status": "ok", "types": types})


@api_bp.route("/api/worker-config/<worker_name>", methods=["GET", "POST"])
def worker_config(worker_name):
    """Get or update worker config by worker_name (Used by Dashboard UI)."""
    from flask import session
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthenticated"}), 401

    if request.method == "POST":
        # Admin-only (matches Worker Details UI)
        if not database.is_admin(user_id):
            return jsonify({"error": "admin only"}), 403
        script_location = request.form.get("script_location", "").strip()
        if not script_location:
            script_location = r"C:\Automation\scripts"
        changed = database.update_worker_config(worker_name, script_location)

        # Ask worker to apply new path and resync immediately (poll still works as fallback)
        if changed:
            import json
            database.create_command(
                worker_name,
                "reload_config",
                json.dumps({"script_location": script_location}),
            )
            _audit(
                user_id,
                "worker_config_updated",
                f"Updated script path for {worker_name} to {script_location}",
                worker_name=worker_name,
            )
            database.mark_worker_tree_sync_started(worker_name, reset=True)
            database.clear_worker_file_tree(worker_name)

        # AJAX form submit (worker detail page) expects JSON
        accepts = (request.headers.get("Accept") or "").lower()
        if "application/json" in accepts or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            if changed:
                return jsonify({
                    "status": "ok",
                    "changed": True,
                    "script_location": script_location,
                    "message": f"Config updated for {worker_name}. Syncing new path...",
                })
            return jsonify({
                "status": "ok",
                "changed": False,
                "script_location": script_location,
                "message": "Already synced — this path is already configured.",
            })

        from flask import redirect, url_for, flash
        if changed:
            flash(f"Config updated for {worker_name}", "success")
        else:
            flash("Already synced — this path is already configured.", "info")
        next_url = request.form.get("next") or url_for("web.dashboard")
        return redirect(next_url)

    worker = database.get_worker(worker_name)
    if not worker:
        return jsonify({}), 404
    return jsonify({"script_location": worker.get("script_location")})


@api_bp.route("/api/my-config", methods=["GET"])
def my_config():
    """Worker uses this to fetch its config based on its worker_name."""
    ip = _client_ip()
    worker_name = request.args.get("worker_name")
    worker = None
    if worker_name:
        worker = database.get_worker(worker_name)
    
    if not worker:
        worker = database.get_worker_by_ip(ip)

    if not worker:
        return jsonify({}), 404

    tree_files, tree_size, tree_entries = database.get_worker_file_tree_stats(worker["worker_name"])
    config = {
        "script_location": worker.get("script_location"),
        "worker_name": worker["worker_name"],
        "tree_entry_count": tree_entries,
        "tree_file_count": tree_files,
        "tree_total_size": tree_size,
    }

    return jsonify(config)


@api_bp.route("/api/schedules/list", methods=["GET"])
def api_schedules_list():
    """Paginated and searchable list of schedules for the UI."""
    from flask import session
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
        
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 50, type=int)
    search = request.args.get("search", "").strip().lower()
    worker = (request.args.get("worker") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    running = (request.args.get("running") or "").strip().lower()
    scope, err = _parse_view_user(uid)
    if err:
        return err

    all_schedules = database.list_schedules_for_viewer(
        uid,
        scope,
        exclude_folder_members=True,
    )

    if search:
        all_schedules = [
            sch for sch in all_schedules
            if search in (sch.get("script_name") or "").lower()
            or search in (sch.get("worker_name") or "").lower()
            or search in (sch.get("username") or "").lower()
        ]
    if worker:
        all_schedules = [sch for sch in all_schedules if (sch.get("worker_name") or "") == worker]
    if status in ("enabled", "disabled"):
        want_on = status == "enabled"
        all_schedules = [
            sch for sch in all_schedules
            if (sch.get("enabled") in (1, True, "1", "t", "true", "True")) == want_on
        ]
    if running:
        all_schedules = [
            sch for sch in all_schedules
            if (sch.get("running_status") or "idle").lower() == running
        ]
        
    total = len(all_schedules)
    start = (page - 1) * limit
    end = start + limit
    paginated = all_schedules[start:end]
    
    return jsonify({
        "schedules": paginated,
        "page": page,
        "limit": limit,
        "total": total
    })


@api_bp.route("/api/scripts/days-map", methods=["GET"])
def api_scripts_days_map():
    """Lightweight id→days map so scheduler UI updates when scripts gain/lose days = N."""
    from flask import session

    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"days": database.get_scripts_days_map()})


@api_bp.route("/api/schedule/<int:schedule_id>/days", methods=["POST"])
def update_schedule_days_api(schedule_id):
    """
    Set schedule days override used for the next run.
    Worker applies it on a temp script copy; the original file is not permanently changed.
    """
    from flask import session
    from app.services.script_days import has_days_variable, parse_days_input

    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    days_val, err = parse_days_input(data.get("days"))
    if err:
        return jsonify({"error": err}), 400
    
    sch = database.get_schedule(schedule_id)
    if not sch:
        return jsonify({"error": "not found"}), 404

    if not database.user_can_set_days(uid):
        return jsonify({"error": "unauthorized"}), 403
        
    # Admin or granted Edit permission
    if not database.user_can_edit_schedule(uid, schedule_id):
        return jsonify({"error": "unauthorized"}), 403

    # Only meaningful when the script has a days variable
    script = database.get_script(sch["script_id"]) if sch.get("script_id") else None
    if script is not None and not has_days_variable(script.get("days")) and days_val is not None:
        return jsonify({"error": "script has no days variable"}), 400
                
    success = database.update_schedule_days(schedule_id, days_val)
    if success:
        return jsonify({
            "status": "ok",
            "days": days_val,
            "has_days_variable": 1 if has_days_variable((script or {}).get("days")) else 0,
        })
    return jsonify({"error": "failed to update"}), 500

@api_bp.route("/api/schedule/<int:schedule_id>/time", methods=["POST"])
def update_schedule_time_api(schedule_id):
    """Update the daily time for a schedule."""
    from flask import session
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    run_time = data.get("time")
    if not run_time:
        return jsonify({"error": "time is required"}), 400
    
    sch = database.get_schedule(schedule_id)
    if not sch:
        return jsonify({"error": "not found"}), 404
        
    if not database.user_can_edit_schedule(uid, schedule_id):
        return jsonify({"error": "unauthorized"}), 403
                
    result = database.update_schedule(schedule_id, run_time=run_time.strip())
    if result is not None:
        return jsonify({"status": "ok"})
    return jsonify({"error": "failed to update"}), 500

@api_bp.route("/api/script/<int:script_id>/days", methods=["POST"])
def update_script_days_api(script_id):
    """Update the days interval for a script."""
    from flask import session
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
        
    data = request.get_json(silent=True) or {}
    days = data.get("days", 0)
    
    # Check if admin or owner
    if not database.is_admin(uid):
        script = database.get_script(script_id)
        if not script or script.get("owner_id") != uid:
            # Also check if user has update access explicitly
            if not database.check_script_access(uid, script_id, "update"):
                return jsonify({"error": "unauthorized"}), 403
                
    success = database.update_script_days(script_id, int(days))
    if success:
        import json
        payload = json.dumps({"script_path": script["script_path"], "days": int(days)})
        database.create_command(script["worker_name"], "update_days", payload)
        database.log_action(uid, "script_days_updated", f"Updated days to {days} for script #{script_id}", _client_ip(), worker_name=script["worker_name"])
        return jsonify({"status": "ok"})
    return jsonify({"error": "failed to update"}), 500


# ============================================================
# Schedule Folders API
# ============================================================

def _folder_uid():
    uid = session.get("user_id")
    return uid


def _parse_view_user(viewer_id):
    """Return (scope_user_id or None, error_response or None).

    Default (missing param) is the viewer's own data.
    view_user_id=all is admin-only all-users scope (None).
    """
    raw = (request.args.get("view_user_id") or "").strip()
    if raw.lower() == "all":
        if database.is_admin(viewer_id):
            return None, None
        return int(viewer_id), None
    if not raw:
        return int(viewer_id), None
    try:
        vid = int(raw)
    except (TypeError, ValueError):
        return int(viewer_id), None
    if not database.can_view_scheduler_user(viewer_id, vid):
        return None, (jsonify({"error": "forbidden"}), 403)
    return int(vid), None


def _create_job_for_folder(worker_name, script_id, schedule_id, folder_run_id):
    return database.create_job(worker_name, script_id, schedule_id=schedule_id, folder_run_id=folder_run_id)


def _visible_schedule_ids(uid: int) -> set[int]:
    return {int(s["id"]) for s in database.list_schedules(uid, exclude_folder_members=False)}


@api_bp.route("/api/schedule-folders", methods=["GET"])
def api_list_folders():
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    scope, err = _parse_view_user(uid)
    if err:
        return err
    folders = sf.list_folders_for_viewer(uid, scope)
    return jsonify({"folders": folders})


def _folder_timing_from_payload(data: dict):
    """Reuse Scheduler drawer timing validation for folder create/edit."""
    freq = (data.get("frequency") or data.get("schedule_type") or "daily").strip() or "daily"
    weekdays = data.get("weekdays") or []
    if isinstance(weekdays, str):
        weekdays = [w.strip() for w in weekdays.split(",") if w.strip()]
    normalized, err = database.normalize_schedule_timing(
        schedule_type=freq,
        run_time=(data.get("run_time") or "").strip(),
        weekdays=weekdays,
        interval_numeric=str(data.get("interval_numeric") or ""),
        interval_unit=str(data.get("interval_unit") or ""),
        interval_use_window=data.get("interval_use_window") in (True, 1, "1", "true", "True"),
        interval_window_start=str(data.get("interval_window_start") or ""),
        interval_window_end=str(data.get("interval_window_end") or ""),
        full_date=str(data.get("full_date") or ""),
        day_of_month=str(data.get("day_of_month") or ""),
    )
    return normalized, err


@api_bp.route("/api/schedule-folders", methods=["POST"])
def api_create_folder():
    from app.services import schedule_folders as sf
    from app.services.script_days import parse_days_input
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    timing, err = _folder_timing_from_payload(data)
    if err:
        return jsonify({"error": err}), 400
    folder_days = None
    if "days" in data and database.user_can_set_days(uid):
        folder_days, days_err = parse_days_input(data.get("days"))
        if days_err:
            return jsonify({"error": days_err}), 400
    folder = sf.create_folder(
        uid,
        name,
        run_time=timing["run_time"],
        schedule_type=timing["schedule_type"],
        schedule_config=timing["schedule_config"],
        days=folder_days,
    )
    # Optional parallel settings on create (default remains sequential)
    if folder and any(k in data for k in ("parallel_enabled", "max_concurrent", "script_gap_seconds", "reset_parallel")):
        sf.update_folder(
            int(folder["id"]),
            parallel_enabled=data.get("parallel_enabled"),
            max_concurrent=data.get("max_concurrent"),
            script_gap_seconds=data.get("script_gap_seconds"),
            reset_parallel=bool(data.get("reset_parallel")),
        )
        folder = sf.get_folder(int(folder["id"]))
    if folder:
        _audit(uid, "folder_created", f"Created folder scheduler '{name}' (#{folder.get('id')})")
    return jsonify({"folder": folder})


@api_bp.route("/api/schedule-folders/<int:folder_id>", methods=["PATCH"])
def api_update_folder(folder_id):
    from app.services import schedule_folders as sf
    from app.services.script_days import parse_days_input
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    data = request.get_json(silent=True) or {}
    want_enabled = data.get("enabled") is not None
    want_parallel = any(
        k in data for k in ("parallel_enabled", "max_concurrent", "script_gap_seconds", "reset_parallel")
    )
    want_days = "days" in data
    want_edit = data.get("name") is not None or any(
        k in data for k in ("frequency", "schedule_type", "run_time", "weekdays")
    ) or want_parallel or want_days
    enabled_val = data.get("enabled")
    if want_enabled:
        can_toggle = (
            sf.user_can_folder(uid, folder_id, "can_enable", admin)
            or sf.user_can_folder(uid, folder_id, "can_disable", admin)
        )
        if not can_toggle:
            if not want_edit:
                return jsonify({"error": "forbidden"}), 403
            enabled_val = None
    if want_edit and not sf.user_can_folder(uid, folder_id, "can_edit", admin):
        return jsonify({"error": "forbidden"}), 403
    if not want_edit and not want_enabled:
        if not sf.user_can_folder(uid, folder_id, "can_edit", admin):
            return jsonify({"error": "forbidden"}), 403
    run_time = None
    schedule_type = None
    schedule_config = None
    if any(k in data for k in ("frequency", "schedule_type", "run_time", "weekdays")):
        timing, err = _folder_timing_from_payload(data)
        if err:
            return jsonify({"error": err}), 400
        run_time = timing["run_time"]
        schedule_type = timing["schedule_type"]
        schedule_config = timing["schedule_config"]
    folder_days = "__omit__"
    applied = 0
    if want_days:
        if not database.user_can_set_days(uid):
            return jsonify({"error": "You don't have permission to set days"}), 403
        folder_days, days_err = parse_days_input(data.get("days"))
        if days_err:
            return jsonify({"error": days_err}), 400
    ok = sf.update_folder(
        folder_id,
        name=data.get("name") if (want_edit and data.get("name") is not None) else None,
        enabled=enabled_val,
        run_time=run_time if want_edit else None,
        schedule_type=schedule_type if want_edit else None,
        schedule_config=schedule_config if want_edit else None,
        parallel_enabled=data.get("parallel_enabled") if want_parallel and not data.get("reset_parallel") else None,
        max_concurrent=data.get("max_concurrent") if want_parallel and not data.get("reset_parallel") else None,
        script_gap_seconds=data.get("script_gap_seconds") if want_parallel and not data.get("reset_parallel") else None,
        reset_parallel=bool(data.get("reset_parallel")),
        days=folder_days,
    )
    if ok and want_days and folder_days is not None and folder_days != "__omit__":
        applied = sf.apply_folder_days_to_members(folder_id, int(folder_days))
    if ok:
        folder = sf.get_folder(folder_id) or {}
        fname = folder.get("name") or folder_id
        if want_enabled and enabled_val is not None and not want_edit:
            state = "enabled" if int(enabled_val or 0) else "disabled"
            _audit(uid, "folder_toggled", f"{state.title()} folder scheduler '{fname}' (#{folder_id})")
        else:
            _audit(uid, "folder_updated", f"Updated folder scheduler '{fname}' (#{folder_id})")
        return jsonify({"ok": True, "folder": folder, "days_applied": applied})
    return jsonify({"error": "failed"}), 500


@api_bp.route("/api/schedule-folders/<int:folder_id>", methods=["DELETE"])
def api_delete_folder(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not (admin or sf.user_can_folder(uid, folder_id, "can_delete", False)):
        return jsonify({"error": "forbidden"}), 403
    folder = sf.get_folder(folder_id) or {}
    ok = sf.delete_folder(folder_id)
    if ok:
        _audit(uid, "folder_deleted", f"Deleted folder scheduler '{folder.get('name') or folder_id}' (#{folder_id})")
    return jsonify({"ok": ok})


@api_bp.route("/api/schedule-folders/<int:folder_id>/items", methods=["GET"])
def api_folder_items(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not sf.get_folder(folder_id):
        return jsonify({"error": "not found"}), 404
    scope, err = _parse_view_user(uid)
    if err:
        return err
    listed = {int(f["id"]): f for f in sf.list_folders_for_viewer(uid, scope)}
    if int(folder_id) not in listed:
        return jsonify({"error": "forbidden"}), 403
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 25, type=int)
    search = (request.args.get("search") or "").strip().lower()
    # Script actions use the same schedule_access flags as the Schedules tab (no folder override)
    items = sf.list_folder_items(folder_id, user_id=uid, is_admin=admin if not scope else False)
    if scope:
        items = database.overlay_schedule_flags(items, uid)
    if search:
        items = [
            i for i in items
            if search in (i.get("script_name") or "").lower()
            or search in (i.get("worker_name") or "").lower()
            or search in (i.get("username") or "").lower()
        ]
    total = len(items)
    start = (page - 1) * limit
    page_items = items[start : start + limit]
    folder = listed.get(folder_id) or sf.get_folder(folder_id)
    run = sf.get_active_folder_run(folder_id) or sf.get_latest_folder_run(folder_id)
    return jsonify({
        "folder": folder,
        "items": page_items,
        "page": page,
        "limit": limit,
        "total": total,
        "run": run,
        "history": sf.list_folder_runs(folder_id, limit=20),
    })


@api_bp.route("/api/schedule-folders/<int:folder_id>/items", methods=["POST"])
def api_folder_add_items(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not sf.user_can_folder(uid, folder_id, "can_manage", admin) and not sf.user_can_folder(uid, folder_id, "can_edit", admin):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    visible = _visible_schedule_ids(uid)
    ids = [int(x) for x in (data.get("schedule_ids") or []) if int(x) in visible]
    n = sf.add_schedules_to_folder(folder_id, ids, as_copies=True)
    if n:
        _audit(uid, "folder_updated", f"Copied {n} schedule(s) into folder #{folder_id}")
    return jsonify({"ok": True, "added": n})


@api_bp.route("/api/schedule-folders/<int:folder_id>/items/remove", methods=["POST"])
def api_folder_remove_items(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not sf.user_can_folder(uid, folder_id, "can_manage", admin) and not sf.user_can_folder(uid, folder_id, "can_edit", admin):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    ids = data.get("schedule_ids") or []
    n = sf.remove_schedules_from_folder(folder_id, [int(x) for x in ids])
    if n:
        _audit(uid, "folder_updated", f"Removed {n} schedule(s) from folder #{folder_id}")
    return jsonify({"ok": True, "removed": n})


@api_bp.route("/api/schedule-folders/<int:folder_id>/reorder", methods=["POST"])
def api_folder_reorder(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not sf.user_can_folder(uid, folder_id, "can_edit", admin) and not sf.user_can_folder(uid, folder_id, "can_manage", admin):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    ids = data.get("schedule_ids") or []
    sf.reorder_folder_items(folder_id, [int(x) for x in ids])
    return jsonify({"ok": True})


@api_bp.route("/api/schedule-folders/<int:folder_id>/bulk", methods=["POST"])
def api_folder_bulk(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    members = sf.folder_member_schedule_ids(folder_id)
    ids = [int(x) for x in (data.get("schedule_ids") or []) if int(x) in members]
    target_folder = data.get("target_folder_id")

    if action == "enable":
        for sid in ids:
            if database.user_can_schedule_flag(uid, sid, "can_enable"):
                sf.set_item_enabled(folder_id, sid, 1)
    elif action == "disable":
        for sid in ids:
            if database.user_can_schedule_flag(uid, sid, "can_disable"):
                sf.set_item_enabled(folder_id, sid, 0)
    elif action == "remove":
        if not sf.user_can_folder(uid, folder_id, "can_manage", admin) and not sf.user_can_folder(uid, folder_id, "can_edit", admin):
            return jsonify({"error": "forbidden"}), 403
        sf.remove_schedules_from_folder(folder_id, ids)
    elif action == "delete":
        kept = []
        for sid in ids:
            if database.user_can_schedule_flag(uid, sid, "can_delete"):
                database.delete_schedule(sid)
            else:
                kept.append(sid)
        gone = [sid for sid in ids if sid not in kept]
        if gone:
            sf.remove_schedules_from_folder(folder_id, gone)
    elif action == "move":
        if not target_folder:
            return jsonify({"error": "target_folder_id required"}), 400
        if not sf.user_can_folder(uid, folder_id, "can_manage", admin) and not sf.user_can_folder(uid, folder_id, "can_edit", admin):
            return jsonify({"error": "forbidden"}), 403
        tid = int(target_folder)
        if not sf.user_can_folder(uid, tid, "can_manage", admin) and not sf.user_can_folder(uid, tid, "can_edit", admin):
            return jsonify({"error": "forbidden"}), 403
        sf.add_schedules_to_folder(tid, ids)
    elif action == "reorder":
        if not sf.user_can_folder(uid, folder_id, "can_edit", admin) and not sf.user_can_folder(uid, folder_id, "can_manage", admin):
            return jsonify({"error": "forbidden"}), 403
        sf.reorder_folder_items(folder_id, ids)
    else:
        return jsonify({"error": "unknown action"}), 400
    if ids:
        _audit(uid, "folder_updated", f"Bulk {action} on folder #{folder_id} ({len(ids)} item(s))")
    return jsonify({"ok": True})


@api_bp.route("/api/schedule-folders/<int:folder_id>/run", methods=["POST"])
def api_folder_run(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not sf.user_can_folder(uid, folder_id, "can_run", admin):
        return jsonify({"error": "forbidden"}), 403
    result = sf.start_folder_run(folder_id, triggered_by=uid, create_job_fn=_create_job_for_folder)
    if result.get("error"):
        return jsonify(result), 400
    _audit(uid, "folder_run", f"Started folder scheduler run #{folder_id}")
    return jsonify(result)


@api_bp.route("/api/schedule-folders/<int:folder_id>/stop", methods=["POST"])
def api_folder_stop(folder_id):
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    admin = database.is_admin(uid)
    if not sf.user_can_folder(uid, folder_id, "can_run", admin):
        return jsonify({"error": "forbidden"}), 403
    ok = sf.stop_folder_run(folder_id)
    if ok:
        _audit(uid, "folder_stopped", f"Stopped folder scheduler run #{folder_id}")
    return jsonify({"ok": ok})


@api_bp.route("/api/schedules/unassigned", methods=["GET"])
def api_unassigned_schedules():
    """Schedules available to copy into a folder (not folder deep-copies; originals stay listed)."""
    from app.services import schedule_folders as sf
    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    in_folder = sf.schedule_ids_in_any_folder()
    # Prefer originals: hide rows that are already folder deep-copies or currently linked
    schedules = []
    for s in database.list_schedules(uid, exclude_folder_members=False):
        sid = int(s["id"])
        if sid in in_folder:
            continue
        if int(s.get("is_folder_copy") or 0) == 1:
            continue
        schedules.append(s)
    return jsonify({"schedules": schedules})


# ============================================================
# System Scheduler INI import (on-demand worker scan)
# ============================================================

def _user_can_use_worker(uid: int, worker_name: str) -> bool:
    if not uid or not worker_name:
        return False
    if database.is_admin(uid):
        return True
    names = {w["worker_name"] for w in database.list_accessible_workers(uid)}
    return worker_name in names


@api_bp.route("/api/schedule-imports/scan", methods=["POST"])
def api_schedule_imports_scan():
    """Queue worker command to scan dfms_schedule_import — no auto-scan elsewhere."""
    import json as _json

    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    worker_name = (data.get("worker_name") or "").strip()
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400
    if not _user_can_use_worker(uid, worker_name):
        return jsonify({"error": "forbidden"}), 403

    worker = database.get_worker(worker_name)
    if not worker:
        return jsonify({"error": "Worker not found"}), 404
    if (worker.get("status") or "") != "online":
        return jsonify({"error": "Worker is offline — start the worker to scan the import folder"}), 400

    cmd = database.create_command(worker_name, "scan_schedule_imports", _json.dumps({}))
    _audit(uid, "schedule_import_scan", f"Queued INI scan on {worker_name}", worker_name=worker_name)
    return jsonify({
        "ok": True,
        "cmd_id": cmd["id"],
        "worker_name": worker_name,
        "import_dir_hint": r"C:\Automation\dfms_schedule_import",
    })


@api_bp.route("/api/schedule-imports/poll/<int:cmd_id>", methods=["GET"])
def api_schedule_imports_poll(cmd_id: int):
    """Poll scan command; on completion enrich rows with DFMS script matches."""
    import json as _json

    from app.services import schedule_imports as si

    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401

    cmd = database.get_command(cmd_id)
    if not cmd:
        return jsonify({"error": "Command not found"}), 404
    if cmd.get("command") != "scan_schedule_imports":
        return jsonify({"error": "Invalid command"}), 400

    worker_name = (cmd.get("worker_name") or "").strip()
    if not _user_can_use_worker(uid, worker_name):
        return jsonify({"error": "forbidden"}), 403

    status = cmd.get("status") or "pending"
    if status in ("pending", "running"):
        return jsonify({"status": status, "worker_name": worker_name})

    if status == "error":
        return jsonify({
            "status": "error",
            "error": cmd.get("output") or "Scan failed",
            "worker_name": worker_name,
        })

    # completed
    raw_out = cmd.get("output") or "{}"
    try:
        payload = _json.loads(raw_out)
    except Exception:
        return jsonify({
            "status": "error",
            "error": "Worker returned invalid scan data",
            "worker_name": worker_name,
        })

    if not payload.get("ok", True) and payload.get("error"):
        return jsonify({
            "status": "error",
            "error": payload.get("error"),
            "import_dir": payload.get("import_dir"),
            "worker_name": worker_name,
        })

    items = si.enrich_scan_items(
        worker_name,
        payload.get("items") or [],
        user_id=uid,
    )
    return jsonify({
        "status": "completed",
        "ok": True,
        "worker_name": worker_name,
        "import_dir": payload.get("import_dir") or r"C:\Automation\dfms_schedule_import",
        "items": items,
        "skipped": payload.get("skipped") or 0,
        "errors": payload.get("errors") or [],
        "importable_count": sum(1 for i in items if i.get("can_import")),
    })


@api_bp.route("/api/schedule-imports/confirm", methods=["POST"])
def api_schedule_imports_confirm():
    """Create DFMS schedules from selected preview rows."""
    from app.services import schedule_imports as si

    uid = _folder_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    worker_name = (data.get("worker_name") or "").strip()
    items = data.get("items") or []
    if not worker_name:
        return jsonify({"error": "worker_name required"}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Select at least one schedule to import"}), 400
    if not _user_can_use_worker(uid, worker_name):
        return jsonify({"error": "forbidden"}), 403

    result = si.import_selected_items(user_id=uid, worker_name=worker_name, items=items)
    _audit(
        uid,
        "schedule_import_confirm",
        f"Imported {result.get('created_count', 0)} schedule(s) from System Scheduler on {worker_name}",
        worker_name=worker_name,
    )
    for c in result.get("created") or []:
        try:
            database.log_action(
                uid,
                "schedule_created",
                f"Imported schedule #{c.get('schedule_id')} for {c.get('script_name')} "
                f"({c.get('type_label')} @ {c.get('run_time')})",
                _client_ip(),
                worker_name=worker_name,
            )
        except Exception:
            pass
    return jsonify(result)

