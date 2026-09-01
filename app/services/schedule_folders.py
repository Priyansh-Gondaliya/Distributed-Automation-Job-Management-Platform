"""
Scheduler folders — sequential multi-script runs with history and permissions.

Maps to live Postgres tables (logical names rewritten by db_compat):
  tbl_dfms_schedule_folders / _runs / _access / _items
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db_compat import db_cursor


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        return datetime.strptime(str(value).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _duration_seconds(started_at: Any, ended_at: Any) -> Optional[float]:
    s = _parse_ts(started_at)
    e = _parse_ts(ended_at)
    if not s or not e:
        return None
    return (e - s).total_seconds()


def _table_exists(cur, physical_name: str) -> bool:
    cur.execute(
        """
        SELECT 1 AS ok FROM information_schema.tables
        WHERE table_schema = current_schema()
          AND table_name = ?
        """,
        (physical_name,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, physical_table: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1 AS ok FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = ?
          AND column_name = ?
        """,
        (physical_table, column_name),
    )
    return cur.fetchone() is not None


def _ensure_column(cur, logical_table: str, physical_table: str, column: str, ddl: str) -> None:
    if not _column_exists(cur, physical_table, column):
        cur.execute(f"ALTER TABLE {logical_table} ADD COLUMN {ddl}")


def ensure_schedule_folders_schema() -> None:
    """Create missing folder tables/columns only (idempotent; never drops)."""
    with db_cursor() as cur:
        # Items membership table (does not exist yet on live DBs)
        if not _table_exists(cur, "tbl_dfms_schedule_folder_items"):
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_folder_items (
                    id           BIGSERIAL PRIMARY KEY,
                    folder_id    BIGINT NOT NULL,
                    schedule_id  BIGINT NOT NULL,
                    sort_order   INTEGER NOT NULL DEFAULT 0,
                    enabled      INTEGER NOT NULL DEFAULT 1,
                    CONSTRAINT uq_schedule_folder_items UNIQUE (folder_id, schedule_id),
                    CONSTRAINT fk_folder_items_folder
                        FOREIGN KEY (folder_id) REFERENCES schedule_folders(id) ON DELETE CASCADE,
                    CONSTRAINT fk_folder_items_schedule
                        FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folder_items_folder_order
                ON schedule_folder_items (folder_id, sort_order)
                """
            )

        # Soft-add any missing columns on existing live tables (no drops / renames)
        if _table_exists(cur, "tbl_dfms_schedule_folders"):
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "description", "description TEXT")
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "last_run_at", "last_run_at TEXT")
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "current_run_id", "current_run_id BIGINT")
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "schedule_type", "schedule_type TEXT DEFAULT 'daily'")
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "schedule_config", "schedule_config TEXT DEFAULT '{}'")
            # Opt-in parallel run: 0 = existing sequential one-by-one behavior
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "parallel_enabled", "parallel_enabled INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "max_concurrent", "max_concurrent INTEGER NOT NULL DEFAULT 1")
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "script_gap_seconds", "script_gap_seconds INTEGER NOT NULL DEFAULT 0")
            # Optional script days=N override applied to members that have the variable
            _ensure_column(cur, "schedule_folders", "tbl_dfms_schedule_folders",
                           "days", "days INTEGER")

        if _table_exists(cur, "tbl_dfms_schedule_folder_runs"):
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "duration_seconds", "duration_seconds DOUBLE PRECISION")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "total_count", "total_count INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "successful_count", "successful_count INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "failed_count", "failed_count INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "skipped_count", "skipped_count INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "current_index", "current_index INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "current_job_id", "current_job_id BIGINT")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "current_schedule_id", "current_schedule_id BIGINT")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "triggered_by", "triggered_by BIGINT")
            # Snapshot of parallel settings for this run (NULL/0 = sequential path)
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "parallel_enabled", "parallel_enabled INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "max_concurrent", "max_concurrent INTEGER NOT NULL DEFAULT 1")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "script_gap_seconds", "script_gap_seconds INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "schedule_folder_runs", "tbl_dfms_schedule_folder_runs",
                           "next_launch_at", "next_launch_at TEXT")

        if _table_exists(cur, "tbl_dfms_schedule_folder_access"):
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_manage", "can_manage INTEGER DEFAULT 0")
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_manage_members", "can_manage_members INTEGER DEFAULT 0")
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_edit", "can_edit INTEGER DEFAULT 0")
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_delete", "can_delete INTEGER DEFAULT 0")
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_enable", "can_enable INTEGER DEFAULT 0")
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_disable", "can_disable INTEGER DEFAULT 0")
            _ensure_column(cur, "schedule_folder_access", "tbl_dfms_schedule_folder_access",
                           "can_run", "can_run INTEGER DEFAULT 0")

        # jobs.folder_run_id for chaining (already present on most live DBs)
        if _table_exists(cur, "tbl_dfms_jobs") and not _column_exists(cur, "tbl_dfms_jobs", "folder_run_id"):
            cur.execute("ALTER TABLE jobs ADD COLUMN folder_run_id BIGINT")

        # Mark schedules that exist only as folder deep-copies
        if _table_exists(cur, "tbl_dfms_schedules"):
            _ensure_column(cur, "schedules", "tbl_dfms_schedules",
                           "is_folder_copy", "is_folder_copy INTEGER NOT NULL DEFAULT 0")

    # One-time: previously "Add Existing" moved schedules into folders — replace
    # those links with deep copies so originals return to the Schedules section.
    try:
        restore_count = restore_schedules_section_via_folder_copies()
        if restore_count:
            print(f"[schedule_folders] Restored {restore_count} schedule(s) to Schedules section via deep copy", flush=True)
    except Exception as exc:
        print(f"[schedule_folders] restore copies skipped: {exc}", flush=True)


def restore_schedules_section_via_folder_copies() -> int:
    """
    For each folder member that is still the original schedule (is_folder_copy=0),
    deep-copy it into the folder and leave the original unassigned (visible in Schedules).
    Safe to call repeatedly — already-copied members are skipped.
    """
    from app import database as db

    restored = 0
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT i.id AS item_id, i.folder_id, i.schedule_id, i.sort_order, i.enabled AS item_enabled,
                   COALESCE(sch.is_folder_copy, 0) AS is_folder_copy
            FROM schedule_folder_items i
            JOIN schedules sch ON sch.id = i.schedule_id
            WHERE COALESCE(sch.is_deleted, 0) = 0
            ORDER BY i.folder_id, i.sort_order, i.id
            """
        )
        rows = [_row(r) for r in cur.fetchall()]

    for row in rows:
        if not row:
            continue
        if int(row.get("is_folder_copy") or 0) == 1:
            continue
        src_id = int(row["schedule_id"])
        folder_id = int(row["folder_id"])
        item_id = int(row["item_id"])
        clone = db.clone_schedule_independent(src_id, as_folder_copy=True)
        if not clone or not clone.get("id"):
            continue
        new_id = int(clone["id"])
        with db_cursor() as cur:
            # Point folder item at the independent copy
            cur.execute(
                """
                UPDATE schedule_folder_items
                SET schedule_id = ?
                WHERE id = ? AND folder_id = ? AND schedule_id = ?
                """,
                (new_id, item_id, folder_id, src_id),
            )
            if cur.rowcount:
                # Keep item enabled flag in sync with copy
                cur.execute(
                    "UPDATE schedule_folder_items SET enabled = ? WHERE id = ?",
                    (1 if int(row.get("item_enabled") or 0) else 0, item_id),
                )
                restored += 1
    return restored


def _row(r) -> Optional[dict[str, Any]]:
    return dict(r) if r is not None else None


def _access_has_flag(row: Any, flag: str) -> bool:
    """True if access row grants flag. can_manage accepts can_manage OR can_manage_members."""
    if not row:
        return False
    if flag == "can_manage":
        return int(row.get("can_manage") or 0) == 1 or int(row.get("can_manage_members") or 0) == 1
    return int(row.get(flag) or 0) == 1


def create_folder(
    user_id: int,
    name: str,
    run_time: str = "00:00",
    schedule_type: str = "daily",
    schedule_config: str = "{}",
    days: Optional[int] = None,
) -> dict[str, Any]:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO schedule_folders
                (user_id, name, enabled, is_deleted, run_time, schedule_type, schedule_config,
                 days, status, created_at, updated_at)
            VALUES (?, ?, 1, 0, ?, ?, ?, ?, 'idle', ?, ?)
            """,
            (user_id, name.strip(), run_time or "00:00", schedule_type or "daily",
             schedule_config or "{}", days, now, now),
        )
        folder_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO schedule_folder_access
                (folder_id, user_id, can_delete, can_enable, can_disable, can_run, can_edit,
                 can_manage, can_manage_members, granted_by, granted_at)
            VALUES (?, ?, 0, 1, 1, 1, 1, 1, 1, ?, ?)
            """,
            (folder_id, user_id, user_id, now),
        )
        cur.execute("SELECT * FROM schedule_folders WHERE id = ?", (folder_id,))
        return _row(cur.fetchone())


def _normalize_parallel_settings(
    parallel_enabled: Any = None,
    max_concurrent: Any = None,
    script_gap_seconds: Any = None,
) -> tuple[int, int, int]:
    """Return sanitized (parallel_enabled, max_concurrent, script_gap_seconds).

    Default / reset: (0, 1, 0) — existing one-by-one folder behavior.
    """
    enabled = 1 if int(parallel_enabled or 0) == 1 else 0
    try:
        max_c = int(max_concurrent if max_concurrent is not None else 1)
    except (TypeError, ValueError):
        max_c = 1
    try:
        gap = int(script_gap_seconds if script_gap_seconds is not None else 0)
    except (TypeError, ValueError):
        gap = 0
    if not enabled:
        return 0, 1, 0
    max_c = max(1, min(max_c, 50))
    gap = max(0, min(gap, 3600))
    return 1, max_c, gap


def update_folder(
    folder_id: int,
    name: Optional[str] = None,
    enabled: Optional[int] = None,
    run_time: Optional[str] = None,
    schedule_type: Optional[str] = None,
    schedule_config: Optional[str] = None,
    parallel_enabled: Optional[int] = None,
    max_concurrent: Optional[int] = None,
    script_gap_seconds: Optional[int] = None,
    reset_parallel: bool = False,
    days: Any = "__omit__",
) -> bool:
    now = _utc_now()
    sets = ["updated_at = ?"]
    params: list[Any] = [now]
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip())
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(int(enabled))
    if run_time is not None:
        sets.append("run_time = ?")
        params.append(run_time)
    if schedule_type is not None:
        sets.append("schedule_type = ?")
        params.append(schedule_type)
    if schedule_config is not None:
        sets.append("schedule_config = ?")
        params.append(schedule_config)
    if days != "__omit__":
        sets.append("days = ?")
        params.append(days)
    if reset_parallel:
        sets.extend([
            "parallel_enabled = ?",
            "max_concurrent = ?",
            "script_gap_seconds = ?",
        ])
        params.extend([0, 1, 0])
    elif parallel_enabled is not None or max_concurrent is not None or script_gap_seconds is not None:
        # Merge with existing when only some fields sent
        existing = get_folder(folder_id) or {}
        pe = parallel_enabled if parallel_enabled is not None else existing.get("parallel_enabled")
        mc = max_concurrent if max_concurrent is not None else existing.get("max_concurrent")
        gap = script_gap_seconds if script_gap_seconds is not None else existing.get("script_gap_seconds")
        pe_n, mc_n, gap_n = _normalize_parallel_settings(pe, mc, gap)
        sets.extend([
            "parallel_enabled = ?",
            "max_concurrent = ?",
            "script_gap_seconds = ?",
        ])
        params.extend([pe_n, mc_n, gap_n])
    params.append(folder_id)
    with db_cursor() as cur:
        cur.execute(
            f"UPDATE schedule_folders SET {', '.join(sets)} WHERE id = ? AND is_deleted = 0",
            tuple(params),
        )
        return cur.rowcount > 0


def apply_folder_days_to_members(folder_id: int, days: int) -> int:
    """
    Set schedule.days for members whose scripts have a days = N variable.
    Scripts without the variable are not modified.
    Returns number of schedules updated.
    """
    from app import database as db

    updated = 0
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT i.schedule_id
            FROM schedule_folder_items i
            JOIN schedules sch ON sch.id = i.schedule_id
            JOIN scripts s ON s.id = sch.script_id
            WHERE i.folder_id = ?
              AND COALESCE(sch.is_deleted, 0) = 0
              AND s.days IS NOT NULL
            """,
            (folder_id,),
        )
        ids = [int(r["schedule_id"]) for r in cur.fetchall()]
    for sid in ids:
        if db.update_schedule_days(sid, int(days)):
            updated += 1
    return updated


def delete_folder(folder_id: int) -> bool:
    """Soft-delete folder and release members back to the regular Scheduler."""
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute("DELETE FROM schedule_folder_items WHERE folder_id = ?", (folder_id,))
        cur.execute(
            """
            UPDATE schedule_folders
            SET is_deleted = 1, enabled = 0, status = 'idle', current_run_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, folder_id),
        )
        return cur.rowcount > 0


def get_folder(folder_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM schedule_folders WHERE id = ? AND is_deleted = 0",
            (folder_id,),
        )
        return _row(cur.fetchone())


def list_folders(user_id: Optional[int] = None, is_admin: bool = False) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        if is_admin or user_id is None:
            cur.execute(
                """
                SELECT f.*, u.username,
                       (SELECT COUNT(*) FROM schedule_folder_items i WHERE i.folder_id = f.id) AS item_count,
                       1 as can_delete, 1 as can_enable, 1 as can_disable,
                       1 as can_run, 1 as can_edit, 1 as can_manage
                FROM schedule_folders f
                JOIN users u ON u.id = f.user_id
                WHERE f.is_deleted = 0
                ORDER BY f.name
                """
            )
        else:
            cur.execute(
                """
                SELECT f.*, u.username,
                       (SELECT COUNT(*) FROM schedule_folder_items i WHERE i.folder_id = f.id) AS item_count,
                       COALESCE(fa.can_delete, 0) as can_delete,
                       CASE
                           WHEN fa.id IS NOT NULL THEN COALESCE(fa.can_enable, 0)
                           WHEN f.user_id = ? THEN 1 ELSE 0
                       END as can_enable,
                       CASE
                           WHEN fa.id IS NOT NULL THEN COALESCE(fa.can_disable, 0)
                           WHEN f.user_id = ? THEN 1 ELSE 0
                       END as can_disable,
                       CASE
                           WHEN fa.id IS NOT NULL THEN COALESCE(fa.can_run, 0)
                           WHEN f.user_id = ? THEN 1 ELSE 0
                       END as can_run,
                       CASE
                           WHEN fa.id IS NOT NULL THEN COALESCE(fa.can_edit, 0)
                           WHEN f.user_id = ? THEN 1 ELSE 0
                       END as can_edit,
                       CASE
                           WHEN fa.id IS NOT NULL THEN
                               CASE WHEN COALESCE(fa.can_manage, 0) = 1
                                          OR COALESCE(fa.can_manage_members, 0) = 1
                                    THEN 1 ELSE 0 END
                           WHEN f.user_id = ? THEN 1 ELSE 0
                       END as can_manage
                FROM schedule_folders f
                JOIN users u ON u.id = f.user_id
                LEFT JOIN schedule_folder_access fa ON fa.folder_id = f.id AND fa.user_id = ?
                WHERE f.is_deleted = 0 AND (f.user_id = ? OR fa.id IS NOT NULL)
                ORDER BY f.name
                """,
                (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
            )
        rows = [_row(r) for r in cur.fetchall()]

    for f in rows:
        run = get_active_folder_run(f["id"]) or get_latest_folder_run(f["id"])
        if run:
            f["run_status"] = run.get("status")
            f["progress_done"] = int(run.get("successful_count") or 0) + int(run.get("failed_count") or 0)
            f["progress_total"] = int(run.get("total_count") or 0)
            f["run_started_at"] = run.get("started_at")
            f["active_run_id"] = run["id"] if run.get("status") == "running" else None
        else:
            f["run_status"] = f.get("status") or "idle"
            f["progress_done"] = 0
            f["progress_total"] = int(f.get("item_count") or 0)
            f["run_started_at"] = None
            f["active_run_id"] = None
    return rows


_FOLDER_ACTION_FLAGS = (
    "can_delete", "can_enable", "can_disable", "can_run", "can_edit", "can_manage",
)


def overlay_folder_flags(folders: list[dict[str, Any]], viewer_id: int) -> list[dict[str, Any]]:
    """Replace folder can_* with the viewer's grants (read-only unless separately granted)."""
    from app import database as db
    if db.is_admin(viewer_id):
        for f in folders:
            for k in _FOLDER_ACTION_FLAGS:
                f[k] = 1
        return folders
    mine = {int(f["id"]): f for f in list_folders(viewer_id, is_admin=False)}
    for f in folders:
        src = mine.get(int(f["id"]))
        for k in _FOLDER_ACTION_FLAGS:
            f[k] = (src.get(k) or 0) if src else 0
    return folders


def list_folders_for_viewer(viewer_id: int, scope_user_id: Optional[int] = None) -> list[dict[str, Any]]:
    from app import database as db
    if scope_user_id is None:
        return list_folders(viewer_id, is_admin=db.is_admin(viewer_id))
    rows = list_folders(int(scope_user_id), is_admin=False)
    return overlay_folder_flags(rows, viewer_id)


def user_can_folder(user_id: int, folder_id: int, flag: str, is_admin: bool = False) -> bool:
    if is_admin:
        return True
    folder = get_folder(folder_id)
    if not folder:
        return False
    if int(folder["user_id"]) == int(user_id) and flag in (
        "can_edit", "can_run", "can_enable", "can_disable", "can_manage",
    ):
        # Owner has manage/edit/run/enable by default even without access row
        with db_cursor() as cur:
            cur.execute(
                "SELECT * FROM schedule_folder_access WHERE folder_id = ? AND user_id = ?",
                (folder_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return flag != "can_delete"
            return _access_has_flag(row, flag)
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM schedule_folder_access WHERE folder_id = ? AND user_id = ?",
            (folder_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return False
        return _access_has_flag(row, flag)


def grant_folder_access(
    folder_id: int,
    user_id: int,
    granted_by: int,
    can_delete: int = 0,
    can_enable: int = 0,
    can_disable: int = 0,
    can_run: int = 0,
    can_edit: int = 0,
    can_manage: int = 0,
) -> bool:
    now = _utc_now()
    manage = int(can_manage)
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO schedule_folder_access
                (folder_id, user_id, can_delete, can_enable, can_disable, can_run, can_edit,
                 can_manage, can_manage_members, granted_by, granted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(folder_id, user_id) DO UPDATE SET
                can_delete=excluded.can_delete,
                can_enable=excluded.can_enable,
                can_disable=excluded.can_disable,
                can_run=excluded.can_run,
                can_edit=excluded.can_edit,
                can_manage=excluded.can_manage,
                can_manage_members=excluded.can_manage_members,
                granted_by=excluded.granted_by,
                granted_at=excluded.granted_at
            """,
            (folder_id, user_id, can_delete, can_enable, can_disable, can_run, can_edit,
             manage, manage, granted_by, now),
        )
        return True


def revoke_all_folder_access(user_id: int) -> None:
    """Revoke grants for folders the user does not own.

    Owner rows stay so Delete Folder remains off-by-default until an admin
    explicitly re-grants it. Owner edit/enable/run defaults are unchanged.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            DELETE FROM schedule_folder_access
            WHERE user_id = ?
              AND folder_id NOT IN (
                  SELECT id FROM schedule_folders WHERE user_id = ? AND is_deleted = 0
              )
            """,
            (user_id, user_id),
        )


def get_all_folder_access() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT fa.*, u.username, f.name as folder_name
            FROM schedule_folder_access fa
            JOIN users u ON u.id = fa.user_id
            JOIN schedule_folders f ON f.id = fa.folder_id
            WHERE f.is_deleted = 0
            ORDER BY u.username, f.name
            """
        )
        return [_row(r) for r in cur.fetchall()]


def list_folders_for_permissions() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.name, u.username
            FROM schedule_folders f
            JOIN users u ON u.id = f.user_id
            WHERE f.is_deleted = 0
            ORDER BY f.name
            """
        )
        return [_row(r) for r in cur.fetchall()]


def list_folder_items(folder_id: int, user_id: Optional[int] = None, is_admin: bool = False) -> list[dict[str, Any]]:
    """Schedules inside a folder, ordered — same shape as list_schedules rows where possible."""
    job_join = """
        LEFT JOIN (
            SELECT schedule_id, MAX(id) as max_job_id
            FROM jobs WHERE schedule_id IS NOT NULL
            GROUP BY schedule_id
        ) jmax ON jmax.schedule_id = sch.id
        LEFT JOIN jobs jlatest ON jlatest.id = jmax.max_job_id
    """
    with db_cursor() as cur:
        if is_admin or user_id is None:
            cur.execute(
                f"""
                SELECT i.id as item_id, i.folder_id, i.sort_order, i.enabled as item_enabled,
                       sch.*, s.script_name, s.script_path, s.days as script_days,
                       CASE WHEN s.days IS NOT NULL THEN 1 ELSE 0 END as has_days_variable,
                       u.username,
                       jlatest.status as running_status, jlatest.paused_at as paused_at,
                       jlatest.start_time as job_start_time, jlatest.created_at as job_created_at,
                       1 as can_delete, 1 as can_enable, 1 as can_disable,
                       1 as can_run, 1 as can_duplicate, 1 as can_edit
                FROM schedule_folder_items i
                JOIN schedules sch ON sch.id = i.schedule_id
                JOIN scripts s ON s.id = sch.script_id
                JOIN users u ON u.id = sch.user_id
                {job_join}
                WHERE i.folder_id = ? AND sch.is_deleted = 0
                ORDER BY i.sort_order ASC, i.id ASC
                """,
                (folder_id,),
            )
        else:
            cur.execute(
                f"""
                SELECT i.id as item_id, i.folder_id, i.sort_order, i.enabled as item_enabled,
                       sch.*, s.script_name, s.script_path, s.days as script_days,
                       CASE WHEN s.days IS NOT NULL THEN 1 ELSE 0 END as has_days_variable,
                       u.username,
                       jlatest.status as running_status, jlatest.paused_at as paused_at,
                       jlatest.start_time as job_start_time, jlatest.created_at as job_created_at,
                       COALESCE(sa.can_delete, 0) as can_delete,
                       COALESCE(sa.can_enable, 0) as can_enable,
                       COALESCE(sa.can_disable, 0) as can_disable,
                       COALESCE(sa.can_run, 0) as can_run,
                       COALESCE(sa.can_duplicate, 0) as can_duplicate,
                       CASE
                           WHEN sa.id IS NOT NULL THEN COALESCE(sa.can_edit, 0)
                           WHEN sch.user_id = ? THEN 1 ELSE 0
                       END as can_edit
                FROM schedule_folder_items i
                JOIN schedules sch ON sch.id = i.schedule_id
                JOIN scripts s ON s.id = sch.script_id
                JOIN users u ON u.id = sch.user_id
                LEFT JOIN schedule_access sa ON sa.schedule_id = sch.id AND sa.user_id = ?
                {job_join}
                WHERE i.folder_id = ? AND sch.is_deleted = 0
                ORDER BY i.sort_order ASC, i.id ASC
                """,
                (user_id, user_id, folder_id),
            )
        rows = []
        for r in cur.fetchall():
            item = _row(r)
            if item:
                rows.append(item)

    try:
        from app.services.script_days import enrich_schedule_days_fields
        for sch in rows:
            enrich_schedule_days_fields(sch)
    except Exception:
        for sch in rows:
            if sch.get("days") is None and sch.get("script_days") is not None:
                sch["effective_days"] = sch.get("script_days")
            else:
                sch["effective_days"] = sch.get("days")
            if sch.get("has_days_variable") is None:
                sch["has_days_variable"] = 1 if sch.get("script_days") is not None else 0

    # Match Schedules tab local last_run display
    try:
        from datetime import datetime, timezone

        def _to_local_last_run(val):
            if val is None or val == "":
                return None
            try:
                if hasattr(val, "strftime"):
                    dt = val
                    if getattr(dt, "tzinfo", None) is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone().strftime("%Y-%m-%d %H:%M")
                raw = str(val).replace("T", " ")[:19]
                last_utc = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                return last_utc.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                return str(val)[:16] if val else None

        for sch in rows:
            # Prefer schedule.last_run; fall back to latest job times (folder runs often skip mark_schedule_run)
            last_raw = sch.get("last_run") or sch.get("job_start_time") or sch.get("job_created_at")
            sch["last_run"] = _to_local_last_run(last_raw)
            if not sch.get("enabled"):
                sch["next_run"] = None
                continue
            # Folder members are not clock-scheduled; next run is via Run Folder
            sch["next_run"] = "—"
    except Exception:
        pass
    return rows


def get_schedule_folder_id(schedule_id: int) -> Optional[int]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT folder_id FROM schedule_folder_items WHERE schedule_id = ? LIMIT 1",
            (schedule_id,),
        )
        row = cur.fetchone()
        return int(row["folder_id"]) if row else None


def add_schedules_to_folder(
    folder_id: int,
    schedule_ids: list[int],
    *,
    as_copies: bool = False,
) -> int:
    """
    Add schedules to a folder.

    as_copies=False (default for newly created / move-between-folders):
        Link these schedule ids into the folder (may move membership from another folder).

    as_copies=True (Add Existing from Schedules section):
        Deep-copy each schedule into a new independent row and link the copies.
        Originals stay in the Schedules section, unchanged and unlinked.
    """
    from app import database as db

    added = 0
    with db_cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM schedule_folder_items WHERE folder_id = ?",
            (folder_id,),
        )
        next_order = int(cur.fetchone()["m"]) + 1

    for sid in schedule_ids:
        sid = int(sid)
        link_id = sid
        if as_copies:
            clone = db.clone_schedule_independent(sid, as_folder_copy=True)
            if not clone or not clone.get("id"):
                continue
            link_id = int(clone["id"])
        with db_cursor() as cur:
            if not as_copies:
                # Move membership only for the schedule being linked (folder copies / new creates)
                cur.execute("DELETE FROM schedule_folder_items WHERE schedule_id = ?", (link_id,))
            cur.execute(
                """
                INSERT INTO schedule_folder_items (folder_id, schedule_id, sort_order, enabled)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(folder_id, schedule_id) DO UPDATE SET sort_order = excluded.sort_order
                """,
                (folder_id, link_id, next_order),
            )
            next_order += 1
            added += 1

    # Apply folder days preference to newly linked members that have days = N
    folder = get_folder(folder_id) or {}
    if folder.get("days") is not None and added:
        try:
            apply_folder_days_to_members(folder_id, int(folder["days"]))
        except Exception:
            pass
    return added


def remove_schedules_from_folder(folder_id: int, schedule_ids: list[int]) -> int:
    """Unlink from folder. Folder deep-copies are soft-deleted so they do not clutter Schedules."""
    from app import database as db

    with db_cursor() as cur:
        removed = 0
        for sid in schedule_ids:
            sid = int(sid)
            cur.execute(
                "SELECT COALESCE(is_folder_copy, 0) AS is_folder_copy FROM schedules WHERE id = ?",
                (sid,),
            )
            row = cur.fetchone()
            is_copy = bool(row and int(row["is_folder_copy"] or 0) == 1)
            cur.execute(
                "DELETE FROM schedule_folder_items WHERE folder_id = ? AND schedule_id = ?",
                (folder_id, sid),
            )
            if cur.rowcount:
                removed += 1
                if is_copy:
                    try:
                        db.delete_schedule(sid)
                    except Exception:
                        pass
        return removed


def set_item_enabled(folder_id: int, schedule_id: int, enabled: int) -> bool:
    """Toggle folder-item enable and keep schedules.enabled in sync (shared Status UI)."""
    enabled = 1 if int(enabled) else 0
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE schedule_folder_items SET enabled = ?
            WHERE folder_id = ? AND schedule_id = ?
            """,
            (enabled, folder_id, schedule_id),
        )
        ok = cur.rowcount > 0
        cur.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?",
            (enabled, schedule_id),
        )
        return ok


def reorder_folder_items(folder_id: int, schedule_ids_in_order: list[int]) -> bool:
    with db_cursor() as cur:
        for idx, sid in enumerate(schedule_ids_in_order):
            cur.execute(
                """
                UPDATE schedule_folder_items SET sort_order = ?
                WHERE folder_id = ? AND schedule_id = ?
                """,
                (idx, folder_id, int(sid)),
            )
        return True


def get_active_folder_run(folder_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM schedule_folder_runs
            WHERE folder_id = ? AND status = 'running'
            ORDER BY id DESC LIMIT 1
            """,
            (folder_id,),
        )
        return _row(cur.fetchone())


def get_latest_folder_run(folder_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM schedule_folder_runs
            WHERE folder_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (folder_id,),
        )
        return _row(cur.fetchone())


def list_folder_runs(folder_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM schedule_folder_runs
            WHERE folder_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (folder_id, limit),
        )
        return [_row(r) for r in cur.fetchall()]


def _enabled_items(folder_id: int) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT i.*, sch.script_id, sch.worker_name, sch.enabled as schedule_enabled
            FROM schedule_folder_items i
            JOIN schedules sch ON sch.id = i.schedule_id
            WHERE i.folder_id = ? AND sch.is_deleted = 0
            ORDER BY i.sort_order ASC, i.id ASC
            """,
            (folder_id,),
        )
        return [_row(r) for r in cur.fetchall()]


def _runnable_items(folder_id: int) -> list[dict[str, Any]]:
    """Enabled folder members in sort order (same filter as sequential runs)."""
    all_items = _enabled_items(folder_id)
    return [
        i for i in all_items
        if int(i.get("schedule_enabled") if i.get("schedule_enabled") is not None else i.get("enabled") or 0) == 1
    ]


def _folder_uses_parallel(folder_or_run: dict[str, Any]) -> bool:
    """True only when opt-in parallel feature is enabled for this folder/run."""
    return int(folder_or_run.get("parallel_enabled") or 0) == 1


def _count_active_folder_jobs(folder_run_id: int) -> int:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM jobs
            WHERE folder_run_id = ?
              AND status IN ('pending', 'running', 'paused')
            """,
            (folder_run_id,),
        )
        row = cur.fetchone()
        return int(row["c"] if row else 0)


def _active_folder_job_ids(folder_run_id: int) -> list[int]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id FROM jobs
            WHERE folder_run_id = ?
              AND status IN ('pending', 'running', 'paused')
            ORDER BY id ASC
            """,
            (folder_run_id,),
        )
        return [int(r["id"]) for r in cur.fetchall()]


def _add_seconds_iso(ts: str, seconds: int) -> str:
    base = _parse_ts(ts) or datetime.now(timezone.utc).replace(tzinfo=None)
    return (base + timedelta(seconds=max(0, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")


def _is_parallel_launch_due(run: dict[str, Any], now: Optional[str] = None) -> bool:
    """True when a parallel folder run may start the next script (gap elapsed or no gap)."""
    gap = max(0, int(run.get("script_gap_seconds") or 0))
    if gap <= 0:
        return True
    next_at = run.get("next_launch_at")
    if not next_at:
        return True
    now_dt = _parse_ts(now or _utc_now())
    due_dt = _parse_ts(next_at)
    if not due_dt or not now_dt:
        return True
    return now_dt >= due_dt


def _finalize_folder_run(
    folder_run_id: int,
    folder_id: int,
    completed: int,
    failed: int,
    current_index: int,
) -> dict[str, Any]:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute("SELECT * FROM schedule_folder_runs WHERE id = ?", (folder_run_id,))
        run = _row(cur.fetchone())
        if not run or run.get("status") != "running":
            return {"done": True, "status": (run or {}).get("status") or "stopped"}
        final = "completed" if failed == 0 else "failed"
        duration = _duration_seconds(run.get("started_at"), now)
        cur.execute(
            """
            UPDATE schedule_folder_runs
            SET status = ?, ended_at = ?, duration_seconds = ?,
                successful_count = ?, failed_count = ?, current_index = ?,
                current_job_id = NULL, current_schedule_id = NULL, next_launch_at = NULL
            WHERE id = ? AND status = 'running'
            """,
            (final, now, duration, completed, failed, current_index, folder_run_id),
        )
        if cur.rowcount == 0:
            return {"done": True, "status": "stopped"}
        cur.execute(
            """
            UPDATE schedule_folders
            SET status = ?, current_run_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (final, now, folder_id),
        )
    return {"done": True, "status": final}


def _launch_next_parallel_slot(run: dict[str, Any], create_job_fn) -> Optional[dict[str, Any]]:
    """
    Launch at most one next script for a parallel folder run if a slot is free
    and the start gap has elapsed. Returns launch info or None.
    """
    if create_job_fn is None:
        return None
    folder_run_id = int(run["id"])
    folder_id = int(run["folder_id"])
    if run.get("status") != "running" or not _folder_uses_parallel(run):
        return None

    max_c = max(1, int(run.get("max_concurrent") or 1))
    gap = max(0, int(run.get("script_gap_seconds") or 0))
    items = _runnable_items(folder_id)
    next_idx = int(run.get("current_index") or 0)
    completed = int(run.get("successful_count") or 0)
    failed = int(run.get("failed_count") or 0)

    if next_idx >= len(items):
        if _count_active_folder_jobs(folder_run_id) == 0:
            return _finalize_folder_run(folder_run_id, folder_id, completed, failed, next_idx)
        return None

    active = _count_active_folder_jobs(folder_run_id)
    if active >= max_c:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE schedule_folder_runs SET next_launch_at = NULL WHERE id = ? AND status = 'running'",
                (folder_run_id,),
            )
        return None

    now = _utc_now()
    next_at = run.get("next_launch_at")
    if next_at:
        due = _parse_ts(next_at)
        now_dt = _parse_ts(now)
        if due and now_dt and now_dt < due:
            return None

    nxt = items[next_idx]
    job = create_job_fn(nxt["worker_name"], nxt["script_id"], nxt["schedule_id"], folder_run_id)
    job_id = job.get("id") if isinstance(job, dict) else None
    new_idx = next_idx + 1
    active_after = active + 1
    # Schedule next staggered start if more scripts and slots remain
    more_to_start = new_idx < len(items) and active_after < max_c
    next_launch = _add_seconds_iso(now, gap) if (more_to_start and gap > 0) else None
    if more_to_start and gap <= 0:
        next_launch = now  # immediate follow-up on next process tick / recursive fill

    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE schedule_folder_runs
            SET current_index = ?, current_job_id = ?, current_schedule_id = ?,
                next_launch_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (new_idx, job_id, nxt["schedule_id"], next_launch, folder_run_id),
        )
        if cur.rowcount == 0:
            return None
    return {"done": False, "job": job, "index": next_idx, "launched": True}


def _fill_parallel_slots(run: dict[str, Any], create_job_fn, max_launches: int = 50) -> list[dict[str, Any]]:
    """Launch as many scripts as gap/slots allow (gap>0 → one per call wave via next_launch_at)."""
    launched: list[dict[str, Any]] = []
    gap = max(0, int(run.get("script_gap_seconds") or 0))
    for _ in range(max_launches):
        with db_cursor() as cur:
            cur.execute("SELECT * FROM schedule_folder_runs WHERE id = ?", (run["id"],))
            fresh = _row(cur.fetchone())
        if not fresh or fresh.get("status") != "running":
            break
        result = _launch_next_parallel_slot(fresh, create_job_fn)
        if not result or not result.get("launched"):
            if result and result.get("done"):
                launched.append(result)
            break
        launched.append(result)
        # With a gap, only one start per wave; scheduler resumes after next_launch_at
        if gap > 0:
            break
    return launched


def process_pending_folder_launches(create_job_fn=None) -> int:
    """Scheduler hook: start next parallel-folder scripts when gap elapsed / slots free."""
    if create_job_fn is None:
        return 0
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM schedule_folder_runs
            WHERE status = 'running'
              AND COALESCE(parallel_enabled, 0) = 1
              AND (next_launch_at IS NULL OR next_launch_at <= ?)
            ORDER BY id ASC
            """,
            (now,),
        )
        runs = [_row(r) for r in cur.fetchall()]
    started = 0
    for run in runs:
        results = _fill_parallel_slots(run, create_job_fn)
        started += sum(1 for r in results if r.get("launched"))
    return started


def start_folder_run(folder_id: int, triggered_by: Optional[int] = None, create_job_fn=None) -> dict[str, Any]:
    """
    Start folder execution.
    Default (parallel_enabled=0): sequential one-by-one (unchanged).
    Opt-in parallel: up to max_concurrent scripts with script_gap_seconds between starts.
    create_job_fn(worker_name, script_id, schedule_id, folder_run_id) must be provided.
    """
    if create_job_fn is None:
        raise ValueError("create_job_fn required")

    folder = get_folder(folder_id)
    if not folder:
        return {"error": "Folder not found"}
    if not folder.get("enabled"):
        return {"error": "Folder is disabled"}
    if get_active_folder_run(folder_id):
        return {"error": "Folder is already running"}

    all_items = _enabled_items(folder_id)
    # Use schedule.enable (same Status column as Schedules table); keep item flag as fallback
    items = [
        i for i in all_items
        if int(i.get("schedule_enabled") if i.get("schedule_enabled") is not None else i.get("enabled") or 0) == 1
    ]
    skipped = len(all_items) - len(items)
    if not items:
        return {"error": "No enabled scripts in folder"}

    pe, max_c, gap = _normalize_parallel_settings(
        folder.get("parallel_enabled"),
        folder.get("max_concurrent"),
        folder.get("script_gap_seconds"),
    )
    use_parallel = pe == 1

    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO schedule_folder_runs
                (folder_id, status, started_at, total_count, successful_count, failed_count,
                 skipped_count, current_index, triggered_by,
                 parallel_enabled, max_concurrent, script_gap_seconds, next_launch_at)
            VALUES (?, 'running', ?, ?, 0, 0, ?, 0, ?, ?, ?, ?, NULL)
            """,
            (folder_id, now, len(items), skipped, triggered_by, pe, max_c, gap),
        )
        run_id = cur.lastrowid
        cur.execute(
            """
            UPDATE schedule_folders
            SET status = 'running', last_run_at = ?, current_run_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, run_id, now, folder_id),
        )

    if not use_parallel:
        # —— Existing sequential path (behavior unchanged) ——
        first = items[0]
        job = create_job_fn(first["worker_name"], first["script_id"], first["schedule_id"], run_id)
        job_id = job.get("id") if isinstance(job, dict) else None
        with db_cursor() as cur:
            cur.execute(
                """
                UPDATE schedule_folder_runs
                SET current_job_id = ?, current_schedule_id = ?
                WHERE id = ?
                """,
                (job_id, first["schedule_id"], run_id),
            )
        return {"run_id": run_id, "job": job, "total": len(items)}

    # —— Parallel path ——
    with db_cursor() as cur:
        cur.execute("SELECT * FROM schedule_folder_runs WHERE id = ?", (run_id,))
        run = _row(cur.fetchone())
    launched = _fill_parallel_slots(run, create_job_fn) if run else []
    first_job = next((r.get("job") for r in launched if r.get("job")), None)
    return {
        "run_id": run_id,
        "job": first_job,
        "total": len(items),
        "parallel": True,
        "max_concurrent": max_c,
        "script_gap_seconds": gap,
        "started_now": sum(1 for r in launched if r.get("launched")),
    }


def stop_folder_run(folder_id: int) -> bool:
    run = get_active_folder_run(folder_id)
    if not run:
        return False
    now = _utc_now()
    duration = _duration_seconds(run.get("started_at"), now)
    job_id = run.get("current_job_id")
    parallel = _folder_uses_parallel(run)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE schedule_folder_runs
            SET status = 'stopped', ended_at = ?, duration_seconds = ?,
                current_job_id = NULL, current_schedule_id = NULL, next_launch_at = NULL
            WHERE id = ?
            """,
            (now, duration, run["id"]),
        )
        cur.execute(
            """
            UPDATE schedule_folders
            SET status = 'stopped', current_run_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, folder_id),
        )
    # Stop active script job(s) so the chain does not continue
    try:
        from app import database as db
        if parallel:
            for jid in _active_folder_job_ids(int(run["id"])):
                try:
                    db.stop_job(int(jid))
                except Exception:
                    pass
        elif job_id:
            db.stop_job(int(job_id))
    except Exception:
        pass
    return True


def advance_folder_run_after_job(job: dict[str, Any], terminal_status: str, create_job_fn=None) -> Optional[dict[str, Any]]:
    """Called when a job finishes. Starts next script or closes the run."""
    folder_run_id = job.get("folder_run_id")
    if not folder_run_id:
        return None
    if create_job_fn is None:
        return None

    with db_cursor() as cur:
        cur.execute("SELECT * FROM schedule_folder_runs WHERE id = ?", (folder_run_id,))
        run = _row(cur.fetchone())
    if not run or run.get("status") != "running":
        return None

    # —— Opt-in parallel path ——
    if _folder_uses_parallel(run):
        folder_id = int(run["folder_id"])
        items = _runnable_items(folder_id)
        completed = int(run.get("successful_count") or 0)
        failed = int(run.get("failed_count") or 0)
        next_idx = int(run.get("current_index") or 0)
        gap = max(0, int(run.get("script_gap_seconds") or 0))

        if terminal_status == "completed":
            completed += 1
        elif terminal_status in ("error", "stopped"):
            failed += 1

        now = _utc_now()
        with db_cursor() as cur:
            cur.execute(
                """
                UPDATE schedule_folder_runs
                SET successful_count = ?, failed_count = ?
                WHERE id = ? AND status = 'running'
                """,
                (completed, failed, folder_run_id),
            )
            if cur.rowcount == 0:
                return {"done": True, "status": "stopped"}

        active = _count_active_folder_jobs(int(folder_run_id))
        if next_idx >= len(items) and active == 0:
            return _finalize_folder_run(int(folder_run_id), folder_id, completed, failed, next_idx)

        # Free slot: launch when gap elapsed, otherwise schedule next start
        max_c = max(1, int(run.get("max_concurrent") or 1))
        if next_idx < len(items) and active < max_c:
            with db_cursor() as cur:
                cur.execute("SELECT * FROM schedule_folder_runs WHERE id = ?", (folder_run_id,))
                fresh = _row(cur.fetchone())
            if fresh and _is_parallel_launch_due(fresh, now):
                results = _fill_parallel_slots(fresh, create_job_fn)
                if results:
                    return results[-1]
                return {"done": False}
            if gap > 0:
                # Gap not elapsed yet — keep earliest pending launch time
                with db_cursor() as cur:
                    cur.execute(
                        """
                        UPDATE schedule_folder_runs
                        SET next_launch_at = COALESCE(
                            CASE
                                WHEN next_launch_at IS NOT NULL AND next_launch_at > ? THEN next_launch_at
                                ELSE NULL
                            END,
                            ?
                        )
                        WHERE id = ? AND status = 'running'
                        """,
                        (now, _add_seconds_iso(now, gap), folder_run_id),
                    )
                return {"done": False, "waiting_gap": True}
            return {"done": False}

        return {"done": False, "active": active}

    # —— Existing sequential path (behavior unchanged) ——
    folder_id = int(run["folder_id"])
    items = [
        i for i in _enabled_items(folder_id)
        if int(i.get("schedule_enabled") if i.get("schedule_enabled") is not None else i.get("enabled") or 0) == 1
    ]
    idx = int(run.get("current_index") or 0)
    completed = int(run.get("successful_count") or 0)
    failed = int(run.get("failed_count") or 0)

    if terminal_status == "completed":
        completed += 1
    elif terminal_status in ("error", "stopped"):
        failed += 1

    next_idx = idx + 1
    now = _utc_now()

    if next_idx >= len(items):
        final = "completed" if failed == 0 else "failed"
        duration = _duration_seconds(run.get("started_at"), now)
        with db_cursor() as cur:
            cur.execute("SELECT status FROM schedule_folder_runs WHERE id = ?", (folder_run_id,))
            fresh = cur.fetchone()
            if fresh and fresh["status"] != "running":
                return {"done": True, "status": fresh["status"]}
            cur.execute(
                """
                UPDATE schedule_folder_runs
                SET status = ?, ended_at = ?, duration_seconds = ?,
                    successful_count = ?, failed_count = ?, current_index = ?,
                    current_job_id = NULL, current_schedule_id = NULL
                WHERE id = ?
                """,
                (final, now, duration, completed, failed, next_idx, folder_run_id),
            )
            cur.execute(
                """
                UPDATE schedule_folders
                SET status = ?, current_run_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (final, now, folder_id),
            )
        return {"done": True, "status": final}

    nxt = items[next_idx]
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE schedule_folder_runs
            SET successful_count = ?, failed_count = ?, current_index = ?
            WHERE id = ? AND status = 'running'
            """,
            (completed, failed, next_idx, folder_run_id),
        )
        if cur.rowcount == 0:
            return {"done": True, "status": "stopped"}

    next_job = create_job_fn(nxt["worker_name"], nxt["script_id"], nxt["schedule_id"], folder_run_id)
    job_id = next_job.get("id") if isinstance(next_job, dict) else None
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE schedule_folder_runs
            SET current_job_id = ?, current_schedule_id = ?
            WHERE id = ? AND status = 'running'
            """,
            (job_id, nxt["schedule_id"], folder_run_id),
        )
    return {"done": False, "job": next_job, "index": next_idx}


def get_due_folders() -> list[dict[str, Any]]:
    """Enabled idle folders whose schedule (same rules as regular Scheduler) is due."""
    from app import database as db

    now = datetime.now()
    due = []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM schedule_folders
            WHERE is_deleted = 0 AND enabled = 1 AND COALESCE(status, 'idle') != 'running'
            """
        )
        rows = [_row(r) for r in cur.fetchall()]
    for f in rows:
        sch_type = (f.get("schedule_type") or "daily").lower()
        if sch_type == "manual":
            continue
        probe = {
            "run_time": f.get("run_time"),
            "schedule_type": f.get("schedule_type") or "daily",
            "schedule_config": f.get("schedule_config"),
            "last_run": f.get("last_run_at"),
            "updated_at": f.get("updated_at"),
        }
        if db.schedule_is_due(probe, now):
            due.append(f)
    return due


def schedule_ids_in_any_folder() -> set[int]:
    """Schedule IDs currently belonging to a non-deleted folder."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT i.schedule_id
            FROM schedule_folder_items i
            JOIN schedule_folders f ON f.id = i.folder_id
            WHERE COALESCE(f.is_deleted, 0) = 0
            """
        )
        return {int(r["schedule_id"]) for r in cur.fetchall()}


def schedule_ids_in_enabled_folders() -> set[int]:
    """Schedule IDs in enabled, non-deleted folders (folder scheduler owns these)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT i.schedule_id
            FROM schedule_folder_items i
            JOIN schedule_folders f ON f.id = i.folder_id
            WHERE COALESCE(f.is_deleted, 0) = 0 AND COALESCE(f.enabled, 0) = 1
            """
        )
        return {int(r["schedule_id"]) for r in cur.fetchall()}


def folder_member_schedule_ids(folder_id: int) -> set[int]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT schedule_id FROM schedule_folder_items WHERE folder_id = ?",
            (folder_id,),
        )
        return {int(r["schedule_id"]) for r in cur.fetchall()}
