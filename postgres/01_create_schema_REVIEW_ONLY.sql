-- =============================================================================
-- DFMS / AutoControl — PostgreSQL DDL (schema only, NO data / NO INSERT)
-- Target DB: sitewisedata | Schema: public | Prefix: tbl_dfms_
--
-- Project mapping (logical name in code → physical table):
--   See app/db_compat.py. App entry: python run.py
--
-- How to run in pgAdmin Query Tool:
--   1) Connect to Host 192.168.50.18, database sitewisedata
--   2) Open Query Tool on that database
--   3) Optional pre-check:
--        SELECT tablename FROM pg_tables
--        WHERE schemaname = 'public' AND tablename LIKE 'tbl_dfms_%'
--        ORDER BY 1;
--   4) Paste this whole script and Execute (F5)
--   5) Need CREATE privilege on schema public
--
-- Safe to re-run:
--   - CREATE TABLE / INDEX use IF NOT EXISTS (will not drop data)
--   - Section 8 applies missing columns on older DBs
-- Wrapped in a transaction: all succeed or all roll back
--
-- Expected tables (24) — must match live app + app/db_compat.py:
--   users, workers, scripts, schedules, jobs, commands,
--   user_pc_access, user_pc_access_periods, user_script_access,
--   schedule_access, schedule_folders, schedule_folder_items,
--   schedule_folder_runs, schedule_folder_access, scheduler_view_access,
--   history_log, worker_file_tree, worker_tree_sync, file_history,
--   user_starred_files, file_watchlist,
--   scraper_reports, scraper_report_errors, scraper_report_files
--
-- Do NOT create (obsolete — app drops if present):
--   tbl_dfms_file_versions, tbl_dfms_job_checkpoints,
--   tbl_dfms_schedule_folder_members, tbl_dfms_user_ui_prefs
-- Do NOT add users.chat_username / users.chat_user_id (removed)
-- =============================================================================

BEGIN;

SET LOCAL search_path TO public;

-- -----------------------------------------------------------------------------
-- 1) Core identity / workers
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tbl_dfms_users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user',
    nickname        TEXT,
    registered_ip   TEXT,
    last_login_ip   TEXT,
    can_set_days    INTEGER NOT NULL DEFAULT 0,
    is_disabled     INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_tbl_dfms_users_username UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS tbl_dfms_workers (
    id               BIGSERIAL PRIMARY KEY,
    worker_name      TEXT NOT NULL,
    ip_address       TEXT,
    status           TEXT NOT NULL DEFAULT 'offline',
    state            TEXT NOT NULL DEFAULT 'idle',
    script_location  TEXT DEFAULT '',
    env_details      TEXT DEFAULT '{}',
    last_seen        TEXT,
    owner_id         BIGINT,
    CONSTRAINT uq_tbl_dfms_workers_worker_name UNIQUE (worker_name),
    CONSTRAINT uq_tbl_dfms_workers_ip_address UNIQUE (ip_address),
    CONSTRAINT fk_tbl_dfms_workers_owner
        FOREIGN KEY (owner_id) REFERENCES tbl_dfms_users(id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 2) Scripts
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tbl_dfms_scripts (
    id           BIGSERIAL PRIMARY KEY,
    worker_name  TEXT NOT NULL,
    script_name  TEXT NOT NULL,
    script_path  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    owner_id     BIGINT,
    days         INTEGER DEFAULT 0,
    file_type    TEXT DEFAULT 'unknown',
    category     TEXT DEFAULT '',
    tags         TEXT DEFAULT '',
    is_starred   INTEGER DEFAULT 0,
    CONSTRAINT uq_tbl_dfms_scripts_worker_name UNIQUE (worker_name, script_name),
    CONSTRAINT fk_tbl_dfms_scripts_worker
        FOREIGN KEY (worker_name) REFERENCES tbl_dfms_workers(worker_name) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_scripts_owner
        FOREIGN KEY (owner_id) REFERENCES tbl_dfms_users(id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 3) Schedules (must exist before jobs.schedule_id FK)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tbl_dfms_schedules (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL,
    script_id        BIGINT NOT NULL,
    worker_name      TEXT NOT NULL,
    run_time         TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_run         TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_log         TEXT,
    days             INTEGER,
    is_deleted       INTEGER DEFAULT 0,
    schedule_type    TEXT DEFAULT 'daily',
    priority         TEXT DEFAULT 'normal',
    retry_policy     TEXT DEFAULT 'never',
    trigger_type     TEXT DEFAULT 'time',
    dependencies     TEXT DEFAULT '[]',
    tags             TEXT DEFAULT '',
    category         TEXT DEFAULT '',
    is_pinned        INTEGER DEFAULT 0,
    schedule_config  TEXT,
    tracking_status  TEXT,
    CONSTRAINT fk_tbl_dfms_schedules_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_schedules_script
        FOREIGN KEY (script_id) REFERENCES tbl_dfms_scripts(id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 4) Jobs
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tbl_dfms_jobs (
    id               BIGSERIAL PRIMARY KEY,
    worker_name      TEXT NOT NULL,
    script_id        BIGINT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    output           TEXT DEFAULT '',
    start_time       TEXT,
    end_time         TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    duration         DOUBLE PRECISION,
    total_images     INTEGER,
    output_count     INTEGER,
    pid              INTEGER,
    exit_code        INTEGER,
    schedule_id      BIGINT,
    is_scheduled     INTEGER DEFAULT 0,
    paused_at        TEXT,
    priority         TEXT DEFAULT 'normal',
    execution_state  TEXT DEFAULT 'queued',
    folder_run_id    BIGINT,
    CONSTRAINT fk_tbl_dfms_jobs_script
        FOREIGN KEY (script_id) REFERENCES tbl_dfms_scripts(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_jobs_schedule
        FOREIGN KEY (schedule_id) REFERENCES tbl_dfms_schedules(id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 5) Commands / access / history / file tree
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tbl_dfms_commands (
    id           BIGSERIAL PRIMARY KEY,
    worker_name  TEXT NOT NULL,
    command      TEXT NOT NULL,
    payload      TEXT DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    output       TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    response     TEXT DEFAULT '',
    CONSTRAINT fk_tbl_dfms_commands_worker
        FOREIGN KEY (worker_name) REFERENCES tbl_dfms_workers(worker_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_user_pc_access (
    id                    BIGSERIAL PRIMARY KEY,
    user_id               BIGINT NOT NULL,
    worker_name           TEXT NOT NULL,
    granted_by            BIGINT,
    granted_at            TEXT NOT NULL,
    allowed_paths         TEXT DEFAULT '',
    allowed_extensions    TEXT DEFAULT '',
    can_create_folder     INTEGER DEFAULT 0,
    can_rename_folder     INTEGER DEFAULT 0,
    can_update_file       INTEGER DEFAULT 0,
    can_create_file       INTEGER DEFAULT 0,
    can_delete_file       INTEGER DEFAULT 0,
    can_delete_folder     INTEGER DEFAULT 0,
    can_rename_file       INTEGER DEFAULT 0,
    can_edit_file         INTEGER DEFAULT 0,
    can_access_all_files  INTEGER DEFAULT 0,
    can_run               INTEGER DEFAULT 0,
    CONSTRAINT uq_tbl_dfms_user_pc_access UNIQUE (user_id, worker_name),
    CONSTRAINT fk_tbl_dfms_user_pc_access_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_user_pc_access_worker
        FOREIGN KEY (worker_name) REFERENCES tbl_dfms_workers(worker_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_user_script_access (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    script_id    BIGINT NOT NULL,
    can_run      INTEGER NOT NULL DEFAULT 1,
    can_update   INTEGER NOT NULL DEFAULT 0,
    can_delete   INTEGER NOT NULL DEFAULT 0,
    granted_by   BIGINT,
    granted_at   TEXT NOT NULL,
    CONSTRAINT uq_tbl_dfms_user_script_access UNIQUE (user_id, script_id),
    CONSTRAINT fk_tbl_dfms_user_script_access_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_user_script_access_script
        FOREIGN KEY (script_id) REFERENCES tbl_dfms_scripts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_schedule_access (
    id             BIGSERIAL PRIMARY KEY,
    schedule_id    BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    granted_by     BIGINT,
    granted_at     TEXT NOT NULL,
    can_delete     INTEGER DEFAULT 0,
    can_enable     INTEGER DEFAULT 0,
    can_disable    INTEGER DEFAULT 0,
    can_run        INTEGER DEFAULT 0,
    can_duplicate  INTEGER DEFAULT 0,
    can_edit       INTEGER DEFAULT 0,
    CONSTRAINT uq_tbl_dfms_schedule_access UNIQUE (schedule_id, user_id),
    CONSTRAINT fk_tbl_dfms_schedule_access_schedule
        FOREIGN KEY (schedule_id) REFERENCES tbl_dfms_schedules(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_schedule_access_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_history_log (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT,
    action       TEXT NOT NULL,
    details      TEXT NOT NULL,
    ip_address   TEXT,
    created_at   TEXT NOT NULL,
    worker_name  TEXT,
    CONSTRAINT fk_tbl_dfms_history_log_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tbl_dfms_user_pc_access_periods (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    worker_name  TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    CONSTRAINT fk_tbl_dfms_user_pc_access_periods_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pc_access_periods_user_worker
    ON tbl_dfms_user_pc_access_periods (user_id, worker_name);

CREATE TABLE IF NOT EXISTS tbl_dfms_schedule_folders (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'idle',
    run_time         TEXT DEFAULT '00:00',
    schedule_type    TEXT DEFAULT 'daily',
    schedule_config  TEXT DEFAULT '{}',
    last_run_at      TEXT,
    current_run_id   BIGINT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    tracking_status  TEXT,
    CONSTRAINT fk_tbl_dfms_schedule_folders_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_schedule_folder_items (
    id           BIGSERIAL PRIMARY KEY,
    folder_id    BIGINT NOT NULL,
    schedule_id  BIGINT NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT uq_tbl_dfms_schedule_folder_items UNIQUE (folder_id, schedule_id),
    CONSTRAINT fk_tbl_dfms_folder_items_folder
        FOREIGN KEY (folder_id) REFERENCES tbl_dfms_schedule_folders(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_folder_items_schedule
        FOREIGN KEY (schedule_id) REFERENCES tbl_dfms_schedules(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_folder_items_folder_order
    ON tbl_dfms_schedule_folder_items (folder_id, sort_order);

CREATE TABLE IF NOT EXISTS tbl_dfms_schedule_folder_runs (
    id                  BIGSERIAL PRIMARY KEY,
    folder_id           BIGINT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running',
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    duration_seconds    DOUBLE PRECISION,
    total_count         INTEGER NOT NULL DEFAULT 0,
    successful_count    INTEGER NOT NULL DEFAULT 0,
    failed_count        INTEGER NOT NULL DEFAULT 0,
    skipped_count       INTEGER NOT NULL DEFAULT 0,
    current_index       INTEGER NOT NULL DEFAULT 0,
    current_job_id      BIGINT,
    current_schedule_id BIGINT,
    triggered_by        BIGINT,
    CONSTRAINT fk_tbl_dfms_folder_runs_folder
        FOREIGN KEY (folder_id) REFERENCES tbl_dfms_schedule_folders(id) ON DELETE CASCADE
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_tbl_dfms_jobs_folder_run'
    ) THEN
        ALTER TABLE tbl_dfms_jobs
            ADD CONSTRAINT fk_tbl_dfms_jobs_folder_run
            FOREIGN KEY (folder_run_id) REFERENCES tbl_dfms_schedule_folder_runs(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS tbl_dfms_schedule_folder_access (
    id                  BIGSERIAL PRIMARY KEY,
    folder_id           BIGINT NOT NULL,
    user_id             BIGINT NOT NULL,
    can_edit            INTEGER DEFAULT 0,
    can_delete          INTEGER DEFAULT 0,
    can_enable          INTEGER DEFAULT 0,
    can_disable         INTEGER DEFAULT 0,
    can_run             INTEGER DEFAULT 0,
    can_manage_members  INTEGER DEFAULT 0,
    can_manage          INTEGER DEFAULT 0,
    granted_by          BIGINT,
    granted_at          TEXT NOT NULL,
    CONSTRAINT uq_tbl_dfms_schedule_folder_access UNIQUE (folder_id, user_id),
    CONSTRAINT fk_tbl_dfms_folder_access_folder
        FOREIGN KEY (folder_id) REFERENCES tbl_dfms_schedule_folders(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_folder_access_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_scheduler_view_access (
    id              BIGSERIAL PRIMARY KEY,
    viewer_user_id  BIGINT NOT NULL,
    target_user_id  BIGINT NOT NULL,
    granted_by      BIGINT,
    granted_at      TEXT NOT NULL,
    CONSTRAINT uq_tbl_dfms_scheduler_view_access UNIQUE (viewer_user_id, target_user_id),
    CONSTRAINT fk_tbl_dfms_scheduler_view_viewer
        FOREIGN KEY (viewer_user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_scheduler_view_target
        FOREIGN KEY (target_user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_worker_file_tree (
    id           BIGSERIAL PRIMARY KEY,
    worker_name  TEXT NOT NULL,
    path         TEXT NOT NULL,
    parent_path  TEXT NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    size         BIGINT,
    mtime        DOUBLE PRECISION,
    CONSTRAINT uq_tbl_dfms_worker_file_tree_path UNIQUE (worker_name, path),
    CONSTRAINT fk_tbl_dfms_worker_file_tree_worker
        FOREIGN KEY (worker_name) REFERENCES tbl_dfms_workers(worker_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_worker_tree_sync (
    worker_name  TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    elapsed_s    DOUBLE PRECISION,
    item_count   BIGINT DEFAULT 0,
    next_batch   INTEGER DEFAULT 0,
    CONSTRAINT fk_tbl_dfms_worker_tree_sync_worker
        FOREIGN KEY (worker_name) REFERENCES tbl_dfms_workers(worker_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_file_history (
    id           BIGSERIAL PRIMARY KEY,
    file_path    TEXT NOT NULL,
    worker_name  TEXT NOT NULL,
    user_id      BIGINT,
    action       TEXT NOT NULL,
    old_content  TEXT,
    new_content  TEXT,
    created_at   TEXT NOT NULL,
    CONSTRAINT fk_tbl_dfms_file_history_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tbl_dfms_user_starred_files (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    worker_name  TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    CONSTRAINT uq_tbl_dfms_user_starred_files UNIQUE (user_id, worker_name, file_path),
    CONSTRAINT fk_tbl_dfms_user_starred_files_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_file_watchlist (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    worker_name  TEXT NOT NULL DEFAULT '',
    file_path    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    CONSTRAINT uq_tbl_dfms_file_watchlist UNIQUE (user_id, worker_name, file_path),
    CONSTRAINT fk_tbl_dfms_file_watchlist_user
        FOREIGN KEY (user_id) REFERENCES tbl_dfms_users(id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 6) Reports (no download_path — removed from product)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tbl_dfms_scraper_reports (
    id                  BIGSERIAL PRIMARY KEY,
    worker_name         TEXT NOT NULL,
    script_name         TEXT NOT NULL,
    script_id           BIGINT NOT NULL,
    job_id              BIGINT NOT NULL,
    folder_path         TEXT NOT NULL,
    status              TEXT NOT NULL,
    start_time          TEXT NOT NULL,
    end_time            TEXT NOT NULL,
    duration            DOUBLE PRECISION,
    image_count         INTEGER DEFAULT 0,
    pdf_count           INTEGER DEFAULT 0,
    file_count          INTEGER DEFAULT 0,
    log_count           INTEGER DEFAULT 0,
    warning_count       INTEGER DEFAULT 0,
    error_count         INTEGER DEFAULT 0,
    total_folder_size   BIGINT DEFAULT 0,
    error_details       TEXT,
    failed_downloads    INTEGER DEFAULT 0,
    CONSTRAINT fk_tbl_dfms_scraper_reports_script
        FOREIGN KEY (script_id) REFERENCES tbl_dfms_scripts(id) ON DELETE CASCADE,
    CONSTRAINT fk_tbl_dfms_scraper_reports_job
        FOREIGN KEY (job_id) REFERENCES tbl_dfms_jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_scraper_report_errors (
    id               BIGSERIAL PRIMARY KEY,
    report_id        BIGINT NOT NULL,
    error_category   TEXT,
    error_message    TEXT,
    source_file      TEXT,
    line_number      TEXT,
    traceback        TEXT,
    CONSTRAINT fk_tbl_dfms_scraper_report_errors_report
        FOREIGN KEY (report_id) REFERENCES tbl_dfms_scraper_reports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tbl_dfms_scraper_report_files (
    id            BIGSERIAL PRIMARY KEY,
    report_id     BIGINT NOT NULL,
    file_path     TEXT NOT NULL,
    folder_path   TEXT NOT NULL,
    issue_type    TEXT NOT NULL,
    CONSTRAINT fk_tbl_dfms_scraper_report_files_report
        FOREIGN KEY (report_id) REFERENCES tbl_dfms_scraper_reports(id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 7) Indexes
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_commands_worker_status_created
    ON tbl_dfms_commands (worker_name, status, created_at);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_jobs_worker_status
    ON tbl_dfms_jobs (worker_name, status);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_jobs_worker_status_created
    ON tbl_dfms_jobs (worker_name, status, created_at);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_jobs_script_id
    ON tbl_dfms_jobs (script_id, id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_jobs_schedule_id
    ON tbl_dfms_jobs (schedule_id, id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_jobs_status_updated
    ON tbl_dfms_jobs (status, updated_at);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_jobs_status_paused
    ON tbl_dfms_jobs (status, paused_at);

CREATE INDEX IF NOT EXISTS idx_jobs_folder_run_id
    ON tbl_dfms_jobs (folder_run_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scripts_worker
    ON tbl_dfms_scripts (worker_name);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_schedules_enabled_deleted
    ON tbl_dfms_schedules (enabled, is_deleted);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_schedules_script_enabled
    ON tbl_dfms_schedules (script_id, enabled, id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_schedules_user_deleted
    ON tbl_dfms_schedules (user_id, is_deleted);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_schedule_access_user
    ON tbl_dfms_schedule_access (user_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_reports_worker_start
    ON tbl_dfms_scraper_reports (worker_name, start_time);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_reports_script_start
    ON tbl_dfms_scraper_reports (script_id, start_time);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_reports_job_id
    ON tbl_dfms_scraper_reports (job_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_reports_folder
    ON tbl_dfms_scraper_reports (folder_path);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_reports_status_start
    ON tbl_dfms_scraper_reports (status, start_time);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_report_errors_report
    ON tbl_dfms_scraper_report_errors (report_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_report_errors_category
    ON tbl_dfms_scraper_report_errors (error_category);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_report_files_report
    ON tbl_dfms_scraper_report_files (report_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_report_files_path_issue
    ON tbl_dfms_scraper_report_files (file_path, issue_type);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_scraper_report_files_folder_issue
    ON tbl_dfms_scraper_report_files (folder_path, issue_type);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_history_log_created
    ON tbl_dfms_history_log (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_history_log_user_created
    ON tbl_dfms_history_log (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_history_log_worker_created
    ON tbl_dfms_history_log (worker_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_file_history_created
    ON tbl_dfms_file_history (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_file_history_user_created
    ON tbl_dfms_file_history (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_file_history_worker_created
    ON tbl_dfms_file_history (worker_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_workers_owner
    ON tbl_dfms_workers (owner_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_workers_status_seen
    ON tbl_dfms_workers (status, last_seen);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_user_script_access_script
    ON tbl_dfms_user_script_access (script_id);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_user_pc_access_worker
    ON tbl_dfms_user_pc_access (worker_name);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_users_registered_ip
    ON tbl_dfms_users (registered_ip);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_worker_file_tree_parent
    ON tbl_dfms_worker_file_tree (worker_name, parent_path);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_worker_file_tree_type
    ON tbl_dfms_worker_file_tree (worker_name, type);

CREATE INDEX IF NOT EXISTS idx_tbl_dfms_file_watchlist_user
    ON tbl_dfms_file_watchlist (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_scheduler_view_access_viewer
    ON tbl_dfms_scheduler_view_access (viewer_user_id);

CREATE INDEX IF NOT EXISTS idx_scheduler_view_access_target
    ON tbl_dfms_scheduler_view_access (target_user_id);

CREATE INDEX IF NOT EXISTS idx_schedule_folder_access_user
    ON tbl_dfms_schedule_folder_access (user_id);

CREATE INDEX IF NOT EXISTS idx_schedule_folder_runs_folder_started
    ON tbl_dfms_schedule_folder_runs (folder_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_schedule_folder_runs_folder_status
    ON tbl_dfms_schedule_folder_runs (folder_id, status);

-- -----------------------------------------------------------------------------
-- 8) Safe upgrades for older DBs (CREATE IF NOT EXISTS cannot add columns)
--     Matches app/database.py _ensure_* helpers.
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_users' AND column_name = 'can_set_days'
    ) THEN
        ALTER TABLE tbl_dfms_users ADD COLUMN can_set_days INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_users' AND column_name = 'is_disabled'
    ) THEN
        ALTER TABLE tbl_dfms_users ADD COLUMN is_disabled INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_schedule_access' AND column_name = 'can_edit'
    ) THEN
        ALTER TABLE tbl_dfms_schedule_access ADD COLUMN can_edit INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_schedules' AND column_name = 'tracking_status'
    ) THEN
        ALTER TABLE tbl_dfms_schedules ADD COLUMN tracking_status TEXT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_schedule_folders' AND column_name = 'tracking_status'
    ) THEN
        ALTER TABLE tbl_dfms_schedule_folders ADD COLUMN tracking_status TEXT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_jobs' AND column_name = 'folder_run_id'
    ) THEN
        ALTER TABLE tbl_dfms_jobs ADD COLUMN folder_run_id BIGINT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_worker_tree_sync' AND column_name = 'next_batch'
    ) THEN
        ALTER TABLE tbl_dfms_worker_tree_sync ADD COLUMN next_batch INTEGER DEFAULT 0;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tbl_dfms_scraper_reports' AND column_name = 'failed_downloads'
    ) THEN
        ALTER TABLE tbl_dfms_scraper_reports ADD COLUMN failed_downloads INTEGER DEFAULT 0;
    END IF;
END $$;

-- Obsolete leftovers (same cleanup intent as app init_schema)
DROP TABLE IF EXISTS tbl_dfms_file_versions;
DROP TABLE IF EXISTS tbl_dfms_job_checkpoints;
DROP TABLE IF EXISTS tbl_dfms_schedule_folder_members;
DROP TABLE IF EXISTS tbl_dfms_user_ui_prefs;
ALTER TABLE tbl_dfms_users DROP COLUMN IF EXISTS chat_user_id;
ALTER TABLE tbl_dfms_users DROP COLUMN IF EXISTS chat_username;
ALTER TABLE tbl_dfms_scraper_reports DROP COLUMN IF EXISTS download_path;

COMMIT;

-- Verify after run (optional):
-- SELECT tablename FROM pg_tables
-- WHERE schemaname = 'public' AND tablename LIKE 'tbl_dfms_%'
-- ORDER BY tablename;
-- Expected: 24 tables
--
-- App readiness: python scripts/init_db.py
-- Smoke:        python postgres/smoke_test.py
