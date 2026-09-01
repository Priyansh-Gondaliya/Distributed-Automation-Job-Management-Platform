"""
System Scheduler INI → DFMS schedule import helpers.

Worker scans C:\\Automation\\dfms_schedule_import on demand; controller matches
scripts and creates schedules. Additive only — does not change existing flows.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from app import database


FREQ_LABELS = {
    "once": "Once",
    "daily": "Every Day / Week",
    "monthly": "Every Month",
    "interval": "Interval",
}


def _norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip().rstrip("/").lower()


def _basename(path: str) -> str:
    return _norm_path(path).rsplit("/", 1)[-1]


def _basename_variants(base: str) -> set[str]:
    """Basenames as stored in System Scheduler vs DFMS (dots ↔ underscores)."""
    b = (base or "").strip().lower()
    if not b:
        return set()
    out = {b}
    stem, ext = os.path.splitext(b)
    if ext:
        out.add(stem.replace(".", "_") + ext)
        out.add(stem.replace("_", ".") + ext)
    return out


def _path_tail(path: str, parts: int = 2) -> str:
    segs = [s for s in _norm_path(path).split("/") if s]
    if not segs:
        return ""
    return "/".join(segs[-parts:])


def find_script_for_program(worker_name: str, program_path: str) -> Optional[dict[str, Any]]:
    """Match INI ProgramName to a scripts row on this worker (abs path preferred)."""
    target = _norm_path(program_path)
    if not worker_name or not target:
        return None
    base = _basename(target)
    base_vars = _basename_variants(base)
    tail2 = _path_tail(target, 2)
    tail3 = _path_tail(target, 3)

    rows = database.list_worker_script_paths(worker_name)
    best = None
    best_score = -1
    for row in rows:
        sp = _norm_path(row.get("script_path") or "")
        sn = _norm_path(row.get("script_name") or "")
        sn_base = sn.rsplit("/", 1)[-1]
        sp_base = sp.rsplit("/", 1)[-1]
        score = -1
        if sp == target or sn == target:
            score = 10
        elif sp.endswith("/" + target) or target.endswith("/" + sp):
            score = 9
        elif tail3 and (_path_tail(sp, 3) == tail3 or _path_tail(sn, 3) == tail3):
            score = 7
        elif tail2 and (_path_tail(sp, 2) == tail2 or _path_tail(sn, 2) == tail2):
            score = 6
        elif sp_base in base_vars or sn_base in base_vars or sn in base_vars:
            score = 3
        if score > best_score:
            best_score = score
            best = row
            if score >= 10:
                break

    if best is None:
        return None

    # Weak basename-only: require uniqueness among variants
    if best_score <= 3:
        hits = [
            r
            for r in rows
            if _basename(r.get("script_path") or "") in base_vars
            or _basename(r.get("script_name") or "") in base_vars
            or _norm_path(r.get("script_name") or "") in base_vars
        ]
        if len(hits) != 1:
            return None
        return hits[0]
    return best


def script_name_for_import(worker_name: str, program_path: str, hint_name: str = "") -> str:
    """
    Unique scripts.script_name for register.
    Prefer path relative to worker script_location; otherwise keep a stable abs path.
    """
    program = (program_path or "").strip()
    root = _norm_path(database.get_worker_script_location(worker_name) or "")
    prog = _norm_path(program)
    if root and (prog == root or prog.startswith(root + "/")):
        rel = prog[len(root) :].lstrip("/")
        return rel.replace("/", "\\") if rel else (hint_name or _basename(program))
    # Outside watched tree — absolute path avoids basename collisions
    return program.replace("/", "\\")


def ensure_script_for_import(
    worker_name: str,
    program_path: str,
    *,
    hint_name: str = "",
) -> Optional[dict[str, Any]]:
    """Find existing script or register ProgramName when the file is a valid import path."""
    program = (program_path or "").strip()
    if not worker_name or not program:
        return None
    found = find_script_for_program(worker_name, program)
    if found:
        return found
    name = script_name_for_import(worker_name, program, hint_name=hint_name)
    return database.register_script(worker_name, name, program)


def enrich_scan_items(
    worker_name: str,
    items: list[dict[str, Any]],
    *,
    user_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Attach DFMS script match + import readiness to worker scan rows."""
    existing_keys, scheduled_by_script, scheduled_by_path = _existing_schedule_index(
        user_id, worker_name
    )
    enriched = []
    for raw in items or []:
        row = dict(raw)
        program = (row.get("program_path") or "").strip()
        script = find_script_for_program(worker_name, program) if program else None
        schedule_type = (row.get("schedule_type") or "daily").strip() or "daily"
        if schedule_type not in FREQ_LABELS:
            schedule_type = "daily"
        row["schedule_type"] = schedule_type
        row["type_label"] = row.get("type_label") or FREQ_LABELS.get(schedule_type, schedule_type)
        if script:
            row["script_id"] = int(script["id"])
            row["dfms_script_name"] = script.get("script_name")
            row["dfms_script_path"] = script.get("script_path")
            row["in_dfms"] = True
            row["will_register"] = False
        else:
            row["script_id"] = None
            row["dfms_script_name"] = None
            row["dfms_script_path"] = None
            row["in_dfms"] = False
            # Valid on-disk path can be registered at import time
            row["will_register"] = bool(program) and bool(row.get("path_exists"))

        reasons = []
        if not program:
            reasons.append("No program path in INI")
        elif not row.get("path_exists"):
            reasons.append("Path missing on worker PC")
        # Do NOT block when unregistered — will_register handles it on confirm
        if schedule_type == "once" and not (row.get("full_date") or "").strip():
            reasons.append("Once schedule needs a date")
        if schedule_type == "daily" and not (row.get("weekdays") or []):
            reasons.append("No weekdays selected")
        if schedule_type == "interval" and (
            not row.get("interval_numeric") or not row.get("interval_unit")
        ):
            reasons.append("Interval incomplete")
        if schedule_type == "monthly" and not (row.get("day_of_month") or "").strip():
            reasons.append("Monthly day missing")

        # Same script path already scheduled? (show only when true)
        path_info = None
        if row.get("script_id"):
            path_info = scheduled_by_script.get(int(row["script_id"]))
        if path_info is None and program:
            path_info = scheduled_by_path.get(_norm_path(program))
        if path_info is None and row.get("dfms_script_path"):
            path_info = scheduled_by_path.get(_norm_path(str(row["dfms_script_path"])))

        path_scheduled = path_info is not None
        row["path_scheduled"] = path_scheduled
        row["scheduled_run_time"] = (path_info or {}).get("run_time") or ""
        row["scheduled_type_label"] = (path_info or {}).get("type_label") or ""
        if path_scheduled:
            t = row["scheduled_run_time"]
            row["scheduled_label"] = f"Scheduled{(' · ' + t) if t else ''}"
        else:
            row["scheduled_label"] = ""

        already = False
        if not reasons and path_scheduled:
            already = True
        elif row.get("script_id") and not reasons:
            timing, err = database.normalize_schedule_timing(
                schedule_type=schedule_type,
                run_time=(row.get("run_time") or "00:00"),
                weekdays=list(row.get("weekdays") or []),
                interval_numeric=str(row.get("interval_numeric") or ""),
                interval_unit=str(row.get("interval_unit") or ""),
                full_date=str(row.get("full_date") or ""),
                day_of_month=str(row.get("day_of_month") or ""),
            )
            if not err and timing:
                key = (
                    int(row["script_id"]),
                    timing["run_time"],
                    timing["schedule_type"],
                )
                if key in existing_keys:
                    already = True

        row["already_scheduled"] = already or path_scheduled
        # Import when path exists on PC (registered or will register) and not already scheduled
        row["can_import"] = (
            len(reasons) == 0
            and bool(program)
            and bool(row.get("path_exists"))
            and not path_scheduled
        )
        row["block_reason"] = "; ".join(reasons) if reasons else ""
        enriched.append(row)
    return enriched


def _list_user_schedules_for_worker(user_id: Optional[int], worker_name: str) -> list[dict[str, Any]]:
    if not user_id:
        return []
    try:
        rows = database.list_schedules(
            user_id, exclude_folder_members=True, as_user=True
        )
    except TypeError:
        rows = database.list_schedules(user_id, exclude_folder_members=True)
    return [s for s in rows if (s.get("worker_name") or "") == worker_name]


def _existing_schedule_index(
    user_id: Optional[int], worker_name: str
) -> tuple[set[tuple], dict[int, dict[str, str]], dict[str, dict[str, str]]]:
    """
    Returns:
      - exact keys (script_id, run_time, schedule_type)
      - scheduled_by_script_id → {run_time, type_label}
      - scheduled_by_norm_path → {run_time, type_label}
    """
    keys: set[tuple] = set()
    by_script: dict[int, dict[str, str]] = {}
    by_path: dict[str, dict[str, str]] = {}
    for sch in _list_user_schedules_for_worker(user_id, worker_name):
        sid = int(sch.get("script_id") or 0)
        run_time = (sch.get("run_time") or "").strip()
        sch_type = (sch.get("schedule_type") or "daily").strip()
        info = {
            "run_time": run_time,
            "type_label": FREQ_LABELS.get(sch_type, sch_type),
        }
        if sid:
            keys.add((sid, run_time, sch_type))
            by_script.setdefault(sid, info)
        sp = _norm_path(sch.get("script_path") or "")
        if sp:
            by_path.setdefault(sp, info)
    return keys, by_script, by_path


def _existing_schedule_keys(user_id: Optional[int], worker_name: str) -> set[tuple]:
    keys, _, _ = _existing_schedule_index(user_id, worker_name)
    return keys


def _existing_scheduled_script_ids(user_id: Optional[int], worker_name: str) -> set[int]:
    _, by_script, _ = _existing_schedule_index(user_id, worker_name)
    return set(by_script.keys())


def import_selected_items(
    *,
    user_id: int,
    worker_name: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Create DFMS schedules for selected import rows.
    Unregistered but valid ProgramName paths are registered, then scheduled.
    """
    created = []
    skipped = []
    errors = []
    existing = _existing_schedule_keys(user_id, worker_name)
    scheduled_scripts = _existing_scheduled_script_ids(user_id, worker_name)
    _, _, scheduled_by_path = _existing_schedule_index(user_id, worker_name)

    for idx, raw in enumerate(items or []):
        label = (raw.get("script_name") or raw.get("task_name") or f"item {idx + 1}").strip()
        program = (raw.get("program_path") or "").strip()
        try:
            script_id = int(raw.get("script_id") or 0)
        except (TypeError, ValueError):
            script_id = 0

        script = database.get_script(script_id) if script_id else None
        if script and (script.get("worker_name") or "") != worker_name:
            script = None
            script_id = 0

        if not script:
            if not program:
                errors.append({"item": label, "error": "Missing script path"})
                continue
            if program and scheduled_by_path.get(_norm_path(program)):
                skipped.append({"item": label, "reason": "Same path already scheduled"})
                continue
            try:
                script = ensure_script_for_import(
                    worker_name,
                    program,
                    hint_name=label,
                )
            except Exception as exc:
                errors.append({"item": label, "error": f"Could not register script: {exc}"})
                continue
            if not script:
                errors.append({"item": label, "error": "Could not register script for this path"})
                continue
            script_id = int(script["id"])

        if not database.check_script_access(user_id, script_id, "run"):
            errors.append({"item": label, "error": "No permission to schedule this script"})
            continue

        if script_id in scheduled_scripts:
            skipped.append({"item": label, "reason": "Same path already scheduled"})
            continue
        if program and scheduled_by_path.get(_norm_path(program)):
            skipped.append({"item": label, "reason": "Same path already scheduled"})
            continue

        schedule_type = (raw.get("schedule_type") or "daily").strip() or "daily"
        run_time = (raw.get("run_time") or "00:00").strip() or "00:00"
        weekdays = list(raw.get("weekdays") or [])
        if schedule_type == "daily" and not weekdays:
            weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        timing, err = database.normalize_schedule_timing(
            schedule_type=schedule_type,
            run_time=run_time,
            weekdays=weekdays,
            interval_numeric=str(raw.get("interval_numeric") or ""),
            interval_unit=str(raw.get("interval_unit") or ""),
            full_date=str(raw.get("full_date") or ""),
            day_of_month=str(raw.get("day_of_month") or ""),
        )
        if err or not timing:
            errors.append({"item": label, "error": err or "Invalid schedule timing"})
            continue

        key = (script_id, timing["run_time"], timing["schedule_type"])
        if key in existing:
            skipped.append({"item": label, "reason": "Already scheduled (same script, time, type)"})
            continue

        sch = database.create_schedule(
            script_id,
            user_id,
            worker_name,
            timing["run_time"],
            None,
            timing["schedule_config"],
            timing["schedule_type"],
        )
        enabled = 1 if raw.get("enabled", True) else 0
        if enabled == 0:
            database.update_schedule(int(sch["id"]), enabled=0)

        existing.add(key)
        scheduled_scripts.add(script_id)
        if program:
            scheduled_by_path[_norm_path(program)] = {
                "run_time": timing["run_time"],
                "type_label": FREQ_LABELS.get(timing["schedule_type"], timing["schedule_type"]),
            }
        created.append(
            {
                "schedule_id": sch["id"],
                "script_id": script_id,
                "script_name": (script or {}).get("script_name") or label,
                "run_time": timing["run_time"],
                "schedule_type": timing["schedule_type"],
                "type_label": FREQ_LABELS.get(timing["schedule_type"], timing["schedule_type"]),
            }
        )

    return {
        "ok": True,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }
