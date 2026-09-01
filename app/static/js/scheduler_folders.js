/**
 * Scheduler Folders — thin controller
 * List / detail / sequential run. Row actions use shared Schedules
 * quickAction / bulkAction (+ folder_id). Selection uses global selectedRows only.
 */
(function () {
    'use strict';

    const state = {
        folders: [],
        currentFolderId: null,
        page: 1,
        limit: 25,
        total: 0,
        lastItems: [],
        folderScriptIds: new Set(),
        itemsLoadToken: 0,
        pollTimer: null,
        searchTimer: null,
        addExistingCache: [],
        addExistingSelected: new Set(),
        movePendingIds: [],
        moveTargetId: null,
    };

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function toast(msg, type) {
        if (typeof showToast === 'function') showToast(msg, type || 'success');
        else alert(msg);
    }

    function withViewUser(url) {
        if (typeof schedulerViewQuery !== 'function') return url;
        const q = schedulerViewQuery();
        if (!q) return url;
        return url + (url.indexOf('?') >= 0 ? '&' : '?') + q;
    }

    async function api(url, opts) {
        const method = ((opts && opts.method) || 'GET').toUpperCase();
        if (method === 'GET') url = withViewUser(url);
        const options = Object.assign({
            headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        }, opts || {});
        if (opts && opts.headers) {
            options.headers = Object.assign({}, options.headers, opts.headers);
        }
        const res = await fetch(url, options);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || data.message || ('HTTP ' + res.status));
        return data;
    }

    function clearGlobalSelection() {
        if (typeof clearRowSelection === 'function') clearRowSelection();
        else if (typeof selectedRows !== 'undefined') {
            selectedRows.clear();
            if (typeof updateBulkBar === 'function') updateBulkBar();
        }
    }

    function selectedScheduleIds() {
        if (typeof selectedRows === 'undefined' || !selectedRows.size) return [];
        return Array.from(selectedRows).map((x) => parseInt(x, 10)).filter(Boolean);
    }

    function isFolderDetailVisible() {
        const tab = document.getElementById('tab-folders');
        const detail = document.getElementById('folders-detail-view');
        if (!tab || !detail) return false;
        if (window.getComputedStyle(tab).display === 'none') return false;
        return detail.style.display !== 'none';
    }

    function currentFolderId() {
        return state.currentFolderId;
    }

    function currentFolder() {
        return state.folders.find((f) => Number(f.id) === Number(state.currentFolderId)) || null;
    }

    function folderFlag(f, flag) {
        return Number(f && f[flag]) === 1;
    }

    function normalizeScriptPath(path) {
        return String(path || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
    }

    function scriptParentDir(path) {
        const p = String(path || '').replace(/\//g, '\\');
        const idx = p.lastIndexOf('\\');
        if (idx <= 0) return p;
        return p.slice(0, idx);
    }

    function matchesSchedulePathFilter(schedule, folderPathFilter, currentFolderOnly) {
        const filterRaw = (folderPathFilter || '').trim();
        if (!filterRaw) return true;
        const normFilter = normalizeScriptPath(filterRaw);
        const scriptPath = normalizeScriptPath(schedule.script_path || '');
        const parentDir = normalizeScriptPath(scriptParentDir(schedule.script_path || ''));
        if (currentFolderOnly) {
            return parentDir === normFilter;
        }
        return scriptPath.startsWith(normFilter + '\\') || scriptPath === normFilter || parentDir === normFilter;
    }

    function getCachedItems() {
        return state.lastItems || [];
    }

    function getFolderScriptIds() {
        return state.folderScriptIds || new Set();
    }

    function syncFolderScriptIdsFromItems(items) {
        if (!Array.isArray(items)) return;
        const next = new Set(state.folderScriptIds || []);
        items.forEach((it) => {
            if (it && it.script_id != null) next.add(String(it.script_id));
        });
        state.folderScriptIds = next;
    }

    async function refreshFolderScriptIds(folderId) {
        const id = folderId || state.currentFolderId;
        if (!id) {
            state.folderScriptIds = new Set();
            return state.folderScriptIds;
        }
        try {
            const data = await api(`/api/schedule-folders/${id}/items?page=1&limit=5000`);
            state.folderScriptIds = new Set(
                (data.items || [])
                    .map((it) => (it && it.script_id != null ? String(it.script_id) : ''))
                    .filter(Boolean)
            );
        } catch (_) {
            // Keep previous cache if refresh fails
        }
        return state.folderScriptIds;
    }

    // ---------- Folder list ----------

    async function loadFolders() {
        try {
            const data = await api('/api/schedule-folders');
            state.folders = data.folders || [];
            renderFolderList();
            return state.folders;
        } catch (e) {
            toast(e.message, 'error');
            const tbody = document.getElementById('foldersTableBody');
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--danger);">Failed to load folders.</td></tr>';
            }
            return [];
        }
    }

    function filterFolders() {
        renderFolderList();
    }

    function folderStatusHtml(f) {
        const s = (f.run_status || f.status || 'idle').toLowerCase();
        if (s === 'running') {
            return '<span class="status-pulse"><span class="status-pulse-dot running"></span>Running</span>';
        }
        if (s === 'completed') return '<span class="badge badge-green">Completed</span>';
        if (s === 'failed' || s === 'error') return '<span class="badge badge-red">Failed</span>';
        if (s === 'stopped') return '<span class="badge badge-amber">Stopped</span>';
        return '<span class="badge badge-slate">Idle</span>';
    }

    function buildFolderRowHtml(f) {
        const enabled = Number(f.enabled) === 1;
        const progress = `${f.progress_done || 0} / ${f.progress_total || f.item_count || 0}`;
        const timeLabel = (window.SchedulerUI && SchedulerUI.formatScheduleTimeLabel)
            ? SchedulerUI.formatScheduleTimeLabel(f)
            : ((f.run_time || '').toString().slice(0, 5) || '—');
        const running = String(f.run_status || f.status || 'idle').toLowerCase() === 'running';
        const canRun = folderFlag(f, 'can_run');
        const canEdit = folderFlag(f, 'can_edit') || folderFlag(f, 'can_enable') || folderFlag(f, 'can_disable');
        const canDelete = folderFlag(f, 'can_delete');
        const ddId = 'folder-list-dd-' + f.id;
        const menu = `
                ${canRun && !running ? `
                    <button type="button" class="dropdown-item" onclick="SchedulerFolders.runFolder(${f.id})">
                        <svg width="14" height="14"><use href="#icon-play" /></svg> Run
                    </button>` : ''}
                ${canRun && running ? `
                    <button type="button" class="dropdown-item danger" onclick="SchedulerFolders.stopFolder(${f.id})">
                        <svg width="14" height="14"><use href="#icon-pause" /></svg> Stop
                    </button>` : ''}
                ${canEdit ? `<button type="button" class="dropdown-item" onclick="SchedulerFolders.editFolder(${f.id})">
                    <svg width="14" height="14"><use href="#icon-edit" /></svg> Edit
                </button>` : ''}
                ${canDelete ? `
                    <div class="dropdown-divider"></div>
                    <button type="button" class="dropdown-item danger" onclick="SchedulerFolders.deleteFolder(${f.id})">
                        <svg width="14" height="14"><use href="#icon-trash" /></svg> Delete
                    </button>` : ''}
            `;
        const parallelOn = Number(f.parallel_enabled) === 1;
        const parallelHint = parallelOn
            ? `<div style="font-size:0.72rem;color:var(--text-tertiary);margin-top:0.15rem;">Multi-run: ${Number(f.max_concurrent) || 2} at once · ${Number(f.script_gap_seconds) || 0}s gap</div>`
            : '';
        return `<tr class="sch-row" data-folder-id="${f.id}" data-running="${running ? '1' : '0'}" style="cursor:pointer" onclick="SchedulerFolders.openFolder(${f.id})">
                <td class="col-script"><div style="font-weight:600;">${esc(f.name)}</div>
                    <div style="font-size:0.75rem;color:var(--text-tertiary);">${esc(f.username || '')}</div>${parallelHint}</td>
                <td>${f.item_count || 0}</td>
                <td class="col-time mono" style="text-align:center;">${esc(timeLabel || '—')}</td>
                <td class="col-status">${folderStatusHtml(f)}</td>
                <td class="mono">${progress}</td>
                <td style="text-align:center;">
                    <span class="badge ${enabled ? 'badge-green' : 'badge-red'}">${enabled ? 'Enabled' : 'Disabled'}</span>
                </td>
                <td onclick="event.stopPropagation()" style="white-space:nowrap;text-align:right;">
                    <div class="dropdown" id="${ddId}">
                        <button type="button" class="btn btn-ghost btn-icon btn-sm" onclick="toggleDropdown('${ddId}', event)" data-tooltip="Actions">
                            <svg width="16" height="16"><use href="#icon-more" /></svg>
                        </button>
                        <div class="dropdown-menu">${menu}</div>
                    </div>
                </td>
            </tr>`;
    }

    function filteredFolders() {
        const q = (document.getElementById('folderSearch')?.value || '').toLowerCase();
        let rows = state.folders;
        if (q) rows = rows.filter((f) => (f.name || '').toLowerCase().includes(q));
        return rows;
    }

    function patchFolderListRows() {
        const tbody = document.getElementById('foldersTableBody');
        if (!tbody) return;
        const rows = filteredFolders();
        if (!rows.length) {
            if (!state.folders.length) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--text-tertiary);">No folders yet. Create one to run scripts sequentially.</td></tr>';
            } else {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--text-tertiary);">No folders match your search.</td></tr>';
            }
            return;
        }
        const placeholder = tbody.querySelector('tr:not(.sch-row)');
        if (placeholder) placeholder.remove();

        const rowMap = new Map();
        Array.from(tbody.querySelectorAll('tr.sch-row')).forEach((tr) => {
            rowMap.set(tr.getAttribute('data-folder-id'), tr);
        });

        rows.forEach((f) => {
            const id = String(f.id);
            const existing = rowMap.get(id);
            const running = String(f.run_status || f.status || 'idle').toLowerCase() === 'running';
            const enabled = Number(f.enabled) === 1;
            const progress = `${f.progress_done || 0} / ${f.progress_total || f.item_count || 0}`;

            if (existing) {
                existing.children[1].textContent = String(f.item_count || 0);
                existing.children[3].innerHTML = folderStatusHtml(f);
                existing.children[4].textContent = progress;
                existing.children[5].innerHTML = `<span class="badge ${enabled ? 'badge-green' : 'badge-red'}">${enabled ? 'Enabled' : 'Disabled'}</span>`;
                const wasRunning = existing.getAttribute('data-running') === '1';
                if (wasRunning !== running) {
                    existing.outerHTML = buildFolderRowHtml(f);
                } else {
                    existing.setAttribute('data-running', running ? '1' : '0');
                }
                rowMap.delete(id);
                return;
            }
            tbody.insertAdjacentHTML('beforeend', buildFolderRowHtml(f));
        });

        rowMap.forEach((tr) => tr.remove());

        const ordered = rows.map((f) => tbody.querySelector(`tr.sch-row[data-folder-id="${f.id}"]`)).filter(Boolean);
        ordered.forEach((tr) => tbody.appendChild(tr));
    }

    async function refreshFolderDetail() {
        if (!state.currentFolderId) return;
        const btn = document.getElementById('btnRefreshFolderDetail');
        if (btn && btn.disabled) return;
        if (btn) {
            btn.disabled = true;
            btn.classList.add('is-refreshing');
        }
        try {
            await reloadItems(true);
            await refreshFolderScriptIds(state.currentFolderId);
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('is-refreshing');
            }
        }
    }

    function renderFolderList() {
        const tbody = document.getElementById('foldersTableBody');
        if (!tbody) return;
        const rows = filteredFolders();
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--text-tertiary);">No folders yet. Create one to run scripts sequentially.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((f) => buildFolderRowHtml(f)).join('');
    }

    // ---------- Folder CRUD modal ----------

    function setFolderWeekdays(days) {
        const wanted = new Set((days || []).map((d) => String(d)));
        document.querySelectorAll('input[name="folder_weekdays"]').forEach((cb) => {
            cb.checked = wanted.size ? wanted.has(cb.value) : true;
        });
    }

    function resetFolderScheduleFields() {
        const freq = document.getElementById('folderFrequencySelect');
        if (freq) freq.value = 'daily';
        const time = document.getElementById('folderFlatpickr');
        if (time) time.value = '12:00';
        const date = document.getElementById('folderFullDateInput');
        if (date) date.value = '';
        const dom = document.getElementById('folderDayOfMonthInput');
        if (dom) dom.value = '1';
        const num = document.getElementById('folderIntervalNumericInput');
        if (num) num.value = '1';
        const unit = document.getElementById('folderIntervalUnitSelect');
        if (unit) unit.value = 'm';
        const useWin = document.getElementById('folderIntervalUseWindow');
        if (useWin) useWin.checked = false;
        const winStart = document.getElementById('folderIntervalWindowStart');
        if (winStart) winStart.value = '09:00';
        const winEnd = document.getElementById('folderIntervalWindowEnd');
        if (winEnd) winEnd.value = '18:00';
        setFolderWeekdays(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']);
        if (typeof toggleFolderIntervalInput === 'function') toggleFolderIntervalInput();
    }

    function fillFolderScheduleFields(f) {
        resetFolderScheduleFields();
        let cfg = {};
        try {
            if (f.schedule_config) {
                cfg = typeof f.schedule_config === 'string' ? JSON.parse(f.schedule_config) : (f.schedule_config || {});
            }
        } catch (e) { cfg = {}; }
        let schType = f.schedule_type || 'daily';
        if (schType === 'manual') schType = 'daily';
        let runTimeVal = (f.run_time || '12:00').toString();
        if (schType === 'once') {
            document.getElementById('folderFrequencySelect').value = 'once';
            const parts = runTimeVal.split(' ');
            if (parts.length === 2) {
                document.getElementById('folderFullDateInput').value = parts[0];
                runTimeVal = parts[1];
            }
        } else if (schType === 'monthly') {
            document.getElementById('folderFrequencySelect').value = 'monthly';
            const parts = runTimeVal.split(':');
            if (parts.length === 3) {
                document.getElementById('folderDayOfMonthInput').value = String(parseInt(parts[0], 10) || 1);
                runTimeVal = parts[1] + ':' + parts[2];
            }
        } else if (schType === 'interval' || schType === 'minute' || schType === 'hour') {
            document.getElementById('folderFrequencySelect').value = 'interval';
            if (cfg.interval_val) {
                document.getElementById('folderIntervalNumericInput').value = cfg.interval_val.slice(0, -1) || '1';
                document.getElementById('folderIntervalUnitSelect').value = cfg.interval_val.slice(-1) || 'm';
            }
            if (cfg.window_start && cfg.window_end) {
                document.getElementById('folderIntervalUseWindow').checked = true;
                document.getElementById('folderIntervalWindowStart').value = cfg.window_start;
                document.getElementById('folderIntervalWindowEnd').value = cfg.window_end;
            }
        } else {
            document.getElementById('folderFrequencySelect').value = 'daily';
            if (cfg.weekdays) setFolderWeekdays(cfg.weekdays);
        }
        document.getElementById('folderFlatpickr').value = runTimeVal.slice(0, 5) || '12:00';
        if (typeof toggleFolderIntervalInput === 'function') toggleFolderIntervalInput();
    }

    function collectFolderSchedulePayload() {
        const freq = document.getElementById('folderFrequencySelect').value || 'daily';
        const weekdays = Array.from(document.querySelectorAll('input[name="folder_weekdays"]:checked')).map((cb) => cb.value);
        if (freq === 'daily' && !weekdays.length) {
            return { error: 'Select at least one day of the week.' };
        }
        const useWindow = !!document.getElementById('folderIntervalUseWindow')?.checked;
        return {
            frequency: freq,
            run_time: (document.getElementById('folderFlatpickr').value || '').trim(),
            weekdays,
            interval_numeric: document.getElementById('folderIntervalNumericInput')?.value || '',
            interval_unit: document.getElementById('folderIntervalUnitSelect')?.value || '',
            interval_use_window: useWindow,
            interval_window_start: document.getElementById('folderIntervalWindowStart')?.value || '',
            interval_window_end: document.getElementById('folderIntervalWindowEnd')?.value || '',
            full_date: (document.getElementById('folderFullDateInput')?.value || '').trim(),
            day_of_month: document.getElementById('folderDayOfMonthInput')?.value || '',
        };
    }

    function setFolderFormEnabled(on) {
        const enabled = !!on;
        const group = document.getElementById('folderEnabledGroup');
        const badge = document.getElementById('folderEnabledBadge');
        const btn = document.getElementById('folderEnabledToggle');
        if (group) group.dataset.enabled = enabled ? '1' : '0';
        if (badge) {
            badge.textContent = enabled ? 'Enabled' : 'Disabled';
            badge.className = enabled ? 'badge badge-green' : 'badge badge-red';
        }
        if (btn) btn.textContent = enabled ? 'Disable' : 'Enable';
    }

    function setFolderFormEditable(canEdit) {
        const name = document.getElementById('folderFormName');
        if (name) name.disabled = !canEdit;
        const settings = document.getElementById('folderScheduleSettings');
        if (settings) {
            settings.style.opacity = canEdit ? '' : '0.6';
            settings.querySelectorAll('input, select, textarea, button').forEach((el) => {
                el.disabled = !canEdit;
            });
        }
        const parallel = document.getElementById('folderParallelSettings');
        if (parallel) {
            parallel.style.opacity = canEdit ? '' : '0.6';
            parallel.querySelectorAll('input, select, textarea, button').forEach((el) => {
                el.disabled = !canEdit;
            });
        }
        const daysSettings = document.getElementById('folderDaysSettings');
        if (daysSettings) {
            daysSettings.style.opacity = canEdit ? '' : '0.6';
            daysSettings.querySelectorAll('input, select, textarea, button').forEach((el) => {
                el.disabled = !canEdit;
            });
        }
    }

    function resetFolderDaysField() {
        const el = document.getElementById('folderDaysInput');
        if (el) el.value = '';
    }

    function fillFolderDaysField(f) {
        const el = document.getElementById('folderDaysInput');
        if (!el) return;
        if (f && f.days !== null && f.days !== undefined && f.days !== '') {
            el.value = String(f.days);
        } else {
            el.value = '';
        }
    }

    function collectFolderDaysPayload() {
        if (!window.CAN_SET_DAYS) return {};
        const el = document.getElementById('folderDaysInput');
        if (!el) return {};
        const raw = String(el.value || '').trim();
        if (raw === '') {
            // Explicit clear when editing — omit on create if empty
            return { days: null, _daysProvided: true };
        }
        if (!/^\d+$/.test(raw)) {
            return { error: 'Days must be a non-negative integer (0, 1, 2, …)' };
        }
        return { days: parseInt(raw, 10), _daysProvided: true };
    }

    function toggleFolderParallelFields() {
        const on = !!document.getElementById('folderParallelEnabled')?.checked;
        const fields = document.getElementById('folderParallelFields');
        if (fields) fields.style.display = on ? '' : 'none';
        if (on) {
            const maxEl = document.getElementById('folderMaxConcurrent');
            const gapEl = document.getElementById('folderScriptGap');
            if (maxEl && (!maxEl.value || Number(maxEl.value) < 2)) maxEl.value = '3';
            if (gapEl && (gapEl.value === '' || gapEl.value == null)) gapEl.value = '20';
        }
    }

    function resetFolderParallelDefaults() {
        const cb = document.getElementById('folderParallelEnabled');
        if (cb) cb.checked = false;
        const maxEl = document.getElementById('folderMaxConcurrent');
        if (maxEl) maxEl.value = '3';
        const gapEl = document.getElementById('folderScriptGap');
        if (gapEl) gapEl.value = '20';
        toggleFolderParallelFields();
    }

    function fillFolderParallelFields(f) {
        const enabled = Number(f && f.parallel_enabled) === 1;
        const cb = document.getElementById('folderParallelEnabled');
        if (cb) cb.checked = enabled;
        const maxEl = document.getElementById('folderMaxConcurrent');
        if (maxEl) maxEl.value = String(Math.max(2, Number(f && f.max_concurrent) || 3));
        const gapEl = document.getElementById('folderScriptGap');
        if (gapEl) {
            const g = Number(f && f.script_gap_seconds);
            gapEl.value = String(Number.isFinite(g) ? g : 20);
        }
        toggleFolderParallelFields();
    }

    function collectFolderParallelPayload() {
        const enabled = !!document.getElementById('folderParallelEnabled')?.checked;
        if (!enabled) {
            return { parallel_enabled: 0, max_concurrent: 1, script_gap_seconds: 0 };
        }
        let maxC = parseInt(document.getElementById('folderMaxConcurrent')?.value || '3', 10);
        let gap = parseInt(document.getElementById('folderScriptGap')?.value || '0', 10);
        if (!Number.isFinite(maxC) || maxC < 2) {
            return { error: 'Scripts at once must be at least 2 when multi-script run is on.' };
        }
        if (maxC > 50) maxC = 50;
        if (!Number.isFinite(gap) || gap < 0) {
            return { error: 'Gap must be 0 or a positive number of seconds.' };
        }
        if (gap > 3600) gap = 3600;
        return { parallel_enabled: 1, max_concurrent: maxC, script_gap_seconds: gap };
    }

    function toggleFolderFormEnabled() {
        const group = document.getElementById('folderEnabledGroup');
        if (group && group.dataset.canToggle !== '1') return;
        const on = group && group.dataset.enabled === '1';
        setFolderFormEnabled(!on);
    }

    function openCreateFolderModal() {
        if (typeof isViewingOtherUser === 'function' && isViewingOtherUser()) return;
        document.getElementById('folderFormTitle').textContent = 'New Folder';
        document.getElementById('folderFormSubmit').textContent = 'Create Folder';
        document.getElementById('folderFormId').value = '';
        document.getElementById('folderFormName').value = '';
        resetFolderScheduleFields();
        resetFolderParallelDefaults();
        resetFolderDaysField();
        setFolderFormEnabled(true);
        setFolderFormEditable(true);
        const enabledGroup = document.getElementById('folderEnabledGroup');
        if (enabledGroup) {
            enabledGroup.style.display = 'none';
            enabledGroup.dataset.canToggle = '0';
        }
        document.getElementById('folderFormModal').classList.add('open');
        setTimeout(() => document.getElementById('folderFormName')?.focus(), 50);
    }

    function editFolder(id) {
        const f = state.folders.find((x) => Number(x.id) === Number(id));
        if (!f) return toast('Folder not found', 'error');
        const canEdit = folderFlag(f, 'can_edit');
        const canToggle = folderFlag(f, 'can_enable') || folderFlag(f, 'can_disable');
        if (!canEdit && !canToggle) return toast('You do not have permission to edit this folder', 'error');
        document.getElementById('folderFormTitle').textContent = 'Edit Folder';
        document.getElementById('folderFormSubmit').textContent = 'Save Changes';
        document.getElementById('folderFormId').value = String(f.id);
        document.getElementById('folderFormName').value = f.name || '';
        fillFolderScheduleFields(f);
        fillFolderParallelFields(f);
        fillFolderDaysField(f);
        setFolderFormEnabled(Number(f.enabled) === 1);
        setFolderFormEditable(canEdit);
        const enabledGroup = document.getElementById('folderEnabledGroup');
        if (enabledGroup) {
            enabledGroup.style.display = canToggle ? '' : 'none';
            enabledGroup.dataset.canToggle = canToggle ? '1' : '0';
        }
        const toggleBtn = document.getElementById('folderEnabledToggle');
        if (toggleBtn) toggleBtn.style.display = canToggle ? '' : 'none';
        document.getElementById('folderFormModal').classList.add('open');
        setTimeout(() => document.getElementById('folderFormName')?.focus(), 50);
    }

    function closeFolderModal() {
        document.getElementById('folderFormModal')?.classList.remove('open');
    }

    function submitFolderForm(ev) {
        if (ev) ev.preventDefault();
        const id = (document.getElementById('folderFormId').value || '').trim();
        const name = (document.getElementById('folderFormName').value || '').trim();
        const f = id ? state.folders.find((x) => Number(x.id) === Number(id)) : null;
        const canEdit = !id || folderFlag(f, 'can_edit');
        const canToggle = !!id && (folderFlag(f, 'can_enable') || folderFlag(f, 'can_disable'));
        if (canEdit && !name) {
            toast('Folder name is required', 'error');
            return false;
        }
        let timing = {};
        let parallel = {};
        let daysPayload = {};
        if (canEdit) {
            timing = collectFolderSchedulePayload();
            if (timing.error) {
                toast(timing.error, 'error');
                return false;
            }
            parallel = collectFolderParallelPayload();
            if (parallel.error) {
                toast(parallel.error, 'error');
                return false;
            }
            daysPayload = collectFolderDaysPayload();
            if (daysPayload.error) {
                toast(daysPayload.error, 'error');
                return false;
            }
        }
        const btn = document.getElementById('folderFormSubmit');
        if (btn) btn.disabled = true;
        const body = {};
        if (id) {
            if (canEdit) {
                Object.assign(body, { name }, timing, parallel);
                if (daysPayload._daysProvided) body.days = daysPayload.days;
            }
            if (canToggle) {
                const enabledGroup = document.getElementById('folderEnabledGroup');
                body.enabled = enabledGroup && enabledGroup.dataset.enabled === '1' ? 1 : 0;
            }
            if (!canEdit && !canToggle) {
                if (btn) btn.disabled = false;
                toast('You do not have permission to update this folder', 'error');
                return false;
            }
        } else {
            Object.assign(body, { name }, timing, parallel);
            if (daysPayload._daysProvided && daysPayload.days !== null) body.days = daysPayload.days;
        }
        const req = id
            ? api('/api/schedule-folders/' + id, {
                method: 'PATCH',
                body: JSON.stringify(body),
            })
            : api('/api/schedule-folders', {
                method: 'POST',
                body: JSON.stringify(body),
            });
        req.then((data) => {
            let msg = id ? 'Folder updated' : 'Folder created';
            if (data && data.days_applied != null && Number(data.days_applied) > 0) {
                msg += ` · days applied to ${data.days_applied} script(s)`;
            }
            toast(msg);
            closeFolderModal();
            loadFolders();
            if (id && state.currentFolderId === Number(id)) reloadItems(true);
        }).catch((err) => toast(err.message, 'error'))
            .finally(() => { if (btn) btn.disabled = false; });
        return false;
    }

    function deleteFolder(id) {
        const f = state.folders.find((x) => Number(x.id) === Number(id));
        if (f && !folderFlag(f, 'can_delete')) {
            return toast('You do not have permission to delete this folder', 'error');
        }
        if (!confirm('Delete this folder? Scripts inside will return to the regular Scheduler (not deleted).')) return;
        api('/api/schedule-folders/' + id, { method: 'DELETE' })
            .then(() => {
                toast('Folder deleted');
                if (state.currentFolderId === id) backToFolders();
                loadFolders();
            })
            .catch((e) => toast(e.message, 'error'));
    }

    function toggleFolderEnabled(id, on) {
        api('/api/schedule-folders/' + id, {
            method: 'PATCH',
            body: JSON.stringify({ enabled: on ? 1 : 0 }),
        }).then(() => loadFolders()).catch((e) => toast(e.message, 'error'));
    }

    // ---------- Detail view ----------

    function showFolderDetailLoading(folderName) {
        const title = document.getElementById('folderDetailTitle');
        const meta = document.getElementById('folderDetailMeta');
        const tbody = document.getElementById('folderItemsBody');
        const historyBody = document.getElementById('folderHistoryBody');
        const pagination = document.getElementById('folderItemsPagination');
        if (title) title.textContent = folderName || 'Loading…';
        if (meta) meta.textContent = 'Loading folder scripts…';
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;padding:2rem;color:var(--text-tertiary);">Loading scripts…</td></tr>';
        }
        if (historyBody) {
            historyBody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:1rem;color:var(--text-tertiary);">Loading…</td></tr>';
        }
        if (pagination) pagination.style.display = 'none';
        state.lastItems = [];
    }

    function openFolder(id) {
        const folderId = parseInt(id, 10);
        state.currentFolderId = folderId;
        state.page = 1;
        state.itemsLoadToken += 1;
        clearGlobalSelection();
        try { sessionStorage.setItem('activeSchedulerFolderId', String(folderId)); } catch (_) {}
        document.getElementById('folders-list-view').style.display = 'none';
        document.getElementById('folders-detail-view').style.display = 'block';
        const search = document.getElementById('folderItemSearch');
        if (search) search.value = '';
        const folder = state.folders.find((f) => Number(f.id) === folderId);
        showFolderDetailLoading(folder && folder.name ? folder.name : 'Folder');
        reloadItems(false);
        startPoll();
    }

    function backToFolders() {
        stopPoll();
        state.currentFolderId = null;
        state.lastItems = [];
        state.folderScriptIds = new Set();
        try { sessionStorage.removeItem('activeSchedulerFolderId'); } catch (_) {}
        clearGlobalSelection();
        document.getElementById('folders-detail-view').style.display = 'none';
        document.getElementById('folders-list-view').style.display = 'block';
        loadFolders();
    }

    function ensureFolderList() {
        const listView = document.getElementById('folders-list-view');
        const detailView = document.getElementById('folders-detail-view');
        const inDetail = !!(state.currentFolderId && detailView && detailView.style.display !== 'none');
        if (inDetail) return;
        if (listView) listView.style.display = 'block';
        if (detailView) detailView.style.display = 'none';
        const tbody = document.getElementById('foldersTableBody');
        const hasRows = !!(tbody && tbody.querySelector('tr.sch-row'));
        if (hasRows) return;
        if (state.folders && state.folders.length) {
            renderFolderList();
            return;
        }
        loadFolders();
    }

    function initFromPage() {
        let savedTab = 'schedules';
        try { savedTab = localStorage.getItem('activeSchedulerTab') || 'schedules'; } catch (_) {}
        if (savedTab !== 'folders') return;

        const foldersTab = document.getElementById('tab-folders');
        if (foldersTab) foldersTab.style.display = 'block';
        const schedulesTab = document.getElementById('tab-schedules');
        if (schedulesTab) schedulesTab.style.display = 'none';
        const jobsTab = document.getElementById('tab-jobs');
        if (jobsTab) jobsTab.style.display = 'none';

        try { sessionStorage.removeItem('activeSchedulerFolderId'); } catch (_) {}
        const listView = document.getElementById('folders-list-view');
        const detailView = document.getElementById('folders-detail-view');
        if (listView) listView.style.display = 'block';
        if (detailView) detailView.style.display = 'none';
        state.currentFolderId = null;
        loadFolders();
    }

    function startPoll() {
        stopPoll();
        state.pollTimer = setInterval(() => {
            if (state.currentFolderId && isFolderDetailVisible()) reloadItems(true);
        }, 4000);
    }

    function stopPoll() {
        if (state.pollTimer) clearInterval(state.pollTimer);
        state.pollTimer = null;
    }

    function debouncedReloadItems() {
        clearTimeout(state.searchTimer);
        state.searchTimer = setTimeout(() => {
            state.page = 1;
            reloadItems(false);
        }, 250);
    }

    async function reloadItems(silent) {
        if (!state.currentFolderId) return;
        // Never wipe the table while a row action menu is open
        if (silent && document.querySelector('#folderItemsBody .dropdown.open, #folders-detail-view .dropdown.open')) {
            return;
        }
        const loadToken = ++state.itemsLoadToken;
        const folderId = state.currentFolderId;
        const search = encodeURIComponent(document.getElementById('folderItemSearch')?.value || '');
        try {
            const data = await api(
                `/api/schedule-folders/${folderId}/items?page=${state.page}&limit=${state.limit}&search=${search}`
            );
            if (loadToken !== state.itemsLoadToken || folderId !== state.currentFolderId) return;
            const f = data.folder || {};
            const title = document.getElementById('folderDetailTitle');
            if (title) title.textContent = f.name || 'Folder';
            const run = data.run;
            const prog = run
                ? `${(run.successful_count || 0) + (run.failed_count || 0)} / ${run.total_count || 0} · ${run.status}`
                : (f.status || 'idle');
            const meta = document.getElementById('folderDetailMeta');
            if (meta) {
                const timeLabel = (window.SchedulerUI && SchedulerUI.formatScheduleTimeLabel)
                    ? SchedulerUI.formatScheduleTimeLabel(f)
                    : ((f.run_time || '').toString().slice(0, 5) || '—');
                meta.textContent = `${timeLabel} · ${prog}`;
            }
            const running = run && run.status === 'running';
            const folderMeta = (f && f.id != null) ? f : currentFolder();
            const canRunFolder = folderFlag(folderMeta, 'can_run');
            const btnRun = document.getElementById('btnFolderRun');
            const btnStop = document.getElementById('btnFolderStop');
            if (btnRun) btnRun.style.display = (!running && canRunFolder) ? '' : 'none';
            if (btnStop) btnStop.style.display = (running && canRunFolder) ? '' : 'none';

            state.total = data.total || 0;
            const items = data.items || [];
            state.lastItems = items;
            window.folderSchedulesCache = items;
            syncFolderScriptIdsFromItems(items);
            // Keep full-folder script id cache accurate for "already in folder" marks
            if (!silent || !(state.folderScriptIds && state.folderScriptIds.size)) {
                refreshFolderScriptIds(state.currentFolderId);
            }

            if (silent && smartUpdateItems(items)) {
                renderHistory(data.history || []);
                updatePagination();
                if (typeof updateBulkBar === 'function') updateBulkBar();
                return;
            }
            renderItems(items);
            renderHistory(data.history || []);
            updatePagination();
        } catch (e) {
            if (!silent) toast(e.message, 'error');
        }
    }

    function smartUpdateItems(items) {
        const tbody = document.getElementById('folderItemsBody');
        if (!tbody || !items.length) return false;
        const rows = tbody.querySelectorAll('tr.sch-row');
        if (rows.length !== items.length) return false;
        for (let i = 0; i < items.length; i++) {
            if (String(rows[i].dataset.id) !== String(items[i].id)) return false;
        }
        const ui = window.SchedulerUI;
        if (!ui) return false;

        items.forEach((it, i) => {
            const tr = rows[i];
            const schRunning = it.running_status || 'idle';
            const statusCell = tr.querySelector('.col-status');
            if (statusCell && ui.getStatusHtml) {
                if (!tr.querySelector('.dropdown.open')) {
                    const html = ui.getStatusHtml(schRunning, it);
                    if (statusCell.innerHTML.trim() !== html.trim()) statusCell.innerHTML = html;
                }
            }
            const enabledCell = tr.querySelector('.col-enabled');
            if (enabledCell) {
                const enabled = Number(it.enabled) === 1;
                const html = enabled
                    ? '<span class="badge badge-green">Enabled</span>'
                    : '<span class="badge badge-red">Disabled</span>';
                if (enabledCell.innerHTML.trim() !== html) enabledCell.innerHTML = html;
            }
            const lastCell = tr.querySelector('.col-last');
            if (lastCell && lastCell.textContent !== (it.last_run || '—')) lastCell.textContent = it.last_run || '—';
            const nextCell = tr.querySelector('.col-next');
            if (nextCell && nextCell.textContent !== (it.next_run || '—')) nextCell.textContent = it.next_run || '—';
            const menu = tr.querySelector('.dropdown-menu');
            if (menu && ui.getDropdownHtml && !tr.querySelector('.dropdown.open')) {
                const f = currentFolder();
                const canManageMembers = folderFlag(f, 'can_edit') || folderFlag(f, 'can_manage');
                menu.innerHTML = ui.getDropdownHtml(it, schRunning, { showRemoveFromFolder: canManageMembers });
            }
            tr.setAttribute('data-can-edit', Number(it.can_edit) === 1 ? '1' : '0');
            tr.setAttribute('data-can-run', Number(it.can_run) === 1 ? '1' : '0');
            tr.setAttribute('data-can-enable', Number(it.can_enable) === 1 ? '1' : '0');
            tr.setAttribute('data-can-disable', Number(it.can_disable) === 1 ? '1' : '0');
            tr.setAttribute('data-can-delete', Number(it.can_delete) === 1 ? '1' : '0');
        });
        return true;
    }

    function renderItems(items) {
        const tbody = document.getElementById('folderItemsBody');
        if (!tbody) return;
        if (!items.length) {
            tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;padding:2rem;color:var(--text-tertiary);">No scripts in this folder. Use + Add Scripts or Add Existing.</td></tr>';
            if (typeof updateBulkBar === 'function') updateBulkBar();
            return;
        }
        const build = window.SchedulerUI && window.SchedulerUI.buildScheduleRowHtml;
        if (!build) {
            tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;padding:2rem;color:var(--danger);">Schedule UI not ready. Refresh the page.</td></tr>';
            return;
        }
        const f = currentFolder();
        const canManageMembers = folderFlag(f, 'can_edit') || folderFlag(f, 'can_manage');
        tbody.innerHTML = items.map((it, idx) => {
            const order = (state.page - 1) * state.limit + idx + 1;
            const idStr = String(it.id);
            const isChecked = typeof selectedRows !== 'undefined' && selectedRows.has(idStr);
            return build(it, {
                order,
                isChecked,
                draggable: canManageMembers,
                showRemoveFromFolder: canManageMembers,
                hideDays: !window.CAN_SET_DAYS,
                hideTime: true,
                dropdownPrefix: 'folder-dd',
            });
        }).join('');
        if (typeof applySavedSchedulerColumns === 'function') applySavedSchedulerColumns();
        if (typeof updateBulkBar === 'function') updateBulkBar();
    }

    function renderHistory(rows) {
        const tbody = document.getElementById('folderHistoryBody');
        if (!tbody) return;
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:1rem;color:var(--text-tertiary);">No runs yet.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((r) => {
            const dur = r.duration_seconds != null ? `${Math.round(r.duration_seconds)}s` : '—';
            const done = (r.successful_count || 0) + (r.failed_count || 0);
            const st = (r.status || '').toLowerCase();
            let badge = '<span class="badge badge-slate">' + esc(r.status || '—') + '</span>';
            if (st === 'completed') badge = '<span class="badge badge-green">completed</span>';
            else if (st === 'failed' || st === 'error') badge = '<span class="badge badge-red">' + esc(st) + '</span>';
            else if (st === 'running') badge = '<span class="status-pulse"><span class="status-pulse-dot running"></span>running</span>';
            else if (st === 'stopped') badge = '<span class="badge badge-amber">stopped</span>';
            return `<tr>
                <td class="mono">${esc(r.started_at || '')}</td>
                <td class="mono">${esc(r.ended_at || '—')}</td>
                <td class="mono">${dur}</td>
                <td>${badge}</td>
                <td>${r.successful_count || 0}</td>
                <td>${r.failed_count || 0}</td>
                <td>${r.skipped_count || 0}</td>
                <td class="mono">${done} / ${r.total_count || 0}</td>
            </tr>`;
        }).join('');
    }

    function updatePagination() {
        const el = document.getElementById('folderItemsPagination');
        if (!el) return;
        if (state.total <= 0) {
            el.style.display = 'none';
            return;
        }
        el.style.display = 'flex';
        const start = (state.page - 1) * state.limit + 1;
        const end = Math.min(state.page * state.limit, state.total);
        document.getElementById('fiPageStart').textContent = start;
        document.getElementById('fiPageEnd').textContent = end;
        document.getElementById('fiPageTotal').textContent = state.total;
    }

    function changePageSize() {
        state.limit = parseInt(document.getElementById('fiPageSize').value, 10) || 25;
        state.page = 1;
        reloadItems(false);
    }

    function prevPage() {
        if (state.page > 1) {
            state.page--;
            reloadItems(false);
        }
    }

    function nextPage() {
        if (state.page * state.limit < state.total) {
            state.page++;
            reloadItems(false);
        }
    }

    // ---------- Folder-only bulk: remove / move ----------

    async function removeSelected() {
        const ids = selectedScheduleIds();
        if (!ids.length) return toast('Select scripts first', 'error');
        if (!confirm('Remove selected script(s) from this folder? They will return to the Schedules tab.')) return;
        try {
            await api(`/api/schedule-folders/${state.currentFolderId}/bulk`, {
                method: 'POST',
                body: JSON.stringify({ action: 'remove', schedule_ids: ids }),
            });
            clearGlobalSelection();
            toast('Removed from folder');
            reloadItems(false);
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    function removeOne(id) {
        if (typeof selectedRows !== 'undefined') {
            selectedRows.clear();
            selectedRows.add(String(id));
        }
        if (typeof updateBulkBar === 'function') updateBulkBar();
        removeSelected();
    }

    async function bulkMove() {
        const ids = selectedScheduleIds();
        if (!ids.length) return toast('Select scripts first', 'error');
        state.movePendingIds = ids;
        state.moveTargetId = null;
        const submit = document.getElementById('moveFolderSubmit');
        if (submit) submit.disabled = true;
        document.getElementById('moveFolderDesc').textContent =
            `Move ${ids.length} selected script(s) to another folder.`;
        document.getElementById('moveFolderSearch').value = '';
        document.getElementById('moveFolderList').innerHTML =
            '<div style="padding:1rem;text-align:center;color:var(--text-tertiary);">Loading…</div>';
        document.getElementById('moveFolderModal').classList.add('open');
        try {
            await loadFolders();
            renderMoveFolderList();
        } catch (e) {
            toast(e.message, 'error');
            closeMoveFolderModal();
        }
    }

    function closeMoveFolderModal() {
        document.getElementById('moveFolderModal')?.classList.remove('open');
        state.movePendingIds = [];
        state.moveTargetId = null;
    }

    function filterMoveFolders() {
        renderMoveFolderList();
    }

    function renderMoveFolderList() {
        const list = document.getElementById('moveFolderList');
        if (!list) return;
        const q = (document.getElementById('moveFolderSearch')?.value || '').toLowerCase();
        const choices = state.folders.filter((f) =>
            Number(f.id) !== Number(state.currentFolderId)
            && (!q || (f.name || '').toLowerCase().includes(q))
        );
        const submit = document.getElementById('moveFolderSubmit');
        if (!choices.length) {
            list.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-tertiary);">No other folders available.</div>';
            if (submit) submit.disabled = true;
            return;
        }
        list.innerHTML = choices.map((f) => {
            const checked = Number(state.moveTargetId) === Number(f.id) ? 'checked' : '';
            const active = Number(state.moveTargetId) === Number(f.id);
            return `<label style="display:flex;align-items:center;gap:0.65rem;padding:0.65rem 0.75rem;border-radius:6px;cursor:pointer;${active ? 'background:var(--bg-secondary,rgba(0,0,0,0.04));' : ''}">
                <input type="radio" name="moveFolderTarget" value="${f.id}" ${checked}
                    onchange="SchedulerFolders.selectMoveTarget(${f.id})">
                <span style="flex:1;min-width:0;">
                    <strong style="display:block;">${esc(f.name)}</strong>
                    <span style="font-size:0.75rem;color:var(--text-tertiary);">${f.item_count || 0} scripts</span>
                </span>
            </label>`;
        }).join('');
    }

    function selectMoveTarget(id) {
        state.moveTargetId = id;
        const submit = document.getElementById('moveFolderSubmit');
        if (submit) submit.disabled = !id;
        renderMoveFolderList();
    }

    async function confirmMoveFolder() {
        if (!state.moveTargetId || !state.movePendingIds.length) {
            return toast('Select a destination folder', 'error');
        }
        const submit = document.getElementById('moveFolderSubmit');
        if (submit) submit.disabled = true;
        try {
            await api(`/api/schedule-folders/${state.currentFolderId}/bulk`, {
                method: 'POST',
                body: JSON.stringify({
                    action: 'move',
                    schedule_ids: state.movePendingIds,
                    target_folder_id: state.moveTargetId,
                }),
            });
            closeMoveFolderModal();
            clearGlobalSelection();
            toast('Moved to folder');
            reloadItems(false);
        } catch (e) {
            toast(e.message, 'error');
            if (submit) submit.disabled = false;
        }
    }

    // ---------- Add existing ----------

    async function openAddExisting() {
        if (!state.currentFolderId) return;
        state.addExistingSelected = new Set();
        document.getElementById('addExistingSearch').value = '';
        document.getElementById('addExistingPathFilter').value = '';
        const pathOnly = document.getElementById('addExistingCurrentFolderOnly');
        if (pathOnly) pathOnly.checked = false;
        const workerSel = document.getElementById('addExistingWorkerFilter');
        if (workerSel) workerSel.value = '';
        updateAddExistingSelectAllBtn(false, 0);
        document.getElementById('addExistingList').innerHTML =
            '<div style="padding:1rem;text-align:center;color:var(--text-tertiary);">Loading…</div>';
        updateAddExistingCount();
        document.getElementById('addExistingModal').classList.add('open');
        try {
            const data = await api('/api/schedules/unassigned');
            state.addExistingCache = data.schedules || [];
            populateAddExistingWorkers();
            renderAddExistingList();
            if (!state.addExistingCache.length) {
                document.getElementById('addExistingList').innerHTML =
                    '<div style="padding:1rem;text-align:center;color:var(--text-tertiary);">No unassigned schedules available.</div>';
            }
        } catch (e) {
            toast(e.message, 'error');
            closeAddExistingModal();
        }
    }

    function populateAddExistingWorkers() {
        const sel = document.getElementById('addExistingWorkerFilter');
        if (!sel || sel.dataset.populated === '1') return;
        const workers = [...new Set((state.addExistingCache || []).map((s) => s.worker_name).filter(Boolean))].sort();
        workers.forEach((w) => {
            const opt = document.createElement('option');
            opt.value = w;
            opt.textContent = w;
            sel.appendChild(opt);
        });
        sel.dataset.populated = '1';
    }

    function closeAddExistingModal() {
        document.getElementById('addExistingModal')?.classList.remove('open');
        state.addExistingCache = [];
        state.addExistingSelected = new Set();
        const workerSel = document.getElementById('addExistingWorkerFilter');
        if (workerSel) {
            workerSel.innerHTML = '<option value="">All Workers</option>';
            workerSel.dataset.populated = '';
        }
    }

    function filterAddExisting() {
        renderAddExistingList();
    }

    function visibleAddExisting() {
        const q = (document.getElementById('addExistingSearch')?.value || '').toLowerCase();
        const worker = document.getElementById('addExistingWorkerFilter')?.value || '';
        const pathFilter = document.getElementById('addExistingPathFilter')?.value || '';
        const currentFolderOnly = !!document.getElementById('addExistingCurrentFolderOnly')?.checked;
        let rows = state.addExistingCache || [];
        if (worker) {
            rows = rows.filter((s) => s.worker_name === worker);
        }
        if (pathFilter.trim()) {
            rows = rows.filter((s) => matchesSchedulePathFilter(s, pathFilter, currentFolderOnly));
        }
        if (q) {
            rows = rows.filter((s) =>
                (s.script_name || '').toLowerCase().includes(q)
                || (s.worker_name || '').toLowerCase().includes(q)
                || (s.username || '').toLowerCase().includes(q)
                || (s.script_path || '').toLowerCase().includes(q)
            );
        }
        return rows;
    }

    function renderAddExistingList() {
        const list = document.getElementById('addExistingList');
        if (!list) return;
        const rows = visibleAddExisting();
        if (!rows.length) {
            list.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-tertiary);">No matching schedules.</div>';
            return;
        }
        list.innerHTML = rows.map((s) => {
            const id = String(s.id);
            const checked = state.addExistingSelected.has(id) ? 'checked' : '';
            const pathHint = s.script_path
                ? `<div style="font-size:0.68rem;color:var(--text-tertiary);margin-top:0.15rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(s.script_path)}">${esc(s.script_path)}</div>`
                : '';
            return `<label style="display:flex;align-items:flex-start;gap:0.65rem;padding:0.65rem 0.75rem;border-radius:6px;cursor:pointer;">
                <input type="checkbox" value="${id}" ${checked}
                    onchange="SchedulerFolders.toggleAddExisting(${s.id}, this.checked)" style="margin-top:0.2rem;">
                <span style="flex:1;min-width:0;">
                    <strong style="display:block;word-break:break-all;">${esc(s.script_name)}</strong>
                    <span style="font-size:0.75rem;color:var(--text-tertiary);">
                        ${esc(s.worker_name || '')} · ${esc(s.username || '')}
                        ${s.run_time ? ' · ' + esc(String(s.run_time).slice(0, 5)) : ''}
                    </span>${pathHint}
                </span>
            </label>`;
        }).join('');
        const allVisible = rows.length > 0 && rows.every((s) => state.addExistingSelected.has(String(s.id)));
        updateAddExistingSelectAllBtn(allVisible, rows.length);
        updateAddExistingCount();
    }

    function updateAddExistingSelectAllBtn(allVisible, visibleCount) {
        const btn = document.getElementById('addExistingSelectAllBtn');
        if (!btn) return;
        btn.disabled = !visibleCount;
        btn.textContent = allVisible ? 'Deselect all' : 'Select all';
        btn.title = allVisible ? 'Clear selection of visible schedules' : 'Select all visible schedules';
    }

    function toggleAddExisting(id, on) {
        const key = String(id);
        if (on) state.addExistingSelected.add(key);
        else state.addExistingSelected.delete(key);
        updateAddExistingCount();
        const rows = visibleAddExisting();
        const allVisible = rows.length > 0 && rows.every((s) => state.addExistingSelected.has(String(s.id)));
        updateAddExistingSelectAllBtn(allVisible, rows.length);
    }

    function toggleAddExistingAll() {
        const rows = visibleAddExisting();
        const allVisible = rows.length > 0 && rows.every((s) => state.addExistingSelected.has(String(s.id)));
        rows.forEach((s) => {
            if (allVisible) state.addExistingSelected.delete(String(s.id));
            else state.addExistingSelected.add(String(s.id));
        });
        renderAddExistingList();
    }

    function updateAddExistingCount() {
        const el = document.getElementById('addExistingCount');
        if (el) el.textContent = `${state.addExistingSelected.size} selected`;
        const btn = document.getElementById('addExistingSubmit');
        if (btn) btn.disabled = state.addExistingSelected.size === 0;
    }

    async function confirmAddExisting() {
        const ids = Array.from(state.addExistingSelected).map((x) => parseInt(x, 10)).filter(Boolean);
        if (!ids.length) return toast('Select at least one schedule', 'error');
        const btn = document.getElementById('addExistingSubmit');
        if (btn) btn.disabled = true;
        try {
            await api(`/api/schedule-folders/${state.currentFolderId}/items`, {
                method: 'POST',
                body: JSON.stringify({ schedule_ids: ids }),
            });
            closeAddExistingModal();
            toast(`Copied ${ids.length} schedule(s) into folder (originals kept in Schedules)`);
            reloadItems(false);
        } catch (e) {
            toast(e.message, 'error');
            if (btn) btn.disabled = false;
        }
    }

    function addScriptsViaDrawer() {
        if (!state.currentFolderId) return;
        const hid = document.getElementById('createFolderId');
        if (hid) {
            hid.value = String(state.currentFolderId);
            hid.dataset.lock = '1';
        }
        if (typeof openDrawer === 'function') openDrawer();
        refreshFolderScriptIds(state.currentFolderId).then(() => {
            const drawer = document.getElementById('createDrawer');
            if (!drawer || !drawer.classList.contains('open')) return;
            const activeFolder = (document.getElementById('createFolderId') || {}).value || '';
            if (String(activeFolder) !== String(state.currentFolderId)) return;
            if (typeof refreshDrawerScriptList === 'function') {
                refreshDrawerScriptList();
            }
        });
    }

    // ---------- Run / stop folder ----------

    async function runFolder(folderId) {
        const id = folderId || state.currentFolderId;
        if (!id) return;
        try {
            await api(`/api/schedule-folders/${id}/run`, { method: 'POST', body: '{}' });
            toast('Folder run started');
            if (Number(state.currentFolderId) === Number(id) && isFolderDetailVisible()) {
                reloadItems(false);
            } else {
                loadFolders();
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    async function stopFolder(folderId) {
        const id = folderId || state.currentFolderId;
        if (!id) return;
        try {
            await api(`/api/schedule-folders/${id}/stop`, { method: 'POST', body: '{}' });
            toast('Folder stopped');
            if (Number(state.currentFolderId) === Number(id) && isFolderDetailVisible()) {
                reloadItems(false);
            } else {
                loadFolders();
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    // ---------- Drag reorder ----------

    let dragId = null;

    function onDragStart(e) {
        dragId = e.currentTarget.getAttribute('data-id');
        e.dataTransfer.effectAllowed = 'move';
    }

    function onDragOver(e) {
        e.preventDefault();
    }

    function onDrop(e) {
        e.preventDefault();
        const target = e.currentTarget.getAttribute('data-id');
        if (!dragId || !target || dragId === target) return;
        const tbody = document.getElementById('folderItemsBody');
        const rows = Array.from(tbody.querySelectorAll('tr.sch-row[data-id]'));
        const ids = rows.map((r) => r.getAttribute('data-id'));
        const from = ids.indexOf(dragId);
        const to = ids.indexOf(target);
        if (from < 0 || to < 0) return;
        ids.splice(to, 0, ids.splice(from, 1)[0]);
        const frag = document.createDocumentFragment();
        ids.forEach((id) => {
            const row = rows.find((x) => x.getAttribute('data-id') === id);
            if (row) frag.appendChild(row);
        });
        tbody.appendChild(frag);
        // renumber #
        Array.from(tbody.querySelectorAll('tr.sch-row')).forEach((tr, i) => {
            const cell = tr.children[1];
            if (cell) cell.textContent = String((state.page - 1) * state.limit + i + 1);
        });
        dragId = null;
        saveOrder(true);
    }

    async function saveOrder(silent) {
        if (!state.currentFolderId) return;
        const ids = Array.from(document.querySelectorAll('#folderItemsBody tr.sch-row[data-id]'))
            .map((r) => parseInt(r.getAttribute('data-id'), 10))
            .filter(Boolean);
        if (!ids.length) return;
        try {
            const data = await api(`/api/schedule-folders/${state.currentFolderId}/items?page=1&limit=5000`);
            const allIds = (data.items || []).map((i) => i.id);
            const start = (state.page - 1) * state.limit;
            const newAll = allIds.slice(0, start).concat(ids).concat(allIds.slice(start + ids.length));
            await api(`/api/schedule-folders/${state.currentFolderId}/reorder`, {
                method: 'POST',
                body: JSON.stringify({ schedule_ids: newAll }),
            });
            if (!silent) toast('Order saved');
            reloadItems(true);
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    // ---------- Boot ----------

    document.addEventListener('DOMContentLoaded', () => {
        const form = document.getElementById('createScheduleForm');
        if (form) {
            form.addEventListener('submit', () => {
                setTimeout(() => {
                    if (state.currentFolderId) reloadItems(false);
                }, 1200);
            });
        }
        ['folderFormModal', 'addExistingModal', 'moveFolderModal'].forEach((id) => {
            const modal = document.getElementById(id);
            if (modal) {
                modal.addEventListener('click', (ev) => {
                    if (ev.target === modal) {
                        if (id === 'folderFormModal') closeFolderModal();
                        if (id === 'addExistingModal') closeAddExistingModal();
                        if (id === 'moveFolderModal') closeMoveFolderModal();
                    }
                });
            }
        });
        document.addEventListener('keydown', (ev) => {
            if (ev.key !== 'Escape') return;
            closeFolderModal();
            closeAddExistingModal();
            closeMoveFolderModal();
        });
        // Defer until Schedules helpers exist
        setTimeout(initFromPage, 0);
    });

    async function refresh() {
        await loadFolders();
        if (!state.currentFolderId) return;
        const still = state.folders.some((f) => Number(f.id) === Number(state.currentFolderId));
        if (still) await reloadItems();
        else backToFolders();
    }

    window.SchedulerFolders = {
        loadFolders,
        refresh,
        refreshFolderDetail,
        filterFolders,
        openCreateFolderModal,
        editFolder,
        closeFolderModal,
        submitFolderForm,
        deleteFolder,
        toggleFolderEnabled,
        toggleFolderFormEnabled,
        toggleFolderParallelFields,
        resetFolderParallelDefaults,
        openFolder,
        backToFolders,
        ensureFolderList,
        initFromPage,
        reloadItems,
        debouncedReloadItems,
        changePageSize,
        prevPage,
        nextPage,
        currentFolderId,
        isFolderDetailVisible,
        getCachedItems,
        getFolderScriptIds,
        refreshFolderScriptIds,
        removeSelected,
        removeOne,
        bulkMove,
        closeMoveFolderModal,
        filterMoveFolders,
        selectMoveTarget,
        confirmMoveFolder,
        openAddExisting,
        closeAddExistingModal,
        filterAddExisting,
        toggleAddExisting,
        toggleAddExistingAll,
        confirmAddExisting,
        addScriptsViaDrawer,
        runFolder,
        stopFolder,
        onDragStart,
        onDragOver,
        onDrop,
        saveOrder,
        // aliases used by existing HTML
        bulk: function (action) {
            if (action === 'remove') return removeSelected();
            toast('Use the bulk bar for ' + action, 'info');
        },
    };
})();
