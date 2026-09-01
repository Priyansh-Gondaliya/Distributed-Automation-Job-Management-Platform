"""
 Thread-safe PostgreSQL helpers for the automation controller (tbl_dfms_*).
Includes strict IP-based access control, ownership, and history tracking.
"""
import threading
import re
from datetime import datetime, timezone
from typing import Any, Optional

from werkzeug.security import generate_password_hash

from app import config
from app.db_compat import (
    IntegrityError,
    db_cursor,
    get_connection,
)

_local = threading.local()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _worker_is_fresh(last_seen: Any, offline_seconds: Optional[int] = None) -> bool:
    """True when last_seen is within the online window (UTC wall clock)."""
    ls = _parse_utc_ts(last_seen)
    if not ls:
        return False
    window = offline_seconds if offline_seconds is not None else config.WORKER_OFFLINE_SECONDS
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - ls).total_seconds()
    return age <= window


def _normalize_worker_status(worker: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Keep displayed status aligned with last_seen (fixes stale offline badge)."""
    if not worker:
        return worker
    if _worker_is_fresh(worker.get("last_seen")):
        worker["status"] = "online"
    else:
        worker["status"] = "offline"
    return worker


def init_schema() -> None:
    """Seed defaults. Tables come from postgres/01_create_schema_REVIEW_ONLY.sql."""
    _drop_unused_legacy_tables()
    _ensure_schedule_access_can_edit()
    _ensure_users_can_set_days()
    _ensure_users_is_disabled()
    _ensure_pc_access_periods()
    _ensure_scheduler_view_access()
    _ensure_worker_tree_sync()
    try:
        from app.services import schedule_folders as _sf
        _sf.ensure_schedule_folders_schema()
    except Exception as exc:
        print(f"schedule folders schema ensure failed: {exc}", flush=True)
    _ensure_tracking_status_columns()
    _seed_admin()
    try:
        cleanup_orphan_worker_uploads()
    except Exception:
        pass


def _drop_unused_legacy_tables() -> None:
    """Drop leftover tables/columns with no app queries and no future use."""
    with db_cursor() as cur:
        for physical in (
            "tbl_dfms_file_versions",
            "tbl_dfms_job_checkpoints",
            "tbl_dfms_schedule_folder_members",
            "tbl_dfms_user_ui_prefs",
        ):
            cur.execute(f"DROP TABLE IF EXISTS {physical}")
        # Unused chat-link columns (prefs moved off DB; no code reads these)
        cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS chat_user_id")
        cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS chat_username")


def _ensure_schedule_access_can_edit() -> None:
    """Add schedule_access.can_edit if missing; backfill from prior can_run edit gate."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'tbl_dfms_schedule_access'
              AND column_name = 'can_edit'
            """
        )
        if cur.fetchone():
            return

    with db_cursor() as cur:
        cur.execute("ALTER TABLE schedule_access ADD COLUMN can_edit INTEGER DEFAULT 0")
        # Edit was previously shown whenever can_run was granted
        cur.execute(
            "UPDATE schedule_access SET can_edit = 1 WHERE COALESCE(can_run, 0) = 1"
        )


def _ensure_users_can_set_days() -> None:
    """Add users.can_set_days if missing (scheduler Days column / bulk / drawer)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'tbl_dfms_users'
              AND column_name = 'can_set_days'
            """
        )
        if cur.fetchone():
            return
    with db_cursor() as cur:
        cur.execute("ALTER TABLE users ADD COLUMN can_set_days INTEGER DEFAULT 0")


def _ensure_users_is_disabled() -> None:
    """Add users.is_disabled if missing (blocks login when set)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'tbl_dfms_users'
              AND column_name = 'is_disabled'
            """
        )
        if cur.fetchone():
            return
    with db_cursor() as cur:
        cur.execute("ALTER TABLE users ADD COLUMN is_disabled INTEGER DEFAULT 0")


def user_is_disabled(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    user = get_user_by_id(int(user_id))
    if not user:
        return False
    return int(user.get("is_disabled") or 0) == 1


def user_can_set_days(user_id: Optional[int]) -> bool:
    """True if admin or users.can_set_days is granted."""
    if user_id is None:
        return False
    if is_admin(user_id):
        return True
    user = get_user_by_id(int(user_id))
    if not user:
        return False
    return int(user.get("can_set_days") or 0) == 1


def set_user_can_set_days(user_id: int, enabled: bool) -> bool:
    with db_cursor() as cur:
        cur.execute(
            "UPDATE users SET can_set_days = ? WHERE id = ?",
            (1 if enabled else 0, int(user_id)),
        )
        return cur.rowcount > 0


def _ensure_pc_access_periods() -> None:
    """Track PC assignment windows so History can retain past rows after revoke."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = 'tbl_dfms_user_pc_access_periods'
            """
        )
        if not cur.fetchone():
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_pc_access_periods (
                    id           BIGSERIAL PRIMARY KEY,
                    user_id      BIGINT NOT NULL,
                    worker_name  TEXT NOT NULL,
                    started_at   TEXT NOT NULL,
                    ended_at     TEXT,
                    CONSTRAINT fk_pc_access_periods_user
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pc_access_periods_user_worker
                ON user_pc_access_periods (user_id, worker_name)
                """
            )

    epoch = "1970-01-01 00:00:00"
    with db_cursor() as cur:
        # Open period for every current PC grant.
        # Use epoch on first migration so existing installs keep prior worker history;
        # new grants after this use open_pc_access_period() with the real start time.
        cur.execute(
            """
            INSERT INTO user_pc_access_periods (user_id, worker_name, started_at, ended_at)
            SELECT upa.user_id, upa.worker_name, ?, NULL
            FROM user_pc_access upa
            WHERE NOT EXISTS (
                SELECT 1 FROM user_pc_access_periods p
                WHERE p.user_id = upa.user_id
                  AND p.worker_name = upa.worker_name
                  AND p.ended_at IS NULL
            )
            """,
            (epoch,),
        )
        # Owned workers without an explicit grant still appear in list_accessible_workers
        cur.execute(
            """
            INSERT INTO user_pc_access_periods (user_id, worker_name, started_at, ended_at)
            SELECT w.owner_id, w.worker_name, ?, NULL
            FROM workers w
            WHERE w.owner_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM user_pc_access upa
                WHERE upa.user_id = w.owner_id AND upa.worker_name = w.worker_name
              )
              AND NOT EXISTS (
                SELECT 1 FROM user_pc_access_periods p
                WHERE p.user_id = w.owner_id
                  AND p.worker_name = w.worker_name
                  AND p.ended_at IS NULL
              )
            """,
            (epoch,),
        )


def _ensure_column_if_missing(physical_table: str, column_name: str, logical_ddl: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ?
              AND column_name = ?
            """,
            (physical_table, column_name),
        )
        if cur.fetchone():
            return
    with db_cursor() as cur:
        cur.execute(logical_ddl)


def _ensure_tracking_status_columns() -> None:
    """Manual Schedule Tracking override (admin Reports). Null = derive from latest job."""
    _ensure_column_if_missing(
        "tbl_dfms_schedules",
        "tracking_status",
        "ALTER TABLE schedules ADD COLUMN tracking_status TEXT",
    )
    _ensure_column_if_missing(
        "tbl_dfms_schedule_folders",
        "tracking_status",
        "ALTER TABLE schedule_folders ADD COLUMN tracking_status TEXT",
    )


def _seed_admin() -> None:
    """Create the admin user if it doesn't already exist."""
    with db_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            now = _utc_now()
            hashed = generate_password_hash("admin123")
            cur.execute(
                "INSERT INTO users (username, password_hash, role, nickname, registered_ip, created_at) VALUES (?, ?, 'admin', 'Administrator', '127.0.0.1', ?)",
                ("admin", hashed, now),
            )
            log_action(None, "user_created", "Seeded admin account", "127.0.0.1")


def row_to_dict(row: Optional[Any]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row)

# --- History Logging ---

def log_action(user_id: Optional[int], action: str, details: str, ip_address: Optional[str] = None, worker_name: Optional[str] = None) -> None:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO history_log (user_id, action, details, ip_address, worker_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, action, details, ip_address, worker_name, now)
        )

def get_history(limit: int = 200, user_id: Optional[int] = None, accessible_workers: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Return history rows.

    When user_id is set (non-admin scoped view):
      - always include the user's own actions
      - include worker-scoped rows whose created_at falls in a PC assignment period
        (current grants and retained past windows after revoke)
    accessible_workers is kept for callers but period windows are authoritative.
    """
    with db_cursor() as cur:
        if user_id:
            cur.execute(
                """
                SELECT h.*, u.username, u.nickname, u.role
                FROM history_log h
                LEFT JOIN users u ON h.user_id = u.id
                WHERE h.user_id = ?
                   OR (
                        h.worker_name IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM user_pc_access_periods p
                            WHERE p.user_id = ?
                              AND p.worker_name = h.worker_name
                              AND h.created_at >= p.started_at
                              AND (p.ended_at IS NULL OR h.created_at <= p.ended_at)
                        )
                   )
                ORDER BY h.created_at DESC
                LIMIT ?
                """,
                (user_id, user_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT h.*, u.username, u.nickname, u.role
                FROM history_log h
                LEFT JOIN users u ON h.user_id = u.id
                ORDER BY h.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [row_to_dict(r) for r in cur.fetchall()]


def user_can_view_history_at(user_id: int, worker_name: Optional[str], created_at: Optional[str], event_user_id: Optional[int] = None) -> bool:
    """True if user may view a history/file/job event (admin, own action, or period window)."""
    if is_admin(user_id):
        return True
    if event_user_id is not None and int(event_user_id) == int(user_id):
        return True
    if not worker_name or not created_at:
        return False
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok FROM user_pc_access_periods
            WHERE user_id = ?
              AND worker_name = ?
              AND ? >= started_at
              AND (ended_at IS NULL OR ? <= ended_at)
            LIMIT 1
            """,
            (user_id, worker_name, created_at, created_at),
        )
        return cur.fetchone() is not None

def toggle_starred_file(user_id: int, worker_name: str, file_path: str) -> bool:
    """Toggles the starred status of a file. Returns True if starred, False if unstarred."""
    with db_cursor() as cur:
        cur.execute("SELECT id FROM user_starred_files WHERE user_id = ? AND worker_name = ? AND file_path = ?", (user_id, worker_name, file_path))
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM user_starred_files WHERE id = ?", (row['id'],))
            return False
        else:
            cur.execute("INSERT INTO user_starred_files (user_id, worker_name, file_path, created_at) VALUES (?, ?, ?, ?)", (user_id, worker_name, file_path, _utc_now()))
            return True

def get_starred_files(user_id: int, worker_name: str) -> list[str]:
    """Returns a list of starred file paths for a user and worker."""
    with db_cursor() as cur:
        cur.execute("SELECT file_path FROM user_starred_files WHERE user_id = ? AND worker_name = ?", (user_id, worker_name))
        return [r['file_path'] for r in cur.fetchall()]


def get_starred_files_for_user(user_id: int) -> list[dict[str, Any]]:
    """All starred explorer paths for a user across workers."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT worker_name, file_path, created_at
            FROM user_starred_files
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def list_scripts_by_ids(script_ids: list[int], user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Full script rows (with last_run / next_run / access flags) for a small id set."""
    if not script_ids:
        return []
    placeholders = ",".join("?" * len(script_ids))
    admin = bool(user_id is not None and is_admin(user_id))
    with db_cursor() as cur:
        if user_id is None or admin:
            cur.execute(
                f"""
                SELECT s.*, u.username as username,
                       j.created_at as last_run,
                       j.status as log_status,
                       sch.run_time as next_run
                FROM scripts s
                LEFT JOIN users u ON s.owner_id = u.id
                LEFT JOIN (
                    SELECT script_id, MAX(id) as max_job_id
                    FROM jobs
                    GROUP BY script_id
                ) jmax ON jmax.script_id = s.id
                LEFT JOIN jobs j ON j.id = jmax.max_job_id
                LEFT JOIN (
                    SELECT script_id, MAX(id) as max_sch_id
                    FROM schedules
                    WHERE enabled = 1
                    GROUP BY script_id
                ) schmax ON schmax.script_id = s.id
                LEFT JOIN schedules sch ON sch.id = schmax.max_sch_id
                WHERE s.id IN ({placeholders})
                ORDER BY s.worker_name, s.script_name
                """,
                script_ids,
            )
        else:
            cur.execute(
                f"""
                SELECT s.*, u.username as username,
                       CASE WHEN s.owner_id = ? OR COALESCE(upa.can_access_all_files, 0) = 1 THEN 1 ELSE COALESCE(usa.can_run, 0) END as can_run,
                       CASE WHEN s.owner_id = ? OR COALESCE(upa.can_access_all_files, 0) = 1 THEN 1 ELSE COALESCE(usa.can_update, 0) END as can_update,
                       CASE WHEN s.owner_id = ? OR COALESCE(upa.can_access_all_files, 0) = 1 THEN 1 ELSE COALESCE(usa.can_delete, 0) END as can_delete,
                       j.created_at as last_run,
                       j.status as log_status,
                       sch.run_time as next_run
                FROM scripts s
                LEFT JOIN users u ON s.owner_id = u.id
                LEFT JOIN user_pc_access upa ON upa.worker_name = s.worker_name AND upa.user_id = ?
                LEFT JOIN user_script_access usa ON usa.script_id = s.id AND usa.user_id = ?
                LEFT JOIN (
                    SELECT script_id, MAX(id) as max_job_id
                    FROM jobs
                    GROUP BY script_id
                ) jmax ON jmax.script_id = s.id
                LEFT JOIN jobs j ON j.id = jmax.max_job_id
                LEFT JOIN (
                    SELECT script_id, MAX(id) as max_sch_id
                    FROM schedules
                    WHERE enabled = 1
                    GROUP BY script_id
                ) schmax ON schmax.script_id = s.id
                LEFT JOIN schedules sch ON sch.id = schmax.max_sch_id
                WHERE s.id IN ({placeholders})
                  AND (
                    s.owner_id = ?
                    OR upa.user_id IS NOT NULL
                    OR usa.user_id = ?
                  )
                ORDER BY s.worker_name, s.script_name
                """,
                [user_id, user_id, user_id, user_id, user_id, *script_ids, user_id, user_id],
            )
        return [row_to_dict(r) for r in cur.fetchall()]


def list_starred_scripts_for_dashboard(user_id: int) -> list[dict[str, Any]]:
    """Home page Scripts table: only File Explorer–starred files that map to scripts.

    Returns [] when the user has no stars — never falls back to the full catalog.
    Uses targeted lookups (not loading every script for the worker).
    """
    starred = get_starred_files_for_user(user_id)
    if not starred:
        return []

    script_ids: list[int] = []
    seen: set[int] = set()

    with db_cursor() as cur:
        for item in starred:
            worker_name = (item.get("worker_name") or "").strip()
            rel = _norm_fs_path(item.get("file_path") or "").lstrip("/")
            if not worker_name or not rel:
                continue
            base = rel.split("/")[-1]
            root = _norm_fs_path(get_worker_script_location(worker_name)).rstrip("/")
            abs_path = f"{root}/{rel}" if root else rel

            # Exact matches only — never LIKE '%name' (that can pull huge sets)
            cur.execute(
                """
                SELECT id, script_name, script_path FROM scripts
                WHERE worker_name = ?
                  AND (
                    script_name = ? COLLATE NOCASE
                    OR script_name = ? COLLATE NOCASE
                    OR replace(script_path, '\\', '/') = ? COLLATE NOCASE
                    OR replace(script_path, '\\', '/') = ? COLLATE NOCASE
                  )
                LIMIT 5
                """,
                (worker_name, rel, base, rel, abs_path),
            )
            rows = cur.fetchall()
            if not rows:
                continue
            # Prefer exact relative / absolute path, then full rel as script_name, then basename
            best = None
            best_score = -1
            rel_l, abs_l, base_l = rel.lower(), abs_path.lower(), base.lower()
            for row in rows:
                sp = _norm_fs_path(row["script_path"]).lower()
                sn = _norm_fs_path(row["script_name"]).lower()
                score = -1
                if sp == abs_l or sp == rel_l:
                    score = 3
                elif sn == rel_l:
                    score = 2
                elif sn == base_l:
                    score = 1
                if score > best_score:
                    best_score = score
                    best = row
            if best and int(best["id"]) not in seen:
                seen.add(int(best["id"]))
                script_ids.append(int(best["id"]))

    if not script_ids:
        return []
    return list_scripts_by_ids(script_ids, user_id=user_id)

# --- File History & Versions ---

def log_file_action(user_id: Optional[int], worker_name: str, file_path: str, action: str, old_content: Optional[str] = None, new_content: Optional[str] = None) -> None:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO file_history (file_path, worker_name, user_id, action, old_content, new_content, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (file_path, worker_name, user_id, action, old_content, new_content, now)
        )

def get_file_history(
    file_path: Optional[str] = None,
    worker_name: Optional[str] = None,
    limit: int = 100,
    user_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """File history. When user_id is set, scope to own rows + assignment-period worker rows."""
    with db_cursor() as cur:
        query = """
            SELECT f.*, u.username, u.nickname, u.role
            FROM file_history f
            LEFT JOIN users u ON f.user_id = u.id
        """
        params: list[Any] = []
        conditions = [
            # Permission grants belong in history_log (Permissions tab), not file history.
            # Use bound params so PostgreSQL/psycopg2 does not treat LIKE '%' as a format stub.
            "f.action NOT LIKE ?",
            "COALESCE(f.file_path, '') <> ?",
        ]
        params.extend(["Permission %", "PC Access"])
        if file_path:
            conditions.append("f.file_path = ?")
            params.append(file_path)
        if worker_name:
            conditions.append("f.worker_name = ?")
            params.append(worker_name)
        if user_id is not None:
            conditions.append(
                """(
                    f.user_id = ?
                    OR EXISTS (
                        SELECT 1 FROM user_pc_access_periods p
                        WHERE p.user_id = ?
                          AND p.worker_name = f.worker_name
                          AND f.created_at >= p.started_at
                          AND (p.ended_at IS NULL OR f.created_at <= p.ended_at)
                    )
                )"""
            )
            params.extend([user_id, user_id])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY f.created_at DESC LIMIT ?"
        params.append(limit)

        cur.execute(query, tuple(params))
        return [row_to_dict(r) for r in cur.fetchall()]

def get_file_history_by_id(history_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT f.*, u.username, u.username 
            FROM file_history f
            LEFT JOIN users u ON f.user_id = u.id
            WHERE f.id = ?
            """,
            (history_id,)
        )
        return row_to_dict(cur.fetchone())

def get_worker_script_location(worker_name: str) -> str:
    """Return the worker's configured script root (absolute path on the worker)."""
    worker = get_worker(worker_name)
    return (worker.get("script_location") if worker else None) or r"C:\Automation\scripts"


def normalize_worker_rel_path(path: str, worker_root: str = "") -> str:
    """Normalize a path to a forward-slash relative path under the worker root.

    Accepts relative tree paths or absolute Windows paths that include the worker root,
    so Allowed Paths entries like ``C:\\Automation\\scripts\\foo`` still match tree path ``foo``.
    """
    p = (path or "").replace("\\", "/").strip()
    if not p or p == "/":
        return ""
    root = (worker_root or "").replace("\\", "/").rstrip("/")
    if root:
        pl, rl = p.lower(), root.lower()
        if pl == rl:
            return ""
        if pl.startswith(rl + "/"):
            p = p[len(root) + 1 :]
    return p.strip("/")


def pc_has_access_all_files(perms: Optional[dict[str, Any]]) -> bool:
    return bool(perms and int(perms.get("can_access_all_files") or 0) == 1)


def pc_has_flag(perms: Optional[dict[str, Any]], flag: str) -> bool:
    """True if perms grant the flag, or All Files Access is enabled."""
    if not perms:
        return False
    if pc_has_access_all_files(perms):
        return True
    return int(perms.get(flag) or 0) == 1


def path_allowed_by_perms(
    path: str,
    perms: Optional[dict[str, Any]],
    worker_root: str = "",
    *,
    is_folder: bool = False,
) -> bool:
    """Check allowed_paths. Empty allowed_paths / All Files Access = unrestricted."""
    if not perms or pc_has_access_all_files(perms):
        return True
    allowed_str = (perms.get("allowed_paths") or "").strip()
    if not allowed_str:
        return True
    rel = normalize_worker_rel_path(path, worker_root)
    allowed_set = {
        normalize_worker_rel_path(p, worker_root)
        for p in allowed_str.split(",")
        if p.strip()
    }
    # Empty string after normalize means the worker root itself was listed → all paths
    if "" in allowed_set:
        return True
    allowed_set.discard("")
    if not allowed_set:
        return True
    if is_folder:
        # Show ancestors (to navigate in) and descendants of allowed dirs
        if rel == "":
            return True
        return any(
            rel == a or rel.startswith(a + "/") or a.startswith(rel + "/")
            for a in allowed_set
        )
    # Files must live under an allowed directory (or be the allowed path itself)
    return any(rel == a or rel.startswith(a + "/") for a in allowed_set)


def extension_allowed_by_perms(path: str, perms: Optional[dict[str, Any]]) -> bool:
    """Check allowed_extensions. Empty / All Files Access = unrestricted. Folders always OK."""
    if not perms or pc_has_access_all_files(perms):
        return True
    allowed = (perms.get("allowed_extensions") or "").strip()
    if not allowed:
        return True
    import os
    ext = os.path.splitext(path.replace("\\", "/"))[1].lower()
    allowed_set = {e.strip().lower() for e in allowed.split(",") if e.strip()}
    if not allowed_set:
        return True
    return ext in allowed_set


def build_frontend_pc_perms(user_id: int, worker_name: str, perms: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Permissions object returned to File Explorer. All Files Access unlocks every CRUD flag."""
    admin = is_admin(user_id)
    if perms is None:
        perms = get_pc_access_details(user_id, worker_name) or {}
    access_all = admin or pc_has_access_all_files(perms)
    return {
        "is_admin": admin,
        "can_access_all_files": access_all,
        "can_run": access_all or int(perms.get("can_run") or 0) == 1,
        "can_create_folder": access_all or int(perms.get("can_create_folder") or 0) == 1,
        "can_rename_folder": access_all or int(perms.get("can_rename_folder") or 0) == 1,
        "can_delete_folder": access_all or int(perms.get("can_delete_folder") or 0) == 1,
        "can_update_file": access_all or int(perms.get("can_update_file") or 0) == 1,
        "can_create_file": access_all or int(perms.get("can_create_file") or 0) == 1,
        "can_delete_file": access_all or int(perms.get("can_delete_file") or 0) == 1,
        "can_rename_file": access_all or int(perms.get("can_rename_file") or 0) == 1,
        "can_edit_file": access_all or int(perms.get("can_edit_file") or 0) == 1,
    }


def check_pc_file_operation(
    user_id: int,
    worker_name: str,
    flag: str,
    *paths: str,
    is_folder: bool = False,
    check_ext: bool = True,
    perms: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Validate PC flag + allowed_paths (+ extensions for files).

    Returns an error string, or None if the operation is allowed.
    """
    if is_admin(user_id):
        return None
    if perms is None:
        perms = get_pc_access_details(user_id, worker_name)
    if not perms:
        return "permission denied"
    if not pc_has_flag(perms, flag):
        return "permission denied"
    worker_root = get_worker_script_location(worker_name)
    for raw in paths:
        if raw is None:
            continue
        p = str(raw).replace("\\", "/").strip()
        if not path_allowed_by_perms(p, perms, worker_root, is_folder=is_folder):
            return "path not allowed"
        if check_ext and not is_folder and not extension_allowed_by_perms(p, perms):
            return "extension not allowed"
    return None


def check_pc_file_view(
    user_id: int,
    worker_name: str,
    *paths: str,
    is_folder: bool = False,
    check_ext: bool = True,
    perms: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Allow viewing a file with PC access (path/ext only) — does not require Edit File."""
    if is_admin(user_id):
        return None
    if perms is None:
        perms = get_pc_access_details(user_id, worker_name)
    if not perms:
        worker = get_worker(worker_name)
        if worker and worker.get("owner_id") == user_id:
            ensure_ip_matched_pc_access(user_id, worker_name, granted_by=user_id)
            perms = get_pc_access_details(user_id, worker_name)
        if not perms:
            return "permission denied"
    worker_root = get_worker_script_location(worker_name)
    for raw in paths:
        if raw is None:
            continue
        p = str(raw).replace("\\", "/").strip()
        if not path_allowed_by_perms(p, perms, worker_root, is_folder=is_folder):
            return "path not allowed"
        if check_ext and not is_folder and not extension_allowed_by_perms(p, perms):
            return "extension not allowed"
    return None


def user_can_edit_pc_file(
    user_id: int,
    worker_name: str,
    file_path: str,
    perms: Optional[dict[str, Any]] = None,
) -> bool:
    """True when Edit File (or All Files Access / admin) allows modifying this path."""
    return (
        check_pc_file_operation(
            user_id,
            worker_name,
            "can_edit_file",
            file_path,
            is_folder=False,
            check_ext=True,
            perms=perms,
        )
        is None
    )


def is_admin(user_id: int) -> bool:
    cache = getattr(_local, "admin_cache", None)
    if cache is None:
        _local.admin_cache = {}
        cache = _local.admin_cache
    if user_id in cache:
        return cache[user_id]
    user = get_user_by_id(user_id)
    result = user is not None and user.get("role") == "admin"
    cache[user_id] = result
    return result

def create_user(username: str, password_hash: str, registered_ip: str, role: str = "user") -> Optional[dict[str, Any]]:
    now = _utc_now()
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, registered_ip, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, password_hash, role, registered_ip, now)
            )
            user_id = cur.lastrowid
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            user = row_to_dict(cur.fetchone())
    except IntegrityError:
        return None  # Username exists

    # Outside create transaction: IP link must never undo a successful registration
    if user and registered_ip:
        try:
            associate_user_with_workers_by_ip(user["id"], registered_ip)
        except Exception as e:
            print(f"associate_user_with_workers_by_ip after register failed: {e}")
    return user


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        return row_to_dict(cur.fetchone())


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return row_to_dict(cur.fetchone())

def get_user_by_registered_ip(ip_address: str) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE registered_ip = ?", (ip_address,))
        return row_to_dict(cur.fetchone())

def update_user_login(user_id: int, ip_address: str) -> None:
    with db_cursor() as cur:
        cur.execute("UPDATE users SET last_login_ip = ? WHERE id = ?", (ip_address, user_id))


def update_script_days(script_id: int, days: Optional[int]) -> bool:
    with db_cursor() as cur:
        cur.execute("UPDATE scripts SET days = ? WHERE id = ?", (days, script_id))
        return True


def get_scripts_days_map() -> dict[str, Any]:
    """id(str) → days (int|None) for scheduler UI refresh after worker sync."""
    with db_cursor() as cur:
        cur.execute("SELECT id, days FROM scripts")
        return {str(int(r["id"])): r["days"] for r in cur.fetchall()}


def list_worker_script_paths(worker_name: str) -> list[dict[str, Any]]:
    """Lightweight paths for worker days refresh (id, name, path)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, script_name, script_path, days
            FROM scripts
            WHERE worker_name = ?
            ORDER BY id
            """,
            (worker_name,),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def backfill_script_days_from_disk(worker_name: Optional[str] = None) -> dict[str, int]:
    """
    Re-read days = N from script files the controller can open.
    Useful when scripts were registered without a scan (File Explorer) or
    live outside the worker SCRIPTS_DIR so sync never updates days.
    """
    from pathlib import Path
    from app.services.script_days import extract_days_from_source

    updated = skipped = missing = 0
    rows = list_worker_script_paths(worker_name) if worker_name else None
    if rows is None:
        with db_cursor() as cur:
            cur.execute("SELECT id, script_name, script_path, days FROM scripts")
            rows = [row_to_dict(r) for r in cur.fetchall()]

    for row in rows:
        path = Path(row.get("script_path") or "")
        if not path.is_file() or path.suffix.lower() != ".py":
            missing += 1
            continue
        try:
            extracted = extract_days_from_source(
                path.read_text(encoding="utf-8", errors="ignore")
            )
        except Exception:
            skipped += 1
            continue
        if row.get("days") == extracted:
            skipped += 1
            continue
        update_script_days(int(row["id"]), extracted)
        updated += 1
    return {
        "updated": updated,
        "skipped": skipped,
        "missing_or_non_py": missing,
        "total": len(rows),
    }


def _ensure_scheduler_view_access() -> None:
    """Per-user grant: viewer may open another user's Schedules / Folders / Job History."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = 'tbl_dfms_scheduler_view_access'
            """
        )
        if cur.fetchone():
            return
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_view_access (
                id              BIGSERIAL PRIMARY KEY,
                viewer_user_id  BIGINT NOT NULL,
                target_user_id  BIGINT NOT NULL,
                granted_by      BIGINT,
                granted_at      TEXT NOT NULL,
                CONSTRAINT uq_scheduler_view_access UNIQUE (viewer_user_id, target_user_id),
                CONSTRAINT fk_scheduler_view_viewer
                    FOREIGN KEY (viewer_user_id) REFERENCES users(id) ON DELETE CASCADE,
                CONSTRAINT fk_scheduler_view_target
                    FOREIGN KEY (target_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )


_SCHEDULE_ACTION_FLAGS = (
    "can_delete", "can_enable", "can_disable", "can_run", "can_duplicate", "can_edit",
)


def grant_scheduler_view_access(viewer_user_id: int, target_user_id: int, granted_by: int) -> bool:
    if int(viewer_user_id) == int(target_user_id):
        return False
    if is_admin(target_user_id):
        return False
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO scheduler_view_access (viewer_user_id, target_user_id, granted_by, granted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (viewer_user_id, target_user_id) DO UPDATE SET
                granted_by = excluded.granted_by,
                granted_at = excluded.granted_at
            """,
            (viewer_user_id, target_user_id, granted_by, now),
        )
    return True


def revoke_all_scheduler_view_access(viewer_user_id: int) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM scheduler_view_access WHERE viewer_user_id = ?", (viewer_user_id,))


def get_all_scheduler_view_access() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT va.*, v.username AS viewer_username, t.username AS target_username
            FROM scheduler_view_access va
            JOIN users v ON v.id = va.viewer_user_id
            JOIN users t ON t.id = va.target_user_id
            ORDER BY v.username, t.username
            """
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def list_scheduler_view_targets(viewer_user_id: int) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role
            FROM scheduler_view_access va
            JOIN users u ON u.id = va.target_user_id
            WHERE va.viewer_user_id = ?
              AND COALESCE(u.role, '') <> 'admin'
            ORDER BY u.username
            """,
            (viewer_user_id,),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def can_view_scheduler_user(viewer_user_id: int, target_user_id: int) -> bool:
    if int(viewer_user_id) == int(target_user_id):
        return True
    if is_admin(viewer_user_id):
        return True
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT 1 AS ok FROM scheduler_view_access
            WHERE viewer_user_id = ? AND target_user_id = ?
            """,
            (viewer_user_id, target_user_id),
        )
        return cur.fetchone() is not None


def overlay_schedule_flags(rows: list[dict[str, Any]], viewer_id: int) -> list[dict[str, Any]]:
    """Replace can_* with the viewer's own grants (read-only unless separately granted)."""
    if is_admin(viewer_id):
        for r in rows:
            for k in _SCHEDULE_ACTION_FLAGS:
                r[k] = 1
        return rows
    mine = {
        int(s["id"]): s
        for s in list_schedules(viewer_id, exclude_folder_members=False)
    }
    for r in rows:
        src = mine.get(int(r["id"]))
        for k in _SCHEDULE_ACTION_FLAGS:
            r[k] = (src.get(k) or 0) if src else 0
    return rows


def list_schedules_for_viewer(
    viewer_id: int,
    scope_user_id: Optional[int] = None,
    *,
    exclude_folder_members: bool = True,
) -> list[dict[str, Any]]:
    """Schedules visible for the scheduler page, with action flags for the viewer."""
    if scope_user_id is None:
        return list_schedules(viewer_id, exclude_folder_members=exclude_folder_members)
    rows = list_schedules(
        int(scope_user_id),
        exclude_folder_members=exclude_folder_members,
        as_user=True,
    )
    return overlay_schedule_flags(rows, viewer_id)


def get_all_users() -> list[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users ORDER BY username")
        return [row_to_dict(r) for r in cur.fetchall()]


def update_user_full(
    user_id: int,
    username: str,
    registered_ip: str,
    role: str,
    password_hash: Optional[str] = None,
    is_disabled: Optional[int] = None,
) -> bool:
    """Update user. Admin role cannot be demoted to user. Primary admin cannot be disabled."""
    existing = get_user_by_id(user_id)
    if not existing:
        return False

    # Promote user→admin allowed; demote admin→user never allowed
    if (existing.get("role") or "") == "admin":
        role = "admin"
    elif role not in ("user", "admin"):
        role = "user"

    # Primary admin account stays admin and enabled
    if (existing.get("username") or "") == "admin" or username == "admin":
        role = "admin"
        is_disabled = 0

    disabled_val = None
    if is_disabled is not None:
        disabled_val = 1 if int(is_disabled) else 0

    try:
        with db_cursor() as cur:
            if password_hash and disabled_val is not None:
                cur.execute(
                    "UPDATE users SET username = ?, registered_ip = ?, role = ?, password_hash = ?, is_disabled = ? WHERE id = ?",
                    (username, registered_ip, role, password_hash, disabled_val, user_id),
                )
            elif password_hash:
                cur.execute(
                    "UPDATE users SET username = ?, registered_ip = ?, role = ?, password_hash = ? WHERE id = ?",
                    (username, registered_ip, role, password_hash, user_id),
                )
            elif disabled_val is not None:
                cur.execute(
                    "UPDATE users SET username = ?, registered_ip = ?, role = ?, is_disabled = ? WHERE id = ?",
                    (username, registered_ip, role, disabled_val, user_id),
                )
            else:
                cur.execute(
                    "UPDATE users SET username = ?, registered_ip = ?, role = ? WHERE id = ?",
                    (username, registered_ip, role, user_id),
                )
            ok = cur.rowcount > 0
        if ok:
            cache = getattr(_local, "admin_cache", None)
            if cache is not None:
                cache.pop(user_id, None)
        if ok and registered_ip:
            associate_user_with_workers_by_ip(user_id, registered_ip)
        return ok
    except IntegrityError:
        return False
    except Exception as e:
        return False


# --- Workers ---


def register_worker(worker_name: str, ip_address: str, state: str = "idle") -> dict[str, Any]:
    now = _utc_now()
    
    # Try to find an owner based on the worker's IP matching a user's registered IP
    owner = get_user_by_registered_ip(ip_address)
    owner_id = owner["id"] if owner else None

    with db_cursor() as cur:
        cur.execute("SELECT worker_name, owner_id FROM workers WHERE ip_address = ?", (ip_address,))
        existing = cur.fetchone()
        
        if existing:
            # If the IP exists, we DO NOT automatically trust the worker's reported name.
            # Keep existing owner_id if we didn't find one via IP match.
            final_owner = owner_id if owner_id is not None else existing["owner_id"]
            
            cur.execute(
                """
                UPDATE workers SET
                    status = 'online',
                    state = ?,
                    last_seen = ?,
                    owner_id = ?
                WHERE ip_address = ?
                """,
                (state, now, final_owner, ip_address),
            )
            cur.execute("SELECT * FROM workers WHERE ip_address = ?", (ip_address,))
        else:
            cur.execute(
                """
                INSERT INTO workers (worker_name, ip_address, status, state, owner_id, last_seen)
                VALUES (?, ?, 'online', ?, ?, ?)
                ON CONFLICT(worker_name) DO UPDATE SET
                    ip_address = excluded.ip_address,
                    status = 'online',
                    state = excluded.state,
                    owner_id = COALESCE(excluded.owner_id, workers.owner_id),
                    last_seen = excluded.last_seen
                """,
                (worker_name, ip_address, state, owner_id, now),
            )
            cur.execute("SELECT * FROM workers WHERE worker_name = ?", (worker_name,))
            
        worker = row_to_dict(cur.fetchone())

    # Auto PC access with Run for IP-matched owner (does not overwrite existing grants)
    if worker and owner_id:
        ensure_ip_matched_pc_access(owner_id, worker["worker_name"], granted_by=owner_id)
    return worker


def touch_worker(worker_name: str, ip_address: Optional[str] = None, state: Optional[str] = None) -> None:
    """Mark worker online. Throttles identical heartbeats to cut write load on poll storms.

    Offline detection uses a 30s last_seen window; skipping identical touches for a few
    seconds does not change online/offline classification or returned API payloads.
    Exception: if the row was marked offline, always heal status/last_seen immediately
    so the UI cannot stay stuck on badge-offline while the agent is still polling.
    """
    import time
    now_m = time.monotonic()
    cache = getattr(_local, "touch_cache", None)
    if cache is None:
        _local.touch_cache = {}
        cache = _local.touch_cache
    prev = cache.get(worker_name)
    # Throttle only when ip/state payload is unchanged (same heartbeat)
    if (
        prev
        and (now_m - prev[0]) < 5.0
        and prev[1] == ip_address
        and prev[2] == state
    ):
        # Fast heal: do not skip if status flipped to offline during the throttle window
        with db_cursor() as cur:
            cur.execute(
                """
                UPDATE workers
                SET status = 'online', last_seen = ?
                WHERE worker_name = ? AND status != 'online'
                """,
                (_utc_now(), worker_name),
            )
            if cur.rowcount == 0:
                return
        cache[worker_name] = (now_m, ip_address, state)
        return

    now = _utc_now()
    with db_cursor() as cur:
        if ip_address and state:
            cur.execute(
                """
                UPDATE workers
                SET last_seen = ?, status = 'online', ip_address = ?, state = ?
                WHERE worker_name = ?
                """,
                (now, ip_address, state, worker_name),
            )
        elif ip_address:
            cur.execute(
                """
                UPDATE workers
                SET last_seen = ?, status = 'online', ip_address = ?
                WHERE worker_name = ?
                """,
                (now, ip_address, worker_name),
            )
        elif state:
            cur.execute(
                """
                UPDATE workers
                SET last_seen = ?, status = 'online', state = ?
                WHERE worker_name = ?
                """,
                (now, state, worker_name),
            )
        else:
            cur.execute(
                """
                UPDATE workers
                SET last_seen = ?, status = 'online'
                WHERE worker_name = ?
                """,
                (now, worker_name),
            )
    cache[worker_name] = (now_m, ip_address, state)

def rename_worker(old_name: str, new_name: str) -> bool:
    """Rename a worker across all relevant tables."""
    if old_name == new_name:
        return True
    with db_cursor() as cur:
        cur.execute("SELECT id FROM workers WHERE worker_name = ?", (new_name,))
        if cur.fetchone():
            return False

        # No ON UPDATE CASCADE on worker_name FKs: clone row, retarget children, drop old.
        cur.execute("SELECT * FROM workers WHERE worker_name = ?", (old_name,))
        w = cur.fetchone()
        if not w:
            return False
        cur.execute(
            "UPDATE workers SET ip_address = NULL WHERE worker_name = ?",
            (old_name,),
        )
        cur.execute(
            """
            INSERT INTO workers (
                worker_name, ip_address, status, state, script_location,
                owner_id, last_seen, env_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_name,
                w["ip_address"],
                w["status"],
                w["state"],
                w["script_location"] if "script_location" in w.keys() else "",
                w["owner_id"],
                w["last_seen"],
                w["env_details"] if "env_details" in w.keys() else "{}",
            ),
        )
        for table in (
            "scripts",
            "jobs",
            "commands",
            "user_pc_access",
            "user_pc_access_periods",
            "schedules",
            "file_history",
            "history_log",
            "worker_file_tree",
            "worker_tree_sync",
            "user_starred_files",
            "file_watchlist",
            "scraper_reports",
        ):
            cur.execute(
                f"UPDATE {table} SET worker_name = ? WHERE worker_name = ?",
                (new_name, old_name),
            )
        cur.execute("DELETE FROM workers WHERE worker_name = ?", (old_name,))
    delete_legacy_worker_tree_json(old_name)
    delete_legacy_worker_tree_json(new_name)
    return True


def legacy_worker_tree_json_path(worker_name: str) -> str:
    """Path to legacy uploads/{worker}_tree.json (obsolete file-explorer cache)."""
    import os
    safe = (worker_name or "").replace("\\", "_").replace("/", "_").strip()
    return os.path.join(config.WORKER_ROOT, f"{safe}_tree.json")


def delete_legacy_worker_tree_json(worker_name: str) -> bool:
    """Delete legacy tree JSON for a worker if present."""
    import os
    path = legacy_worker_tree_json_path(worker_name)
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


def cleanup_orphan_worker_uploads() -> dict[str, Any]:
    """Align uploads/ with current DB + File Explorer API.

    - Live trees live in SQLite ``worker_file_tree`` (/api/sync-file-tree).
    - Legacy ``uploads/{worker}_tree.json`` files are obsolete → remove all of them.
    - Also purge ``worker_file_tree`` rows whose worker no longer exists.
    """
    import os
    os.makedirs(config.WORKER_ROOT, exist_ok=True)

    with db_cursor() as cur:
        cur.execute("SELECT worker_name FROM workers")
        known = {row["worker_name"] for row in cur.fetchall()}

    removed_files: list[str] = []
    for name in os.listdir(config.WORKER_ROOT):
        if not name.endswith("_tree.json"):
            continue
        path = os.path.join(config.WORKER_ROOT, name)
        try:
            os.remove(path)
            removed_files.append(name)
        except OSError:
            pass

    # Orphan SQLite tree rows (worker deleted without cascade cleanup)
    removed_db = 0
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT worker_name FROM worker_file_tree")
        tree_workers = [row["worker_name"] for row in cur.fetchall()]
        orphans = [w for w in tree_workers if w not in known]
        for w in orphans:
            cur.execute("DELETE FROM worker_file_tree WHERE worker_name = ?", (w,))
            removed_db += cur.rowcount
            invalidate_worker_file_tree_stats(w)

    return {"removed_files": removed_files, "removed_db_rows": removed_db, "known_workers": sorted(known)}


def refresh_worker_statuses(offline_seconds: int) -> None:
    """Mark workers offline when last_seen is too old. Also cleanup zombie jobs.

    Uses Python UTC comparison (same clock as last_seen writes) so status cannot
    drift from SQLite datetime('now') edge cases. Also heals rows that still have
    a fresh last_seen but were left status='offline'.

    Throttled (~2s) so parallel home-page polls do not rewrite workers repeatedly.
    """
    import time
    now_m = time.monotonic()
    last = getattr(_local, "last_status_refresh", 0.0)
    if (now_m - last) < 2.0:
        return
    _local.last_status_refresh = now_m

    now_utc_str = _utc_now()
    with db_cursor() as cur:
        cur.execute("SELECT worker_name, status, last_seen FROM workers")
        rows = cur.fetchall()
        offline_workers: list[str] = []
        heal_online: list[str] = []
        for row in rows:
            name = row["worker_name"]
            fresh = _worker_is_fresh(row["last_seen"], offline_seconds)
            status = (row["status"] or "").lower()
            if not fresh and status != "offline":
                offline_workers.append(name)
            elif fresh and status != "online":
                heal_online.append(name)

        if heal_online:
            placeholders = ",".join("?" * len(heal_online))
            cur.execute(
                f"""
                UPDATE workers
                SET status = 'online'
                WHERE worker_name IN ({placeholders})
                """,
                heal_online,
            )

        if offline_workers:
            placeholders = ",".join("?" * len(offline_workers))
            cur.execute(
                f"""
                UPDATE workers
                SET status = 'offline', state = 'idle'
                WHERE worker_name IN ({placeholders})
                """,
                offline_workers,
            )

            # Cleanup zombie jobs: any 'running' or 'paused' job for these newly offline workers becomes 'error'
            cur.execute(
                f"""
                UPDATE jobs
                SET status = 'error', updated_at = ?, paused_at = NULL,
                    output = COALESCE(output, '') || chr(10) || '[Worker went offline unexpectedly]'
                WHERE status IN ('running', 'paused') AND worker_name IN ({placeholders})
                """,
                [now_utc_str, *offline_workers],
            )


def list_workers() -> list[dict[str, Any]]:
    refresh_worker_statuses(config.WORKER_OFFLINE_SECONDS)
    with db_cursor() as cur:
        cur.execute("SELECT w.*, u.username as username FROM workers w LEFT JOIN users u ON w.owner_id = u.id ORDER BY w.worker_name")
        return [_normalize_worker_status(row_to_dict(r)) for r in cur.fetchall()]


def list_owned_worker_names(owner_id: int) -> list[str]:
    """Worker PCs owned by a user (for Site Master desktop notify)."""
    if not owner_id:
        return []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT worker_name FROM workers
            WHERE owner_id = ?
            ORDER BY worker_name
            """,
            (int(owner_id),),
        )
        return [(r["worker_name"] or "").strip() for r in cur.fetchall() if (r["worker_name"] or "").strip()]


def get_worker(worker_name: str) -> Optional[dict[str, Any]]:
    refresh_worker_statuses(config.WORKER_OFFLINE_SECONDS)
    with db_cursor() as cur:
        cur.execute("SELECT w.*, u.username as username FROM workers w LEFT JOIN users u ON w.owner_id = u.id WHERE w.worker_name = ?", (worker_name,))
        return _normalize_worker_status(row_to_dict(cur.fetchone()))

def get_worker_by_ip(ip_address: str) -> Optional[dict[str, Any]]:
    refresh_worker_statuses(config.WORKER_OFFLINE_SECONDS)
    with db_cursor() as cur:
        cur.execute("SELECT w.*, u.username as username FROM workers w LEFT JOIN users u ON w.owner_id = u.id WHERE w.ip_address = ?", (ip_address,))
        return _normalize_worker_status(row_to_dict(cur.fetchone()))

def update_worker_owner(worker_name: str, owner_id: Optional[int]) -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT owner_id FROM workers WHERE worker_name = ?", (worker_name,))
        row = cur.fetchone()
        prev_owner = row["owner_id"] if row else None
        cur.execute("UPDATE workers SET owner_id = ? WHERE worker_name = ?", (owner_id, worker_name))
        ok = cur.rowcount > 0
    if ok:
        # Close ownership-only visibility window for previous owner if they have no PC grant
        if prev_owner and (owner_id is None or int(prev_owner) != int(owner_id)):
            if not get_pc_access_details(int(prev_owner), worker_name):
                close_pc_access_period(int(prev_owner), worker_name)
        if owner_id is not None:
            # Ensure new owner has an open history window (grant may also open one)
            open_pc_access_period(int(owner_id), worker_name)
    return ok

# --- Scripts ---


def _norm_fs_path(path: str) -> str:
    return (path or "").replace("\\", "/").strip()


def _normalize_win_path(path: str) -> str:
    n = (path or "").replace("/", "\\").strip().lower()
    while "\\\\" in n:
        n = n.replace("\\\\", "\\")
    return n.rstrip("\\")


def _path_under_root(path: str, root: str) -> bool:
    """True if path is the root or a file/folder under it (case-insensitive)."""
    p = _normalize_win_path(path)
    r = _normalize_win_path(root)
    if not p or not r:
        return False
    return p == r or p.startswith(r + "\\")


def _win_drive(path: str) -> str:
    """Windows drive letter (e.g. 'c:') or empty. Avoids importing os in this module."""
    n = _normalize_win_path(path)
    if len(n) >= 2 and n[1] == ":":
        return n[:2]
    return ""


def _is_automation_scripts_path(path: str) -> bool:
    """True when path is under C:\\Automation\\scripts (worker default tree)."""
    return _path_under_root(path, r"C:\Automation\scripts")


def _prefer_script_path(
    existing: Optional[str],
    incoming: str,
    worker_root: Optional[str] = None,
) -> str:
    """
    Keep scheduled/Event absolute paths when worker sync re-registers the same
    script_name from the configured script_location tree (basename collision).

    Rule: an incoming path under the worker config root must not replace an
    existing path that lives outside that root.
    """
    inc = (incoming or "").strip()
    ex = (existing or "").strip()
    if not ex:
        return inc
    if not inc:
        return ex
    root = (worker_root or "").strip() or r"C:\Automation\scripts"
    if _path_under_root(inc, root) and not _path_under_root(ex, root):
        # Same machine: keep intentional paths outside config root (e.g. Desktop\Event).
        ex_drive = _win_drive(ex)
        root_drive = _win_drive(root)
        if ex_drive and root_drive and ex_drive == root_drive:
            return ex
        # Different drive — stale path from another PC; prefer worker-reported local path.
        return inc
    if _is_automation_scripts_path(inc) and not _is_automation_scripts_path(ex):
        return ex
    return inc


def register_script(
    worker_name: str,
    script_name: str,
    script_path: str,
    days: Optional[int] = None,
    *,
    sync_days: bool = False,
) -> dict[str, Any]:
    """
    Register or update a script row.

    sync_days=False (File Explorer / ensure): do not overwrite scripts.days with NULL.
    sync_days=True (worker scan): always store the scanned days value (incl. None).
    """
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, owner_id FROM workers WHERE worker_name = ?", (worker_name,)
        )
        worker = cur.fetchone()
        if not worker:
            worker_data = register_worker(worker_name, "unknown")
            owner_id = worker_data.get("owner_id")
        else:
            owner_id = worker["owner_id"]

        worker_root = get_worker_script_location(worker_name)
        cur.execute(
            "SELECT script_path FROM scripts WHERE worker_name = ? AND script_name = ?",
            (worker_name, script_name),
        )
        prev = cur.fetchone()
        prev_path = prev["script_path"] if prev else None
        path_to_store = _prefer_script_path(prev_path, script_path, worker_root)

        if sync_days:
            cur.execute(
                """
                INSERT INTO scripts (worker_name, script_name, script_path, owner_id, days, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_name, script_name) DO UPDATE SET
                    script_path = excluded.script_path,
                    days = excluded.days,
                    owner_id = COALESCE(scripts.owner_id, excluded.owner_id)
                """,
                (worker_name, script_name, path_to_store, owner_id, days, now),
            )
        else:
            cur.execute(
                """
                INSERT INTO scripts (worker_name, script_name, script_path, owner_id, days, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_name, script_name) DO UPDATE SET
                    script_path = excluded.script_path,
                    days = COALESCE(excluded.days, scripts.days),
                    owner_id = COALESCE(scripts.owner_id, excluded.owner_id)
                """,
                (worker_name, script_name, path_to_store, owner_id, days, now),
            )
        cur.execute(
            """
            SELECT * FROM scripts
            WHERE worker_name = ? AND script_name = ?
            """,
            (worker_name, script_name),
        )
        return row_to_dict(cur.fetchone())


def find_script_by_worker_file_path(worker_name: str, file_rel_path: str) -> Optional[dict[str, Any]]:
    """Match a file-tree relative path to a scripts row (handles abs/rel + case)."""
    rel = _norm_fs_path(file_rel_path).lstrip("/")
    if not worker_name or not rel:
        return None
    root = _norm_fs_path(get_worker_script_location(worker_name)).rstrip("/")
    abs_path = f"{root}/{rel}" if root else rel
    rel_l = rel.lower()
    abs_l = abs_path.lower()
    root_l = root.lower()

    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM scripts WHERE worker_name = ?",
            (worker_name,),
        )
        best = None
        best_score = -1
        for row in cur.fetchall():
            sp = _norm_fs_path(row["script_path"])
            sp_l = sp.lower()
            score = -1
            if sp_l == abs_l or sp_l == rel_l:
                score = 3
            elif root_l and sp_l.startswith(root_l + "/") and sp_l[len(root_l) + 1 :] == rel_l:
                score = 3
            elif sp_l.endswith("/" + rel_l):
                score = 2
            elif sp_l.endswith(rel_l) and (len(sp_l) == len(rel_l) or sp_l[-(len(rel_l) + 1)] == "/"):
                score = 1
            if score > best_score:
                best_score = score
                best = row
                if score >= 3:
                    break
        return row_to_dict(best) if best is not None else None


def ensure_script_for_worker_file_path(worker_name: str, file_rel_path: str) -> Optional[dict[str, Any]]:
    """Find or register a script for a File Explorer path so Run works for duplicate basenames."""
    import os
    rel = _norm_fs_path(file_rel_path).lstrip("/")
    if not worker_name or not rel:
        return None
    existing = find_script_by_worker_file_path(worker_name, rel)
    if existing:
        return existing

    root = _norm_fs_path(get_worker_script_location(worker_name)).rstrip("/")
    abs_path = f"{root}/{rel}" if root else rel
    abs_path_store = os.path.normpath(abs_path.replace("/", os.sep))
    # Relative path as script_name avoids basename collisions across folders
    return register_script(worker_name, rel, abs_path_store)

def register_scripts_bulk(worker_name: str, scripts_data: list[dict]) -> int:
    """Register multiple scripts in a single transaction for high performance."""
    if not scripts_data:
        return 0
        
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute("SELECT id, owner_id FROM workers WHERE worker_name = ?", (worker_name,))
        worker = cur.fetchone()
        if not worker:
            worker_data = register_worker(worker_name, "unknown")
            owner_id = worker_data.get("owner_id")
        else:
            owner_id = worker["owner_id"]

        worker_root = get_worker_script_location(worker_name)
        # Preserve Desktop/Event paths when bulk sync sends config-tree duplicates
        cur.execute(
            "SELECT script_name, script_path FROM scripts WHERE worker_name = ?",
            (worker_name,),
        )
        existing = {
            (r["script_name"] or ""): (r["script_path"] or "")
            for r in cur.fetchall()
        }
            
        values = []
        for s in scripts_data:
            name = (s.get("script_name") or "").strip()
            path = (s.get("script_path") or "").strip()
            days = s.get("days")
            if name and path:
                path = _prefer_script_path(existing.get(name), path, worker_root)
                values.append((worker_name, name, path, owner_id, days, now))
                
        if not values:
            return 0
            
        cur.executemany(
            """
            INSERT INTO scripts (worker_name, script_name, script_path, owner_id, days, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_name, script_name) DO UPDATE SET
                script_path = excluded.script_path,
                days = excluded.days,
                owner_id = COALESCE(scripts.owner_id, excluded.owner_id)
            """,
            values
        )
        return len(values)


def list_scripts(worker_name: Optional[str] = None) -> list[dict[str, Any]]:
    with db_cursor() as cur:
        query = """
            SELECT s.*, u.username as username,
                   j.created_at as last_run,
                   j.status as log_status,
                   sch.run_time as next_run
            FROM scripts s 
            LEFT JOIN users u ON s.owner_id = u.id
            LEFT JOIN (
                SELECT script_id, MAX(id) as max_job_id
                FROM jobs
                GROUP BY script_id
            ) jmax ON jmax.script_id = s.id
            LEFT JOIN jobs j ON j.id = jmax.max_job_id
            LEFT JOIN (
                SELECT script_id, MAX(id) as max_sch_id
                FROM schedules
                WHERE enabled = 1
                GROUP BY script_id
            ) schmax ON schmax.script_id = s.id
            LEFT JOIN schedules sch ON sch.id = schmax.max_sch_id
        """
        if worker_name:
            cur.execute(query + " WHERE s.worker_name = ? ORDER BY s.script_name", (worker_name,))
        else:
            cur.execute(query + " ORDER BY s.worker_name, s.script_name")
        return [row_to_dict(r) for r in cur.fetchall()]


def list_scripts_for_permissions() -> list[dict[str, Any]]:
    """Lightweight script catalog for the Permissions page (Python scripts only)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.worker_name, s.script_name, s.script_path, u.username
            FROM scripts s
            LEFT JOIN users u ON s.owner_id = u.id
            WHERE (
                LOWER(COALESCE(s.script_name, '')) LIKE '%.py'
                OR LOWER(COALESCE(s.script_path, '')) LIKE '%.py'
            )
            ORDER BY s.worker_name, s.script_name
            """
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def list_script_paths_for_explorer(user_id: int, worker_name: str, is_admin_user: bool = False) -> list[dict[str, Any]]:
    """Return id/script_path only for File Explorer path→script mapping (no job/schedule joins)."""
    with db_cursor() as cur:
        if is_admin_user or is_admin(user_id):
            cur.execute(
                "SELECT id, script_path FROM scripts WHERE worker_name = ? ORDER BY script_name",
                (worker_name,),
            )
        else:
            cur.execute(
                """
                SELECT s.id, s.script_path
                FROM scripts s
                LEFT JOIN user_pc_access upa ON upa.worker_name = s.worker_name AND upa.user_id = ?
                LEFT JOIN user_script_access usa ON usa.script_id = s.id AND usa.user_id = ?
                WHERE s.worker_name = ?
                  AND (upa.user_id IS NOT NULL OR usa.user_id = ? OR s.owner_id = ?)
                ORDER BY s.script_name
                """,
                (user_id, user_id, worker_name, user_id, user_id),
            )
        return [row_to_dict(r) for r in cur.fetchall()]


def list_scheduler_script_folders(
    user_id: int, worker_name: str, is_admin_user: bool = False
) -> list[dict[str, Any]]:
    """Unique parent folders of that worker's scripts (scheduler / watchlist picker).

    New .py scripts show their folder immediately; deleting the last script in a
    folder removes that folder. Paths are relative to the worker root.
    """
    worker_root = get_worker_script_location(worker_name)
    rows = list_script_paths_for_explorer(user_id, worker_name, is_admin_user=is_admin_user)
    scheduled_ids: set[int] = set()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.id
            FROM scripts s
            JOIN schedules sch ON sch.script_id = s.id
            WHERE s.worker_name = ?
              AND COALESCE(sch.is_deleted, 0) = 0
            """,
            (worker_name,),
        )
        scheduled_ids = {int(r["id"]) for r in cur.fetchall() if r and r["id"] is not None}

    folders: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = (row.get("script_path") or "").strip()
        if not raw:
            continue
        rel = normalize_worker_rel_path(raw, worker_root).replace("\\", "/")
        if not rel:
            parent = ""
        else:
            base = rel.rsplit("/", 1)[-1]
            if "." in base:
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            else:
                parent = rel
        if parent and _is_watchlist_noise_path(parent):
            continue
        key = parent.lower()
        sid = int(row["id"]) if row.get("id") is not None else 0
        item = folders.get(key)
        if item is None:
            name = parent.rsplit("/", 1)[-1] if parent else "Worker root"
            folders[key] = {
                "path": parent,
                "name": name,
                "script_count": 1,
                "scheduled_count": 1 if sid in scheduled_ids else 0,
                "type": "folder",
            }
        else:
            item["script_count"] = int(item.get("script_count") or 0) + 1
            if sid in scheduled_ids:
                item["scheduled_count"] = int(item.get("scheduled_count") or 0) + 1

    out = list(folders.values())
    out.sort(key=lambda f: ((f.get("path") or "").lower(), f.get("name") or ""))
    return out


def list_schedules_for_permissions() -> list[dict[str, Any]]:
    """Lightweight schedule catalog for the Permissions page (no per-row job subqueries)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sch.id, sch.run_time, sch.user_id, s.script_name,
                   u.username, w.owner_id AS worker_owner_id
            FROM schedules sch
            JOIN scripts s ON s.id = sch.script_id
            JOIN users u ON u.id = sch.user_id
            LEFT JOIN workers w ON w.worker_name = s.worker_name
            WHERE sch.is_deleted = 0
            ORDER BY sch.run_time
            """
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def get_script(script_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT s.*, u.username as username FROM scripts s LEFT JOIN users u ON s.owner_id = u.id WHERE s.id = ?", (script_id,))
        return row_to_dict(cur.fetchone())


def remove_scripts_not_in_list(worker_name: str, script_names: list[str]) -> int:
    """Remove scripts that no longer exist on the worker (auto-sync).

    Scripts that still have active scheduler rows (is_deleted = 0) are never
    deleted, so changing the worker Script Location / explorer resync cannot
    cascade-delete schedules via scripts ON DELETE CASCADE.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.script_name
            FROM scripts s
            JOIN schedules sch ON sch.script_id = s.id
            WHERE s.worker_name = ?
              AND COALESCE(sch.is_deleted, 0) = 0
            """,
            (worker_name,),
        )
        protected = {row["script_name"] for row in cur.fetchall()}

        if not script_names:
            if not protected:
                cur.execute("DELETE FROM scripts WHERE worker_name = ?", (worker_name,))
                return cur.rowcount
            placeholders = ",".join("?" * len(protected))
            cur.execute(
                f"DELETE FROM scripts WHERE worker_name = ? AND script_name NOT IN ({placeholders})",
                [worker_name, *protected],
            )
            return cur.rowcount

        cur.execute("SELECT script_name FROM scripts WHERE worker_name = ?", (worker_name,))
        existing = {row["script_name"] for row in cur.fetchall()}
        incoming = set(script_names)
        to_delete = [n for n in (existing - incoming) if n not in protected]

        deleted_count = 0
        for i in range(0, len(to_delete), 500):
            batch = to_delete[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                f"DELETE FROM scripts WHERE worker_name = ? AND script_name IN ({placeholders})",
                [worker_name, *batch],
            )
            deleted_count += cur.rowcount

        return deleted_count

def update_script_owner(script_id: int, owner_id: Optional[int]) -> bool:
    with db_cursor() as cur:
        cur.execute("UPDATE scripts SET owner_id = ? WHERE id = ?", (owner_id, script_id))
        return cur.rowcount > 0

# --- Jobs ---


def create_job(
    worker_name: str,
    script_id: int,
    schedule_id: Optional[int] = None,
    folder_run_id: Optional[int] = None,
) -> dict[str, Any]:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (worker_name, script_id, schedule_id, status, output, created_at, updated_at, folder_run_id)
            VALUES (?, ?, ?, 'pending', '', ?, ?, ?)
            """,
            (worker_name, script_id, schedule_id, now, now, folder_run_id),
        )
        job_id = cur.lastrowid
        cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return row_to_dict(cur.fetchone())


def claim_pending_job(worker_name: str) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending job for a worker."""
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT j.id
            FROM jobs j
            WHERE j.worker_name = ? AND j.status = 'pending'
            ORDER BY j.created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (worker_name,),
        )
        locked = cur.fetchone()
        if not locked:
            return None
        job_id = locked["id"]
        cur.execute(
            """
            UPDATE jobs SET status = 'running', updated_at = ?, start_time = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now, now, job_id),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(
            """
            SELECT j.*, s.script_name, s.script_path, COALESCE(sch.days, s.days) as days
            FROM jobs j
            JOIN scripts s ON s.id = j.script_id
            LEFT JOIN schedules sch ON sch.id = j.schedule_id
            WHERE j.id = ?
            """,
            (job_id,),
        )
        job = row_to_dict(cur.fetchone())
        if job and job.get("schedule_id"):
            mark_schedule_run(
                int(job["schedule_id"]),
                f"Job #{job_id} claimed by worker",
            )
        return job


def update_job_pid(job_id: int, pid: int) -> Optional[dict[str, Any]]:
    """Set the OS process ID for a running job."""
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "UPDATE jobs SET pid = ?, updated_at = ? WHERE id = ?",
            (pid, now, job_id),
        )
        cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return row_to_dict(cur.fetchone())


def update_job_output(job_id: int, output: str) -> None:
    with db_cursor() as cur:
        cur.execute("UPDATE jobs SET output = ? WHERE id = ?", (output, job_id))


def update_job(
    job_id: int,
    status: str,
    output: str = "",
    duration: Optional[float] = None,
    total_images: Optional[int] = None,
    output_count: Optional[int] = None,
    exit_code: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    now = _utc_now()
    with db_cursor() as cur:
        if status in ("completed", "error", "stopped"):
            cur.execute(
                """
                UPDATE jobs SET status = ?, output = ?, updated_at = ?, end_time = ?,
                    duration = ?, total_images = ?, output_count = ?, exit_code = ?, pid = NULL, paused_at = NULL
                WHERE id = ?
                """,
                (status, output, now, now, duration, total_images, output_count, exit_code, job_id),
            )
        elif status == "paused":
            cur.execute(
                """
                UPDATE jobs SET status = 'paused', output = ?, updated_at = ?, paused_at = ?
                WHERE id = ?
                """,
                (output, now, now, job_id),
            )
        else:
            cur.execute(
                """
                UPDATE jobs SET status = ?, output = ?, updated_at = ?, paused_at = NULL
                WHERE id = ?
                """,
                (status, output, now, job_id),
            )
        cur.execute("SELECT * FROM jobs WHERE id = ?",(job_id,))
        job = row_to_dict(cur.fetchone())

    if job and status in ("completed", "error", "stopped"):
        # Keep schedule Last Run in sync for folder + manual runs
        sid = job.get("schedule_id")
        if sid:
            try:
                mark_schedule_run(int(sid), f"Job #{job_id} {status}")
            except Exception:
                pass
        try:
            from app.services import schedule_folders as sf

            def _create(worker_name, script_id, schedule_id, folder_run_id):
                return create_job(worker_name, script_id, schedule_id=schedule_id, folder_run_id=folder_run_id)

            sf.advance_folder_run_after_job(job, status, create_job_fn=_create)
            # Do not wait for the 15s scheduler tick — resume parallel folder runs immediately
            sf.process_pending_folder_launches(create_job_fn=_create)
        except Exception as exc:
            print(f"folder advance after job #{job_id} failed: {exc}", flush=True)
    return job


def list_jobs(limit: int = 100, status: Optional[str] = None, user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """List jobs. When user_id is set, only jobs whose created_at falls in a PC assignment period."""
    with db_cursor() as cur:
        params: list[Any] = []
        where = []
        if status:
            where.append("j.status = ?")
            params.append(status)
        if user_id is not None:
            where.append(
                """EXISTS (
                    SELECT 1 FROM user_pc_access_periods p
                    WHERE p.user_id = ?
                      AND p.worker_name = j.worker_name
                      AND COALESCE(j.created_at, j.updated_at) >= p.started_at
                      AND (p.ended_at IS NULL OR COALESCE(j.created_at, j.updated_at) <= p.ended_at)
                )"""
            )
            params.append(user_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        cur.execute(
            f"""
            SELECT j.*, s.script_name, s.script_path, s.owner_id AS owner_user_id, u.username as username
            FROM jobs j
            JOIN scripts s ON s.id = j.script_id
            LEFT JOIN users u ON s.owner_id = u.id
            {where_sql}
            ORDER BY j.updated_at DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def get_job(job_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT j.*, s.script_name, s.script_path
            FROM jobs j
            JOIN scripts s ON s.id = j.script_id
            WHERE j.id = ?
            """,
            (job_id,),
        )
        return row_to_dict(cur.fetchone())


def retry_job(job_id: int) -> Optional[dict[str, Any]]:
    job = get_job(job_id)
    if not job or job["status"] not in ("completed", "error", "stopped"):
        return None
    script = get_script(job["script_id"])
    if not script:
        return None
    return create_job(script["worker_name"], job["script_id"], schedule_id=job.get("schedule_id"))


def _output_looks_pc_terminated(output: str) -> bool:
    low = (output or "").lower()
    return any(
        s in low
        for s in (
            "[terminated on worker pc]",
            "process ended without report",
            "keyboardinterrupt",
            "ctrl+c",
        )
    )


def stop_job(job_id: int) -> Optional[dict[str, Any]]:
    job = get_job(job_id)
    if not job or job["status"] not in ("pending", "running", "paused"):
        return None
    out = job.get("output") or ""
    # Already killed on the worker — do not relabel as a dashboard Stop.
    if _output_looks_pc_terminated(out):
        updated = update_job(job_id, "stopped", output=out)
        try:
            ensure_stopped_job_report(job_id)
        except Exception:
            pass
        return updated
    updated = update_job(
        job_id,
        "stopped",
        output=(out + "\n[Stop requested from dashboard]").strip(),
    )
    try:
        ensure_stopped_job_report(job_id)
    except Exception:
        pass
    return updated


def pause_job(job_id: int) -> Optional[dict[str, Any]]:
    """Pause a running job. Only works if current status is 'running'."""
    job = get_job(job_id)
    if not job or job["status"] != "running":
        return None
    return update_job(job_id, "paused", output=job.get("output", ""))


def resume_job(job_id: int) -> Optional[dict[str, Any]]:
    """Resume a paused job. Only works if current status is 'paused'."""
    job = get_job(job_id)
    if not job or job["status"] != "paused":
        return None
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'running', paused_at = NULL, updated_at = ? WHERE id = ?",
            (now, job_id),
        )
        cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return row_to_dict(cur.fetchone())


def get_stale_paused_jobs(timeout_minutes: int = 10) -> list[dict[str, Any]]:
    """Return jobs that have been paused longer than timeout_minutes."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT j.*, s.script_name, s.script_path
            FROM jobs j
            JOIN scripts s ON s.id = j.script_id
            WHERE j.status = 'paused'
              AND j.paused_at IS NOT NULL
              AND j.paused_at < ?
            """,
            (cutoff,),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def reconcile_orphaned_running_jobs(
    worker_name: str,
    active_job_ids: list[int],
    grace_seconds: int = 45,
) -> list[int]:
    """
    Mark running jobs for this worker as terminal when the worker no longer has them.
    Crash (Traceback) → error; clean finish → completed; otherwise → stopped.
    """
    from datetime import timedelta

    active = {int(x) for x in (active_job_ids or []) if x is not None}
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    stopped_ids: list[int] = []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, updated_at, start_time
            FROM jobs
            WHERE worker_name = ? AND status = 'running'
            """,
            (worker_name,),
        )
        rows = [row_to_dict(r) for r in cur.fetchall()]

    for row in rows:
        jid = int(row["id"])
        if jid in active:
            continue
        stamp = row.get("updated_at") or row.get("start_time") or ""
        # Still within grace window (just claimed / starting)
        if stamp and str(stamp)[:19] > cutoff:
            continue
        job = get_job(jid)
        prev_output = (job or {}).get("output") or ""
        low = prev_output.lower()
        ended_fail = "traceback (most recent call last)" in low
        try:
            from app.blueprints.api.routes import _ended_with_failure
            ended_fail = _ended_with_failure(prev_output)
        except Exception:
            pass
        if ended_fail:
            status = "error"
            note = "[Stopped] Process ended without report after script crash (Traceback)."
        else:
            # Prefer not inventing "completed" here — leave stopped unless log clearly finished
            status = "stopped"
            note = "[Stopped] Process ended without report (manual stop or crash on PC)."
        merged = (prev_output + "\n" + note) if prev_output else note
        update_job(jid, status, output=merged)
        try:
            ensure_stopped_job_report(jid)
        except Exception:
            pass
        stopped_ids.append(jid)
    return stopped_ids


def reconcile_stale_pending_jobs(
    max_age_seconds: int = 7200,
    worker_name: Optional[str] = None,
) -> list[int]:
    """
    Expire pending jobs that were never claimed within max_age_seconds.
    Typical case: job queued for an offline worker and left forever.
    Does not touch running/paused/completed jobs.
    """
    from datetime import timedelta

    age = max(300, int(max_age_seconds or 7200))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    expired: list[int] = []
    with db_cursor() as cur:
        if worker_name:
            cur.execute(
                """
                SELECT id, worker_name, created_at
                FROM jobs
                WHERE status = 'pending' AND created_at < ?
                  AND worker_name = ?
                ORDER BY id
                LIMIT 100
                """,
                (cutoff, worker_name),
            )
        else:
            cur.execute(
                """
                SELECT id, worker_name, created_at
                FROM jobs
                WHERE status = 'pending' AND created_at < ?
                ORDER BY id
                LIMIT 100
                """,
                (cutoff,),
            )
        rows = [row_to_dict(r) for r in cur.fetchall()]

    mins = max(1, age // 60)
    for row in rows:
        jid = int(row["id"])
        wname = row.get("worker_name") or "unknown"
        note = (
            f"[Expired] Pending job was not claimed within {mins} minutes "
            f"(worker '{wname}' offline or busy). Re-queue to run again."
        )
        # Use error so it is distinct from user Stop; exit_code unchanged (None).
        update_job(jid, "error", output=note, exit_code=None)
        try:
            ensure_stopped_job_report(jid)
        except Exception:
            pass
        expired.append(jid)
    return expired


def get_jobs_status_batch(job_ids: list[int]) -> list[dict[str, Any]]:
    """Return id/status/output/duration for many jobs in one query (dashboard live poll)."""
    ids: list[int] = []
    seen: set[int] = set()
    for raw in job_ids or []:
        try:
            jid = int(raw)
        except (TypeError, ValueError):
            continue
        if jid > 0 and jid not in seen:
            seen.add(jid)
            ids.append(jid)
    if not ids:
        return []
    # Cap to protect DB from oversized requests
    ids = ids[:50]
    placeholders = ",".join("?" * len(ids))
    with db_cursor() as cur:
        cur.execute(
            f"""
            SELECT id, status, output, duration
            FROM jobs
            WHERE id IN ({placeholders})
            """,
            ids,
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def list_jobs_for_worker(worker_name: str, limit: int = 100) -> list[dict[str, Any]]:
    """List jobs for a specific worker (efficient DB-side filter)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT j.*, s.script_name, s.script_path
            FROM jobs j
            JOIN scripts s ON s.id = j.script_id
            WHERE j.worker_name = ?
            ORDER BY j.updated_at DESC
            LIMIT ?
            """,
            (worker_name, limit),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def get_job_counts(user_id: Optional[int] = None, accessible_workers: Optional[list[str]] = None) -> dict[str, int]:
    """Return aggregate job counts by status for dashboard stats. 
    If user_id provided, filter by owned or accessible workers."""
    with db_cursor() as cur:
        if user_id and not is_admin(user_id) and accessible_workers is not None:
            if not accessible_workers:
                return {"pending": 0, "running": 0, "completed": 0, "error": 0, "stopped": 0, "total": 0}
            
            placeholders = ",".join("?" * len(accessible_workers))
            cur.execute(
                f"""
                SELECT status, COUNT(*) as cnt
                FROM jobs
                WHERE worker_name IN ({placeholders})
                GROUP BY status
                """,
                accessible_workers
            )
        else:
            cur.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM jobs
                GROUP BY status
                """
            )
        counts = {row["status"]: row["cnt"] for row in cur.fetchall()}
        return {
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "paused": counts.get("paused", 0),
            "completed": counts.get("completed", 0),
            "error": counts.get("error", 0),
            "stopped": counts.get("stopped", 0),
            "total": sum(counts.values()),
        }

# --- Worker Config ---

def update_worker_config(worker_name: str, script_location: str) -> bool:
    """
    Update worker script_location.
    Returns True if the path changed (and tree was cleared), False if unchanged.

    On path change:
      - Clears explorer file/folder index (worker_file_tree) for this worker so the
        worker can replace it with the new path's scan.
      - Clears explorer starred entries for this worker (path-relative to old root).
      - Does NOT delete scripts, schedules, jobs, or schedule_access — active
        scheduler tasks remain until the user removes/updates them.
    """
    import os
    worker = get_worker(worker_name)
    new_path = (script_location or "").strip()
    if not new_path:
        new_path = r"C:\Automation\scripts"

    def _norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p.strip().rstrip("\\/")))

    old_path = (worker.get("script_location") if worker else "") or ""
    if old_path and _norm(old_path) == _norm(new_path):
        return False

    with db_cursor() as cur:
        cur.execute(
            "UPDATE workers SET script_location = ? WHERE worker_name = ?",
            (new_path, worker_name)
        )
        # Stars are relative to the previous root — drop them with the old tree
        cur.execute("DELETE FROM user_starred_files WHERE worker_name = ?", (worker_name,))
    # Clear indexed file tree so explorer shows "syncing" until worker rescans
    clear_worker_file_tree(worker_name)
    return True

# --- Commands ---

def create_command(worker_name: str, command: str, payload: str = '{}') -> dict[str, Any]:
    now = _utc_now()
    with db_cursor() as cur:
        # Coalesce duplicate pending folder resyncs so they cannot starve interactive cmds
        if command == "resync_folder":
            cur.execute(
                """
                SELECT id FROM commands
                WHERE worker_name = ? AND command = 'resync_folder'
                  AND status = 'pending' AND payload = ?
                ORDER BY id ASC LIMIT 1
                """,
                (worker_name, payload),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute("SELECT * FROM commands WHERE id = ?", (existing["id"],))
                return row_to_dict(cur.fetchone())

        # Newer read/write of the same path supersedes older pending ones
        if command in ("read_file", "write_file"):
            cur.execute(
                """
                UPDATE commands
                SET status = 'error',
                    output = 'Superseded by a newer editor request',
                    updated_at = ?
                WHERE worker_name = ?
                  AND command = ?
                  AND status = 'pending'
                  AND payload = ?
                """,
                (now, worker_name, command, payload),
            )

        cur.execute(
            """
            INSERT INTO commands (worker_name, command, payload, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (worker_name, command, payload, now, now)
        )
        cmd_id = cur.lastrowid
        cur.execute("SELECT * FROM commands WHERE id = ?", (cmd_id,))
        return row_to_dict(cur.fetchone())


# Interactive / config commands first — never let resync floods starve the editor
_COMMAND_PRIORITY_SQL = """
CASE command
    WHEN 'desktop_notify' THEN 0
    WHEN 'reload_config' THEN 0
    WHEN 'scan_schedule_imports' THEN 0
    WHEN 'read_file' THEN 1
    WHEN 'write_file' THEN 1
    WHEN 'create_folder' THEN 2
    WHEN 'delete_folder' THEN 2
    WHEN 'delete_file' THEN 2
    WHEN 'rename_folder' THEN 2
    WHEN 'rename_file' THEN 2
    WHEN 'move_item' THEN 2
    WHEN 'update_days' THEN 3
    WHEN 'rename' THEN 4
    WHEN 'resync_folder' THEN 8
    ELSE 5
END
"""


def _fail_stale_running_commands(cur, worker_name: str, max_age_sec: int = 180) -> None:
    """Mark abandoned 'running' commands as error so the UI does not spin forever."""
    # updated_at is ISO-ish UTC text; compare via age using created/updated lexicographic UTC
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        UPDATE commands
        SET status = 'error',
            output = 'Timed out waiting for worker (command stuck in running)',
            updated_at = ?
        WHERE worker_name = ?
          AND status = 'running'
          AND updated_at < ?
        """,
        (_utc_now(), worker_name, cutoff),
    )


def claim_pending_command(worker_name: str) -> Optional[dict[str, Any]]:
    now = _utc_now()
    with db_cursor() as cur:
        _fail_stale_running_commands(cur, worker_name)
        cur.execute(
            f"""
            SELECT id FROM commands
            WHERE worker_name = ? AND status = 'pending'
            ORDER BY {_COMMAND_PRIORITY_SQL}, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (worker_name,),
        )
        locked = cur.fetchone()
        if not locked:
            return None
        cur.execute(
            "UPDATE commands SET status = 'running', updated_at = ? WHERE id = ? AND status = 'pending'",
            (now, locked["id"]),
        )
        if cur.rowcount == 0:
            return None
        cur.execute("SELECT * FROM commands WHERE id = ?", (locked["id"],))
        return row_to_dict(cur.fetchone())


def get_command(cmd_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM commands WHERE id = ?", (cmd_id,))
        return row_to_dict(cur.fetchone())

def update_command(cmd_id: int, status: str, output: str = "") -> Optional[dict[str, Any]]:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "UPDATE commands SET status = ?, output = ?, updated_at = ? WHERE id = ?",
            (status, output, now, cmd_id)
        )
        cur.execute("SELECT * FROM commands WHERE id = ?", (cmd_id,))
        return row_to_dict(cur.fetchone())


# ============================================================
# --- PC Access Control ---
# ============================================================

def get_pc_access_details(user_id: int, worker_name: str) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM user_pc_access WHERE user_id = ? AND worker_name = ?", (user_id, worker_name))
        row = cur.fetchone()
        
        if row:
            return dict(row)
        return None


def list_pc_access_worker_names(user_id: int) -> set[str]:
    """Worker names currently granted via user_pc_access."""
    with db_cursor() as cur:
        cur.execute("SELECT worker_name FROM user_pc_access WHERE user_id = ?", (user_id,))
        return {r["worker_name"] for r in cur.fetchall()}


def open_pc_access_period(user_id: int, worker_name: str, started_at: Optional[str] = None) -> None:
    """Open an assignment window if one is not already open."""
    now = started_at or _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id FROM user_pc_access_periods
            WHERE user_id = ? AND worker_name = ? AND ended_at IS NULL
            LIMIT 1
            """,
            (user_id, worker_name),
        )
        if cur.fetchone():
            return
        cur.execute(
            """
            INSERT INTO user_pc_access_periods (user_id, worker_name, started_at, ended_at)
            VALUES (?, ?, ?, NULL)
            """,
            (user_id, worker_name, now),
        )


def close_pc_access_period(user_id: int, worker_name: str, ended_at: Optional[str] = None) -> None:
    """Close the open assignment window for a user/worker (retain past history)."""
    now = ended_at or _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE user_pc_access_periods
            SET ended_at = ?
            WHERE user_id = ? AND worker_name = ? AND ended_at IS NULL
            """,
            (now, user_id, worker_name),
        )


def close_all_pc_access_periods(user_id: int, ended_at: Optional[str] = None) -> None:
    now = ended_at or _utc_now()
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE user_pc_access_periods
            SET ended_at = ?
            WHERE user_id = ? AND ended_at IS NULL
            """,
            (now, user_id),
        )


def grant_pc_access(user_id: int, worker_name: str, granted_by: int, allowed_paths: str = '', allowed_extensions: str = '', can_create_folder: int = 0, can_rename_folder: int = 0, can_update_file: int = 0, can_create_file: int = 0, can_delete_file: int = 0, can_delete_folder: int = 0, can_rename_file: int = 0, can_edit_file: int = 0, can_access_all_files: int = 0, can_run: int = 0) -> bool:
    with db_cursor() as cur:
        # Ensure parent worker row is addressable by name (guards corrupt UNIQUE index)
        cur.execute("SELECT 1 FROM workers WHERE worker_name = ?", (worker_name,))
        if cur.fetchone() is None:
            cur.execute("SELECT 1 FROM workers WHERE worker_name = ?", (worker_name,))
            if cur.fetchone() is None:
                return False
        cur.execute(
            """
            INSERT INTO user_pc_access (user_id, worker_name, granted_by, granted_at, allowed_paths, allowed_extensions, can_create_folder, can_rename_folder, can_update_file, can_create_file, can_delete_file, can_delete_folder, can_rename_file, can_edit_file, can_access_all_files, can_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, worker_name) DO UPDATE SET
                granted_by=excluded.granted_by,
                granted_at=user_pc_access.granted_at,
                allowed_paths=excluded.allowed_paths,
                allowed_extensions=excluded.allowed_extensions,
                can_create_folder=excluded.can_create_folder,
                can_rename_folder=excluded.can_rename_folder,
                can_update_file=excluded.can_update_file,
                can_create_file=excluded.can_create_file,
                can_delete_file=excluded.can_delete_file,
                can_delete_folder=excluded.can_delete_folder,
                can_rename_file=excluded.can_rename_file,
                can_edit_file=excluded.can_edit_file,
                can_access_all_files=excluded.can_access_all_files,
                can_run=excluded.can_run
            """,
            (user_id, worker_name, granted_by, _utc_now(), allowed_paths, allowed_extensions, can_create_folder, can_rename_folder, can_update_file, can_create_file, can_delete_file, can_delete_folder, can_rename_file, can_edit_file, can_access_all_files, can_run),
        )
    open_pc_access_period(user_id, worker_name)
    return True


def ensure_ip_matched_pc_access(user_id: int, worker_name: str, granted_by: Optional[int] = None) -> bool:
    """Grant minimal PC access with Run for an IP-matched owner.

    Does not overwrite an existing grant (admin assignments stay intact).
    """
    if get_pc_access_details(user_id, worker_name):
        return False
    try:
        return grant_pc_access(
            user_id,
            worker_name,
            granted_by if granted_by is not None else user_id,
            can_run=1,
        )
    except IntegrityError:
        return False


def backfill_owner_run_permissions() -> int:
    """Ensure worker owners have can_run=1 on their PC grant (default own-worker Run)."""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE user_pc_access upa
            SET can_run = 1
            FROM workers w
            WHERE w.worker_name = upa.worker_name
              AND w.owner_id = upa.user_id
              AND COALESCE(upa.can_run, 0) = 0
            """
        )
        return cur.rowcount or 0


def associate_user_with_workers_by_ip(user_id: int, ip_address: str) -> int:
    """Set ownership + default Run PC access for every worker whose IP matches the user."""
    ip = (ip_address or "").strip()
    if not ip or ip == "0.0.0.0":
        return 0
    with db_cursor() as cur:
        cur.execute("SELECT worker_name FROM workers WHERE ip_address = ?", (ip,))
        names = [row["worker_name"] for row in cur.fetchall()]
    linked = 0
    for wn in names:
        try:
            update_worker_owner(wn, user_id)
            ensure_ip_matched_pc_access(user_id, wn, granted_by=user_id)
            linked += 1
        except Exception as e:
            # Never abort login/register because one worker link failed
            print(f"associate_user_with_workers_by_ip: skip worker {wn!r}: {e}")
    return linked


def revoke_pc_access(user_id: int, worker_name: str) -> bool:
    close_pc_access_period(user_id, worker_name)
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM user_pc_access WHERE user_id = ? AND worker_name = ?",
            (user_id, worker_name),
        )
        return cur.rowcount > 0

def revoke_all_pc_access(user_id: int) -> bool:
    close_all_pc_access_periods(user_id)
    with db_cursor() as cur:
        cur.execute("DELETE FROM user_pc_access WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


def check_pc_access(user_id: int, worker_name: str) -> bool:
    """Check if user has access to a specific worker. Admin always has access."""
    if is_admin(user_id):
        return True
    with db_cursor() as cur:
        # Explicit grant
        cur.execute(
            "SELECT id FROM user_pc_access WHERE user_id = ? AND worker_name = ?",
            (user_id, worker_name),
        )
        if cur.fetchone() is not None:
            return True
        # IP-matched / assigned owner
        cur.execute(
            "SELECT id FROM workers WHERE worker_name = ? AND owner_id = ?",
            (worker_name, user_id),
        )
        return cur.fetchone() is not None


def list_accessible_workers(user_id: int) -> list[dict[str, Any]]:
    """List workers accessible to a user (granted or owned). Admin gets all."""
    if is_admin(user_id):
        return list_workers()
    refresh_worker_statuses(config.WORKER_OFFLINE_SECONDS)
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT w.*, u.username as username FROM workers w
            LEFT JOIN users u ON w.owner_id = u.id
            WHERE w.worker_name IN (
                SELECT worker_name FROM user_pc_access WHERE user_id = ?
            ) OR w.owner_id = ?
            ORDER BY w.worker_name
            """,
            (user_id, user_id),
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def get_all_pc_access() -> list[dict[str, Any]]:
    """Return extra PC access grants (admin view).

    Excludes rows where the grantee already owns the worker — ownership is not
    an 'extra' grant and should not appear on Permissions → Active grants.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT upa.*, u.username
            FROM user_pc_access upa
            JOIN users u ON u.id = upa.user_id
            LEFT JOIN workers w ON w.worker_name = upa.worker_name
            WHERE w.owner_id IS NULL OR w.owner_id <> upa.user_id
            ORDER BY u.username, upa.worker_name
            """
        )
        return [row_to_dict(r) for r in cur.fetchall()]


# ============================================================
# --- Script Access Control ---
# ============================================================

def grant_script_access(
    user_id: int, script_id: int, granted_by: int,
    can_run: bool = True, can_update: bool = False, can_delete: bool = False
) -> bool:
    now = _utc_now()
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_script_access
                    (user_id, script_id, can_run, can_update, can_delete, granted_by, granted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, script_id) DO UPDATE SET
                    can_run = excluded.can_run,
                    can_update = excluded.can_update,
                    can_delete = excluded.can_delete,
                    granted_by = excluded.granted_by,
                    granted_at = excluded.granted_at
                """,
                (user_id, script_id, int(can_run), int(can_update), int(can_delete), granted_by, now),
            )
            return True
    except IntegrityError:
        return False


def revoke_script_access(user_id: int, script_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute(
            "DELETE FROM user_script_access WHERE user_id = ? AND script_id = ?",
            (user_id, script_id),
        )
        return cur.rowcount > 0

def revoke_all_script_access(user_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute("DELETE FROM user_script_access WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


def check_script_access(user_id: int, script_id: int, action: str = "run") -> bool:
    """Check if user has specific permission on a script (run, update, delete)."""
    if is_admin(user_id):
        return True
    with db_cursor() as cur:
        cur.execute("SELECT owner_id, worker_name FROM scripts WHERE id = ?", (script_id,))
        script = cur.fetchone()
        if not script:
            return False
            
        cur.execute(
            "SELECT can_access_all_files, can_run FROM user_pc_access WHERE user_id = ? AND worker_name = ?",
            (user_id, script["worker_name"]),
        )
        upa = cur.fetchone()
        if upa and upa["can_access_all_files"] == 1:
            return True
        # Worker-level Run grant allows executing scripts on that worker from File Explorer
        if action == "run" and upa and int(upa["can_run"] or 0) == 1:
            return True
            
        if script["owner_id"] == user_id:
            return True

        col = f"can_{action}"
        if col not in ["can_run", "can_update", "can_delete"]:
            return False
        cur.execute(f"SELECT {col} FROM user_script_access WHERE user_id = ? AND script_id = ?", (user_id, script_id))
        row = cur.fetchone()
        return bool(row and row[col])


def list_accessible_scripts(user_id: int, worker_name: Optional[str] = None) -> list[dict[str, Any]]:
    """List scripts accessible to a user (owned + granted). Admin gets all."""
    if is_admin(user_id):
        return list_scripts(worker_name)
    # Same last_run / log_status / next_run semantics as list_scripts (MAX(id) joins)
    # instead of 3 correlated subqueries per script row.
    base = """
                SELECT s.*, u.username as username,
                       CASE WHEN s.owner_id = ? OR COALESCE(upa.can_access_all_files, 0) = 1 THEN 1 ELSE COALESCE(usa.can_run, 0) END as can_run,
                       CASE WHEN s.owner_id = ? OR COALESCE(upa.can_access_all_files, 0) = 1 THEN 1 ELSE COALESCE(usa.can_update, 0) END as can_update,
                       CASE WHEN s.owner_id = ? OR COALESCE(upa.can_access_all_files, 0) = 1 THEN 1 ELSE COALESCE(usa.can_delete, 0) END as can_delete,
                       j.created_at as last_run,
                       j.status as log_status,
                       sch.run_time as next_run
                FROM scripts s
                LEFT JOIN users u ON s.owner_id = u.id
                LEFT JOIN user_pc_access upa ON upa.worker_name = s.worker_name AND upa.user_id = ?
                LEFT JOIN user_script_access usa ON usa.script_id = s.id AND usa.user_id = ?
                LEFT JOIN (
                    SELECT script_id, MAX(id) as max_job_id
                    FROM jobs
                    GROUP BY script_id
                ) jmax ON jmax.script_id = s.id
                LEFT JOIN jobs j ON j.id = jmax.max_job_id
                LEFT JOIN (
                    SELECT script_id, MAX(id) as max_sch_id
                    FROM schedules
                    WHERE enabled = 1
                    GROUP BY script_id
                ) schmax ON schmax.script_id = s.id
                LEFT JOIN schedules sch ON sch.id = schmax.max_sch_id
    """
    with db_cursor() as cur:
        if worker_name:
            cur.execute(
                base + """
                WHERE (upa.user_id IS NOT NULL OR usa.user_id = ? OR s.owner_id = ?) AND s.worker_name = ?
                ORDER BY s.script_name
                """,
                (user_id, user_id, user_id, user_id, user_id, user_id, user_id, worker_name),
            )
        else:
            cur.execute(
                base + """
                WHERE (upa.user_id IS NOT NULL OR usa.user_id = ? OR s.owner_id = ?)
                ORDER BY s.worker_name, s.script_name
                """,
                (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
            )
        return [row_to_dict(r) for r in cur.fetchall()]


def get_all_script_access() -> list[dict[str, Any]]:
    """Return all script access grants (admin view)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT usa.*, u.username, u.username, s.script_name, s.worker_name
            FROM user_script_access usa
            JOIN users u ON u.id = usa.user_id
            JOIN scripts s ON s.id = usa.script_id
            ORDER BY u.username, s.worker_name, s.script_name
            """
        )
        return [row_to_dict(r) for r in cur.fetchall()]


def update_schedule(schedule_id: int, run_time: Optional[str] = None, enabled: Optional[int] = None) -> Optional[dict[str, Any]]:
    now_utc = _utc_now()
    with db_cursor() as cur:
        if run_time is not None and enabled is not None:
            cur.execute(
                "UPDATE schedules SET run_time = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (run_time, enabled, now_utc, schedule_id),
            )
        elif run_time is not None:
            cur.execute(
                "UPDATE schedules SET run_time = ?, updated_at = ? WHERE id = ?",
                (run_time, now_utc, schedule_id),
            )
        elif enabled is not None:
            cur.execute(
                "UPDATE schedules SET enabled = ?, updated_at = ? WHERE id = ?",
                (enabled, now_utc, schedule_id),
            )
        cur.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return row_to_dict(cur.fetchone())


def normalize_schedule_timing(
    *,
    schedule_type: str = "daily",
    run_time: str = "",
    weekdays: Optional[list] = None,
    interval_numeric: str = "",
    interval_unit: str = "",
    interval_use_window: bool = False,
    interval_window_start: str = "",
    interval_window_end: str = "",
    full_date: str = "",
    day_of_month: str = "",
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """
    Same validation / normalization as creating a schedule from the Scheduler drawer.
    Returns ({run_time, schedule_type, schedule_config}, None) or (None, error_message).
    """
    import json
    import re

    schedule_type = (schedule_type or "daily").strip() or "daily"
    run_time = (run_time or "").strip()
    weekdays = list(weekdays or [])
    interval_numeric = (interval_numeric or "").strip()
    interval_unit = (interval_unit or "").strip()
    interval_window_start = (interval_window_start or "").strip()
    interval_window_end = (interval_window_end or "").strip()
    full_date = (full_date or "").strip()
    day_of_month = str(day_of_month or "").strip()

    schedule_config: dict[str, Any] = {}
    if schedule_type == "daily":
        if not weekdays:
            return None, "Select at least one day of the week."
        schedule_config["weekdays"] = weekdays
    if schedule_type == "interval":
        if not interval_numeric or not interval_unit:
            return None, "Interval value and unit are required."
        try:
            if int(interval_numeric) < 1:
                return None, "Interval must be at least 1."
        except ValueError:
            return None, "Invalid interval value."
        schedule_config["interval_val"] = f"{interval_numeric}{interval_unit}"
        if interval_use_window:
            hm = re.compile(r"^\d{1,2}:\d{2}$")

            def _norm_hm(t: str) -> Optional[str]:
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
                return None, "Valid From/To times are required for the interval time range."
            schedule_config["window_start"] = start_n
            schedule_config["window_end"] = end_n

    if schedule_type == "once":
        if full_date:
            run_time = f"{full_date} {run_time}" if " " not in run_time else run_time
        elif " " not in run_time:
            return None, "Date is required for one-time schedules."
        once_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}$")
        if not once_re.match(run_time or ""):
            return None, "One-time schedules require a valid date and time (YYYY-MM-DD HH:MM)."
    elif schedule_type == "interval":
        run_time = "00:00"
    elif schedule_type == "monthly":
        if day_of_month:
            run_time = f"{str(day_of_month).zfill(2)}:{run_time}"
        elif (run_time or "").count(":") != 2:
            return None, "Day of month is required for monthly schedules."
    elif ":" not in (run_time or ""):
        run_time = "00:00"

    return {
        "run_time": run_time,
        "schedule_type": schedule_type,
        "schedule_config": json.dumps(schedule_config) if schedule_config else "{}",
    }, None


def compute_schedule_next_run(sch: dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    """
    Next-run display time from schedule_type / schedule_config / run_time.
    Does NOT use schedules.days — that column is the script `days` argument (lookback),
    not the calendar interval.
    """
    import json
    from datetime import timedelta

    if not sch.get("enabled"):
        return None
    if now is None:
        now = datetime.now()

    sch_type = (sch.get("schedule_type") or "daily")
    sch_type = str(sch_type).lower()
    if sch_type == "manual":
        return None

    config: dict[str, Any] = {}
    raw_cfg = sch.get("schedule_config")
    if raw_cfg:
        try:
            config = json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        except Exception:
            config = {}

    run_time = sch.get("run_time") or "00:00"
    if hasattr(run_time, "strftime"):
        run_time = run_time.strftime("%Y-%m-%d %H:%M")
    run_time = str(run_time)

    last_run = sch.get("last_run")
    last_run_local = _stored_ts_to_local_naive(last_run)
    now = _local_naive_now(now)
    now_floor = now.replace(second=0, microsecond=0)

    if sch_type == "once":
        try:
            scheduled = datetime.strptime(run_time[:16], "%Y-%m-%d %H:%M")
            return scheduled.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return run_time[:16] if run_time else None

    if sch_type == "interval":
        interval = (config.get("interval_val") or "").strip()
        delta = timedelta(hours=1)
        if interval.endswith("m"):
            try:
                delta = timedelta(minutes=max(1, int(interval[:-1])))
            except ValueError:
                delta = timedelta(minutes=1)
        elif interval.endswith("h"):
            try:
                delta = timedelta(hours=max(1, int(interval[:-1])))
            except ValueError:
                delta = timedelta(hours=1)
        elif interval.endswith("d"):
            try:
                delta = timedelta(days=max(1, int(interval[:-1])))
            except ValueError:
                delta = timedelta(days=1)
        base = last_run_local or now
        nxt = base + delta if last_run_local else now
        if nxt < now:
            nxt = now
        win_start = (config.get("window_start") or "").strip()
        win_end = (config.get("window_end") or "").strip()
        if win_start and win_end:
            for _ in range(48 * 7):  # up to ~1 week of half-hours
                hm = nxt.strftime("%H:%M")
                if _hhmm_in_window(hm, win_start, win_end):
                    break
                nxt += timedelta(minutes=1)
        return nxt.strftime("%Y-%m-%d %H:%M")

    if sch_type == "monthly":
        parts = run_time.split(":")
        try:
            if len(parts) == 3:
                day_n, hour_n, min_n = int(parts[0]), int(parts[1]), int(parts[2])
            else:
                day_n = now.day
                hour_n, min_n = map(int, run_time[:5].split(":"))
        except (ValueError, TypeError):
            return None
        year, month = now.year, now.month
        for _ in range(14):
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            use_day = min(day_n, last_day)
            try:
                candidate = datetime(year, month, use_day, hour_n, min_n)
            except ValueError:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
                continue
            if candidate.replace(second=0, microsecond=0) >= now_floor:
                return candidate.strftime("%Y-%m-%d %H:%M")
            month += 1
            if month > 12:
                month = 1
                year += 1
        return None

    # daily / default — today if HH:MM has not passed, otherwise tomorrow
    try:
        run_hour, run_min = map(int, run_time[:5].split(":"))
    except (ValueError, AttributeError):
        run_hour, run_min = 0, 0
    weekdays = list(config.get("weekdays") or [])
    candidate = now_floor.replace(hour=run_hour, minute=run_min)
    if candidate < now_floor:
        candidate += timedelta(days=1)
    for _ in range(14):
        if weekdays and candidate.strftime("%a") not in weekdays:
            candidate += timedelta(days=1)
            continue
        return candidate.strftime("%Y-%m-%d %H:%M")
    return candidate.strftime("%Y-%m-%d %H:%M")


def _local_naive_now(now: Optional[datetime] = None) -> datetime:
    """Normalize to local wall time without tzinfo (safe for naive/aware mixing)."""
    if now is None:
        return datetime.now().astimezone().replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone().replace(tzinfo=None)
    return now


def _stored_ts_to_local_naive(value: Any) -> Optional[datetime]:
    """Convert a DB UTC timestamp (str or datetime) to local naive datetime.

    Do not stringify aware datetimes first — that drops tzinfo and then
    wrongly re-tags the local clock as UTC (schedules never fire).
    """
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().replace(tzinfo=None)
        text = str(value).replace("T", " ")[:19]
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError):
        return None


def schedule_is_due(sch: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True if a schedule-shaped row (run_time, schedule_type, schedule_config, last_run) is due now."""
    import json

    now = _local_naive_now(now)
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")
    sch_type = sch.get("schedule_type") or "daily"
    if str(sch_type).lower() == "manual":
        return False

    config: dict[str, Any] = {}
    raw_cfg = sch.get("schedule_config")
    if raw_cfg:
        try:
            config = json.loads(raw_cfg) if isinstance(raw_cfg, str) else (raw_cfg or {})
        except Exception:
            config = {}

    if sch_type == "daily" and config.get("weekdays"):
        if now.strftime("%a") not in config["weekdays"]:
            return False

    run_time = sch.get("run_time") or ""
    if hasattr(run_time, "strftime"):
        run_time = run_time.strftime("%Y-%m-%d %H:%M")

    # Daily/monthly: allow a grace window so the 15s scheduler poll can still
    # trigger within the same slot (30 min — matches offline retry / missed logic).
    _GRACE_MINUTES = 30 if sch_type in ("daily", "monthly") else 5

    def _due_hm(hm: str) -> bool:
        try:
            rh, rm = map(int, str(hm)[:5].split(":"))
        except (ValueError, TypeError, AttributeError):
            return str(hm) == current_time
        sched_mins = rh * 60 + rm
        now_mins = now.hour * 60 + now.minute
        return sched_mins <= now_mins <= sched_mins + _GRACE_MINUTES

    if sch_type == "interval":
        win_start = (config.get("window_start") or "").strip()
        win_end = (config.get("window_end") or "").strip()
        if win_start and win_end and not _hhmm_in_window(current_time, win_start, win_end):
            return False
    elif sch_type == "once":
        sch_time = run_time or ""
        try:
            scheduled_local = datetime.strptime(sch_time, "%Y-%m-%d %H:%M")
            if now < scheduled_local:
                return False
        except ValueError:
            current_dt = current_date + " " + current_time
            if sch_time != current_dt:
                return False
    elif sch_type == "monthly":
        parts = str(run_time).split(":")
        if len(parts) == 3:
            sch_d, sch_h, sch_m = parts
            cur_d = current_date.split("-")[2]
            if sch_d != cur_d or not _due_hm(f"{sch_h}:{sch_m}"):
                return False
        else:
            if not _due_hm(run_time):
                return False
    else:
        if not _due_hm(str(run_time)[:5]) and run_time != current_time:
            return False

    last_run = sch.get("last_run")
    last_run_local = _stored_ts_to_local_naive(last_run)

    if last_run_local:
        if sch_type == "interval":
            interval = config.get("interval_val")
            if interval:
                try:
                    if interval.endswith("m"):
                        mins = int(interval[:-1])
                        if (now - last_run_local).total_seconds() < (mins * 60):
                            return False
                    elif interval.endswith("h"):
                        hrs = int(interval[:-1])
                        if (now - last_run_local).total_seconds() < (hrs * 3600):
                            return False
                    elif interval.endswith("d"):
                        dys = int(interval[:-1])
                        if (now - last_run_local).total_seconds() < (dys * 86400):
                            return False
                except (ValueError, TypeError):
                    pass
            else:
                if last_run_local.strftime("%Y-%m-%d %H") == now.strftime("%Y-%m-%d %H"):
                    return False
        else:
            # Already took today's slot. Editing to a later HH:MM today must still run.
            if last_run_local.strftime("%Y-%m-%d") >= current_date:
                if last_run_local.strftime("%H:%M") >= str(run_time)[:5]:
                    return False

        if sch_type == "once":
            return False
    elif last_run:
        if str(last_run).split(" ")[0] >= current_date and sch_type != "interval":
            return False
        if sch_type == "once":
            return False
    else:
        if sch_type != "interval":
            updated_at_local = _stored_ts_to_local_naive(sch.get("updated_at"))
            if updated_at_local is not None:
                if sch_type == "once":
                    try:
                        scheduled_local = datetime.strptime(run_time or "", "%Y-%m-%d %H:%M")
                        upd = updated_at_local.replace(second=0, microsecond=0)
                        if upd > scheduled_local:
                            return False
                    except ValueError:
                        pass
                elif updated_at_local.strftime("%Y-%m-%d") == current_date:
                    compare_hm = str(run_time or "")
                    if sch_type == "monthly":
                        parts = compare_hm.split(":")
                        if len(parts) == 3:
                            compare_hm = f"{parts[1]}:{parts[2]}"
                    if updated_at_local.strftime("%H:%M") > compare_hm[:5]:
                        return False

    return True


def _hhmm_in_window(current_hm: str, start: str, end: str) -> bool:
    """
    True if current_hm (HH:MM) falls within [start, end] inclusive.
    Overnight windows (start > end), e.g. 22:00–06:00, are supported.
    Empty start/end means no restriction.
    """
    if not start or not end:
        return True
    if start == end:
        return True
    if start < end:
        return start <= current_hm <= end
    return current_hm >= start or current_hm <= end


SCHEDULE_DAILY_GRACE_MINUTES = 30


def is_worker_online(worker_name: str) -> bool:
    w = get_worker(worker_name or "")
    return bool(w and (w.get("status") or "").lower() == "online")


def _schedule_run_hm(sch: dict[str, Any]) -> tuple[int, int] | None:
    run_time = sch.get("run_time") or ""
    if hasattr(run_time, "strftime"):
        run_time = run_time.strftime("%H:%M")
    try:
        rh, rm = map(int, str(run_time)[:5].split(":"))
        return rh, rm
    except (ValueError, TypeError, AttributeError):
        return None


def _schedule_slot_bounds_local(
    sch: dict[str, Any], now: Optional[datetime] = None
) -> tuple[Optional[int], Optional[int]]:
    """Return (slot_minutes, grace_end_minutes) for today's daily slot in local time."""
    hm = _schedule_run_hm(sch)
    if not hm:
        return None, None
    rh, rm = hm
    slot_mins = rh * 60 + rm
    return slot_mins, slot_mins + SCHEDULE_DAILY_GRACE_MINUTES


def get_today_job_for_schedule(
    schedule_id: int, now: Optional[datetime] = None
) -> Optional[dict[str, Any]]:
    now = _local_naive_now(now)
    today = now.date()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM jobs
            WHERE schedule_id = ?
            ORDER BY id DESC
            LIMIT 8
            """,
            (schedule_id,),
        )
        for row in cur.fetchall():
            job = row_to_dict(row)
            created = _stored_ts_to_local_naive(job.get("created_at"))
            if created and created.date() == today:
                return job
    return None


def _pending_schedule_job_still_valid(
    sch: dict[str, Any], job: dict[str, Any], now: Optional[datetime] = None
) -> bool:
    if (job.get("status") or "").lower() != "pending":
        return False
    now = _local_naive_now(now)
    slot_mins, grace_end = _schedule_slot_bounds_local(sch, now)
    if slot_mins is None or grace_end is None:
        return False
    now_mins = now.hour * 60 + now.minute
    if now_mins >= grace_end:
        return False
    created = _stored_ts_to_local_naive(job.get("created_at"))
    if not created or created.date() != now.date():
        return False
    created_mins = created.hour * 60 + created.minute
    return created_mins >= slot_mins


def compute_schedule_daily_state(
    sch: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    today_job: Optional[dict[str, Any]] = None,
    worker_online: bool = True,
) -> dict[str, Any]:
    """Daily slot lifecycle for scheduler UI: scheduled/due/missed/completed/running."""
    now = _local_naive_now(now)
    if not sch.get("enabled"):
        return {"daily_state": "scheduled", "daily_state_reason": "disabled"}

    sch_type = (sch.get("schedule_type") or "daily").lower()
    if sch_type == "manual":
        return {"daily_state": "scheduled", "daily_state_reason": ""}

    slot_mins, grace_end = _schedule_slot_bounds_local(sch, now)
    if slot_mins is None or grace_end is None:
        return {"daily_state": "scheduled", "daily_state_reason": ""}

    now_mins = now.hour * 60 + now.minute
    job = today_job
    if job:
        st = (job.get("status") or "").lower()
        if st in ("running", "paused"):
            return {"daily_state": "running", "daily_state_reason": ""}
        if st in ("completed", "success"):
            return {"daily_state": "completed", "daily_state_reason": ""}
        if st == "pending":
            if _pending_schedule_job_still_valid(sch, job, now):
                reason = "worker offline" if not worker_online else ""
                return {"daily_state": "due", "daily_state_reason": reason}
            return {
                "daily_state": "missed",
                "daily_state_reason": "worker offline" if not worker_online else "not run",
            }
        if st in ("error", "failed", "stopped"):
            return {"daily_state": "missed", "daily_state_reason": st}

    if now_mins < slot_mins:
        return {"daily_state": "scheduled", "daily_state_reason": ""}
    if now_mins < grace_end:
        reason = "worker offline" if not worker_online else ""
        return {"daily_state": "due", "daily_state_reason": reason}
    return {
        "daily_state": "missed",
        "daily_state_reason": "worker offline" if not worker_online else "not run",
    }


def _daily_state_to_running_status(
    daily_state: str,
    job_status: Optional[str] = None,
) -> str:
    js = (job_status or "").lower()
    if js in ("running", "paused"):
        return js
    if js in ("completed", "success"):
        return "completed"
    if js in ("error", "failed"):
        return "error"
    if js == "stopped":
        return "stopped"
    if js == "pending":
        return "pending"
    ds = (daily_state or "scheduled").lower()
    if ds == "due":
        return "due"
    if ds == "completed":
        return "completed"
    if ds == "running":
        return "running"
    if ds in ("scheduled", "missed"):
        return "pending"
    return "pending"


def attach_schedule_daily_state(
    sch: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    worker_cache: Optional[dict[str, bool]] = None,
) -> dict[str, Any]:
    now = _local_naive_now(now)
    wname = (sch.get("worker_name") or "").strip()
    if worker_cache is not None and wname in worker_cache:
        worker_online = worker_cache[wname]
    else:
        worker_online = is_worker_online(wname) if wname else False
        if worker_cache is not None:
            worker_cache[wname] = worker_online

    today_job = get_today_job_for_schedule(int(sch["id"]), now)
    st = compute_schedule_daily_state(
        sch,
        now=now,
        today_job=today_job,
        worker_online=worker_online,
    )
    sch["daily_state"] = st["daily_state"]
    sch["daily_state_reason"] = st.get("daily_state_reason") or ""
    job_st = (today_job or {}).get("status")
    sch["running_status"] = _daily_state_to_running_status(st["daily_state"], job_st)
    return sch


def reset_daily_schedule_states() -> int:
    """Midnight IST reset — today's slot not done yet (pending)."""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE schedules
            SET tracking_status = 'pending'
            WHERE COALESCE(is_deleted, 0) = 0 AND COALESCE(enabled, 0) = 1
            """
        )
        return cur.rowcount or 0


def expire_stale_schedule_pending_jobs(now: Optional[datetime] = None) -> list[int]:
    """Drop schedule pending jobs past the 30-minute slot grace (no catch-up on reconnect).

    Folder-run jobs (folder_run_id set) are never expired here — they are chained by the
    folder runner and may start long after the member schedule's daily slot time.
    """
    now = _local_naive_now(now)
    expired: list[int] = []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT j.id, j.schedule_id, j.worker_name, j.created_at, j.folder_run_id
            FROM jobs j
            WHERE j.status = 'pending'
              AND j.schedule_id IS NOT NULL
              AND j.folder_run_id IS NULL
            ORDER BY j.id
            LIMIT 200
            """
        )
        rows = [row_to_dict(r) for r in cur.fetchall()]

    for row in rows:
        # Safety: never expire folder chain jobs (even if older code omitted the SQL filter)
        if row.get("folder_run_id"):
            continue
        sid = int(row["schedule_id"])
        with db_cursor() as cur:
            cur.execute(
                "SELECT * FROM schedules WHERE id = ? AND COALESCE(is_deleted, 0) = 0",
                (sid,),
            )
            sch_row = cur.fetchone()
        if not sch_row:
            continue
        sch = row_to_dict(sch_row)
        job = {
            "status": "pending",
            "created_at": row.get("created_at"),
        }
        if _pending_schedule_job_still_valid(sch, job, now):
            continue
        jid = int(row["id"])
        note = (
            "[Expired] Scheduled slot grace (30 min) ended — worker offline or job not claimed. "
            "Will not auto-run when worker reconnects."
        )
        update_job(jid, "stopped", output=note, exit_code=None)
        try:
            ensure_stopped_job_report(jid)
        except Exception:
            pass
        expired.append(jid)
    return expired


def get_due_schedules():
    now = _local_naive_now()
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sch.*, s.script_name, s.script_path
            FROM schedules sch
            JOIN scripts s ON sch.script_id = s.id
            WHERE sch.enabled = 1 AND sch.is_deleted = 0
            """
        )
        schedules = [row_to_dict(r) for r in cur.fetchall()]

        # Schedules in enabled folders run via folder scheduler; disabled-folder
        # members still run individually at their own run_time.
        try:
            from app.services import schedule_folders as sf
            in_enabled_folder = sf.schedule_ids_in_enabled_folders()
            if in_enabled_folder:
                schedules = [s for s in schedules if int(s["id"]) not in in_enabled_folder]
        except Exception:
            pass

        due = [sch for sch in schedules if schedule_is_due(sch, now)]
        # One job per schedule per slot — skip if already queued or running today.
        filtered: list[dict[str, Any]] = []
        for sch in due:
            sid = int(sch["id"])
            if get_active_job_for_schedule(sid):
                continue
            filtered.append(sch)
        return filtered

def mark_schedule_run(schedule_id: int, log_msg: str = "") -> None:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute("UPDATE schedules SET last_run = ? WHERE id = ?", (now, schedule_id))

def grant_schedule_access(schedule_id: int, user_id: int, granted_by: int, can_delete: int = 0, can_enable: int = 0, can_disable: int = 0, can_run: int = 0, can_duplicate: int = 0, can_edit: int = 0) -> bool:
    """Insert or refresh a schedule_access row (unique on schedule_id + user_id)."""
    now = _utc_now()
    with db_cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO schedule_access (
                    schedule_id, user_id, can_delete, can_enable, can_disable,
                    can_run, can_duplicate, can_edit, granted_by, granted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (schedule_id, user_id) DO UPDATE SET
                    can_delete = EXCLUDED.can_delete,
                    can_enable = EXCLUDED.can_enable,
                    can_disable = EXCLUDED.can_disable,
                    can_run = EXCLUDED.can_run,
                    can_duplicate = EXCLUDED.can_duplicate,
                    can_edit = EXCLUDED.can_edit,
                    granted_by = EXCLUDED.granted_by,
                    granted_at = EXCLUDED.granted_at
                """,
                (
                    schedule_id, user_id, can_delete, can_enable, can_disable,
                    can_run, can_duplicate, can_edit, granted_by, now,
                ),
            )
            return True
        except Exception:
            return False

def revoke_schedule_access(schedule_id: int, user_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute("DELETE FROM schedule_access WHERE schedule_id = ? AND user_id = ?", (schedule_id, user_id))
        return cur.rowcount > 0

def revoke_all_schedule_access(user_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute("DELETE FROM schedule_access WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0

def get_all_schedule_access() -> list[dict[str, Any]]:
    """Extra schedule grants for Permissions → Active grants (excludes schedule owners)."""
    return [
        row for row in get_schedule_access_for_assign()
        if int(row.get("schedule_owner_id") or 0) != int(row.get("user_id") or 0)
    ]


def get_schedule_access_for_assign() -> list[dict[str, Any]]:
    """All schedule_access rows for Permissions → Assign access (includes owner self-grants)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sa.*, u.username,
                   sch.run_time, sch.user_id AS schedule_owner_id,
                   s.script_name, s.worker_name,
                   ou.username AS owner_username
            FROM schedule_access sa
            JOIN users u ON u.id = sa.user_id
            JOIN schedules sch ON sch.id = sa.schedule_id
            JOIN scripts s ON s.id = sch.script_id
            JOIN users ou ON ou.id = sch.user_id
            WHERE COALESCE(sch.is_deleted, 0) = 0
            ORDER BY u.username, sch.run_time, sa.schedule_id
            """
        )
        return [row_to_dict(r) for r in cur.fetchall()]

def list_schedules(
    user_id: Optional[int] = None,
    *,
    exclude_folder_members: bool = False,
    as_user: bool = False,
) -> list[dict[str, Any]]:
    """List schedules. If user_id given and not admin, filter to owned + granted access.

    exclude_folder_members: when True, hide schedules that belong to a Folder Scheduler
    so the regular Schedules tab stays independent (dashboard/default callers unchanged).
    as_user: when True, never expand to admin-all — used for viewing a specific user's data.
    """
    if as_user and user_id is None:
        return []
    # running_status / paused_at from latest job by MAX(id) — same as ORDER BY id DESC LIMIT 1
    job_join = """
                LEFT JOIN (
                    SELECT schedule_id, MAX(id) as max_job_id
                    FROM jobs
                    WHERE schedule_id IS NOT NULL
                    GROUP BY schedule_id
                ) jmax ON jmax.schedule_id = sch.id
                LEFT JOIN jobs jlatest ON jlatest.id = jmax.max_job_id
    """
    with db_cursor() as cur:
        if (not as_user) and (user_id is None or is_admin(user_id)):
            cur.execute(
                f"""
                SELECT sch.*, s.script_name, s.script_path, s.days as script_days,
                       CASE WHEN s.days IS NOT NULL THEN 1 ELSE 0 END as has_days_variable,
                       u.username,
                       jlatest.status as running_status,
                       jlatest.paused_at as paused_at,
                       1 as can_delete,
                       1 as can_enable,
                       1 as can_disable,
                       1 as can_run,
                       1 as can_duplicate,
                       1 as can_edit
                FROM schedules sch
                JOIN scripts s ON s.id = sch.script_id
                JOIN users u ON u.id = sch.user_id
                {job_join}
                WHERE sch.is_deleted = 0
                ORDER BY sch.run_time
                """
            )
        else:
            cur.execute(
                f"""
                SELECT sch.*, s.script_name, s.script_path, s.days as script_days,
                       CASE WHEN s.days IS NOT NULL THEN 1 ELSE 0 END as has_days_variable,
                       u.username,
                       jlatest.status as running_status,
                       jlatest.paused_at as paused_at,
                       COALESCE(sa.can_delete, 0) as can_delete,
                       COALESCE(sa.can_enable, 0) as can_enable,
                       COALESCE(sa.can_disable, 0) as can_disable,
                       COALESCE(sa.can_run, 0) as can_run,
                       COALESCE(sa.can_duplicate, 0) as can_duplicate,
                       CASE
                           WHEN sa.id IS NOT NULL THEN COALESCE(sa.can_edit, 0)
                           WHEN sch.user_id = ? THEN 1
                           ELSE 0
                       END as can_edit
                FROM schedules sch
                JOIN scripts s ON s.id = sch.script_id
                JOIN users u ON u.id = sch.user_id
                LEFT JOIN schedule_access sa ON sa.schedule_id = sch.id AND sa.user_id = ?
                {job_join}
                WHERE sch.is_deleted = 0 AND (sch.user_id = ? OR sa.id IS NOT NULL)
                ORDER BY sch.run_time
                """,
                (user_id, user_id, user_id),
            )
        rows = [row_to_dict(r) for r in cur.fetchall()]

    if exclude_folder_members:
        try:
            from app.services import schedule_folders as sf
            in_folder = sf.schedule_ids_in_any_folder()
            if in_folder:
                rows = [r for r in rows if int(r["id"]) not in in_folder]
        except Exception:
            pass

    # Compute next_run for each schedule (from schedule_type/config — not script days)
    from datetime import datetime, timezone
    now = datetime.now()
    for sch in rows:
        # Convert UTC last_run to local display first (compute_schedule_next_run needs raw/UTC)
        raw_last = sch.get("last_run")
        if raw_last:
            try:
                if hasattr(raw_last, "strftime"):
                    lr = raw_last
                    if getattr(lr, "tzinfo", None) is None:
                        lr = lr.replace(tzinfo=timezone.utc)
                    sch["last_run"] = lr.astimezone().strftime("%Y-%m-%d %H:%M")
                    # Keep a UTC string for due/next helpers if needed
                    sch["_last_run_utc"] = lr.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    last_run_utc = datetime.strptime(
                        str(raw_last).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    sch["last_run"] = last_run_utc.astimezone().strftime("%Y-%m-%d %H:%M")
                    sch["_last_run_utc"] = last_run_utc.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, AttributeError, TypeError):
                pass

        # Effective script-days value for UI/sort (schedule override or script default)
        try:
            from app.services.script_days import enrich_schedule_days_fields
            enrich_schedule_days_fields(sch)
        except Exception:
            if sch.get("days") is None and sch.get("script_days") is not None:
                sch["effective_days"] = sch.get("script_days")
            else:
                sch["effective_days"] = sch.get("days")
            if sch.get("has_days_variable") is None:
                sch["has_days_variable"] = 1 if sch.get("script_days") is not None else 0

        nxt_src = dict(sch)
        if sch.get("_last_run_utc"):
            nxt_src["last_run"] = sch["_last_run_utc"]
        elif raw_last is not None:
            nxt_src["last_run"] = raw_last
        sch["next_run"] = compute_schedule_next_run(nxt_src, now)
        sch.pop("_last_run_utc", None)

    worker_cache: dict[str, bool] = {}
    for sch in rows:
        attach_schedule_daily_state(sch, now=now, worker_cache=worker_cache)

    return rows

def get_scheduler_jobs(worker_name: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None, limit: int = 50, offset: int = 0, user_id: Optional[int] = None) -> tuple[list[dict[str, Any]], int]:
    """Get jobs triggered by the scheduler, with pagination."""
    with db_cursor() as cur:
        query = """
            SELECT j.*, s.script_name, u.username
            FROM jobs j
            JOIN scripts s ON j.script_id = s.id
            JOIN schedules sch ON j.schedule_id = sch.id
            JOIN users u ON sch.user_id = u.id
            WHERE j.schedule_id IS NOT NULL
        """
        count_query = """
            SELECT COUNT(*)
            FROM jobs j
            JOIN scripts s ON j.script_id = s.id
            JOIN schedules sch ON j.schedule_id = sch.id
            JOIN users u ON sch.user_id = u.id
            WHERE j.schedule_id IS NOT NULL
        """
        params = []
        if worker_name:
            query += " AND j.worker_name = ?"
            count_query += " AND j.worker_name = ?"
            params.append(worker_name)
            
        if status:
            query += " AND j.status = ?"
            count_query += " AND j.status = ?"
            params.append(status)
            
        if search:
            query += " AND (j.worker_name LIKE ? OR s.script_name LIKE ?)"
            count_query += " AND (j.worker_name LIKE ? OR s.script_name LIKE ?)"
            search_param = f"%{search}%"
            params.extend([search_param, search_param])
            
        if user_id is not None:
            query += " AND (sch.user_id = ? OR s.owner_id = ?)"
            count_query += " AND (sch.user_id = ? OR s.owner_id = ?)"
            params.extend([user_id, user_id])
            
        cur.execute(count_query, tuple(params))
        total_count = cur.fetchone()[0]
        
        query += " ORDER BY j.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        cur.execute(query, tuple(params))
        return [row_to_dict(r) for r in cur.fetchall()], total_count


# --- Admin Reports: schedule tracking (folders vs regular, no new tables) ---

_TRACKING_STATUS_ALLOWED = ("pending", "in_progress", "completed", "failed")

_TRACKING_STATUS_SQL = """
CASE
  WHEN LOWER(COALESCE(sch.tracking_status, '')) IN ('pending', 'in_progress', 'completed', 'failed')
    THEN LOWER(sch.tracking_status)
  WHEN jlatest.status IN ('running', 'paused') THEN 'in_progress'
  WHEN jlatest.status IN ('error', 'failed') THEN 'failed'
  WHEN jlatest.status IN ('completed', 'success') THEN 'completed'
  ELSE 'pending'
END
"""


def _tracking_date_bound(value: Optional[str], end_of_day: bool) -> str:
    v = (value or "").strip()
    if not v:
        return v
    if " " in v:
        return v
    return f"{v} {'23:59:59' if end_of_day else '00:00:00'}"


def _tracking_job_range_sql(date_from: Optional[str], date_to: Optional[str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    ts = "COALESCE(start_time, created_at)"
    if date_from:
        clauses.append(f"{ts} >= ?")
        params.append(_tracking_date_bound(date_from, False))
    if date_to:
        clauses.append(f"{ts} <= ?")
        params.append(_tracking_date_bound(date_to, True))
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    return extra, params


def _tracking_job_joins(date_from: Optional[str], date_to: Optional[str]) -> tuple[str, list[Any]]:
    date_sql, date_params = _tracking_job_range_sql(date_from, date_to)
    sql = f"""
        LEFT JOIN (
            SELECT schedule_id, MAX(id) AS max_job_id
            FROM jobs
            WHERE schedule_id IS NOT NULL{date_sql}
            GROUP BY schedule_id
        ) jmax ON jmax.schedule_id = sch.id
        LEFT JOIN jobs jlatest ON jlatest.id = jmax.max_job_id
        LEFT JOIN (
            SELECT
                schedule_id,
                COUNT(*) AS total_jobs,
                SUM(CASE WHEN status IN ('completed', 'success') THEN 1 ELSE 0 END) AS completed_jobs,
                SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END) AS failed_jobs,
                SUM(CASE WHEN status IN ('running', 'paused') THEN 1 ELSE 0 END) AS running_jobs,
                SUM(CASE WHEN status IN ('pending', 'queued') THEN 1 ELSE 0 END) AS pending_jobs
            FROM jobs
            WHERE schedule_id IS NOT NULL{date_sql}
            GROUP BY schedule_id
        ) js ON js.schedule_id = sch.id
    """
    return sql, list(date_params) + list(date_params)


def _aggregate_tracking_status(statuses: list[str]) -> str:
    s = {(x or "pending").lower() for x in statuses}
    if not s:
        return "pending"
    if "in_progress" in s:
        return "in_progress"
    if "failed" in s:
        return "failed"
    if s == {"completed"}:
        return "completed"
    return "pending"


def _decorate_tracking_script(row: dict[str, Any]) -> dict[str, Any]:
    total = int(row.get("total_jobs") or 0)
    completed = int(row.get("completed_jobs") or 0)
    row["total_jobs"] = total
    row["completed_jobs"] = completed
    row["failed_jobs"] = int(row.get("failed_jobs") or 0)
    row["running_jobs"] = int(row.get("running_jobs") or 0)
    row["pending_jobs"] = int(row.get("pending_jobs") or 0)
    row["completion_pct"] = round((100.0 * completed / total), 1) if total else 0.0
    row["tracking_status"] = (row.get("tracking_status") or "pending").lower()
    if row["tracking_status"] == "running":
        row["tracking_status"] = "in_progress"
    override = (row.get("tracking_status_override") or "").strip().lower()
    row["status_is_manual"] = override in _TRACKING_STATUS_ALLOWED
    return row


def _fetch_admin_tracking_scripts(
    *,
    exclude_folder_members: bool,
    only_folder_id: Optional[int] = None,
    user_id: Optional[int] = None,
    worker_name: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict[str, Any]]:
    joins, join_params = _tracking_job_joins(date_from, date_to)
    where = ["sch.is_deleted = 0"]
    params: list[Any] = list(join_params)

    folder_join = ""
    if only_folder_id is not None:
        folder_join = "JOIN schedule_folder_items sfi ON sfi.schedule_id = sch.id"
        where.append("sfi.folder_id = ?")
        params.append(int(only_folder_id))
    elif exclude_folder_members:
        where.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM schedule_folder_items sfi2
                JOIN schedule_folders sf2 ON sf2.id = sfi2.folder_id
                WHERE sfi2.schedule_id = sch.id AND COALESCE(sf2.is_deleted, 0) = 0
            )
            """
        )

    if user_id:
        where.append("sch.user_id = ?")
        params.append(int(user_id))
    if worker_name:
        where.append("sch.worker_name = ?")
        params.append(worker_name)
    if search:
        where.append("(s.script_name LIKE ? OR sch.worker_name LIKE ?)")
        q = f"%{search}%"
        params.extend([q, q])
    if status:
        st = status.strip().lower().replace(" ", "_")
        if st == "running":
            st = "in_progress"
        where.append(f"({_TRACKING_STATUS_SQL}) = ?")
        params.append(st)

    order = "s.script_name, sch.worker_name"
    if only_folder_id is not None:
        order = "sfi.sort_order ASC, s.script_name"

    sql = f"""
        SELECT sch.id, sch.user_id, sch.worker_name, sch.enabled, sch.last_run,
               sch.run_time, sch.schedule_type,
               sch.tracking_status AS tracking_status_override,
               s.script_name, s.script_path,
               u.username,
               jlatest.status AS latest_job_status,
               {_TRACKING_STATUS_SQL} AS tracking_status,
               COALESCE(js.total_jobs, 0) AS total_jobs,
               COALESCE(js.completed_jobs, 0) AS completed_jobs,
               COALESCE(js.failed_jobs, 0) AS failed_jobs,
               COALESCE(js.running_jobs, 0) AS running_jobs,
               COALESCE(js.pending_jobs, 0) AS pending_jobs
        FROM schedules sch
        JOIN scripts s ON s.id = sch.script_id
        JOIN users u ON u.id = sch.user_id
        {folder_join}
        {joins}
        WHERE {" AND ".join(where)}
        ORDER BY {order}
    """
    with db_cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = [row_to_dict(r) for r in cur.fetchall()]
    return [_decorate_tracking_script(r) for r in rows if r]


def list_admin_schedule_tracking(
    *,
    user_id: Optional[int] = None,
    worker_name: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    folder_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict[str, Any]:
    """Admin Reports tracking nested as User → folders + individual scripts."""
    from app.services import schedule_folders as sf

    # Build folder groups (Folder Scheduler only)
    folders_by_user: dict[int, list[dict[str, Any]]] = {}
    all_folders = sf.list_folders(is_admin=True)
    for folder in all_folders:
        fid = int(folder["id"])
        owner_id = int(folder.get("user_id") or 0)
        if user_id and owner_id != int(user_id):
            continue
        if folder_name and folder_name.strip().lower() not in (folder.get("name") or "").lower():
            continue

        scripts = _fetch_admin_tracking_scripts(
            exclude_folder_members=False,
            only_folder_id=fid,
            worker_name=worker_name or None,
            status=None,
            search=None,
            date_from=date_from,
            date_to=date_to,
        )
        if worker_name:
            scripts = [s for s in scripts if (s.get("worker_name") or "") == worker_name]
        if search:
            q = search.strip().lower()
            fname = (folder.get("name") or "").lower()
            if q not in fname:
                scripts = [
                    s for s in scripts
                    if q in (s.get("script_name") or "").lower()
                    or q in (s.get("worker_name") or "").lower()
                ]
                if not scripts:
                    continue
        if not scripts and worker_name:
            continue

        total_jobs = sum(int(s.get("total_jobs") or 0) for s in scripts)
        completed_jobs = sum(int(s.get("completed_jobs") or 0) for s in scripts)
        pct = round((100.0 * completed_jobs / total_jobs), 1) if total_jobs else 0.0
        tracking_status = _aggregate_tracking_status(
            [s.get("tracking_status") or "pending" for s in scripts]
        ) if scripts else "pending"
        if status:
            want = status.strip().lower().replace(" ", "_")
            if want == "running":
                want = "in_progress"
            if tracking_status != want:
                scripts = [s for s in scripts if (s.get("tracking_status") or "pending") == want]
                if not scripts:
                    continue
                total_jobs = sum(int(s.get("total_jobs") or 0) for s in scripts)
                completed_jobs = sum(int(s.get("completed_jobs") or 0) for s in scripts)
                pct = round((100.0 * completed_jobs / total_jobs), 1) if total_jobs else 0.0
                tracking_status = _aggregate_tracking_status(
                    [s.get("tracking_status") or "pending" for s in scripts]
                )

        workers = sorted({(s.get("worker_name") or "") for s in scripts if s.get("worker_name")})
        folder_override = (folder.get("tracking_status") or "").strip().lower()
        if folder_override not in _TRACKING_STATUS_ALLOWED:
            folder_override = ""
        item = {
            "id": fid,
            "name": folder.get("name") or f"Folder #{fid}",
            "user_id": owner_id,
            "username": folder.get("username"),
            "enabled": folder.get("enabled"),
            "workers": workers,
            "script_count": len(scripts),
            "total_jobs": total_jobs,
            "completed_jobs": completed_jobs,
            "completion_pct": pct,
            "tracking_status": folder_override or tracking_status,
            "status_is_manual": bool(folder_override),
            "scripts": scripts,
            "kind": "folder",
        }
        folders_by_user.setdefault(owner_id, []).append(item)

    for lst in folders_by_user.values():
        lst.sort(key=lambda f: (f.get("name") or "").lower())

    # Individual (regular) scripts — never folder members
    regular_scripts = _fetch_admin_tracking_scripts(
        exclude_folder_members=True,
        user_id=user_id,
        worker_name=worker_name or None,
        status=status,
        search=search,
        date_from=date_from,
        date_to=date_to,
    )
    scripts_by_user: dict[int, list[dict[str, Any]]] = {}
    for sch in regular_scripts:
        uid = int(sch.get("user_id") or 0)
        scripts_by_user.setdefault(uid, []).append(sch)

    # Assemble user containers (anyone with folders and/or individual scripts)
    user_ids = set(folders_by_user.keys()) | set(scripts_by_user.keys())
    # Also include filter user even if empty so UI can show empty state
    if user_id:
        user_ids.add(int(user_id))

    # Username lookup
    name_by_id: dict[int, str] = {}
    for uid, flist in folders_by_user.items():
        if flist:
            name_by_id[uid] = flist[0].get("username") or f"User #{uid}"
    for uid, slist in scripts_by_user.items():
        if uid not in name_by_id and slist:
            name_by_id[uid] = slist[0].get("username") or f"User #{uid}"
    missing = [uid for uid in user_ids if uid not in name_by_id]
    if missing:
        with db_cursor() as cur:
            placeholders = ",".join("?" for _ in missing)
            cur.execute(
                f"SELECT id, username FROM users WHERE id IN ({placeholders})",
                tuple(missing),
            )
            for r in cur.fetchall():
                d = row_to_dict(r)
                name_by_id[int(d["id"])] = d.get("username") or f"User #{d['id']}"

    users_out: list[dict[str, Any]] = []
    for uid in sorted(user_ids, key=lambda i: (name_by_id.get(i) or "").lower()):
        folders = folders_by_user.get(uid) or []
        scripts = scripts_by_user.get(uid) or []
        if not folders and not scripts and user_id and int(user_id) == uid:
            # Explicit user filter with nothing to show
            pass
        elif not folders and not scripts:
            continue

        # Aggregate across folder scripts + individual scripts
        all_statuses: list[str] = []
        total_jobs = 0
        completed_jobs = 0
        for f in folders:
            total_jobs += int(f.get("total_jobs") or 0)
            completed_jobs += int(f.get("completed_jobs") or 0)
            all_statuses.append(f.get("tracking_status") or "pending")
            for s in f.get("scripts") or []:
                all_statuses.append(s.get("tracking_status") or "pending")
        for s in scripts:
            total_jobs += int(s.get("total_jobs") or 0)
            completed_jobs += int(s.get("completed_jobs") or 0)
            all_statuses.append(s.get("tracking_status") or "pending")

        workers = sorted({
            *(w for f in folders for w in (f.get("workers") or [])),
            *((s.get("worker_name") or "") for s in scripts if s.get("worker_name")),
        } - {""})

        users_out.append({
            "user_id": uid,
            "username": name_by_id.get(uid) or f"User #{uid}",
            "folder_count": len(folders),
            "script_count": len(scripts),
            "total_jobs": total_jobs,
            "completed_jobs": completed_jobs,
            "completion_pct": round((100.0 * completed_jobs / total_jobs), 1) if total_jobs else 0.0,
            "tracking_status": _aggregate_tracking_status(all_statuses),
            "workers": workers,
            "folders": folders,
            "scripts": scripts,
            "kind": "user",
        })

    if status:
        want = status.strip().lower().replace(" ", "_")
        if want == "running":
            want = "in_progress"
        # Keep users whose aggregate matches OR who still have matching children
        filtered = []
        for u in users_out:
            if u.get("tracking_status") == want:
                filtered.append(u)
                continue
            if any((f.get("tracking_status") == want) or any(
                (s.get("tracking_status") or "pending") == want for s in (f.get("scripts") or [])
            ) for f in (u.get("folders") or [])):
                filtered.append(u)
                continue
            if any((s.get("tracking_status") or "pending") == want for s in (u.get("scripts") or [])):
                filtered.append(u)
        users_out = filtered

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT u.id, u.username
            FROM schedules sch
            JOIN users u ON u.id = sch.user_id
            WHERE sch.is_deleted = 0
            ORDER BY u.username
            """
        )
        filter_users = [row_to_dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT DISTINCT sch.worker_name
            FROM schedules sch
            WHERE sch.is_deleted = 0 AND sch.worker_name IS NOT NULL AND sch.worker_name <> ''
            ORDER BY sch.worker_name
            """
        )
        filter_workers = [(row_to_dict(r).get("worker_name") or "") for r in cur.fetchall()]

    return {
        "users": users_out,
        "filters": {
            "users": filter_users,
            "workers": filter_workers,
        },
        "summary": {
            "users": len(users_out),
            "folder_groups": sum(int(u.get("folder_count") or 0) for u in users_out),
            "regular_scripts": sum(int(u.get("script_count") or 0) for u in users_out),
        },
    }


def _normalize_tracking_status(value: Optional[str]) -> tuple[bool, Optional[str]]:
    raw = (value or "").strip().lower().replace(" ", "_")
    if raw in ("", "auto"):
        return True, None
    if raw == "running":
        raw = "in_progress"
    if raw not in _TRACKING_STATUS_ALLOWED:
        return False, None
    return True, raw


def set_admin_tracking_status(kind: str, item_id: int, status: Optional[str]) -> dict[str, Any]:
    """Admin override for Schedule Tracking. Empty/auto clears override."""
    kind_n = (kind or "").strip().lower()
    if kind_n not in ("script", "folder"):
        return {"ok": False, "error": "kind must be script or folder"}
    ok, normalized = _normalize_tracking_status(status)
    if not ok:
        return {"ok": False, "error": "Invalid status"}
    table = "schedules" if kind_n == "script" else "schedule_folders"
    with db_cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET tracking_status = ? WHERE id = ?",
            (normalized, int(item_id)),
        )
        if cur.rowcount <= 0:
            return {"ok": False, "error": "Not found"}
    return {
        "ok": True,
        "kind": kind_n,
        "id": int(item_id),
        "tracking_status": normalized,
        "status_is_manual": normalized is not None,
    }


CONTROLLER_URL = "http://192.168.50.89:7561"

def create_schedule(script_id: int, user_id: int, worker_name: str, run_time: str, days: Optional[int] = None, schedule_config: Optional[str] = None, schedule_type: str = 'daily') -> dict[str, Any]:
    from datetime import datetime
    now_utc = _utc_now()
    
    # If the file hasn't run via this schedule yet, keep last_run NULL
    last_run = None

    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (script_id, user_id, worker_name, run_time, days, last_run, created_at, updated_at, schedule_config, schedule_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (script_id, user_id, worker_name, run_time, days, last_run, now_utc, now_utc, schedule_config, schedule_type),
        )
        schedule_id = cur.lastrowid
        
        # Grant permissions to the creator except delete
        cur.execute(
            "INSERT INTO schedule_access (user_id, schedule_id, granted_by, granted_at, can_enable, can_disable, can_run, can_duplicate, can_edit, can_delete) VALUES (?, ?, ?, ?, 1, 1, 1, 1, 1, 0)",
            (user_id, schedule_id, user_id, now_utc)
        )
        
        cur.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return row_to_dict(cur.fetchone())

def update_schedule_full(schedule_id: int, script_id: int, worker_name: str, run_time: str, days: Optional[int] = None, schedule_config: Optional[str] = None, schedule_type: str = 'daily') -> bool:
    now_utc = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "UPDATE schedules SET script_id = ?, worker_name = ?, run_time = ?, days = ?, updated_at = ?, schedule_config = ?, schedule_type = ? WHERE id = ?",
            (script_id, worker_name, run_time, days, now_utc, schedule_config, schedule_type, schedule_id)
        )
        return cur.rowcount > 0

def duplicate_schedule(user_id: int, schedule_id: int) -> Optional[dict[str, Any]]:
    sch = get_schedule(schedule_id)
    if not sch:
        return None
    # create new schedule with same settings but disabled
    from datetime import datetime
    now_utc = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (script_id, user_id, worker_name, run_time, days, last_run, created_at, updated_at, enabled, schedule_config, schedule_type) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?)",
            (sch["script_id"], user_id, sch["worker_name"], sch["run_time"], sch["days"], now_utc, now_utc, sch.get("schedule_config"), sch.get("schedule_type", "daily")),
        )
        new_id = cur.lastrowid
        # Grant permissions to the creator except delete
        cur.execute(
            "INSERT INTO schedule_access (user_id, schedule_id, granted_by, granted_at, can_enable, can_disable, can_run, can_duplicate, can_edit, can_delete) VALUES (?, ?, ?, ?, 1, 1, 1, 1, 1, 0)",
            (user_id, new_id, user_id, now_utc)
        )
        cur.execute("SELECT * FROM schedules WHERE id = ?", (new_id,))
        return row_to_dict(cur.fetchone())


def clone_schedule_independent(source_schedule_id: int, *, as_folder_copy: bool = True) -> Optional[dict[str, Any]]:
    """
    Deep-copy a schedule into a new independent row (new id).
    Original is untouched. Used when adding existing schedules into a folder.
    """
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM schedules WHERE id = ? AND COALESCE(is_deleted, 0) = 0",
            (int(source_schedule_id),),
        )
        sch = row_to_dict(cur.fetchone())
        if not sch:
            return None
        now_utc = _utc_now()
        enabled = 1 if int(sch.get("enabled") or 0) == 1 else 0
        folder_copy = 1 if as_folder_copy else 0
        # Prefer full insert including is_folder_copy when column exists
        try:
            cur.execute(
                """
                INSERT INTO schedules (
                    script_id, user_id, worker_name, run_time, days, last_run,
                    created_at, updated_at, enabled, schedule_config, schedule_type,
                    tracking_status, is_folder_copy
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    sch["script_id"],
                    sch["user_id"],
                    sch["worker_name"],
                    sch["run_time"],
                    sch.get("days"),
                    now_utc,
                    now_utc,
                    enabled,
                    sch.get("schedule_config"),
                    sch.get("schedule_type") or "daily",
                    folder_copy,
                ),
            )
        except Exception:
            cur.execute(
                """
                INSERT INTO schedules (
                    script_id, user_id, worker_name, run_time, days, last_run,
                    created_at, updated_at, enabled, schedule_config, schedule_type
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    sch["script_id"],
                    sch["user_id"],
                    sch["worker_name"],
                    sch["run_time"],
                    sch.get("days"),
                    now_utc,
                    now_utc,
                    enabled,
                    sch.get("schedule_config"),
                    sch.get("schedule_type") or "daily",
                ),
            )
        new_id = cur.lastrowid
        # Owner access (same pattern as create/duplicate)
        cur.execute(
            """
            INSERT INTO schedule_access (
                user_id, schedule_id, granted_by, granted_at,
                can_enable, can_disable, can_run, can_duplicate, can_edit, can_delete
            ) VALUES (?, ?, ?, ?, 1, 1, 1, 1, 1, 0)
            """,
            (sch["user_id"], new_id, sch["user_id"], now_utc),
        )
        # Copy other users' grants from the source (independent rows)
        cur.execute(
            """
            INSERT INTO schedule_access (
                user_id, schedule_id, granted_by, granted_at,
                can_enable, can_disable, can_run, can_duplicate, can_edit, can_delete
            )
            SELECT user_id, ?, granted_by, ?, can_enable, can_disable, can_run,
                   can_duplicate, can_edit, can_delete
            FROM schedule_access
            WHERE schedule_id = ? AND user_id != ?
            """,
            (new_id, now_utc, int(source_schedule_id), sch["user_id"]),
        )
        cur.execute("SELECT * FROM schedules WHERE id = ?", (new_id,))
        return row_to_dict(cur.fetchone())

def get_schedule(schedule_id: int) -> Optional[dict[str, Any]]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sch.*, s.script_name, s.script_path, s.worker_name, u.username
            FROM schedules sch
            JOIN scripts s ON s.id = sch.script_id
            JOIN users u ON u.id = sch.user_id
            WHERE sch.id = ?
            """,
            (schedule_id,),
        )
        return row_to_dict(cur.fetchone())


def user_can_schedule_flag(user_id: int, schedule_id: int, flag: str) -> bool:
    """Same Scheduler-section rules: admin, owner, or schedule_access.<flag>."""
    if is_admin(user_id):
        return True
    allowed = {"can_delete", "can_enable", "can_disable", "can_run", "can_duplicate", "can_edit"}
    if flag not in allowed:
        return False
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sch.user_id AS owner_id, sa.id AS access_id,
                   COALESCE(sa.can_delete, 0) AS can_delete,
                   COALESCE(sa.can_enable, 0) AS can_enable,
                   COALESCE(sa.can_disable, 0) AS can_disable,
                   COALESCE(sa.can_run, 0) AS can_run,
                   COALESCE(sa.can_duplicate, 0) AS can_duplicate,
                   COALESCE(sa.can_edit, 0) AS can_edit
            FROM schedules sch
            LEFT JOIN schedule_access sa
              ON sa.schedule_id = sch.id AND sa.user_id = ?
            WHERE sch.id = ? AND COALESCE(sch.is_deleted, 0) = 0
            """,
            (user_id, schedule_id),
        )
        row = cur.fetchone()
        if not row:
            return False
        if int(row["owner_id"] or 0) == int(user_id):
            return True
        if row["access_id"] is None:
            return False
        if flag == "can_disable":
            return int(row["can_disable"] or 0) == 1 or int(row["can_enable"] or 0) == 1
        return int(row[flag] or 0) == 1


def user_can_edit_schedule(user_id: int, schedule_id: int) -> bool:
    """Admin, explicit can_edit grant, or owner with no access row may update."""
    if is_admin(user_id):
        return True
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT sch.user_id AS owner_id, sa.id AS access_id, COALESCE(sa.can_edit, 0) AS can_edit
            FROM schedules sch
            LEFT JOIN schedule_access sa
              ON sa.schedule_id = sch.id AND sa.user_id = ?
            WHERE sch.id = ? AND COALESCE(sch.is_deleted, 0) = 0
            """,
            (user_id, schedule_id),
        )
        row = cur.fetchone()
        if not row:
            return False
        if row["access_id"] is not None:
            return int(row["can_edit"] or 0) == 1
        return int(row["owner_id"] or 0) == int(user_id)

def delete_schedule(schedule_id: int) -> bool:
    with db_cursor() as cur:
        # Soft-delete the schedule to preserve its job history
        cur.execute("UPDATE schedules SET is_deleted = 1, enabled = 0 WHERE id = ?", (schedule_id,))
        return cur.rowcount > 0

def update_schedule_days(schedule_id: int, days: Optional[int]) -> bool:
    now = _utc_now()
    with db_cursor() as cur:
        cur.execute(
            "UPDATE schedules SET days = ?, updated_at = ? WHERE id = ?",
            (days, now, schedule_id),
        )
        return cur.rowcount > 0


def schedule_script_has_days(schedule_id: int) -> bool:
    """True when the schedule's script currently has a days = N variable in DB."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT s.days AS script_days
            FROM schedules sch
            JOIN scripts s ON s.id = sch.script_id
            WHERE sch.id = ? AND COALESCE(sch.is_deleted, 0) = 0
            """,
            (schedule_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        return row["script_days"] is not None


def update_schedule_days_if_script_has_days(schedule_id: int, days: int) -> bool:
    """
    Set schedule.days override only when the linked script has a days variable.
    Scripts without days are left untouched (safe for folder bulk apply).
    """
    if not schedule_script_has_days(schedule_id):
        return False
    return update_schedule_days(schedule_id, int(days))


def reset_all_schedules_days() -> int:
    with db_cursor() as cur:
        cur.execute("UPDATE schedules SET days = 0 WHERE days > 0")
        return cur.rowcount


def get_active_job_for_schedule(schedule_id: int) -> Optional[int]:
    """Return the ID of the currently pending, running, or paused job for a schedule."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id FROM jobs
            WHERE schedule_id = ? AND status IN ('pending', 'running', 'paused')
            ORDER BY id DESC LIMIT 1
            """,
            (schedule_id,),
        )
        row = cur.fetchone()
        return row["id"] if row else None


_IMG_DOWNLOAD_EVIDENCE_RE = None
_IMG_EXT_RE = r"(?:jpg|jpeg|png|webp|gif|bmp|tif|tiff|jfif)"


def log_has_image_download_evidence(output: str) -> bool:
    """True only when the log talks about actually downloading/saving images."""
    import re
    global _IMG_DOWNLOAD_EVIDENCE_RE
    if _IMG_DOWNLOAD_EVIDENCE_RE is None:
        _IMG_DOWNLOAD_EVIDENCE_RE = re.compile(
            r"(?i)(?:"
            r"total\s+images?[:=]\s*\d+"
            r"|(?:downloaded|saved|fetched)\s+\d+\s+images?"
            r"|images?\s+(?:downloaded|saved|fetched)"
            r"|image\s+(?:downloaded|saved|fetched)"
            r"|images?\s+downloaded\s+and\s+saved"
            r")"
        )
    return bool(_IMG_DOWNLOAD_EVIDENCE_RE.search(output or ""))


def counts_from_job_log(output: str) -> dict[str, int]:
    """Best-effort image/pdf counts from a live or final job log.

    Image totals come only from explicit download/save language — not from
    incidental .jpg/.png mentions (script paths, URLs, stack traces).
    """
    import re
    text = output or ""
    image_count = 0
    pdf_count = 0
    page_hints = 0
    m = re.search(r"(?i)total images?[:=]\s*(\d+)", text)
    if m:
        image_count = int(m.group(1))
    for pat in (
        r"(?i)(?:downloaded|saved|fetched)\s+(\d+)\s+images?",
        r"(?i)images?\s+(?:downloaded|saved|fetched)[:=]?\s*(\d+)",
    ):
        m = re.search(pat, text)
        if m:
            image_count = max(image_count, int(m.group(1)))
            break
    dl_img = len(
        re.findall(
            r"(?i)(?:image downloaded|image saved|images? downloaded and saved)",
            text,
        )
    )
    if dl_img:
        image_count = max(image_count, dl_img)
    # Filenames only on lines that look like a download/save, not the whole log
    dl_img_files: set[str] = set()
    for line in text.splitlines():
        if not re.search(r"(?i)\b(?:download(?:ed)?|saved|fetched|wrote|written)\b", line):
            continue
        if re.search(r"(?i)\.pdf\b", line) and not re.search(
            rf"(?i)\.{_IMG_EXT_RE}\b", line
        ):
            continue
        for fn in re.findall(
            rf"(?i)([^\s'\"<>]+\.{_IMG_EXT_RE})\b",
            line,
        ):
            dl_img_files.add(fn.lower())
    if dl_img_files:
        image_count = max(image_count, len(dl_img_files))
    if not log_has_image_download_evidence(text) and not dl_img_files:
        image_count = 0

    m = re.search(r"(?i)(?:total\s+)?pdfs?[:=]\s*(\d+)|(?:downloaded|saved)\s+(\d+)\s+pdfs?", text)
    if m:
        pdf_count = int(m.group(1) or m.group(2) or 0)
    pages = re.findall(r"(?i)\bpage\s*(\d+)\b", text)
    if pages:
        try:
            page_hints = max(int(p) for p in pages)
        except ValueError:
            page_hints = len(pages)
    return {
        "image_count": image_count,
        "pdf_count": pdf_count,
        "file_count": image_count + pdf_count,
        "log_count": page_hints,
    }


def _correct_report_image_counts(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Zero image_count when the job log has no image-download evidence."""
    job_ids = []
    by_job: dict[int, list[dict[str, Any]]] = {}
    for row in reports:
        try:
            jid = int(row.get("job_id") or 0)
        except (TypeError, ValueError):
            jid = 0
        if jid <= 0 or int(row.get("image_count") or 0) <= 0:
            continue
        job_ids.append(jid)
        by_job.setdefault(jid, []).append(row)
    if not job_ids:
        return reports
    placeholders = ",".join("?" * len(job_ids))
    try:
        with db_cursor() as cur:
            cur.execute(
                f"SELECT id, output FROM jobs WHERE id IN ({placeholders})",
                job_ids,
            )
            outputs = {int(r["id"]): (r["output"] or "") for r in cur.fetchall()}
    except Exception:
        return reports
    persist: list[tuple[int, int, int]] = []
    for jid, rows in by_job.items():
        output = outputs.get(jid)
        if output is None:
            continue
        parsed = counts_from_job_log(output)
        if log_has_image_download_evidence(output) or parsed["image_count"] > 0:
            continue
        for row in rows:
            old = int(row.get("image_count") or 0)
            if old <= 0:
                continue
            pdf_n = int(row.get("pdf_count") or 0)
            row["image_count"] = 0
            row["file_count"] = pdf_n
            rid = row.get("id")
            if rid:
                persist.append((0, pdf_n, int(rid)))
    if persist:
        try:
            with db_cursor() as cur:
                cur.executemany(
                    "UPDATE scraper_reports SET image_count = ?, file_count = ? WHERE id = ?",
                    persist,
                )
        except Exception:
            pass
    return reports


def _correct_report_error_display(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Fix Errors/Details display for existing DB rows:
    - completed: clamp inflated mid-run error_count to failed_downloads; hide empty details
    - stopped: error_count=0; label User Interrupted
    - error misclassified as Failed when log is Ctrl+C / KeyboardInterrupt → treat as stopped
    """
    import json
    if not reports:
        return reports

    try:
        from app.blueprints.api.routes import (
            _is_user_interrupted,
            _normalize_report_errors_for_status,
            parse_execution_log,
        )
    except Exception:
        for row in reports:
            status = (row.get("status") or "").lower()
            fd = int(row.get("failed_downloads") or 0)
            if status == "completed" and int(row.get("error_count") or 0) > fd:
                row["error_count"] = fd
                if fd == 0:
                    row["error_details"] = None
            elif status == "stopped":
                row["error_count"] = 0
                row["primary_error_category"] = "User Interrupted"
        return reports

    need_output: list[int] = []
    for row in reports:
        status = (row.get("status") or "").lower()
        fd = int(row.get("failed_downloads") or 0)
        ec = int(row.get("error_count") or 0)
        try:
            jid = int(row.get("job_id") or 0)
        except (TypeError, ValueError):
            jid = 0
        if jid <= 0:
            continue
        # Only fetch logs when display likely wrong or status may be interrupt mis-label
        if status == "error" or (status == "completed" and ec > fd) or (
            status == "stopped" and ec > 0
        ):
            need_output.append(jid)

    outputs: dict[int, tuple[str, Any]] = {}
    if need_output:
        uniq = list(dict.fromkeys(need_output))
        placeholders = ",".join("?" * len(uniq))
        try:
            with db_cursor() as cur:
                cur.execute(
                    f"SELECT id, output, exit_code FROM jobs WHERE id IN ({placeholders})",
                    uniq,
                )
                for r in cur.fetchall():
                    outputs[int(r["id"])] = (r["output"] or "", r["exit_code"])
        except Exception:
            outputs = {}

    persist: list[tuple[Any, ...]] = []
    for row in reports:
        status = (row.get("status") or "").lower()
        fd = int(row.get("failed_downloads") or 0)
        old_ec = int(row.get("error_count") or 0)
        old_status = status
        old_details = row.get("error_details")
        try:
            jid = int(row.get("job_id") or 0)
        except (TypeError, ValueError):
            jid = 0

        if jid not in outputs:
            if status == "completed" and old_ec > fd:
                row["error_count"] = fd
                if fd == 0:
                    row["error_details"] = None
            elif status == "stopped":
                row["error_count"] = 0
                row["primary_error_category"] = "User Interrupted"
            continue

        output, exit_code = outputs[jid]
        details = {}
        if isinstance(old_details, str) and old_details.strip():
            try:
                details = json.loads(old_details)
            except Exception:
                details = {}
        elif isinstance(old_details, dict):
            details = old_details

        parsed = parse_execution_log(output) or {}
        if status == "error" and _is_user_interrupted(
            output, exit_code, parsed if isinstance(parsed, dict) else details
        ):
            status = "stopped"
            row["status"] = "stopped"
            row["primary_error_category"] = "User Interrupted"

        base = {
            "error_details": parsed or details,
            "error_count": old_ec,
            "failed_downloads": fd or int((parsed.get("download_metrics") or {}).get("log_failed_downloads") or 0),
        }
        if not fd:
            fd = int(base["failed_downloads"] or 0)
            row["failed_downloads"] = fd
        normalized = _normalize_report_errors_for_status(base, status, output)
        new_ec = int(normalized.get("error_count") or 0)
        new_details_obj = normalized.get("error_details")
        new_details = (
            json.dumps(new_details_obj) if isinstance(new_details_obj, dict)
            else new_details_obj
        )
        row["error_count"] = new_ec
        row["error_details"] = new_details
        if isinstance(new_details_obj, dict) and new_details_obj.get("primary_error_category"):
            row["primary_error_category"] = new_details_obj["primary_error_category"]
        elif status == "stopped":
            row["primary_error_category"] = "User Interrupted"

        rid = row.get("id")
        if rid is not None and (
            status != old_status
            or new_ec != old_ec
            or (new_details or None) != (old_details or None)
        ):
            persist.append((status, new_ec, new_details, int(rid)))

    if persist:
        try:
            with db_cursor() as cur:
                cur.executemany(
                    "UPDATE scraper_reports SET status = ?, error_count = ?, error_details = ? WHERE id = ?",
                    persist,
                )
        except Exception:
            pass
    return reports


def get_scraper_report_id_for_job(job_id: int) -> Optional[int]:
    with db_cursor() as cur:
        cur.execute("SELECT id FROM scraper_reports WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        return int(row["id"] if hasattr(row, "keys") else row[0])


def ensure_stopped_job_report(job_id: int) -> Optional[int]:
    """Create/update a reports-page row for a stopped/failed job using the job log."""
    import json
    job = get_job(job_id)
    if not job:
        return None
    out = job.get("output") or ""
    counts = counts_from_job_log(out)
    status = job.get("status") or "stopped"
    if status not in ("stopped", "error", "completed"):
        status = "stopped"
    # Prefer structured parse when available (same categories as live job endpoints)
    parsed = {}
    try:
        from app.blueprints.api.routes import (
            parse_execution_log,
            _is_user_interrupted,
            _normalize_report_errors_for_status,
        )
        parsed = parse_execution_log(out) or {}
        if _is_user_interrupted(out, job.get("exit_code"), parsed):
            status = "stopped"
    except Exception:
        parsed = {}
    if _output_looks_pc_terminated(out) and status == "stopped":
        title = "Terminated on worker PC"
    elif "stop requested from dashboard" in out.lower() or "stopped by user" in out.lower():
        title = "Stopped from dashboard"
    elif status == "stopped":
        title = "User Interrupted"
    elif status == "error":
        title = (parsed.get("primary_error_category") if parsed else None) or "Script Error"
    else:
        title = "Stopped"
    if parsed:
        details_obj = dict(parsed)
        details_obj.setdefault("error_title", title)
        details_obj.setdefault("error_type", parsed.get("primary_error_category") or title)
        err_n = int(parsed.get("error_count") or 0)
        warn_n = int(parsed.get("warning_count") or 0)
        failed_dl = int((parsed.get("download_metrics") or {}).get("log_failed_downloads") or 0)
        try:
            normalized = _normalize_report_errors_for_status(
                {"error_details": details_obj, "error_count": err_n, "failed_downloads": failed_dl},
                status,
                out,
            )
            details = json.dumps(normalized.get("error_details")) if normalized.get("error_details") else None
            err_n = int(normalized.get("error_count") or 0)
        except Exception:
            details = json.dumps(details_obj)
    else:
        details = json.dumps({
            "error_title": title,
            "error_type": "User Interrupted" if status == "stopped" else "Error",
            "errors": [{
                "error_type": "User Interrupted" if status == "stopped" else "Error",
                "error_title": title,
                "error_message": title,
                "source_file": "",
                "line_number": "",
                "traceback": "",
            }] if status == "stopped" else [],
            "failed_files": [],
            "missing_files": [],
            "folder_summary": {},
            "error": out[-2000:] if out else title,
        })
        err_n = 0
        warn_n = 0
        failed_dl = 0
    return insert_scraper_report(
        worker_name=job["worker_name"],
        script_name=job.get("script_name") or "Unknown",
        script_id=job["script_id"],
        job_id=job["id"],
        folder_path="",
        status=status,
        start_time=job.get("start_time") or "",
        end_time=job.get("end_time") or _utc_now(),
        duration=job.get("duration") or 0.0,
        image_count=counts["image_count"],
        pdf_count=counts["pdf_count"],
        file_count=counts["file_count"],
        log_count=counts.get("log_count") or 0,
        warning_count=warn_n,
        error_count=err_n,
        failed_downloads=failed_dl,
        error_details=details,
    )


def _populate_scraper_report_children(cur, report_id: int, folder_path: str, error_details: Optional[str]) -> None:
    """Sync normalized error/file rows from error_details JSON (insert or refresh)."""
    if not error_details:
        return
    try:
        import json
        details = json.loads(error_details) if isinstance(error_details, str) else (error_details or {})
        if not isinstance(details, dict):
            return
        errors = details.get("errors") or []
        failed_files = details.get("failed_files") or []
        missing_files = details.get("missing_files") or []
        # Refresh child rows when we have structured errors so category/count stay aligned
        if errors or failed_files or missing_files:
            cur.execute("DELETE FROM scraper_report_errors WHERE report_id = ?", (report_id,))
            cur.execute("DELETE FROM scraper_report_files WHERE report_id = ?", (report_id,))
        if errors:
            cur.executemany(
                """
                INSERT INTO scraper_report_errors (
                    report_id, error_category, error_message, source_file, line_number, traceback
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        report_id,
                        err.get("error_type"),
                        err.get("error_message"),
                        err.get("source_file"),
                        err.get("line_number"),
                        err.get("traceback"),
                    )
                    for err in errors
                ],
            )
        file_rows = [
            (report_id, f, folder_path, "failed") for f in failed_files
        ] + [
            (report_id, f, folder_path, "missing") for f in missing_files
        ]
        if file_rows:
            cur.executemany(
                """
                INSERT INTO scraper_report_files (
                    report_id, file_path, folder_path, issue_type
                ) VALUES (?, ?, ?, ?)
                """,
                file_rows,
            )
    except Exception as e:
        print(f"Failed to populate normalized tables for report {report_id}: {e}")


def insert_scraper_report(worker_name: str, script_name: str, script_id: int, job_id: int, 
                          folder_path: str, status: str, start_time: str, end_time: str, 
                          duration: float, image_count: int=0, pdf_count: int=0, file_count: int=0, 
                          log_count: int=0, warning_count: int=0, error_count: int=0, total_folder_size: int=0, 
                          failed_downloads: int=0, error_details: str = None) -> int:
    folder_path = normalize_report_folder_path(folder_path)
    existing_id = get_scraper_report_id_for_job(job_id)
    image_count = int(image_count or 0)
    pdf_count = int(pdf_count or 0)
    file_count = int(file_count or 0)
    # When structured error_details is present, trust its counts (avoid sticky inflated GREATEST)
    structured_stats = False
    auth_error_count = int(error_count or 0)
    auth_warning_count = int(warning_count or 0)
    auth_failed_downloads = int(failed_downloads or 0)
    if error_details:
        try:
            import json
            det = json.loads(error_details) if isinstance(error_details, str) else error_details
            if isinstance(det, dict) and ("errors" in det or "error_count" in det or "warnings" in det):
                structured_stats = True
                parsed_ec = det.get("error_count")
                if parsed_ec is None:
                    parsed_ec = len(det.get("errors") or [])
                auth_error_count = max(int(parsed_ec or 0), int((det.get("download_metrics") or {}).get("log_failed_downloads") or 0))
                parsed_wc = det.get("warning_count")
                if parsed_wc is None:
                    parsed_wc = len(det.get("warnings") or [])
                auth_warning_count = int(parsed_wc or 0)
                auth_failed_downloads = max(
                    auth_failed_downloads,
                    int((det.get("download_metrics") or {}).get("log_failed_downloads") or 0),
                )
        except Exception:
            structured_stats = False
    with db_cursor() as cur:
        if existing_id:
            # Keep completed unless a later report corrects to error (crash after false complete),
            # or upgrades stopped → completed/error (false PC-terminate). Never downgrade
            # completed → stopped.
            if structured_stats:
                err_sql = "error_count = ?, warning_count = ?, failed_downloads = ?"
                err_params = (auth_error_count, auth_warning_count, auth_failed_downloads)
            else:
                err_sql = (
                    "warning_count = GREATEST(COALESCE(warning_count, 0), ?), "
                    "error_count = GREATEST(COALESCE(error_count, 0), ?), "
                    "failed_downloads = GREATEST(COALESCE(failed_downloads, 0), ?)"
                )
                err_params = (auth_warning_count, auth_error_count, auth_failed_downloads)
            cur.execute(
                f"""
                UPDATE scraper_reports SET
                    status = CASE
                        WHEN ? = 'error' THEN 'error'
                        WHEN status = 'completed' AND ? = 'stopped' THEN status
                        WHEN status = 'stopped' AND ? IN ('completed', 'error') THEN ?
                        ELSE COALESCE(?, status)
                    END,
                    folder_path = CASE WHEN COALESCE(folder_path, '') = '' AND ? <> '' THEN ? ELSE folder_path END,
                    end_time = COALESCE(?, end_time),
                    duration = CASE WHEN ? IS NOT NULL AND ? > COALESCE(duration, 0) THEN ? ELSE duration END,
                    image_count = GREATEST(COALESCE(image_count, 0), ?),
                    pdf_count = GREATEST(COALESCE(pdf_count, 0), ?),
                    file_count = GREATEST(COALESCE(file_count, 0), ?),
                    log_count = GREATEST(COALESCE(log_count, 0), ?),
                    {err_sql},
                    total_folder_size = GREATEST(COALESCE(total_folder_size, 0), ?),
                    error_details = COALESCE(?, error_details)
                WHERE id = ?
                """,
                (
                    status, status, status, status, status,
                    folder_path or "", folder_path or "",
                    end_time, duration, duration, duration,
                    image_count, pdf_count, file_count,
                    int(log_count or 0),
                    *err_params,
                    int(total_folder_size or 0),
                    error_details, existing_id,
                ),
            )
            _populate_scraper_report_children(cur, existing_id, folder_path or "", error_details)
            return existing_id
        cur.execute(
            """
            INSERT INTO scraper_reports (
                worker_name, script_name, script_id, job_id, folder_path, status,
                start_time, end_time, duration, image_count, pdf_count, file_count,
                log_count, warning_count, error_count, total_folder_size, failed_downloads, error_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (worker_name, script_name, script_id, job_id, folder_path, status,
             start_time, end_time, duration, image_count, pdf_count, file_count,
             log_count, auth_warning_count, auth_error_count, total_folder_size, auth_failed_downloads, error_details)
        )
        report_id = cur.lastrowid
        _populate_scraper_report_children(cur, report_id, folder_path or "", error_details)
        return report_id

def get_paginated_reports(worker_name: Optional[str], script_id: Optional[int], 
                          status: Optional[str], date_from: str, date_to: str,
                          search: str, limit: int, offset: int, user_id: Optional[int] = None,
                          folder_path: Optional[str] = None,
                          has_errors: Optional[bool] = None,
                          processed_lt: Optional[int] = None) -> tuple[list[dict], int]:
    where_sql = " WHERE 1=1"
    params: list = []

    if user_id is not None and not is_admin(user_id):
        accessible_workers = [w["worker_name"] for w in list_accessible_workers(user_id)]
        if not accessible_workers:
            return [], 0
        placeholders = ",".join("?" * len(accessible_workers))
        where_sql += f" AND worker_name IN ({placeholders})"
        params.extend(accessible_workers)

    if worker_name:
        where_sql += " AND worker_name = ?"
        params.append(worker_name)
    if script_id:
        where_sql += " AND script_id = ?"
        params.append(script_id)
    if status:
        where_sql += " AND status = ?"
        params.append(status)
    if date_from:
        where_sql += " AND start_time >= ?"
        params.append(date_from)
    if date_to:
        where_sql += " AND start_time <= ?"
        params.append(date_to + "T23:59:59")
    if search:
        where_sql += " AND (script_name LIKE ? OR folder_path LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if has_errors is True:
        where_sql += " AND COALESCE(error_count, 0) > 0"
    elif has_errors is False:
        where_sql += " AND COALESCE(error_count, 0) = 0"
    if processed_lt is not None and processed_lt > 0:
        # Match UI "Processed" column: image_count + pdf_count
        where_sql += (
            " AND (COALESCE(image_count, 0) + COALESCE(pdf_count, 0)) < ?"
        )
        params.append(int(processed_lt))

    # Alias-qualified WHERE for the SELECT (same filters/order as COUNT)
    data_where = where_sql.replace(" worker_name ", " r.worker_name ") \
                          .replace(" script_id ", " r.script_id ") \
                          .replace(" status ", " r.status ") \
                          .replace(" start_time ", " r.start_time ") \
                          .replace(" script_name ", " r.script_name ") \
                          .replace(" folder_path ", " r.folder_path ") \
                          .replace("COALESCE(error_count,", "COALESCE(r.error_count,") \
                          .replace("COALESCE(image_count,", "COALESCE(r.image_count,") \
                          .replace("COALESCE(pdf_count,", "COALESCE(r.pdf_count,")

    count_params = list(params)
    data_params = list(params)
    if folder_path:
        wroot = None
        if worker_name:
            wrec = get_worker(worker_name)
            wroot = (wrec or {}).get("script_location") or None
        where_sql = _append_report_folder_filter(
            where_sql, count_params, folder_path,
            folder_col="folder_path", script_id_col="script_id",
            worker_name=worker_name, worker_root=wroot,
        )
        data_where = _append_report_folder_filter(
            data_where, data_params, folder_path,
            folder_col="r.folder_path", script_id_col="r.script_id",
            worker_name=worker_name, worker_root=wroot,
        )

    count_query = "SELECT COUNT(*) AS cnt FROM scraper_reports" + where_sql
    data_query = (
        "SELECT r.*, "
        "(SELECT error_category FROM scraper_report_errors e WHERE e.report_id = r.id LIMIT 1) "
        "AS primary_error_category, "
        "(SELECT j.schedule_id FROM jobs j WHERE j.id = r.job_id) AS schedule_id "
        "FROM scraper_reports r"
        + data_where
        + " ORDER BY r.id DESC LIMIT ? OFFSET ?"
    )

    try:
        with db_cursor() as cur:
            cur.execute(count_query, count_params)
            total = int(cur.fetchone()["cnt"] or 0)

            safe_limit = max(1, int(limit or 10))
            safe_offset = max(0, int(offset or 0))
            cur.execute(data_query, list(data_params) + [safe_limit, safe_offset])
            reports = [dict(row) for row in cur.fetchall()]
            for row in reports:
                if row.get("folder_path"):
                    row["folder_path"] = normalize_report_folder_path(row.get("folder_path"))
            return _correct_report_error_display(_correct_report_image_counts(reports)), total
    except Exception as e:
        print(f"Error fetching paginated reports: {e}")
        return [], 0
        
def get_report_analytics(worker_name=None, script_id=None, date_from=None, date_to=None, user_id=None, folder_path=None):
    """
    Aggregates insights for the Reports Analytics tab.
    Uses the same access + filter scope as the reports list/summary (workers for
    non-admins; worker/script/date/folder). Status is intentionally not applied so
    success vs failure rates remain meaningful.
    Returns the original keys (folder_health, completion_pct, common_errors,
    script_errors, failed_files_list) plus additive summary fields.
    """
    import json

    with db_cursor() as cur:
        where_clauses = []
        params = []

        # Align access control with get_paginated_reports / get_report_summary_cards
        if user_id is not None and not is_admin(user_id):
            accessible_workers = [w["worker_name"] for w in list_accessible_workers(user_id)]
            if not accessible_workers:
                return {
                    "folder_health": {},
                    "completion_pct": 0,
                    "common_errors": {},
                    "script_errors": {},
                    "failed_files_list": [],
                    "total_runs": 0,
                    "success_runs": 0,
                    "error_runs": 0,
                    "other_runs": 0,
                    "fail_rate": 0,
                    "total_failed_downloads": 0,
                }
            placeholders = ",".join("?" * len(accessible_workers))
            where_clauses.append(f"r.worker_name IN ({placeholders})")
            params.extend(accessible_workers)

        if worker_name:
            where_clauses.append("r.worker_name = ?")
            params.append(worker_name)
        if script_id is not None:
            where_clauses.append("r.script_id = ?")
            params.append(script_id)
        if date_from:
            # Match stored start_time format: "YYYY-MM-DD HH:MM:SS"
            where_clauses.append("r.start_time >= ?")
            params.append(date_from if " " in date_from else date_from + " 00:00:00")
        if date_to:
            where_clauses.append("r.start_time <= ?")
            params.append(date_to if " " in date_to else date_to + " 23:59:59")
        where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
        if folder_path:
            wroot = None
            if worker_name:
                wrec = get_worker(worker_name)
                wroot = (wrec or {}).get("script_location") or None
            where_sql = _append_report_folder_filter(
                where_sql, params, folder_path,
                folder_col="r.folder_path", script_id_col="r.script_id",
                worker_name=worker_name, worker_root=wroot,
            )

        analytics = {
            "folder_health": {},
            "completion_pct": 0,
            "common_errors": {},
            "script_errors": {},
            "failed_files_list": [],
            "total_runs": 0,
            "success_runs": 0,
            "error_runs": 0,
            "other_runs": 0,
            "fail_rate": 0,
            "total_failed_downloads": 0,
        }

        # 1. Run totals + completion / fail rates
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN r.status = 'completed' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END) AS errors,
                SUM(COALESCE(r.failed_downloads, 0)) AS failed_downloads
            FROM scraper_reports r
            WHERE 1=1 {where_sql}
            """,
            params,
        )
        counts = cur.fetchone()
        total_runs = int(counts["total"] or 0)
        success_runs = int(counts["success"] or 0)
        error_runs = int(counts["errors"] or 0)
        analytics["total_runs"] = total_runs
        analytics["success_runs"] = success_runs
        analytics["error_runs"] = error_runs
        analytics["other_runs"] = max(0, total_runs - success_runs - error_runs)
        analytics["total_failed_downloads"] = int(counts["failed_downloads"] or 0)
        analytics["completion_pct"] = round((success_runs / total_runs * 100), 1) if total_runs else 0
        analytics["fail_rate"] = round((error_runs / total_runs * 100), 1) if total_runs else 0

        # 2. Folder health (group empty folder as blank key — UI labels Unassigned)
        cur.execute(
            f"""
            SELECT COALESCE(r.folder_path, '') AS folder_path,
                   SUM(CASE WHEN r.status = 'completed' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END) AS failed
            FROM scraper_reports r
            WHERE 1=1 {where_sql}
            GROUP BY COALESCE(r.folder_path, '')
            ORDER BY (SUM(CASE WHEN r.status = 'completed' THEN 1 ELSE 0 END)
                    + SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END)) DESC
            """,
            params,
        )
        analytics["folder_health"] = {
            row["folder_path"]: {"success": int(row["success"] or 0), "failed": int(row["failed"] or 0)}
            for row in cur.fetchall()
        }

        # 3. Common errors — prefer normalized table; fall back to report-level category
        cur.execute(
            f"""
            SELECT COALESCE(NULLIF(e.error_category, ''), 'Unknown') AS error_category,
                   COUNT(*) AS count
            FROM scraper_report_errors e
            JOIN scraper_reports r ON e.report_id = r.id
            WHERE 1=1 {where_sql}
            GROUP BY COALESCE(NULLIF(e.error_category, ''), 'Unknown')
            ORDER BY count DESC
            """,
            params,
        )
        common = {row["error_category"]: int(row["count"]) for row in cur.fetchall()}

        if not common:
            cur.execute(
                f"""
                SELECT
                    COALESCE(
                        NULLIF((
                            SELECT e.error_category FROM scraper_report_errors e
                            WHERE e.report_id = r.id LIMIT 1
                        ), ''),
                        CASE
                            WHEN r.status = 'error' THEN 'Execution Failure'
                            WHEN r.status = 'stopped' THEN 'Stopped'
                            ELSE r.status
                        END
                    ) AS error_category,
                    COUNT(*) AS count
                FROM scraper_reports r
                WHERE 1=1 {where_sql}
                  AND r.status IN ('error', 'stopped')
                GROUP BY error_category
                ORDER BY count DESC
                """,
                params,
            )
            common = {row["error_category"]: int(row["count"]) for row in cur.fetchall()}

        analytics["common_errors"] = common

        # 4. Failed scripts
        cur.execute(
            f"""
            SELECT COALESCE(NULLIF(r.script_name, ''), 'Unknown') AS script_name,
                   COUNT(*) AS count
            FROM scraper_reports r
            WHERE 1=1 {where_sql} AND r.status = 'error'
            GROUP BY COALESCE(NULLIF(r.script_name, ''), 'Unknown')
            ORDER BY count DESC
            """,
            params,
        )
        analytics["script_errors"] = {
            row["script_name"]: int(row["count"]) for row in cur.fetchall()
        }

        # 5. Failed / missing files — normalized first, then JSON fallback from error_details
        cur.execute(
            f"""
            SELECT f.file_path, f.folder_path, COUNT(*) AS hits
            FROM scraper_report_files f
            JOIN scraper_reports r ON f.report_id = r.id
            WHERE 1=1 {where_sql} AND f.issue_type IN ('failed', 'missing')
            GROUP BY f.file_path, f.folder_path
            ORDER BY hits DESC
            LIMIT 50
            """,
            params,
        )
        failed_files = []
        seen = set()
        for row in cur.fetchall():
            key = (row["file_path"], row["folder_path"])
            if key in seen:
                continue
            seen.add(key)
            failed_files.append({
                "file": row["file_path"],
                "folder": row["folder_path"] or "",
                "hits": int(row["hits"] or 1),
            })

        if not failed_files:
            cur.execute(
                f"""
                SELECT r.error_details, r.folder_path
                FROM scraper_reports r
                WHERE 1=1 {where_sql}
                  AND r.error_details IS NOT NULL AND r.error_details != ''
                ORDER BY r.start_time DESC
                LIMIT 40
                """,
                params,
            )
            for row in cur.fetchall():
                try:
                    details = json.loads(row["error_details"]) if isinstance(row["error_details"], str) else row["error_details"]
                except Exception:
                    continue
                if not isinstance(details, dict):
                    continue
                folder = row["folder_path"] or details.get("folder") or ""
                for key_name in ("failed_files", "missing_files"):
                    for f in (details.get(key_name) or []):
                        name = f if isinstance(f, str) else (f.get("path") or f.get("file") or str(f))
                        if not name:
                            continue
                        key = (name, folder)
                        if key in seen:
                            continue
                        seen.add(key)
                        failed_files.append({"file": name, "folder": folder, "hits": 1})
                        if len(failed_files) >= 50:
                            break
                    if len(failed_files) >= 50:
                        break
                if len(failed_files) >= 50:
                    break

        analytics["failed_files_list"] = failed_files
        return analytics


def get_report_summary_cards(worker_name: Optional[str], script_id: Optional[int], 
                             status: Optional[str], date_from: str, date_to: str, user_id: Optional[int] = None,
                             folder_path: Optional[str] = None,
                             search: str = "",
                             has_errors: Optional[bool] = None,
                             processed_lt: Optional[int] = None) -> dict:
    query = """
        SELECT 
            SUM(image_count) as total_images,
            SUM(pdf_count) as total_pdfs,
            SUM(file_count) as total_files,
            SUM(warning_count) as total_warnings,
            SUM(error_count) as total_errors,
            SUM(COALESCE(file_count, 0)) as total_downloads,
            SUM(COALESCE(failed_downloads, 0)) as total_failed_downloads,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as total_success_runs,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as total_error_runs
        FROM scraper_reports WHERE 1=1
    """
    params = []
    
    if user_id is not None and not is_admin(user_id):
        accessible_workers = [w["worker_name"] for w in list_accessible_workers(user_id)]
        if not accessible_workers:
            return {
                "total_images": 0, "total_pdfs": 0, "total_files": 0, 
                "total_warnings": 0, "total_errors": 0, "total_downloads": 0,
                "total_failed_downloads": 0,
                "total_success_runs": 0, "total_error_runs": 0
            }
        placeholders = ",".join("?" * len(accessible_workers))
        query += f" AND worker_name IN ({placeholders})"
        params.extend(accessible_workers)

    if worker_name:
        query += " AND worker_name = ?"
        params.append(worker_name)
    if script_id:
        query += " AND script_id = ?"
        params.append(script_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if date_from:
        query += " AND start_time >= ?"
        params.append(date_from)
    if date_to:
        query += " AND start_time <= ?"
        params.append(date_to + "T23:59:59")
    if search:
        query += " AND (script_name LIKE ? OR folder_path LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if has_errors is True:
        query += " AND COALESCE(error_count, 0) > 0"
    elif has_errors is False:
        query += " AND COALESCE(error_count, 0) = 0"
    if processed_lt is not None and processed_lt > 0:
        query += " AND (COALESCE(image_count, 0) + COALESCE(pdf_count, 0)) < ?"
        params.append(int(processed_lt))
    if folder_path:
        wroot = None
        if worker_name:
            wrec = get_worker(worker_name)
            wroot = (wrec or {}).get("script_location") or None
        query = _append_report_folder_filter(
            query, params, folder_path,
            folder_col="folder_path", script_id_col="script_id",
            worker_name=worker_name, worker_root=wroot,
        )

    with db_cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row or row["total_images"] is None:
            return {
                "total_images": 0, "total_pdfs": 0, "total_files": 0, 
                "total_warnings": 0, "total_errors": 0, "total_downloads": 0,
                "total_failed_downloads": 0,
                "total_success_runs": 0, "total_error_runs": 0
            }
        return dict(row)

def get_report_folders(user_id: Optional[int] = None) -> list[str]:
    query = "SELECT DISTINCT folder_path FROM scraper_reports WHERE folder_path IS NOT NULL AND folder_path != ''"
    params = []
    
    if user_id is not None and not is_admin(user_id):
        accessible_workers = [w["worker_name"] for w in list_accessible_workers(user_id)]
        if not accessible_workers:
            return []
        placeholders = ",".join("?" * len(accessible_workers))
        query += f" AND worker_name IN ({placeholders})"
        params.extend(accessible_workers)
        
    query += " ORDER BY folder_path ASC"
    
    with db_cursor() as cur:
        cur.execute(query, tuple(params))
        raw = [r["folder_path"] for r in cur.fetchall()]
    return consolidate_report_folder_filter_options(raw)


_DOWNLOAD_DIR_NAMES = frozenset({"download", "downloads", "downloaded_images"})
_DATE_FOLDER_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{2}-\d{2}-\d{4})$")
_LEAF_NOISE_SEGMENTS = frozenset({
    "image", "images", "img", "page", "pages", "pdf", "pdfs",
})


def _path_segments(path: str) -> list[str]:
    return [p for p in (path or "").replace("\\", "/").split("/") if p]


def _is_date_segment(name: str) -> bool:
    return bool(name and _DATE_FOLDER_RE.match(name))


def normalize_report_folder_path(folder_path: Optional[str]) -> str:
    """Collapse ``.../download/<date>/<page>/<image>`` → ``.../download/<date>``.

    Supports ``download``, ``downloads``, and ``downloaded_images``.
    Comma-separated multi-folder values are normalized per entry.
    """
    raw = (folder_path or "").strip()
    if not raw:
        return ""
    parts_out: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        segs = _path_segments(chunk)
        if not segs:
            continue
        lower = [s.lower() for s in segs]
        dl_idx = next((i for i, s in enumerate(lower) if s in _DOWNLOAD_DIR_NAMES), -1)
        if dl_idx >= 0:
            if dl_idx + 1 < len(segs) and _is_date_segment(segs[dl_idx + 1]):
                segs = segs[: dl_idx + 2]
            else:
                segs = segs[: dl_idx + 1]
        else:
            while len(segs) > 1:
                last = segs[-1].lower()
                if last in _LEAF_NOISE_SEGMENTS or last.isdigit():
                    segs = segs[:-1]
                    continue
                break
        # Preserve absolute Windows / POSIX roots
        if segs and len(segs[0]) >= 2 and segs[0][1] == ":":
            drive = segs[0]
            rest = "/".join(segs[1:])
            joined = f"{drive}/{rest}" if rest else drive
        elif chunk.startswith("/"):
            joined = "/" + "/".join(segs)
        else:
            joined = "/".join(segs)
        if joined and joined not in parts_out:
            parts_out.append(joined)
    return ", ".join(parts_out)


def _collapse_date_folder_to_download_parent(path: str) -> str:
    """For filter dropdown: ``.../download/<date>`` → ``.../download``."""
    segs = _path_segments(path)
    if len(segs) < 2:
        return path
    lower = [s.lower() for s in segs]
    if lower[-1] and _is_date_segment(segs[-1]) and lower[-2] in _DOWNLOAD_DIR_NAMES:
        segs = segs[:-1]
        if path[1:3] in (":\\", ":/") or (len(segs) and len(segs[0]) >= 2 and segs[0][1] == ":"):
            drive = segs[0]
            rest = "/".join(segs[1:])
            return f"{drive}/{rest}" if rest else drive
        if path.startswith("/"):
            return "/" + "/".join(segs)
        return "/".join(segs)
    return path


def _remove_redundant_path_prefixes(paths: list[str]) -> list[str]:
    """Drop paths that are strict children of another path in the list."""
    ordered = sorted(
        {(p or "").replace("\\", "/").strip().rstrip("/") for p in paths if (p or "").strip()},
        key=str.lower,
    )
    result: list[str] = []
    for p in ordered:
        pl = p.lower()
        if any(pl != k.lower() and pl.startswith(k.lower() + "/") for k in result):
            continue
        result = [k for k in result if not (k.lower() != pl and k.lower().startswith(pl + "/"))]
        result.append(p)
    return sorted(result, key=str.lower)


def consolidate_report_folder_filter_options(raw_paths: list[str]) -> list[str]:
    """Unique, meaningful folder filter options for Reports.

    - Normalize page/image leaves to ``download/<date>``
    - Group date folders under their common ``download`` parent
    - Drop redundant child paths covered by a parent option
    Filtering still uses prefix matching, so parent options remain accurate.
    """
    date_level: list[str] = []
    for raw in raw_paths or []:
        for part in str(raw).split(","):
            n = normalize_report_folder_path(part.strip())
            if n:
                date_level.append(n)
    # Prefer common download parent over every date child
    parents = [_collapse_date_folder_to_download_parent(p) for p in date_level]
    return _remove_redundant_path_prefixes(parents)


def _normalize_watch_path(path: Optional[str]) -> str:
    return (path or "").replace("\\", "/").strip().strip("/").lower()


_WATCHLIST_NOISE_SEGMENTS = frozenset({
    ".svn", "pristine", "__pycache__", "venv", ".venv", ".git",
    "node_modules", ".idea", ".vscode",
})


def _is_watchlist_noise_path(path: str) -> bool:
    parts = (path or "").replace("\\", "/").split("/")
    return any(p.lower() in _WATCHLIST_NOISE_SEGMENTS for p in parts if p)


def _path_is_under_watched(candidate: str, watched: str, worker_root: Optional[str] = None) -> bool:
    """True if candidate path is the watched path or a descendant (relative or absolute)."""
    c = _normalize_watch_path(candidate)
    w = _normalize_watch_path(watched)
    if not w or not c:
        return False

    # Relativize absolute paths against worker script root when possible
    if worker_root:
        root = _normalize_watch_path(worker_root)
        if root and (c == root or c.startswith(root + "/")):
            c = "" if c == root else c[len(root) + 1 :]

    if c == w or c.startswith(w + "/"):
        return True

    # Absolute / comma-joined leftovers: require path-segment boundaries
    raw = _normalize_watch_path(candidate)
    return raw == w or raw.endswith("/" + w) or f"/{w}/" in f"/{raw}/"


def _report_belongs_to_watch(
    *,
    folder_path: str,
    script_path: str,
    script_name: str,
    watched_path: str,
    worker_root: Optional[str] = None,
) -> bool:
    watched = _normalize_watch_path(watched_path)
    if not watched:
        return False
    for part in (folder_path or "").split(","):
        if _path_is_under_watched(part.strip(), watched, worker_root):
            return True
    if _path_is_under_watched(script_path or "", watched, worker_root):
        return True
    if _path_is_under_watched(script_name or "", watched, worker_root):
        return True
    return False


def toggle_file_watchlist(user_id: int, file_path: str, worker_name: str = "") -> bool:
    """Toggle watchlist entry. Returns True if added, False if removed.

    Identity is (user_id, worker_name, file_path). Paths are stored relative with ``/``.
    """
    now = _utc_now()
    path = (file_path or "").replace("\\", "/").strip().strip("/")
    worker = (worker_name or "").strip()
    if not path:
        return False
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id FROM file_watchlist
            WHERE user_id = ? AND worker_name = ? AND file_path = ?
            """,
            (user_id, worker, path),
        )
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM file_watchlist WHERE id = ?", (row["id"],))
            return False
        # Legacy rows without worker: remove same path with empty worker when adding with worker
        if worker:
            cur.execute(
                """
                DELETE FROM file_watchlist
                WHERE user_id = ? AND file_path = ? AND (worker_name = '' OR worker_name IS NULL)
                """,
                (user_id, path),
            )
        cur.execute(
            """
            INSERT INTO file_watchlist (user_id, worker_name, file_path, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, worker, path, now),
        )
        return True


def get_user_watchlist(user_id: int) -> list[str]:
    """Paths only (used by Analytics star UI). Prefer get_user_watchlist_entries for full rows."""
    with db_cursor() as cur:
        cur.execute("SELECT file_path FROM file_watchlist WHERE user_id = ?", (user_id,))
        return [row["file_path"] for row in cur.fetchall()]


def get_user_watchlist_entries(user_id: int) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, worker_name, file_path, created_at
            FROM file_watchlist
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def get_enriched_watchlist(
    user_id: int,
    worker_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """Watchlist rows enriched with latest matching scraper report signal."""
    import os

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, worker_name, file_path, created_at
            FROM file_watchlist
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        items = [dict(row) for row in cur.fetchall()]
        if not items:
            return []

        accessible = None
        if user_id is not None and not is_admin(user_id):
            accessible = [w["worker_name"] for w in list_accessible_workers(user_id)]
            if not accessible:
                for item in items:
                    item["status"] = "healthy"
                    item["last_failed"] = None
                    item["last_status"] = None
                    item["last_run"] = None
                    item["script_name"] = None
                    item["match_count"] = 0
                    item["type"] = (
                        "folder"
                        if not os.path.splitext(item["file_path"] or "")[1]
                        else "file"
                    )
                return items

        # Worker roots for relativizing absolute report paths
        cur.execute("SELECT worker_name, script_location FROM workers")
        roots = {
            r["worker_name"]: (r["script_location"] or "")
            for r in cur.fetchall()
        }

        report_where = ["1=1"]
        report_params: list[Any] = []
        if worker_name:
            report_where.append("r.worker_name = ?")
            report_params.append(worker_name)
        if accessible is not None:
            placeholders = ",".join("?" * len(accessible))
            report_where.append(f"r.worker_name IN ({placeholders})")
            report_params.extend(accessible)
        if date_from:
            report_where.append("r.start_time >= ?")
            report_params.append(date_from if " " in date_from else date_from + " 00:00:00")
        if date_to:
            report_where.append("r.start_time <= ?")
            report_params.append(date_to if " " in date_to else date_to + " 23:59:59")
        where_sql = " AND ".join(report_where)

        cur.execute(
            f"""
            SELECT r.id, r.folder_path, r.start_time, r.status, r.script_name, r.worker_name,
                   r.error_count, r.failed_downloads, r.script_id,
                   COALESCE(s.script_path, '') AS script_path
            FROM scraper_reports r
            LEFT JOIN scripts s ON s.id = r.script_id
            WHERE {where_sql}
            ORDER BY r.start_time DESC
            LIMIT 2000
            """,
            report_params,
        )
        reports = [dict(row) for row in cur.fetchall()]

        # Optional page-level worker filter on which watchlist rows to show
        if worker_name:
            items = [
                i for i in items
                if not i.get("worker_name") or i.get("worker_name") == worker_name
            ]

        for item in items:
            path = (item.get("file_path") or "").replace("\\", "/").strip().strip("/")
            item_worker = (item.get("worker_name") or "").strip()
            root = roots.get(item_worker) if item_worker else None
            item["type"] = "folder" if not os.path.splitext(path)[1] else "file"
            item["file_path"] = path

            latest = None
            match_count = 0
            for row in reports:
                if item_worker and row.get("worker_name") != item_worker:
                    continue
                # If watchlist row has no worker, allow any worker match
                row_root = roots.get(row.get("worker_name") or "") or root
                if not _report_belongs_to_watch(
                    folder_path=row.get("folder_path") or "",
                    script_path=row.get("script_path") or "",
                    script_name=row.get("script_name") or "",
                    watched_path=path,
                    worker_root=row_root,
                ):
                    continue
                match_count += 1
                if latest is None:
                    is_bad = (
                        (row.get("status") == "error")
                        or int(row.get("error_count") or 0) > 0
                        or int(row.get("failed_downloads") or 0) > 0
                    )
                    latest = {
                        "status": "failing" if is_bad else "healthy",
                        "last_failed": row["start_time"] if is_bad else None,
                        "last_run": row["start_time"],
                        "last_status": row.get("status"),
                        "script_name": row.get("script_name"),
                        "worker_name": row.get("worker_name") or item_worker,
                    }

            item["match_count"] = match_count
            if latest:
                # Keep stored worker_name; fill from match if missing
                stored_worker = item_worker
                item.update(latest)
                if stored_worker:
                    item["worker_name"] = stored_worker
            else:
                item["status"] = "healthy"
                item["last_failed"] = None
                item["last_run"] = None
                item["last_status"] = None
                item["script_name"] = None
                if not item.get("worker_name"):
                    item["worker_name"] = ""

        items.sort(
            key=lambda x: (
                0 if x.get("status") == "failing" else 1,
                (x.get("worker_name") or ""),
                (x.get("file_path") or ""),
            )
        )
        return items


def migrate_old_scraper_reports():
    """Migrates existing error_details JSON strings into the new normalized tables."""
    import json
    with db_cursor() as cur:
        # Check if we already migrated by seeing if there are any rows in scraper_report_errors
        cur.execute("SELECT COUNT(*) FROM scraper_report_errors")
        if cur.fetchone()[0] > 0:
            return
            
        cur.execute("SELECT id, error_details, folder_path FROM scraper_reports WHERE error_details IS NOT NULL AND error_details != ''")
        rows = cur.fetchall()
        
        for row in rows:
            report_id, ed_str, folder_path = row
            try:
                ed = json.loads(ed_str) if isinstance(ed_str, str) else ed_str
                
                # Migrate Errors
                if isinstance(ed.get("errors"), list):
                    for e in ed["errors"]:
                        cur.execute('''
                            INSERT INTO scraper_report_errors 
                            (report_id, error_category, error_message, source_file, line_number, traceback)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            report_id, 
                            e.get("error_type", "Unknown"), 
                            e.get("error_message", ""), 
                            e.get("source_file", ""), 
                            e.get("line_number", ""), 
                            e.get("traceback", "")
                        ))
                elif ed.get("error_type"):
                    cur.execute('''
                        INSERT INTO scraper_report_errors 
                        (report_id, error_category, error_message, traceback)
                        VALUES (?, ?, ?, ?)
                    ''', (
                        report_id, 
                        ed.get("error_type"), 
                        ed.get("error_message", ""), 
                        ed.get("traceback", "")
                    ))
                    
                # Migrate Failed Files
                if isinstance(ed.get("failed_files"), list):
                    for f in ed["failed_files"]:
                        cur.execute('''
                            INSERT INTO scraper_report_files (report_id, file_path, folder_path, issue_type)
                            VALUES (?, ?, ?, 'failed')
                        ''', (report_id, f, folder_path))
                        
                # Migrate Missing Files
                if isinstance(ed.get("missing_files"), list):
                    for f in ed["missing_files"]:
                        cur.execute('''
                            INSERT INTO scraper_report_files (report_id, file_path, folder_path, issue_type)
                            VALUES (?, ?, ?, 'missing')
                        ''', (report_id, f, folder_path))
                        
            except Exception:
                pass


def migrate_reports_page_backfill() -> None:
    """One-shot backfill formerly run on every GET /reports (same updates, once at startup)."""
    import re
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT r.id, j.output
                FROM scraper_reports r
                JOIN jobs j ON r.job_id = j.id
                WHERE r.failed_downloads = 0
                """
            )
            rows = cur.fetchall()
            for row in rows:
                out = row["output"]
                if out:
                    failed = len(
                        re.findall(
                            r"(?i)(error downloading|download_pdf failed|error in download_pdf|failed to download)",
                            out,
                        )
                    )
                    if failed > 0:
                        cur.execute(
                            "UPDATE scraper_reports SET failed_downloads = ? WHERE id = ?",
                            (failed, row["id"]),
                        )

            cur.execute(
                "SELECT id, error_message, traceback FROM scraper_report_errors WHERE error_category = 'Unknown Error'"
            )
            err_rows = cur.fetchall()
            for erow in err_rows:
                text = (str(erow["error_message"]) + " " + str(erow["traceback"])).lower()
                if any(
                    k in text
                    for k in (
                        "indexerror",
                        "attributeerror",
                        "typeerror",
                        "valueerror",
                        "keyerror",
                        "nameerror",
                        "syntaxerror",
                        "indentationerror",
                    )
                ):
                    cur.execute(
                        "UPDATE scraper_report_errors SET error_category = 'Python Script Error' WHERE id = ?",
                        (erow["id"],),
                    )
    except Exception:
        pass


# --- File Explorer Massive Scale Methods ---

_file_tree_stats_cache: dict[str, tuple[int, int, int]] = {}


def invalidate_worker_file_tree_stats(worker_name: str) -> None:
    _file_tree_stats_cache.pop(worker_name, None)


def clear_worker_file_tree(worker_name: str) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM worker_file_tree WHERE worker_name = ?", (worker_name,))
    invalidate_worker_file_tree_stats(worker_name)

def bulk_insert_worker_file_tree(worker_name: str, files: list) -> None:
    """
    Inserts a chunk of files. Expected format:
    [{'name': 'f', 'type': 'file', 'path': 'dir/f', 'size': 123, 'mtime': 123.4}, ...]
    """
    if not files:
        return
        
    import posixpath
    values = []
    for f in files:
        path = (f.get('path') or '').replace('\\', '/').strip('/')
        if not path:
            continue
        parent_path = posixpath.dirname(path)
        values.append((
            worker_name,
            path,
            parent_path,
            f.get('name') or posixpath.basename(path),
            f.get('type', 'file'),
            f.get('size', 0) if f.get('type') == 'file' else None,
            f.get('mtime', 0.0)
        ))
    
    if not values:
        return
        
    with db_cursor() as cur:
        cur.executemany("""
            INSERT INTO worker_file_tree (worker_name, path, parent_path, name, type, size, mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_name, path) DO UPDATE SET
                parent_path=excluded.parent_path,
                name=excluded.name,
                type=excluded.type,
                size=excluded.size,
                mtime=excluded.mtime
        """, values)
    invalidate_worker_file_tree_stats(worker_name)

def delete_worker_file_tree_paths(worker_name: str, exact_paths: list, prefixes: list) -> None:
    with db_cursor() as cur:
        if exact_paths:
            chunk_size = 900
            for i in range(0, len(exact_paths), chunk_size):
                chunk = exact_paths[i:i+chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                cur.execute(f"DELETE FROM worker_file_tree WHERE worker_name = ? AND path IN ({placeholders})", [worker_name] + chunk)
                
        for prefix in prefixes:
            cur.execute("DELETE FROM worker_file_tree WHERE worker_name = ? AND path LIKE ?", (worker_name, prefix + '%'))
    invalidate_worker_file_tree_stats(worker_name)


def upsert_worker_file_tree_entry(
    worker_name: str,
    path: str,
    entry_type: str = "file",
    size: int | None = None,
    mtime: float | None = None,
    name: str | None = None,
) -> None:
    """Optimistic single-row upsert for dashboard mutations (create/rename)."""
    import posixpath
    import time as _time
    path = (path or "").replace("\\", "/").strip("/")
    if not path:
        return
    parent_path = posixpath.dirname(path)
    name = name or posixpath.basename(path)
    if mtime is None:
        mtime = _time.time()
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO worker_file_tree (worker_name, path, parent_path, name, type, size, mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_name, path) DO UPDATE SET
                parent_path=excluded.parent_path,
                name=excluded.name,
                type=excluded.type,
                size=excluded.size,
                mtime=excluded.mtime
            """,
            (worker_name, path, parent_path, name, entry_type, size if entry_type == "file" else None, mtime),
        )
    invalidate_worker_file_tree_stats(worker_name)


def rename_worker_file_tree_entry(worker_name: str, old_path: str, new_path: str, is_folder: bool = False) -> None:
    """Optimistic path rename in DB (file or folder subtree)."""
    import posixpath
    old_path = (old_path or "").replace("\\", "/").strip("/")
    new_path = (new_path or "").replace("\\", "/").strip("/")
    if not old_path or not new_path or old_path == new_path:
        return
    new_name = posixpath.basename(new_path)
    new_parent = posixpath.dirname(new_path)
    with db_cursor() as cur:
        if is_folder:
            # Update the folder row itself
            cur.execute(
                """
                UPDATE worker_file_tree
                SET path = ?, parent_path = ?, name = ?
                WHERE worker_name = ? AND path = ?
                """,
                (new_path, new_parent, new_name, worker_name, old_path),
            )
            # Rewrite descendants: old_path/x -> new_path/x
            prefix = old_path + "/"
            cur.execute(
                "SELECT path FROM worker_file_tree WHERE worker_name = ? AND path LIKE ?",
                (worker_name, prefix + "%"),
            )
            rows = cur.fetchall()
            for row in rows:
                child_old = row["path"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
                child_new = new_path + child_old[len(old_path):]
                child_parent = posixpath.dirname(child_new)
                child_name = posixpath.basename(child_new)
                cur.execute(
                    """
                    UPDATE worker_file_tree
                    SET path = ?, parent_path = ?, name = ?
                    WHERE worker_name = ? AND path = ?
                    """,
                    (child_new, child_parent, child_name, worker_name, child_old),
                )
        else:
            cur.execute(
                """
                UPDATE worker_file_tree
                SET path = ?, parent_path = ?, name = ?
                WHERE worker_name = ? AND path = ?
                """,
                (new_path, new_parent, new_name, worker_name, old_path),
            )
    invalidate_worker_file_tree_stats(worker_name)

def list_all_worker_folders(worker_name: str, *, include_noise: bool = False) -> list[dict]:
    """All folders for a worker (full tree), not only the root level."""
    if not worker_name:
        return []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT path, parent_path, name, type, size, mtime
            FROM worker_file_tree
            WHERE worker_name = ? AND type = 'folder'
            ORDER BY path COLLATE NOCASE
            """,
            (worker_name,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    out = []
    for row in rows:
        path = (row.get("path") or "").replace("\\", "/")
        row["path"] = path
        if row.get("parent_path"):
            row["parent_path"] = str(row["parent_path"]).replace("\\", "/")
        if not include_noise and _is_watchlist_noise_path(path):
            continue
        out.append(row)
    return out


def _append_report_folder_filter(
    where_sql: str,
    params: list,
    folder_path: Optional[str],
    *,
    folder_col: str = "folder_path",
    script_id_col: str = "script_id",
    worker_name: Optional[str] = None,
    worker_root: Optional[str] = None,
) -> str:
    """Scope reports to a watched folder (relative or absolute).

    Matches path segments (not bare substrings) and scripts living under the
    folder. When worker_root is known, also matches absolute output dirs under
    ``{root}/{folder}`` — critical because most reports store abs paths or empty
    folder_path with the script under the watched tree.
    """
    fp = (folder_path or "").replace("\\", "/").strip().strip("/")
    if not fp:
        return where_sql
    needle = fp.lower()
    col = f"replace(lower(COALESCE({folder_col}, '')), '\\', '/')"
    sp = "replace(lower(COALESCE(_sf.script_path, '')), '\\', '/')"
    sn = "replace(lower(COALESCE(_sf.script_name, '')), '\\', '/')"

    folder_clauses = [
        f"{col} = ?",
        f"{col} LIKE ?",
        f"{col} LIKE ?",
        f"{col} LIKE ?",
    ]
    folder_params = [
        needle,
        f"{needle}/%",
        f"%/{needle}",
        f"%/{needle}/%",
    ]

    root = _normalize_watch_path(worker_root or "")
    if root:
        abs_base = f"{root}/{needle}"
        folder_clauses.extend([f"{col} = ?", f"{col} LIKE ?", f"{col} LIKE ?"])
        folder_params.extend([abs_base, f"{abs_base}/%", f"%{abs_base}%"])

    script_clauses = [
        f"{sp} LIKE ?",
        f"{sp} LIKE ?",
        f"{sp} LIKE ?",
        f"{sn} LIKE ?",
        f"{sn} LIKE ?",
    ]
    script_params = [
        f"%/{needle}/%",
        f"%/{needle}",
        f"{needle}/%",
        f"{needle}/%",
        f"%/{needle}/%",
    ]
    if root:
        script_clauses.append(f"{sp} LIKE ?")
        script_params.append(f"{root}/{needle}/%")

    worker_sql = ""
    worker_params: list = []
    if worker_name:
        worker_sql = " AND _sf.worker_name = ?"
        worker_params = [worker_name]

    where_sql += f""" AND (
        {' OR '.join(folder_clauses)}
        OR EXISTS (
            SELECT 1 FROM scripts _sf
            WHERE _sf.id = {script_id_col}
              {worker_sql}
              AND ({' OR '.join(script_clauses)})
        )
    )"""
    params.extend(folder_params + worker_params + script_params)
    return where_sql


def get_worker_file_tree_folder(worker_name: str, parent_path: str = None, search: str = "", file_type: str = "") -> list:
    """List immediate children of parent_path (or search results).

    When ``file_type`` is a file extension (e.g. ``py``), include:
      - matching files in the current folder, and
      - folders in the current folder that contain at least one matching file
        anywhere in their subtree (so hierarchy stays navigable).
    """
    conn = get_connection()
    cur = conn.cursor()

    ft = (file_type or "").strip().lstrip(".").lower()

    # Type filter without search: keep folder hierarchy for matching files
    if ft and ft != "folder" and not search and parent_path is not None:
        like_ext = f"%.{ft}"
        cur.execute(
            """
            SELECT path, parent_path, name, type, size, mtime
            FROM worker_file_tree
            WHERE worker_name = ?
              AND parent_path = ?
              AND type = 'file'
              AND LOWER(name) LIKE ?
            ORDER BY name COLLATE NOCASE
            """,
            (worker_name, parent_path, like_ext),
        )
        files = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """
            SELECT path, parent_path, name, type, size, mtime
            FROM worker_file_tree f
            WHERE f.worker_name = ?
              AND f.parent_path = ?
              AND f.type = 'folder'
              AND EXISTS (
                  SELECT 1
                  FROM worker_file_tree c
                  WHERE c.worker_name = f.worker_name
                    AND c.type = 'file'
                    AND LOWER(c.name) LIKE ?
                    AND c.path LIKE (f.path || '/%')
              )
            ORDER BY name COLLATE NOCASE
            """,
            (worker_name, parent_path, like_ext),
        )
        folders = [dict(row) for row in cur.fetchall()]
        cur.close()
        return folders + files

    query = "SELECT path, parent_path, name, type, size, mtime FROM worker_file_tree WHERE worker_name = ?"
    params: list = [worker_name]

    if search:
        query += " AND (name ILIKE ? OR replace(path, '\\', '/') ILIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
        if parent_path:
            query += " AND (path = ? OR path LIKE ?)"
            params.extend([parent_path, f"{parent_path}/%"])
    else:
        if parent_path is not None:
            query += " AND parent_path = ?"
            params.append(parent_path)

    if ft:
        if ft == "folder":
            query += " AND type = 'folder'"
        else:
            query += " AND type = 'file' AND LOWER(name) LIKE ?"
            params.append(f"%.{ft}")

    query += " ORDER BY CASE WHEN type = 'folder' THEN 0 ELSE 1 END, name COLLATE NOCASE"
    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    return [dict(row) for row in rows]

def get_worker_file_tree_by_paths(worker_name: str, paths: list) -> list:
    if not paths:
        return []
    conn = get_connection()
    cur = conn.cursor()
    results = []
    chunk_size = 900
    for i in range(0, len(paths), chunk_size):
        chunk = paths[i:i + chunk_size]
        placeholders = ",".join(["?"] * len(chunk))
        cur.execute(
            f"SELECT path, parent_path, name, type, size, mtime FROM worker_file_tree WHERE worker_name = ? AND path IN ({placeholders})",
            [worker_name] + chunk,
        )
        results.extend([dict(row) for row in cur.fetchall()])
    cur.close()
    return results

def _ensure_worker_tree_sync() -> None:
    """Tracks full-tree scan progress after a configuration path change."""
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_tree_sync (
                worker_name  TEXT PRIMARY KEY,
                status       TEXT NOT NULL,
                started_at   TEXT,
                finished_at  TEXT,
                elapsed_s    DOUBLE PRECISION,
                item_count   BIGINT DEFAULT 0,
                next_batch   INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'worker_tree_sync'
              AND column_name = 'next_batch'
            """
        )
        if not cur.fetchone():
            cur.execute(
                "ALTER TABLE worker_tree_sync ADD COLUMN next_batch INTEGER DEFAULT 0"
            )


def mark_worker_tree_sync_started(worker_name: str, *, reset: bool = True) -> dict[str, Any]:
    """Mark a full tree load as in progress. reset=False keeps an existing syncing clock."""
    now = _utc_now()
    existing = get_worker_tree_sync_state(worker_name)
    if not reset and existing.get("status") == "syncing" and existing.get("started_at"):
        return existing
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO worker_tree_sync
                (worker_name, status, started_at, finished_at, elapsed_s, item_count, next_batch)
            VALUES (?, 'syncing', ?, NULL, NULL, 0, 0)
            ON CONFLICT (worker_name) DO UPDATE SET
                status = 'syncing',
                started_at = excluded.started_at,
                finished_at = NULL,
                elapsed_s = NULL,
                item_count = 0,
                next_batch = 0
            """,
            (worker_name, now),
        )
    return get_worker_tree_sync_state(worker_name)


def mark_worker_tree_sync_uploading(worker_name: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE worker_tree_sync
            SET status = 'uploading'
            WHERE worker_name = ? AND status IN ('syncing', 'uploading')
            """,
            (worker_name,),
        )


def bump_worker_tree_sync_batch(worker_name: str, batch_index: int) -> bool:
    """Accept this batch only if it is the next expected index for the current load."""
    state = get_worker_tree_sync_state(worker_name)
    expected = int(state.get("next_batch") or 0)
    if int(batch_index or 0) != expected:
        return False
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE worker_tree_sync
            SET next_batch = ?
            WHERE worker_name = ?
            """,
            (expected + 1, worker_name),
        )
    return True


def mark_worker_tree_sync_progress(worker_name: str, item_count: int) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE worker_tree_sync
            SET item_count = ?
            WHERE worker_name = ? AND status IN ('syncing', 'uploading')
            """,
            (int(item_count or 0), worker_name),
        )


def mark_worker_tree_sync_finished(
    worker_name: str,
    item_count: int,
    elapsed_s: Optional[float] = None,
) -> dict[str, Any]:
    now = _utc_now()
    state = get_worker_tree_sync_state(worker_name)
    if elapsed_s is None and state.get("started_at"):
        started = _parse_utc_ts(state["started_at"])
        finished = _parse_utc_ts(now)
        if started and finished:
            elapsed_s = max(0.0, (finished - started).total_seconds())
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO worker_tree_sync (worker_name, status, started_at, finished_at, elapsed_s, item_count, next_batch)
            VALUES (?, 'complete', ?, ?, ?, ?, ?)
            ON CONFLICT (worker_name) DO UPDATE SET
                status = 'complete',
                finished_at = excluded.finished_at,
                elapsed_s = excluded.elapsed_s,
                item_count = excluded.item_count
            """,
            (
                worker_name,
                state.get("started_at") or now,
                now,
                elapsed_s,
                int(item_count or 0),
                int(state.get("next_batch") or 0),
            ),
        )
    return get_worker_tree_sync_state(worker_name)


def get_worker_tree_sync_state(worker_name: str) -> dict[str, Any]:
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                SELECT worker_name, status, started_at, finished_at, elapsed_s, item_count, next_batch
                FROM worker_tree_sync
                WHERE worker_name = ?
                """,
                (worker_name,),
            )
            row = cur.fetchone()
        if not row:
            return {"status": "idle", "started_at": None, "finished_at": None, "elapsed_s": None, "item_count": 0, "next_batch": 0}
        data = row_to_dict(row)
        return {
            "status": data.get("status") or "idle",
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "elapsed_s": float(data["elapsed_s"]) if data.get("elapsed_s") is not None else None,
            "item_count": int(data.get("item_count") or 0),
            "next_batch": int(data.get("next_batch") or 0),
        }
    except Exception:
        return {"status": "idle", "started_at": None, "finished_at": None, "elapsed_s": None, "item_count": 0, "next_batch": 0}


def get_worker_file_tree_stats(worker_name: str) -> tuple[int, int, int]:
    """Cached (file_count, total_size, entry_count) for a worker."""
    cached = _file_tree_stats_cache.get(worker_name)
    if cached is not None:
        return cached
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type = 'file' THEN 1 ELSE 0 END), 0) AS file_count,
                COALESCE(SUM(CASE WHEN type = 'file' THEN size ELSE 0 END), 0) AS total_size,
                COUNT(*) AS entry_count
            FROM worker_file_tree
            WHERE worker_name = ?
            """,
            (worker_name,),
        )
        row = cur.fetchone()
        files = int(row["file_count"] or 0)
        size = int(row["total_size"] or 0)
        entries = int(row["entry_count"] or 0)
    finally:
        cur.close()
    _file_tree_stats_cache[worker_name] = (files, size, entries)
    return files, size, entries


def sync_folder_partial_db(worker_name: str, folder_path: str, contents: list) -> None:
    """
    Replaces all direct children of folder_path for a worker with the given contents.
    Also removes subtrees of any folders that no longer exist.
    """
    sync_folders_partial_batch_db(worker_name, [{"folder_path": folder_path, "contents": contents}])


def sync_folders_partial_batch_db(worker_name: str, folders: list) -> None:
    """
    Apply multiple folder partial syncs in a single DB transaction.
    Each item: {"folder_path": str, "contents": list}
    Same semantics as sync_folder_partial_db per folder.
    """
    import posixpath
    if not folders:
        return
    conn = get_connection()
    cur = conn.cursor()

    try:
        for folder_item in folders:
            folder_path = (folder_item.get("folder_path") or "").replace("\\", "/").strip("/")
            contents = folder_item.get("contents") or []

            cur.execute(
                "SELECT path, type FROM worker_file_tree WHERE worker_name = ? AND parent_path = ?",
                (worker_name, folder_path),
            )
            existing = {row["path"]: row["type"] for row in cur.fetchall()}

            normalized_contents = []
            new_paths = set()
            for item in contents:
                path = (item.get("path") or "").replace("\\", "/").strip("/")
                if not path:
                    continue
                normalized = dict(item)
                normalized["path"] = path
                normalized["name"] = item.get("name") or posixpath.basename(path)
                normalized_contents.append(normalized)
                new_paths.add(path)

            deleted_folders = [p for p, t in existing.items() if t == "folder" and p not in new_paths]
            deleted_files = [p for p in existing if p not in new_paths and existing[p] == "file"]

            if deleted_files:
                chunk_size = 900
                for i in range(0, len(deleted_files), chunk_size):
                    chunk = deleted_files[i : i + chunk_size]
                    placeholders = ",".join(["?"] * len(chunk))
                    cur.execute(
                        f"DELETE FROM worker_file_tree WHERE worker_name = ? AND path IN ({placeholders})",
                        [worker_name] + chunk,
                    )

            for fp in deleted_folders:
                cur.execute(
                    "DELETE FROM worker_file_tree WHERE worker_name = ? AND path = ?",
                    (worker_name, fp),
                )
                cur.execute(
                    "DELETE FROM worker_file_tree WHERE worker_name = ? AND path LIKE ?",
                    (worker_name, fp + "/%"),
                )

            if normalized_contents:
                values = []
                for f in normalized_contents:
                    path = f["path"]
                    parent = posixpath.dirname(path)
                    values.append(
                        (
                            worker_name,
                            path,
                            parent,
                            f.get("name", ""),
                            f.get("type", ""),
                            f.get("size", 0) if f.get("type") == "file" else None,
                            f.get("mtime", 0.0),
                        )
                    )
                cur.executemany(
                    """
                    INSERT INTO worker_file_tree (worker_name, path, parent_path, name, type, size, mtime)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_name, path) DO UPDATE SET
                        parent_path=excluded.parent_path,
                        name=excluded.name,
                        type=excluded.type,
                        size=excluded.size,
                        mtime=excluded.mtime
                    """,
                    values,
                )

        conn.commit()
        invalidate_worker_file_tree_stats(worker_name)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

