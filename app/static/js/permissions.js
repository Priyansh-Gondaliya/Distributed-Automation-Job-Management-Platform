(function () {
    function switchTab(tabId) {
        document.querySelectorAll('.tab-content').forEach(tab => { tab.style.display = 'none'; });
        document.querySelectorAll('.tab-trigger').forEach(btn => {
            btn.classList.remove('active');
            btn.setAttribute('aria-selected', 'false');
        });
        const activeTabElement = document.getElementById('tab-' + tabId);
        if (activeTabElement) activeTabElement.style.display = 'block';
        const activeBtn = document.querySelector(`button[onclick="switchTab('${tabId}')"]`);
        if (activeBtn) {
            activeBtn.classList.add('active');
            activeBtn.setAttribute('aria-selected', 'true');
        }
        try { sessionStorage.setItem('permissions-tab', tabId); } catch (e) { /* ignore */ }
    }
    window.switchTab = switchTab;

    function filterList(input, listId) {
        const filter = input.value.toLowerCase();
        const list = document.getElementById(listId);
        if (!list) return;
        const items = list.getElementsByClassName('filter-item');
        for (let i = 0; i < items.length; i++) {
            const textSpan = items[i].querySelector('.item-text');
            if (textSpan) {
                const textValue = textSpan.textContent || textSpan.innerText;
                items[i].style.display = textValue.toLowerCase().indexOf(filter) > -1 ? '' : 'none';
            }
        }
    }
    window.filterList = filterList;

    function parseJsonEl(id) {
        const el = document.getElementById(id);
        if (!el) return [];
        try { return JSON.parse(el.textContent || '[]'); } catch (e) { return []; }
    }

    const scriptAccessData = parseJsonEl('script-access-data');
    const scheduleAccessData = parseJsonEl('schedule-access-data');
    const folderAccessData = parseJsonEl('folder-access-data');
    const schedulerViewAccessData = parseJsonEl('scheduler-view-access-data');
    const usersCanSetDaysData = (function () {
        const el = document.getElementById('users-can-set-days-data');
        if (!el) return {};
        try { return JSON.parse(el.textContent || '{}'); } catch (e) { return {}; }
    })();
    const pcAccessData = parseJsonEl('pc-access-data');

    let scriptsCatalog = [];
    let schedulesCatalog = (parseJsonEl('schedules-catalog-data') || []).map(r => ({
        id: Number(r[0]),
        username: r[1],
        script_name: r[2],
        run_time: r[3],
        user_id: Number(r[4] || 0),
        worker_owner_id: Number(r[5] || 0),
    }));
    let foldersCatalog = (parseJsonEl('folders-catalog-data') || []).map(r => ({
        id: Number(r[0]), username: r[1], name: r[2]
    }));
    let scriptsById = new Map();
    let foldersById = new Map(foldersCatalog.map(f => [f.id, f]));

    const selectedScripts = new Map();
    const selectedFolders = new Map();
    const grantedSchedules = new Map();
    const pendingScheduleIds = new Set();
    const expandedScheduleOwnerIds = new Set();
    let pendingBulkOwnWorkers = false;
    let currentPermUserId = 0;
    const LIST_LIMIT = 150;

    function flagOn(v) {
        return v === true || v === 1 || v === '1';
    }

    function sameUserId(a, b) {
        return Number(a) === Number(b);
    }

    function grantHasActions(flags) {
        if (!flags) return false;
        return !!(flags.can_enable || flags.can_run || flags.can_edit || flags.can_duplicate || flags.can_delete);
    }

    function readSharedScheduleActions() {
        return {
            can_enable: !!document.getElementById('sch-act-enable')?.checked,
            can_run: !!document.getElementById('sch-act-run')?.checked,
            can_edit: !!document.getElementById('sch-act-edit')?.checked,
            can_duplicate: !!document.getElementById('sch-act-duplicate')?.checked,
            can_delete: !!document.getElementById('sch-act-delete')?.checked,
        };
    }

    function writeSharedScheduleActions(flags) {
        const f = flags || {};
        const enable = document.getElementById('sch-act-enable');
        const run = document.getElementById('sch-act-run');
        const edit = document.getElementById('sch-act-edit');
        const dup = document.getElementById('sch-act-duplicate');
        const del = document.getElementById('sch-act-delete');
        if (enable) enable.checked = !!f.can_enable;
        if (run) run.checked = !!f.can_run;
        if (edit) edit.checked = !!f.can_edit;
        if (dup) dup.checked = !!f.can_duplicate;
        if (del) del.checked = !!f.can_delete;
    }

    function unionGrantedActions() {
        const u = {
            can_enable: false,
            can_run: false,
            can_edit: false,
            can_duplicate: false,
            can_delete: false,
        };
        grantedSchedules.forEach((flags) => {
            if (!grantHasActions(flags)) return;
            if (flags.can_enable) u.can_enable = true;
            if (flags.can_run) u.can_run = true;
            if (flags.can_edit) u.can_edit = true;
            if (flags.can_duplicate) u.can_duplicate = true;
            if (flags.can_delete) u.can_delete = true;
        });
        return u;
    }

    function formatGrantedActions(flags) {
        if (!grantHasActions(flags)) return '';
        const parts = [];
        if (flags.can_enable) parts.push('Enable/Disable');
        if (flags.can_run) parts.push('Run');
        if (flags.can_edit) parts.push('Edit');
        if (flags.can_duplicate) parts.push('Duplicate');
        if (flags.can_delete) parts.push('Delete');
        return parts.join(', ');
    }

    function isOwnWorkerSchedule(s, userId) {
        if (!userId) return false;
        return sameUserId(s.worker_owner_id, userId) || sameUserId(s.user_id, userId);
    }

    function getOwnWorkerSchedules(userId) {
        if (!userId) return [];
        return schedulesCatalog.filter(s => isOwnWorkerSchedule(s, userId));
    }

    function getOtherUserSchedules(userId) {
        if (!userId) return schedulesCatalog.slice();
        return schedulesCatalog.filter(s => !isOwnWorkerSchedule(s, userId));
    }

    function updateScheduleActionsPanel() {
        const panel = document.getElementById('schedule-actions-panel');
        const hint = document.getElementById('schedule-actions-selection-hint');
        if (!panel) return;
        const pendingN = pendingScheduleIds.size + (pendingBulkOwnWorkers ? 1 : 0);
        const hasGrants = [...grantedSchedules.values()].some(grantHasActions);
        panel.hidden = pendingN === 0 && !hasGrants;
        if (pendingN === 0 && hasGrants) {
            writeSharedScheduleActions(unionGrantedActions());
        }
        if (hint) {
            if (!pendingN && !hasGrants) hint.textContent = '';
            else if (!pendingN && hasGrants) {
                hint.textContent = 'Checked actions match this user’s current grants. Tick schedules to apply or change them on Save.';
            } else if (pendingBulkOwnWorkers && pendingScheduleIds.size) {
                const ownN = getOwnWorkerSchedules(currentPermUserId).length;
                hint.textContent = `${pendingScheduleIds.size} other schedule(s) + all ${ownN} own-worker schedule(s) will get these actions on Save.`;
            } else if (pendingBulkOwnWorkers) {
                const ownN = getOwnWorkerSchedules(currentPermUserId).length;
                hint.textContent = `All ${ownN} schedule(s) on this user’s workers will get these actions on Save.`;
            } else {
                hint.textContent = `${pendingScheduleIds.size} schedule(s) will get these actions on Save. Existing grants stay unless selected here.`;
            }
        }
    }

    function escapeHtml(str) {
        return String(str == null ? '' : str)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function scriptLabel(s) {
        return `${s.worker_name} / ${s.script_name}`;
    }

    function scheduleLabel(s) {
        return `#${s.id} — ${s.username}: ${s.script_name} @ ${s.run_time}`;
    }

    function scheduleLabelInGroup(s) {
        return `#${s.id} — ${s.script_name} @ ${s.run_time}`;
    }

    function folderLabel(f) {
        return `${f.name} (Owner: ${f.username || 'None'})`;
    }

    function groupSchedulesByOwner(schedules) {
        const map = new Map();
        schedules.forEach((s) => {
            const key = Number(s.user_id) || 0;
            let g = map.get(key);
            if (!g) {
                g = { userId: key, username: s.username || 'Unknown', schedules: [] };
                map.set(key, g);
            }
            g.schedules.push(s);
        });
        return Array.from(map.values()).sort((a, b) =>
            String(a.username).localeCompare(String(b.username), undefined, { sensitivity: 'base' })
        );
    }

    function ownerGroupHasActiveGrant(group) {
        return group.schedules.some((s) => grantHasActions(grantedSchedules.get(s.id)));
    }

    function getScheduleListMatches() {
        const searchEl = document.getElementById('schedules-search');
        const filter = (searchEl && searchEl.value || '').trim().toLowerCase();
        const own = getOwnWorkerSchedules(currentPermUserId);
        const others = getOtherUserSchedules(currentPermUserId);
        const bulkLabel = `All schedules on this user's workers (${own.length})`.toLowerCase();
        const out = { showBulk: false, others: [], filter };
        if (!currentPermUserId) {
            out.others = [];
            return out;
        }
        const ownGranted = own.some(s => grantHasActions(grantedSchedules.get(s.id)));
        out.showBulk = own.length > 0 && (
            !filter
            || bulkLabel.includes(filter)
            || own.some(s => scheduleLabel(s).toLowerCase().includes(filter))
        );
        const filteredOthers = filter
            ? others.filter(s =>
                scheduleLabel(s).toLowerCase().includes(filter)
                || String(s.username || '').toLowerCase().includes(filter)
            )
            : others.slice();
        // Auto-expand owners that already have active action grants for this user.
        groupSchedulesByOwner(filteredOthers).forEach((g) => {
            if (ownerGroupHasActiveGrant(g)) expandedScheduleOwnerIds.add(g.userId);
        });
        out.others = filteredOthers;
        return out;
    }

    function syncSelectAllCheckbox(kind) {
        if (kind !== 'schedules') return;
        const cb = document.getElementById('schedules-select-all');
        const hint = document.getElementById('schedules-select-all-hint');
        if (!cb) return;
        const matches = getScheduleListMatches();
        const searchEl = document.getElementById('schedules-search');
        const filter = (searchEl && searchEl.value || '').trim();
        if (hint) hint.textContent = filter ? 'search matches' : 'listed schedules';
        const selectable = matches.others.slice();
        const totalSelectable = selectable.length + (matches.showBulk ? 1 : 0);
        if (!totalSelectable) {
            cb.checked = false;
            cb.indeterminate = false;
            cb.disabled = true;
            return;
        }
        cb.disabled = false;
        let selectedCount = 0;
        selectable.forEach(s => {
            if (pendingScheduleIds.has(s.id)) selectedCount += 1;
        });
        if (matches.showBulk && pendingBulkOwnWorkers) selectedCount += 1;
        cb.checked = selectedCount === totalSelectable;
        cb.indeterminate = selectedCount > 0 && selectedCount < totalSelectable;
    }

    window.toggleSelectAllCatalog = function (kind, checkbox) {
        if (kind !== 'schedules') return;
        const matches = getScheduleListMatches();
        const on = !!(checkbox && checkbox.checked);
        matches.others.forEach(s => {
            if (on) pendingScheduleIds.add(s.id);
            else pendingScheduleIds.delete(s.id);
        });
        if (matches.showBulk) pendingBulkOwnWorkers = on;
        renderCatalogList('schedules');
        updateScheduleActionsPanel();
    };

    function syncScriptSelectionFromDom(id) {
        const item = document.querySelector(`#scripts-list input.item-checkbox[data-id="${id}"]`);
        if (!item) return;
        if (!item.checked) {
            selectedScripts.delete(id);
            return;
        }
        selectedScripts.set(id, {
            can_run: !!document.querySelector(`#scripts-list input[name="script_${id}_run"]`)?.checked,
            can_update: !!document.querySelector(`#scripts-list input[name="script_${id}_update"]`)?.checked,
            can_delete: !!document.querySelector(`#scripts-list input[name="script_${id}_delete"]`)?.checked,
        });
    }

    function buildScriptItem(s, flags) {
        const checked = !!flags;
        const canRun = flags ? !!flags.can_run : true;
        const canUpdate = flags ? !!flags.can_update : false;
        const canDelete = flags ? !!flags.can_delete : false;
        const div = document.createElement('div');
        div.className = 'filter-item list-item';
        div.innerHTML = `
            <label class="item-label">
                <input type="checkbox" class="item-checkbox" data-id="${s.id}" value="${s.id}" ${checked ? 'checked' : ''}>
                <span class="item-text">${escapeHtml(scriptLabel(s))}</span>
            </label>
            <div class="sub-drawer" style="display: ${checked ? 'flex' : 'none'}; margin-top: 0.5rem; gap: 0.5rem;">
                <div class="sub-drawer-title">Access Level</div>
                <div class="pill-checkbox-group" style="margin-top: 0;">
                    <label class="pill-checkbox"><input type="checkbox" name="script_${s.id}_run" value="1" ${canRun ? 'checked' : ''}> Run</label>
                    <label class="pill-checkbox"><input type="checkbox" name="script_${s.id}_update" value="1" ${canUpdate ? 'checked' : ''}> Update</label>
                    <label class="pill-checkbox"><input type="checkbox" name="script_${s.id}_delete" value="1" ${canDelete ? 'checked' : ''}> Delete</label>
                </div>
            </div>`;
        const cb = div.querySelector('.item-checkbox');
        const drawer = div.querySelector('.sub-drawer');
        const runCb = div.querySelector(`input[name="script_${s.id}_run"]`);
        const updCb = div.querySelector(`input[name="script_${s.id}_update"]`);
        cb.addEventListener('change', () => {
            drawer.style.display = cb.checked ? 'flex' : 'none';
            if (cb.checked && !runCb.checked && !updCb.checked) runCb.checked = true;
            syncScriptSelectionFromDom(s.id);
        });
        updCb.addEventListener('change', () => {
            if (updCb.checked) runCb.checked = true;
            syncScriptSelectionFromDom(s.id);
        });
        runCb.addEventListener('change', () => syncScriptSelectionFromDom(s.id));
        div.querySelector(`input[name="script_${s.id}_delete"]`).addEventListener('change', () => syncScriptSelectionFromDom(s.id));
        return div;
    }

    function buildScheduleGrantedNote(flags) {
        if (!grantHasActions(flags)) return '';
        const text = formatGrantedActions(flags);
        if (!text) return '';
        return `<div class="schedule-granted-note"><span class="schedule-granted-badge">Granted</span> ${escapeHtml(text)}</div>`;
    }

    function buildScheduleItem(s, opts) {
        const inGroup = !!(opts && opts.inGroup);
        const pending = pendingScheduleIds.has(s.id);
        const granted = grantHasActions(grantedSchedules.get(s.id)) ? grantedSchedules.get(s.id) : null;
        const div = document.createElement('div');
        div.className = 'filter-item list-item' + (inGroup ? ' schedule-in-group' : '');
        div.innerHTML = `
            <label class="item-label">
                <input type="checkbox" class="item-checkbox" data-id="${s.id}" value="${s.id}" ${pending ? 'checked' : ''}>
                <span class="item-text">${escapeHtml(inGroup ? scheduleLabelInGroup(s) : scheduleLabel(s))}</span>
            </label>
            ${buildScheduleGrantedNote(granted)}`;
        const cb = div.querySelector('.item-checkbox');
        cb.addEventListener('change', () => {
            if (cb.checked) {
                pendingScheduleIds.add(s.id);
                if (granted) writeSharedScheduleActions(granted);
            } else {
                pendingScheduleIds.delete(s.id);
            }
            syncSelectAllCheckbox('schedules');
            updateScheduleActionsPanel();
        });
        return div;
    }

    function buildBulkOwnWorkersItem(ownSchedules) {
        const n = ownSchedules.length;
        const grantedOwn = ownSchedules.filter(s => grantHasActions(grantedSchedules.get(s.id)));
        let grantedNote = '';
        if (grantedOwn.length) {
            const sample = grantedSchedules.get(grantedOwn[0].id);
            const allSame = grantedOwn.every(s => {
                const f = grantedSchedules.get(s.id);
                if (!f || !sample) return false;
                return Boolean(f.can_enable) === Boolean(sample.can_enable)
                    && Boolean(f.can_run) === Boolean(sample.can_run)
                    && Boolean(f.can_edit) === Boolean(sample.can_edit)
                    && Boolean(f.can_duplicate) === Boolean(sample.can_duplicate)
                    && Boolean(f.can_delete) === Boolean(sample.can_delete);
            });
            const actions = allSame ? formatGrantedActions(sample) : 'mixed actions';
            grantedNote = `<div class="schedule-granted-note"><span class="schedule-granted-badge">Granted</span> ${grantedOwn.length} of ${n}: ${escapeHtml(actions)}</div>`;
        }
        const div = document.createElement('div');
        div.className = 'filter-item list-item schedule-bulk-own';
        div.innerHTML = `
            <label class="item-label">
                <input type="checkbox" class="item-checkbox" data-bulk-own="1" ${pendingBulkOwnWorkers ? 'checked' : ''}>
                <span class="item-text">All schedules on this user’s workers (${n})</span>
            </label>
            <div class="sec-desc" style="margin:0.35rem 0 0 1.65rem;">One action set applies to every schedule on workers they own (and schedules they created).</div>
            ${grantedNote}`;
        const cb = div.querySelector('.item-checkbox');
        cb.addEventListener('change', () => {
            pendingBulkOwnWorkers = !!cb.checked;
            syncSelectAllCheckbox('schedules');
            updateScheduleActionsPanel();
        });
        return div;
    }

    function ownerInitial(name) {
        const t = String(name || '?').trim();
        return (t.charAt(0) || '?').toUpperCase();
    }

    function buildOwnerGroup(group, opts) {
        const forceExpand = !!(opts && opts.forceExpand);
        const hasGrant = ownerGroupHasActiveGrant(group);
        if (hasGrant || forceExpand) expandedScheduleOwnerIds.add(group.userId);
        const expanded = expandedScheduleOwnerIds.has(group.userId);
        const grantedCount = group.schedules.filter((s) => grantHasActions(grantedSchedules.get(s.id))).length;
        const wrap = document.createElement('div');
        wrap.className = 'schedule-owner-group' + (expanded ? ' is-expanded' : '') + (hasGrant ? ' has-grant' : '');
        wrap.setAttribute('data-owner-id', String(group.userId));

        const head = document.createElement('button');
        head.type = 'button';
        head.className = 'schedule-owner-toggle';
        head.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        const metaText = grantedCount
            ? `${grantedCount} granted · ${group.schedules.length}`
            : `${group.schedules.length} schedule${group.schedules.length === 1 ? '' : 's'}`;
        head.innerHTML = `
            <span class="schedule-owner-chevron" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M9 6l6 6-6 6"/>
                </svg>
            </span>
            <span class="schedule-owner-avatar" aria-hidden="true">${escapeHtml(ownerInitial(group.username))}</span>
            <span class="schedule-owner-name">${escapeHtml(group.username)}</span>
            <span class="schedule-owner-meta">${escapeHtml(metaText)}</span>
        `;

        const body = document.createElement('div');
        body.className = 'schedule-owner-body';
        body.hidden = !expanded;
        const ordered = group.schedules.slice().sort((a, b) => {
            const ga = grantHasActions(grantedSchedules.get(a.id)) ? 0 : 1;
            const gb = grantHasActions(grantedSchedules.get(b.id)) ? 0 : 1;
            if (ga !== gb) return ga - gb;
            return Number(a.id) - Number(b.id);
        });
        ordered.forEach((s) => body.appendChild(buildScheduleItem(s, { inGroup: true })));

        head.addEventListener('click', () => {
            const next = !expandedScheduleOwnerIds.has(group.userId);
            if (next) expandedScheduleOwnerIds.add(group.userId);
            else expandedScheduleOwnerIds.delete(group.userId);
            wrap.classList.toggle('is-expanded', next);
            body.hidden = !next;
            head.setAttribute('aria-expanded', next ? 'true' : 'false');
        });

        wrap.appendChild(head);
        wrap.appendChild(body);
        return wrap;
    }

    function syncFolderSelectionFromDom(id) {
        const item = document.querySelector(`#folders-list input.item-checkbox[data-id="${id}"]`);
        if (!item) return;
        if (!item.checked) {
            selectedFolders.delete(id);
            return;
        }
        selectedFolders.set(id, {
            can_edit: !!document.querySelector(`#folders-list input[name="folder_${id}_edit"]`)?.checked,
            can_enable: !!document.querySelector(`#folders-list input[name="folder_${id}_enable"]`)?.checked,
            can_run: !!document.querySelector(`#folders-list input[name="folder_${id}_run"]`)?.checked,
            can_delete: !!document.querySelector(`#folders-list input[name="folder_${id}_delete"]`)?.checked,
        });
    }

    function buildFolderItem(f, flags) {
        const checked = !!flags;
        const canEdit = flags ? !!flags.can_edit : false;
        const canEnable = flags ? !!flags.can_enable : false;
        const canRun = flags ? !!flags.can_run : true;
        const canDelete = flags ? !!flags.can_delete : false;
        const div = document.createElement('div');
        div.className = 'filter-item list-item';
        div.innerHTML = `
            <label class="item-label">
                <input type="checkbox" class="item-checkbox" data-id="${f.id}" value="${f.id}" ${checked ? 'checked' : ''}>
                <span class="item-text">${escapeHtml(folderLabel(f))}</span>
            </label>
            <div class="sub-drawer" style="display: ${checked ? 'flex' : 'none'}; margin-top: 0.5rem; gap: 0.5rem;">
                <div class="sub-drawer-title">Folder Access</div>
                <div class="pill-checkbox-group" style="margin-top: 0; flex-wrap: wrap;">
                    <label class="pill-checkbox"><input type="checkbox" name="folder_${f.id}_edit" value="1" ${canEdit ? 'checked' : ''}> Edit Folder</label>
                    <label class="pill-checkbox"><input type="checkbox" name="folder_${f.id}_enable" value="1" ${canEnable ? 'checked' : ''}> Enable / Disable Folder</label>
                    <label class="pill-checkbox"><input type="checkbox" name="folder_${f.id}_run" value="1" ${canRun ? 'checked' : ''}> Run Folder</label>
                    <label class="pill-checkbox"><input type="checkbox" name="folder_${f.id}_delete" value="1" ${canDelete ? 'checked' : ''}> Delete Folder</label>
                </div>
            </div>`;
        const cb = div.querySelector('.item-checkbox');
        const drawer = div.querySelector('.sub-drawer');
        cb.addEventListener('change', () => {
            drawer.style.display = cb.checked ? 'flex' : 'none';
            syncFolderSelectionFromDom(f.id);
        });
        drawer.querySelectorAll('input[type="checkbox"]').forEach(inp => {
            inp.addEventListener('change', () => syncFolderSelectionFromDom(f.id));
        });
        return div;
    }

    function renderCatalogList(kind) {
        const isScripts = kind === 'scripts';
        const isFolders = kind === 'folders';
        const isSchedules = !isScripts && !isFolders;
        const listEl = document.getElementById(isScripts ? 'scripts-list' : (isFolders ? 'folders-list' : 'schedules-list'));
        const searchEl = document.getElementById(isScripts ? 'scripts-search' : (isFolders ? 'folders-search' : 'schedules-search'));
        const countEl = document.getElementById(isScripts ? 'scripts-count-label' : (isFolders ? 'folders-count-label' : 'schedules-count-label'));
        if (!listEl) return;
        const filter = (searchEl && searchEl.value || '').trim().toLowerCase();

        if (isSchedules) {
            const matches = getScheduleListMatches();
            const own = getOwnWorkerSchedules(currentPermUserId);
            const grantedN = [...grantedSchedules.values()].filter(grantHasActions).length;
            const pendingN = pendingScheduleIds.size + (pendingBulkOwnWorkers ? 1 : 0);
            if (countEl) {
                countEl.textContent = `(${schedulesCatalog.length.toLocaleString()} total` +
                    (grantedN ? `, ${grantedN} granted` : '') +
                    (pendingN ? `, ${pendingN} selected` : '') + ')';
            }
            let others = matches.others;
            let truncated = false;
            if (others.length > LIST_LIMIT) {
                truncated = true;
                others = others.slice(0, LIST_LIMIT);
            }
            const ownerGroups = groupSchedulesByOwner(others);
            listEl.innerHTML = '';
            const frag = document.createDocumentFragment();
            if (matches.showBulk) {
                frag.appendChild(buildBulkOwnWorkersItem(own));
            }
            ownerGroups.forEach((g) => frag.appendChild(buildOwnerGroup(g, { forceExpand: !!matches.filter })));
            if (!matches.showBulk && !ownerGroups.length) {
                const emptyMsg = matches.filter
                    ? 'No matches. Try another search.'
                    : (currentPermUserId
                        ? 'No schedules from other users.'
                        : 'Select a user to manage schedule access.');
                listEl.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-sub);">${emptyMsg}</div>`;
            } else {
                if (truncated) {
                    const note = document.createElement('div');
                    note.className = 'empty-state';
                    note.style.cssText = 'padding:0.75rem;color:var(--text-sub);font-size:0.8rem;';
                    note.textContent = `Showing first ${LIST_LIMIT} schedules — refine search to narrow results.`;
                    frag.appendChild(note);
                }
                listEl.appendChild(frag);
            }
            syncSelectAllCheckbox('schedules');
            updateScheduleActionsPanel();
            return;
        }

        const catalog = isScripts ? scriptsCatalog : foldersCatalog;
        const selected = isScripts ? selectedScripts : selectedFolders;
        const byId = isScripts ? scriptsById : foldersById;
        const labelFn = isScripts ? scriptLabel : folderLabel;

        if (countEl) {
            countEl.textContent = `(${catalog.length.toLocaleString()} total` +
                (selected.size ? `, ${selected.size} selected` : '') + ')';
        }

        let rows = [];
        let truncated = false;
        if (filter) {
            const allMatches = catalog.filter(s => labelFn(s).toLowerCase().includes(filter));
            truncated = allMatches.length > LIST_LIMIT;
            rows = allMatches.slice(0, LIST_LIMIT);
        } else {
            const selectedRows = [];
            selected.forEach((_, id) => {
                const s = byId.get(id);
                if (s) selectedRows.push(s);
            });
            const selectedIds = new Set(selectedRows.map(s => s.id));
            const rest = catalog.filter(s => !selectedIds.has(s.id)).slice(0, Math.max(0, LIST_LIMIT - selectedRows.length));
            rows = selectedRows.concat(rest);
        }

        listEl.innerHTML = '';
        if (!rows.length) {
            listEl.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-sub);">${filter ? 'No matches. Try another search.' : 'No items.'}</div>`;
            return;
        }
        const frag = document.createDocumentFragment();
        rows.forEach(s => {
            const flags = selected.get(s.id) || null;
            frag.appendChild(isScripts ? buildScriptItem(s, flags) : buildFolderItem(s, flags));
        });
        if (truncated) {
            const note = document.createElement('div');
            note.className = 'empty-state';
            note.style.cssText = 'padding:0.75rem;color:var(--text-sub);font-size:0.8rem;';
            note.textContent = `Showing first ${LIST_LIMIT} matches — refine search to narrow results.`;
            frag.appendChild(note);
        }
        listEl.appendChild(frag);
    }

    window.filterCatalogList = function (kind) {
        renderCatalogList(kind);
    };

    function flushSelectionsToForm() {
        const scriptsPayload = document.getElementById('scripts-selected-payload');
        const schedulesPayload = document.getElementById('schedules-selected-payload');
        const foldersPayload = document.getElementById('folders-selected-payload');
        if (!scriptsPayload || !schedulesPayload) return;
        scriptsPayload.innerHTML = '';
        schedulesPayload.innerHTML = '';
        if (foldersPayload) foldersPayload.innerHTML = '';

        selectedScripts.forEach((flags, id) => {
            const add = (name, val) => {
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = name;
                inp.value = val;
                scriptsPayload.appendChild(inp);
            };
            add('scripts', String(id));
            if (flags.can_run) add(`script_${id}_run`, '1');
            if (flags.can_update) add(`script_${id}_update`, '1');
            if (flags.can_delete) add(`script_${id}_delete`, '1');
        });

        // Preserve existing schedule grants with actions; apply panel flags to pending selection.
        const finalSchedules = new Map();
        grantedSchedules.forEach((flags, id) => {
            if (grantHasActions(flags)) finalSchedules.set(id, { ...flags });
        });
        const panelFlags = readSharedScheduleActions();
        const applyPanel = (id) => {
            finalSchedules.set(Number(id), { ...panelFlags });
        };
        pendingScheduleIds.forEach(applyPanel);
        if (pendingBulkOwnWorkers) {
            getOwnWorkerSchedules(currentPermUserId).forEach(s => applyPanel(s.id));
        }
        finalSchedules.forEach((flags, id) => {
            const add = (name, val) => {
                const inp = document.createElement('input');
                inp.type = 'hidden';
                inp.name = name;
                inp.value = val;
                schedulesPayload.appendChild(inp);
            };
            add('schedules', String(id));
            if (flags.can_enable) add(`schedule_${id}_enable`, '1');
            if (flags.can_run) add(`schedule_${id}_run`, '1');
            if (flags.can_edit) add(`schedule_${id}_edit`, '1');
            if (flags.can_duplicate) add(`schedule_${id}_duplicate`, '1');
            if (flags.can_delete) add(`schedule_${id}_delete`, '1');
        });

        if (foldersPayload) {
            selectedFolders.forEach((flags, id) => {
                const add = (name, val) => {
                    const inp = document.createElement('input');
                    inp.type = 'hidden';
                    inp.name = name;
                    inp.value = val;
                    foldersPayload.appendChild(inp);
                };
                add('folders', String(id));
                if (flags.can_edit) add(`folder_${id}_edit`, '1');
                if (flags.can_enable) add(`folder_${id}_enable`, '1');
                if (flags.can_run) add(`folder_${id}_run`, '1');
                if (flags.can_delete) add(`folder_${id}_delete`, '1');
            });
        }

        document.querySelectorAll('#scripts-list input, #schedules-list input, #folders-list input, #schedule-actions-panel input').forEach(cb => {
            cb.disabled = true;
        });
    }

    function updateWorkersCount() {
        const el = document.getElementById('workers-count-label');
        if (!el) return;
        const n = document.querySelectorAll('#workers-list input[name="workers"]:checked').length;
        el.textContent = n ? `(${n} selected)` : '';
    }

    function updateUserMeta(userId, isAdmin, label) {
        const form = document.getElementById('assign-permissions-form');
        const meta = document.getElementById('perm-user-meta');
        if (form) {
            form.classList.toggle('perm-is-admin', !!isAdmin);
            form.classList.toggle('perm-user-ready', !!userId);
        }
        if (!meta) return;
        if (!userId) {
            meta.textContent = 'Select a user to load their current grants. Changes apply only after you save.';
            return;
        }
        const chip = isAdmin
            ? '<span class="perm-chip is-admin">Admin</span>'
            : '<span class="perm-chip is-user">User</span>';
        meta.innerHTML = isAdmin
            ? `${chip} <strong>${escapeHtml(label)}</strong> already has full access. Saving is usually not needed.`
            : `${chip} Editing grants for <strong>${escapeHtml(label)}</strong>. Search, tick items, then save.`;
    }

    window.onWorkerAccessToggle = function (cb, drawerId) {
        const drawer = document.getElementById(drawerId);
        if (drawer) drawer.style.display = cb.checked ? 'block' : 'none';
        updateWorkersCount();
        if (!cb.checked) return;

        const userSelect = document.getElementById('user-select');
        const userId = parseInt(userSelect && userSelect.value, 10);
        const ownerId = parseInt(cb.getAttribute('data-owner-id') || '', 10);
        const wn = cb.value;
        const runCb = document.querySelector(`input[name="worker_${wn}_run"]`);
        // Own worker: default Run Script on when granting access (admin can still uncheck)
        if (runCb && userId && ownerId === userId) {
            runCb.checked = true;
        }
    };

    window.filterGrantTables = function (input) {
        const q = (input && input.value || '').toLowerCase().trim();
        document.querySelectorAll('.perm-grant-table:not(#schedule-grants-table) tbody tr').forEach(tr => {
            if (tr.querySelector('.empty-state')) return;
            tr.style.display = !q || (tr.textContent || '').toLowerCase().includes(q) ? '' : 'none';
        });
        if (typeof window.setScheduleGrantsFilter === 'function') {
            window.setScheduleGrantsFilter(q, { fromGlobal: true });
        }
    };

    function initScheduleGrantsPagination() {
        const tbody = document.getElementById('schedule-grants-tbody');
        const pagination = document.getElementById('schedule-grants-pagination');
        const countLabel = document.getElementById('schedule-grants-count-label');
        const localSearch = document.getElementById('schedule-grants-search');
        const globalSearch = document.querySelector('#tab-active-grants .perm-grants-toolbar .field-input');
        if (!tbody || !pagination) return;

        const rows = Array.from(tbody.querySelectorAll('tr.schedule-grant-row'));
        const emptyRow = tbody.querySelector('tr.schedule-grants-empty-row');
        const emptyMsg = document.getElementById('schedule-grants-empty-msg');
        if (!rows.length) {
            pagination.style.display = 'none';
            if (countLabel) countLabel.textContent = '0 grants';
            return;
        }

        let currentPage = 1;
        let pageSize = 10;
        let query = '';

        const sizeSelect = pagination.querySelector('.page-size-select');
        if (sizeSelect) {
            pageSize = parseInt(sizeSelect.value, 10) || 10;
            sizeSelect.addEventListener('change', () => {
                pageSize = parseInt(sizeSelect.value, 10) || 10;
                currentPage = 1;
                render();
            });
        }

        function rowMatches(tr) {
            if (!query) return true;
            const hay = (tr.getAttribute('data-search') || tr.textContent || '').toLowerCase();
            return hay.includes(query);
        }

        function pageWindow(totalPages) {
            const pages = [];
            if (totalPages <= 7) {
                for (let i = 1; i <= totalPages; i++) pages.push(i);
                return pages;
            }
            pages.push(1);
            const start = Math.max(2, currentPage - 1);
            const end = Math.min(totalPages - 1, currentPage + 1);
            if (start > 2) pages.push('…');
            for (let i = start; i <= end; i++) pages.push(i);
            if (end < totalPages - 1) pages.push('…');
            pages.push(totalPages);
            return pages;
        }

        function render() {
            const filtered = rows.filter(rowMatches);
            const total = filtered.length;
            const totalPages = Math.max(1, Math.ceil(total / pageSize) || 1);
            if (currentPage > totalPages) currentPage = totalPages;

            rows.forEach(r => { r.style.display = 'none'; });
            if (emptyRow) emptyRow.style.display = total === 0 ? '' : 'none';

            if (total === 0) {
                pagination.style.display = 'none';
                if (emptyRow) emptyRow.style.display = '';
                if (emptyMsg) {
                    emptyMsg.textContent = query
                        ? 'No matching schedule grants.'
                        : 'No extra schedule grants active.';
                }
                if (countLabel) countLabel.textContent = query ? '0 matches' : '0 grants';
                return;
            }

            const startIdx = (currentPage - 1) * pageSize;
            const endIdx = Math.min(startIdx + pageSize, total);
            filtered.slice(startIdx, endIdx).forEach(r => { r.style.display = ''; });

            pagination.style.display = 'flex';
            pagination.querySelector('.page-start').textContent = String(startIdx + 1);
            pagination.querySelector('.page-end').textContent = String(endIdx);
            pagination.querySelector('.page-total').textContent = String(total);
            if (countLabel) {
                countLabel.textContent = query
                    ? `${total} match${total === 1 ? '' : 'es'}`
                    : `${total} grant${total === 1 ? '' : 's'}`;
            }

            const controls = pagination.querySelector('.pagination-controls');
            controls.innerHTML = '';
            const prev = document.createElement('button');
            prev.type = 'button';
            prev.className = 'pagination-btn';
            prev.textContent = '‹';
            prev.disabled = currentPage <= 1;
            prev.addEventListener('click', () => { currentPage -= 1; render(); });
            controls.appendChild(prev);

            pageWindow(totalPages).forEach(p => {
                if (p === '…') {
                    const dots = document.createElement('span');
                    dots.className = 'pagination-dots';
                    dots.textContent = '…';
                    controls.appendChild(dots);
                    return;
                }
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = `pagination-btn${p === currentPage ? ' active' : ''}`;
                btn.textContent = String(p);
                btn.addEventListener('click', () => { currentPage = p; render(); });
                controls.appendChild(btn);
            });

            const next = document.createElement('button');
            next.type = 'button';
            next.className = 'pagination-btn';
            next.textContent = '›';
            next.disabled = currentPage >= totalPages;
            next.addEventListener('click', () => { currentPage += 1; render(); });
            controls.appendChild(next);
        }

        window.setScheduleGrantsFilter = function (q, opts) {
            query = String(q || '').toLowerCase().trim();
            currentPage = 1;
            if (!opts || !opts.fromGlobal) {
                // keep global toolbar in sync when typing in section search
                if (globalSearch && document.activeElement === localSearch) {
                    /* leave global as-is to avoid fighting dual inputs */
                }
            } else if (localSearch && document.activeElement !== localSearch) {
                localSearch.value = q || '';
            }
            render();
        };

        localSearch?.addEventListener('input', () => {
            window.setScheduleGrantsFilter(localSearch.value, { fromGlobal: false });
        });

        render();
    }

    initScheduleGrantsPagination();

    function resetSharedScheduleActionsPanel() {
        const enable = document.getElementById('sch-act-enable');
        const run = document.getElementById('sch-act-run');
        const edit = document.getElementById('sch-act-edit');
        const dup = document.getElementById('sch-act-duplicate');
        const del = document.getElementById('sch-act-delete');
        if (enable) enable.checked = false;
        if (run) run.checked = true;
        if (edit) edit.checked = false;
        if (dup) dup.checked = false;
        if (del) del.checked = false;
        updateScheduleActionsPanel();
    }

    window.populatePermissions = function () {
        const userSelect = document.getElementById('user-select');
        const userId = parseInt(userSelect.value, 10);
        currentPermUserId = userId || 0;
        grantedSchedules.clear();
        pendingScheduleIds.clear();
        pendingBulkOwnWorkers = false;
        expandedScheduleOwnerIds.clear();
        resetSharedScheduleActionsPanel();

        if (!userId) {
            updateUserMeta(null, false, '');
            const daysCb = document.getElementById('can-set-days-cb');
            if (daysCb) {
                daysCb.checked = false;
                daysCb.disabled = false;
            }
            const daysHint = document.getElementById('can-set-days-admin-hint');
            if (daysHint) daysHint.style.display = 'none';
            renderCatalogList('schedules');
            return;
        }

        const selectedOption = userSelect.options[userSelect.selectedIndex];
        const isAdmin = selectedOption.getAttribute('data-role') === 'admin';
        updateUserMeta(userId, isAdmin, selectedOption.textContent || '');

        document.querySelectorAll('#workers-list input[type="checkbox"]').forEach(cb => {
            cb.checked = false;
            cb.disabled = false;
        });
        document.querySelectorAll('#workers-list .sub-drawer').forEach(drawer => {
            drawer.style.display = 'none';
        });
        document.querySelectorAll('#workers-list input[type="text"]').forEach(inp => { inp.value = ''; });
        // Clear worker permission pills
        document.querySelectorAll('#workers-list .pill-checkbox input[type="checkbox"]').forEach(cb => {
            cb.checked = false;
        });

        selectedScripts.clear();
        selectedFolders.clear();
        populateSchedulerViewUsers(userId, isAdmin);
        populateCanSetDays(userId, isAdmin);

        if (isAdmin) {
            document.querySelectorAll('#workers-list input[type="checkbox"]').forEach(cb => {
                cb.checked = true;
                cb.disabled = true;
            });
            document.querySelectorAll('#workers-list .sub-drawer').forEach(drawer => {
                drawer.style.display = 'block';
            });
            renderCatalogList('scripts');
            renderCatalogList('schedules');
            renderCatalogList('folders');
            updateWorkersCount();
            return;
        }

        scriptAccessData.filter(a => sameUserId(a.user_id, userId)).forEach(access => {
            selectedScripts.set(Number(access.script_id), {
                can_run: flagOn(access.can_run),
                can_update: flagOn(access.can_update),
                can_delete: flagOn(access.can_delete),
            });
        });
        scheduleAccessData.filter(a => sameUserId(a.user_id, userId)).forEach(access => {
            const flags = {
                can_enable: flagOn(access.can_enable) || flagOn(access.can_disable),
                can_run: flagOn(access.can_run),
                can_edit: flagOn(access.can_edit),
                can_duplicate: flagOn(access.can_duplicate),
                can_delete: flagOn(access.can_delete),
            };
            // Skip view-only rows (no action flags) — do not show or preserve them.
            if (!grantHasActions(flags)) return;
            grantedSchedules.set(Number(access.schedule_id), flags);
        });
        writeSharedScheduleActions(unionGrantedActions());
        folderAccessData.filter(a => sameUserId(a.user_id, userId)).forEach(access => {
            selectedFolders.set(Number(access.folder_id), {
                can_edit: flagOn(access.can_edit),
                can_enable: flagOn(access.can_enable) || flagOn(access.can_disable),
                can_run: flagOn(access.can_run),
                can_delete: flagOn(access.can_delete),
            });
        });

        renderCatalogList('scripts');
        renderCatalogList('schedules');
        renderCatalogList('folders');

        const grantedWorkers = new Set();
        pcAccessData.filter(a => a.user_id === userId).forEach(access => {
            const pcCb = document.querySelector(`input[name="workers"][value="${access.worker_name}"]`);
            if (!pcCb) return;
            pcCb.checked = true;
            const drawerId = pcCb.closest('.filter-item')?.querySelector('.sub-drawer')?.id;
            if (drawerId) {
                const drawer = document.getElementById(drawerId);
                if (drawer) drawer.style.display = 'block';
            }
            grantedWorkers.add(access.worker_name);

            const setVal = (sel, val) => { const el = document.querySelector(sel); if (el) el.value = val || ''; };
            const setChk = (sel, on) => { const el = document.querySelector(sel); if (el) el.checked = !!on; };
            const wn = access.worker_name;
            setVal(`input[name="worker_${wn}_paths"]`, access.allowed_paths);
            setVal(`input[name="worker_${wn}_exts"]`, access.allowed_extensions);
            setChk(`input[name="worker_${wn}_create_folder"]`, access.can_create_folder == 1);
            setChk(`input[name="worker_${wn}_rename_folder"]`, access.can_rename_folder == 1);
            setChk(`input[name="worker_${wn}_delete_folder"]`, access.can_delete_folder == 1);
            setChk(`input[name="worker_${wn}_update_file"]`, access.can_update_file == 1);
            setChk(`input[name="worker_${wn}_create_file"]`, access.can_create_file == 1);
            setChk(`input[name="worker_${wn}_run"]`, access.can_run == 1);
            setChk(`input[name="worker_${wn}_delete_file"]`, access.can_delete_file == 1);
            setChk(`input[name="worker_${wn}_rename_file"]`, access.can_rename_file == 1);
            setChk(`input[name="worker_${wn}_edit_file"]`, access.can_edit_file == 1);
            setChk(`input[name="worker_${wn}_access_all_files"]`, access.can_access_all_files == 1);
        });

        // Own workers with no PC grant yet: select them and default Run Script on
        document.querySelectorAll('#workers-list input[name="workers"]').forEach(pcCb => {
            const ownerId = parseInt(pcCb.getAttribute('data-owner-id') || '', 10);
            if (ownerId !== userId || grantedWorkers.has(pcCb.value)) return;
            pcCb.checked = true;
            const drawer = pcCb.closest('.filter-item')?.querySelector('.sub-drawer');
            if (drawer) drawer.style.display = 'block';
            const runCb = document.querySelector(`input[name="worker_${pcCb.value}_run"]`);
            if (runCb) runCb.checked = true;
        });
        updateWorkersCount();
    };

    window.filterSchedulerViewUsers = function (input) {
        const filter = (input && input.value || '').toLowerCase();
        const userSelect = document.getElementById('user-select');
        const selfId = parseInt(userSelect && userSelect.value, 10);
        document.querySelectorAll('.scheduler-view-user-row').forEach(row => {
            const id = Number(row.getAttribute('data-user-id'));
            if (id === selfId) {
                row.style.display = 'none';
                return;
            }
            const text = (row.querySelector('.item-text')?.textContent || '').toLowerCase();
            row.style.display = !filter || text.includes(filter) ? '' : 'none';
        });
    };

    function populateCanSetDays(userId, isAdmin) {
        const cb = document.getElementById('can-set-days-cb');
        const hint = document.getElementById('can-set-days-admin-hint');
        if (!cb) return;
        if (isAdmin) {
            cb.checked = true;
            cb.disabled = true;
            if (hint) hint.style.display = '';
            return;
        }
        cb.disabled = false;
        if (hint) hint.style.display = 'none';
        const key = String(userId);
        cb.checked = Number(usersCanSetDaysData[key] || 0) === 1;
    }

    function populateSchedulerViewUsers(userId, isAdmin) {
        const granted = new Set(
            schedulerViewAccessData
                .filter(a => Number(a.viewer_user_id) === userId)
                .map(a => Number(a.target_user_id))
        );
        const rows = document.querySelectorAll('.scheduler-view-user-row');
        let visible = 0;
        rows.forEach(row => {
            const id = Number(row.getAttribute('data-user-id'));
            const cb = row.querySelector('input[type="checkbox"]');
            const hideSelf = id === userId;
            row.style.display = hideSelf ? 'none' : '';
            if (!cb) return;
            cb.disabled = !!isAdmin;
            cb.checked = !isAdmin && granted.has(id);
            if (!hideSelf) visible += 1;
        });
        const hint = document.getElementById('scheduler-view-users-admin-hint');
        if (hint) hint.style.display = isAdmin ? '' : 'none';
        const countEl = document.getElementById('scheduler-view-users-count-label');
        if (countEl) {
            const selected = [...rows].filter(row => {
                const cb = row.querySelector('input[type="checkbox"]');
                return row.style.display !== 'none' && cb && cb.checked;
            }).length;
            countEl.textContent = `(${visible} users` + (selected ? `, ${selected} selected` : '') + ')';
        }
    }

    document.getElementById('assign-permissions-form')?.addEventListener('submit', flushSelectionsToForm);

    function setScriptsLoading(msg) {
        const listEl = document.getElementById('scripts-list');
        const countEl = document.getElementById('scripts-count-label');
        if (listEl) listEl.innerHTML = `<div class="empty-state" style="padding:1rem;color:var(--text-sub);">${msg}</div>`;
        if (countEl) countEl.textContent = '';
    }

    async function loadScriptsCatalog() {
        setScriptsLoading('Loading scripts…');
        try {
            const res = await fetch('/api/permissions/catalog', { credentials: 'same-origin' });
            if (!res.ok) throw new Error('catalog ' + res.status);
            const data = await res.json();
            scriptsCatalog = (data.scripts || []).map(r => ({
                id: Number(r[0]), worker_name: r[1], script_name: r[2], username: r[3] || ''
            })).filter(s => {
                const name = String(s.script_name || '').toLowerCase();
                return name.endsWith('.py');
            });
            scriptsById = new Map(scriptsCatalog.map(s => [s.id, s]));
            renderCatalogList('scripts');
        } catch (e) {
            console.error(e);
            setScriptsLoading('Failed to load scripts catalog. Refresh the page.');
        }
    }

    renderCatalogList('schedules');
    renderCatalogList('folders');
    loadScriptsCatalog();

    try {
        const savedTab = sessionStorage.getItem('permissions-tab');
        if (savedTab && document.getElementById('tab-' + savedTab)) switchTab(savedTab);
    } catch (e) { /* ignore */ }
})();
