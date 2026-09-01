# Permissions UI & Scheduler Delete Fix
Date: 2026-06-24 10:55 UTC

## Summary
- Fixed an issue where the Permissions UI did not correctly show checkboxes as checked when a user was selected. The Javascript `=== 1` strict equality check was failing because SQLite integer 1 could be treated differently during parsing, replaced with loose equality `== 1`.
- Fixed the issue in `scheduler.html` where dynamically generated rows for newly loaded schedules were missing the `data-can-delete` attribute. This caused the bulk action bar to incorrectly show or hide the delete button because the attribute was evaluated as `null`. Added `data-can-delete="${sch.can_delete ? '1' : '0'}"` to the Javascript `tr` generation string.
- Audited all File Explorer actions (`upload`, `create_folder`, `rename`, `delete`) and verified that the backend in `api_routes.py` correctly enforces permissions via `database.get_pc_access_details()`. A user cannot bypass access controls through direct requests.
