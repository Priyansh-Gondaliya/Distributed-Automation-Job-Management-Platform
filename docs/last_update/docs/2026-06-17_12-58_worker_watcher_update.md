# Worker Watcher Update
**Date:** 2026-06-17 12:58:00 IST

## Changes Implemented

### Event-Driven File Watcher
- Replaced the continuous, expensive 60-second recursive polling loops (`rglob` and `os.walk`) in `worker.py` with an event-driven `watchdog` implementation.
- The new `ScriptFolderWatcher` class listens for `on_created`, `on_deleted`, `on_modified`, and `on_moved` events within the `SCRIPTS_DIR`.
- To prevent spamming the controller during bulk file operations, events are debounced using a 2-second background thread lock before syncing.

### Partial Folder Sync API
- Instead of wiping and overwriting the entire `tree.json` on the controller for every file change, `worker.py` now sends "partial updates".
- Added `POST /api/sync-folder-partial` to the controller (`api_routes.py`), which surgically removes old entries for a specific folder and appends the new state of that folder.
- Only the specific folder that was modified is scanned via a shallow `os.scandir()`, massively reducing CPU and network overhead.

### Documentation & Dependencies
- Added `watchdog>=4.0.0` to `requirements.txt`.
- Updated `worker_flow.md` under `last_update/docs` to reflect the removal of the continuous polling loop and the implementation of the new debounced event-driven architecture.
