# Worker Details Page — QA E2E Report

**Date:** 2026-07-31 17:00 IST  
**Environment:** `http://192.168.50.89:7561`  
**Worker under test:** `Priyansh` @ `192.168.50.89`  
**Auth:** admin session  
**Test prefix:** `__qa_e2e_20260731_165851` — **fully cleaned** (DB tree / commands / file_history = 0 leftovers; search finds none)

---

## Executive summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 3 |
| Medium | 6 |
| Low | 3 |
| Info / Feature gap | 2 |
| Pass | 35+ |

Core File Explorer flows (list, create folder, upload, update, delete, star, refresh, search, types, stats, editor deep-link, same-path config) work for an admin. Several permission, consistency, and UX gaps remain. Live browser click-through was not available in this environment; testing used authenticated HTTP against the live controller plus static/code review of `worker_detail.html` / `file_explorer.js`.

---

## Findings (bugs / risks)

### High

1. **Rename worker is not admin-gated on the server**  
   - **Where:** `routes/web_routes.py` → `rename_worker`  
   - **Issue:** UI shows Rename only for admins, but the POST route only checks `check_pc_access`. Any user with PC access can rename the worker.  
   - **Impact:** Privilege escalation / accidental rename by non-admins.

2. **Legacy `/manage-files` upload payload mismatch**  
   - **Where:** `web_routes.manage_files` sets `file_content`; worker `write_file` expects `file_content_b64`.  
   - **Issue:** That legacy path cannot deliver file bytes correctly.  
   - **Note:** Modern explorer uses `/files/upload` (correct `file_content_b64`) — that path passed.

3. **Upload / update have no optimistic DB tree update**  
   - **Where:** `/files/upload`, `/files/update` only queue worker commands.  
   - **Issue:** Create/delete/rename folders/files update `worker_file_tree` immediately; uploads do not. Explorer can stay empty for new files until the worker writes + partial-syncs (observed: after upload+rename, folder listing still only showed nested folder `child_a`).  
   - **Impact:** Users see “success” but file missing for seconds–minutes if worker is busy/offline — inconsistent with delete/create UX.

### Medium

4. **Extra `</section>` in Worker Details HTML**  
   - **Where:** `templates/worker_detail.html` lines 174–175 (`</section>` twice).  
   - **Impact:** Invalid HTML; can confuse DOM parsers / live refresh.

5. **Incomplete escaping in File Explorer rendering**  
   - **Where:** `static/js/file_explorer.js` `escapeJsStr` + HTML string templates.  
   - **Issue:** Escapes `\` and `'` only; names/paths with `"`, `<`, `>` are injected into HTML/attributes.  
   - **Impact:** Broken UI or XSS if a hostile filename exists on the worker.

6. **`#job-count` missing**  
   - **Where:** `liveRefreshWorkerDetail` updates `#job-count`, but Job History badge has no that id.  
   - **Impact:** Live job-count refresh is a no-op.

7. **script_id lag after upload**  
   - Uploaded `qa_runme.py` appeared in the tree (after worker sync) **without** `script_id`, so Run from explorer cannot work until `sync_scripts` runs.  
   - **Impact:** Run / Schedule icons absent until next script registry sync.

8. **Worker command backlog during test**  
   - Commands were still pending after ~30s wait (worker under sync load). File ops are async; UI does not show queue/processing state per action.

9. **Rename optimistic vs worker race**  
   - `rename_file` API returned ok and DB rename ran, but listing still lagged when the source row was never optimistically inserted (because upload never wrote the row). Optimistic rename of a non-existent row is effectively a no-op.

### Low

10. **Aggressive polling (1.5s)** — `fetchFiles` interval adds constant `/files/list` traffic (seen alongside worker sync in server logs).  
11. **Load Time metric** is client fetch duration, not server scan time — easy to misread in FS Stats card.  
12. **Pagination CSS** sets `display: flex` then `display: none` on `#jobs-pagination` — works, but redundant/confusing.

### Info / feature gaps

13. **No Download** in File Explorer context menu or toolbar.  
14. **No Move / Cut / Paste** — only rename within the same parent (new name). Cross-folder move is unsupported.

---

## Passed checks (verified live)

| Area | Result |
|---|---|
| Admin login | Pass |
| `/worker/<ip>` page load | Pass (~100KB HTML) |
| Admin Management section (rename/config/stats) | Pass (admin) |
| File Explorer mount | Pass |
| Root `/files/list` | Pass — 37 root items in ~330ms |
| `include_stats` totals | Pass — ~122,396 files / ~17.4 GB |
| Permissions object | Pass (all can_* true for admin) |
| Unauthenticated `/files/list` | Pass — rejected |
| Search (no hits / with hits) | Pass |
| `/files/types` | Pass — 169 extensions |
| Create folder (root + nested) | Pass — immediate list update |
| Upload `.txt` / `.py` | Pass (API accept) |
| Update file | Pass (API accept) |
| Delete file | Pass — optimistic removal |
| Rename folder/file APIs | Pass (accepted) |
| Star + starred filter | Pass |
| `/files/refresh` | Pass — queues `resync_folder` |
| Same-path config → `changed: false` | Pass |
| Run UI gated to `.py/.pyw/.bat/.cmd` | Pass (code) |
| Editor `?worker_name&file_path` | Pass |
| Job History table / log toggle / pagination shell | Pass |
| Empty folder name rejected | Pass |
| Delete without `file_path` rejected | Pass |
| `..\\evil` folder did not appear in listing | Pass |
| Chevron/expand (list children by `base_path`) | Pass — e.g. `1 Main Domestic File` → 7 children |

---

## Not fully exercised (limitations)

| Item | Why |
|---|---|
| Real browser clicks (context menu, chevron animation, toast) | No browser automation MCP in session; APIs + static UI review used |
| Non-admin permission matrix (deny each can_*) | Would require creating a restricted user; not done to avoid touching user/ACL data |
| Actual script execution window on worker PC | `script_id` missing during window; Run API not fully confirmed end-to-end |
| Download / Move | Features absent |
| Drag-drop upload | Not in UI |
| History page `/history` full UI | Only Job History on Worker Details covered |
| Worker offline behavior | Worker was online but backlogged |

---

## Performance notes

- Root listing with stats: **~330ms** (acceptable).  
- Explorer polls every **1.5s**; combined with worker partial sync this can still produce busy access logs (improved by recent batching, but FE polling remains chatty).  
- Large tree (~122k files) — folder navigation is O(children), which is good; search can be heavier.

---

## Cleanup performed

Only QA artifacts were removed:

1. `POST /files/delete_folder` for `__qa_e2e_20260731_165851` (queues worker delete).  
2. DB deletes for matching `worker_file_tree`, `file_history`, and `commands` rows with that prefix.  
3. Verification: leftover tree/commands/history = **0**; search `__qa_e2e_` = **empty**.  

No other project data, workers, scripts, or user records were modified. Temporary harness scripts used for this run were removed after reporting.

---

## Recommended fixes (priority)

1. Gate `rename_worker` with `is_admin` (or equivalent).  
2. Add optimistic `upsert_worker_file_tree_entry` on upload/update (size/mtime from uploaded file).  
3. Fix `/manage-files` to use `file_content_b64` or remove dead route.  
4. Escape HTML entities for file/folder names in explorer render.  
5. Remove extra `</section>`; add `id="job-count"` or drop dead JS.  
6. After `write_file`, trigger or await scripts sync so `script_id` appears for Run.  
7. Optional: per-action “queued / applied” status instead of silent async.
