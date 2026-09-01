
document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('file-explorer-container');
    if (!container) return;

    const workerName = container.dataset.worker;
    const isAdmin = container.dataset.isAdmin === 'true';

    // State
    let expandedFolders = new Set(); // only explicitly expanded folder paths
    const childrenCache = {}; // path -> immediate children[]
    const loadingFolders = new Set();
    let currentSearch = '';
    let currentTypeFilter = '';
    let currentSort = 'name_asc';
    let filterStarred = false;
    let fetchInterval = null;
    let workerRoot = "";
    let currentNavPath = "";
    let clipboard = null; // { path, name, isFolder } for cut/paste
    let currentPerms = {
        is_admin: isAdmin,
        can_run: isAdmin,
        can_create_folder: isAdmin,
        can_rename_folder: isAdmin,
        can_delete_folder: isAdmin,
        can_update_file: isAdmin,
        can_create_file: isAdmin,
        can_delete_file: isAdmin,
        can_rename_file: isAdmin,
        can_edit_file: isAdmin
    };

    const syncStorageKey = `configSyncStart:${workerName}`;
    const fxStateKey = `fxNavState:${workerName}`;
    let pendingScrollRestore = null;
    let selectedFxPath = '';

    const showFxToast = (message, isSuccess = true) => {
        let toast = document.getElementById('fx-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'fx-toast';
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.style.borderLeftColor = isSuccess ? '#22c55e' : '#ef4444';
        toast.classList.add('show');
        clearTimeout(window._fxToastHide);
        window._fxToastHide = setTimeout(() => toast.classList.remove('show'), 3000);
    };

    const formatFolderLoadDuration = (seconds) => {
        const s = Math.max(0, Number(seconds) || 0);
        if (s < 60) {
            return (s < 10 ? s.toFixed(1) : String(Math.round(s))) + ' s';
        }
        const m = Math.floor(s / 60);
        const rem = Math.round(s - m * 60);
        if (m >= 60) {
            const h = Math.floor(m / 60);
            return `${h}h ${m % 60}m ${rem}s`;
        }
        return `${m}m ${rem}s`;
    };

    const clientSyncElapsedS = () => {
        const startMs = Number(sessionStorage.getItem(syncStorageKey) || 0);
        if (!startMs) return 0;
        return Math.max(0, (Date.now() - startMs) / 1000);
    };

    const renderFsStatsCard = ({ totalFiles, totalSize, loadLabel, statusLabel, statusDone }) => {
        const statsEl = document.getElementById('fs-stats-content');
        if (!statsEl) return;
        const sig = [
            Number(totalFiles || 0),
            Number(totalSize || 0),
            String(loadLabel || ''),
            String(statusLabel || ''),
            statusDone ? 1 : 0,
        ].join('|');
        // Avoid rewriting the card on every poll when nothing visible changed (stops UI flicker).
        if (window._fsStatsRenderSig === sig) return;
        window._fsStatsRenderSig = sig;
        const statusColor = statusDone ? '#22c55e' : 'var(--primary-color, #3b82f6)';
        const statusRow = statusLabel
            ? `<div style="display:flex; justify-content:space-between;"><span>Status:</span> <strong style="color:${statusColor}">${statusLabel}</strong></div>`
            : '';
        statsEl.innerHTML = `
            ${statusRow}
            <div style="display:flex; justify-content:space-between;"><span>Total Files:</span> <strong style="color:var(--text-color)">${Number(totalFiles || 0).toLocaleString()}</strong></div>
            <div style="display:flex; justify-content:space-between;"><span>Total Size:</span> <strong style="color:var(--text-color)">${formatBytes(totalSize || 0)}</strong></div>
            <div style="display:flex; justify-content:space-between;"><span>Load Time:</span> <strong style="color:var(--text-color)">${loadLabel}</strong></div>
        `;
    };

    const stopConfigSyncTimer = () => {
        if (window.configSyncTimerInterval) {
            clearInterval(window.configSyncTimerInterval);
            window.configSyncTimerInterval = null;
        }
        sessionStorage.removeItem(syncStorageKey);
        const timerEl = document.getElementById('config-sync-timer');
        if (timerEl) timerEl.style.display = 'none';
    };

    const markConfigSyncFinished = (elapsedS) => {
        window._configSyncPending = false;
        window._folderLoadFinished = true;
        const totalLabel = formatFolderLoadDuration(elapsedS);
        window._fsStatsLoadLabel = totalLabel;
        window._fsStatsLoaded = true;
        if (window.configSyncTimerInterval) {
            clearInterval(window.configSyncTimerInterval);
            window.configSyncTimerInterval = null;
        }
        const timerEl = document.getElementById('config-sync-timer');
        const strongEl = timerEl && timerEl.querySelector('strong');
        const elapsedEl = document.getElementById('config-elapsed');
        if (elapsedEl) elapsedEl.innerText = String(Math.round(elapsedS));
        if (strongEl) strongEl.textContent = '✓ Loading finished';
        const estimateEl = document.getElementById('config-sync-estimate');
        if (estimateEl) estimateEl.textContent = `Total load time: ${totalLabel}`;
        if (timerEl) {
            timerEl.style.borderLeftColor = '#22c55e';
            timerEl.style.display = 'block';
        }
        clearTimeout(window._configSyncFinishedHide);
        window._configSyncFinishedHide = setTimeout(() => {
            sessionStorage.removeItem(syncStorageKey);
            if (timerEl) timerEl.style.display = 'none';
        }, 10000);
    };

    const startConfigSyncTimer = (forceNew = false) => {
        const timerEl = document.getElementById('config-sync-timer');
        if (!timerEl) return;

        let startMs = Number(sessionStorage.getItem(syncStorageKey) || 0);
        if (forceNew || !startMs) {
            startMs = Date.now();
            sessionStorage.setItem(syncStorageKey, String(startMs));
        }

        timerEl.style.display = 'block';
        const estimateEl = document.getElementById('config-sync-estimate');
        if (estimateEl) {
            estimateEl.textContent = 'First sync of a large tree may take 1–3 minutes. Restarts and small path changes usually finish in a few seconds (incremental sync).';
        }

        if (window.configSyncTimerInterval) {
            clearInterval(window.configSyncTimerInterval);
            window.configSyncTimerInterval = null;
        }

        const tick = () => {
            const elapsed = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
            const elapsedEl = document.getElementById('config-elapsed');
            if (elapsedEl) elapsedEl.innerText = String(elapsed);
            if (window._configSyncPending) {
                const statsEl = document.getElementById('fs-stats-content');
                const loadStrong = statsEl && statsEl.querySelector('div:last-child strong');
                if (loadStrong) loadStrong.textContent = formatFolderLoadDuration(elapsed);
            }
        };
        tick();
        window.configSyncTimerInterval = setInterval(tick, 1000);
    };
    window.startConfigSyncTimer = startConfigSyncTimer;

    // Resume elapsed timer after reload only if a sync was already in progress
    (function resumeSyncTimerIfNeeded() {
        const raw = sessionStorage.getItem(syncStorageKey);
        if (!raw) return;
        const startMs = Number(raw);
        // Drop stale timers older than 30 minutes
        if (!startMs || Date.now() - startMs > 30 * 60 * 1000) {
            sessionStorage.removeItem(syncStorageKey);
            return;
        }
        window._configSyncPending = true;
        window._folderLoadFinished = false;
        startConfigSyncTimer(false);
    })();

    // Vibrant Native OS Icons
    const icons = {
        folder: `<svg width="22" height="22" viewBox="0 0 24 24" fill="#60A5FA" xmlns="http://www.w3.org/2000/svg"><path d="M2.25 6C2.25 4.75736 3.25736 3.75 4.5 3.75H9.81432C10.4287 3.75 11.0163 4.00161 11.4402 4.44525L13.1118 6.19475C13.3237 6.41641 13.6175 6.54045 13.9247 6.54045H19.5C20.7426 6.54045 21.75 7.54781 21.75 8.79045V18C21.75 19.2426 20.7426 20.25 19.5 20.25H4.5C3.25736 20.25 2.25 19.2426 2.25 18V6Z"/></svg>`,
        file: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#A1A1AA" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>`,
        chevronDown: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>`,
        chevronRight: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>`,
        addFolder: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path><line x1="12" y1="11" x2="12" y2="17"></line><line x1="9" y1="14" x2="15" y2="14"></line></svg>`,
        upload: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>`,
        edit: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>`,
        trash: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`,
        refresh: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>`,
        python: `<svg width="20" height="20" viewBox="0 0 128 128"><path fill="#3776ab" d="M64.36 5.54C30.93 5.54 32.1 19.9 32.1 19.9l-.04 14.53h32.7v4.6H29.56C13.24 39.03 10.3 54.4 11.53 66.86c1.55 15.65 9.07 24.32 24.7 25.43h10.9v-15.6s-1.04-14.9 14.28-14.9h26.46s14.8-1.55 14.8-15.02V28.3s1.5-13.84-14.8-19.6C79.8 5.86 71.9 5.54 64.36 5.54zm-14.1 9.9c2.6 0 4.72 2.1 4.72 4.72a4.73 4.73 0 0 1-4.73 4.7 4.73 4.73 0 0 1-4.7-4.7c0-2.6 2.1-4.72 4.7-4.72z"/><path fill="#ffd343" d="M64.08 122.42c33.43 0 32.26-14.36 32.26-14.36l.04-14.5h-32.7V88.94h35.2c16.32 0 19.26-15.35 18.03-27.8-1.55-15.66-9.07-24.33-24.7-25.44H81.3v15.6s1.04 14.9-14.28 14.9H40.56s-14.8 1.55-14.8 15v18.45s-1.5 13.85 14.8 19.6c8.06 2.84 15.96 3.16 23.5 3.16zm14.1-9.9c-2.6 0-4.72-2.1-4.72-4.7 0-2.6 2.1-4.7 4.7-4.7 2.6 0 4.73 2.1 4.73 4.7 0 2.6-2.12 4.7-4.73 4.7z"/></svg>`,
        javascript: `<svg width="20" height="20" viewBox="0 0 128 128"><path fill="#f7df1e" d="M1 1h126v126H1z"/><path fill="#000" d="M38.85 99.63c3.08 5.3 7.82 8.6 15.1 8.6 7.43 0 11.23-3.23 11.23-11.4v-40.4h11.96v41c0 13.63-9.53 21.6-23.72 21.6-11.45 0-19.46-6.07-24.36-15.5l9.78-3.9zm50.6 6.8c-5.83 0-10.05-2.73-12.8-7.23l9.8-4.4c1.86 3.32 4.54 5 7.6 5 3.4 0 5.4-1.6 5.4-3.9 0-2.58-1.85-3.57-7.98-6.17-10.3-4.32-15.82-8.35-15.82-16.7 0-8.98 7-15 17-15 7.03 0 11.3 2.37 14.36 6.8l-9.35 4.9c-1.65-2.56-3.8-3.96-6.17-3.96-2.75 0-4.5 1.5-4.5 3.4 0 2.3 1.9 3.2 8.04 5.9 10.55 4.5 15.65 8.7 15.65 16.9 0 9.8-7.53 15.2-18.02 15.2z"/></svg>`,
        html: `<svg width="20" height="20" viewBox="0 0 128 128"><path fill="#e34f26" d="M11.9 10.4L20.8 110l43.1 12 43.1-12L116.1 10.4z"/><path fill="#ef652a" d="M64 112.5l34.4-9.6L104.9 21.6H64z"/><path fill="#fff" d="M64 56.4h22.9l-1.6 17.5-21.3 5.8-21.3-5.8-1.4-15h11l.7 7.7 11 3.1 11-3.1.6-7H30l-2.4-26.6h58l-1.1 12H31z"/></svg>`,
        css: `<svg width="20" height="20" viewBox="0 0 128 128"><path fill="#1572b6" d="M11.9 10.4L20.8 110l43.1 12 43.1-12L116.1 10.4z"/><path fill="#33a9dc" d="M64 112.5l34.4-9.6L104.9 21.6H64z"/><path fill="#fff" d="M64 56.4h22.9l-1.6 17.5-21.3 5.8-21.3-5.8-1.4-15h11l.7 7.7 11 3.1 11-3.1.6-7H30l-2.4-26.6h58l-1.1 12H31z"/></svg>`,
        json: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fcd34d" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path><polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline><line x1="12" y1="22.08" x2="12" y2="12"></line></svg>`,
        image: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>`,
        video: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>`,
        audio: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>`,
        zip: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V3h13l5 5z"></path><line x1="10" y1="3" x2="10" y2="8"></line><line x1="8" y1="8" x2="8" y2="13"></line><line x1="12" y1="8" x2="12" y2="13"></line><line x1="10" y1="13" x2="10" y2="18"></line></svg>`
    };

    // Initialize UI Layout
    container.innerHTML = `
        <div class="file-explorer">
            <div class="file-explorer-toolbar">
                <div class="file-explorer-actions">
                    <button class="fx-btn" id="btn-refresh" title="Refresh">${icons.refresh} Refresh</button>
                    <button class="fx-btn" id="btn-back-nav" style="display:none;" onclick="explNavigateBack()" title="Go Back">← Back</button>
                    <button class="fx-btn fx-btn-primary" id="btn-create-folder-root">${icons.addFolder} New Folder</button>
                    <button class="fx-btn fx-btn-primary" id="btn-upload-file-root">${icons.upload} Upload</button>
                    <button class="fx-btn" id="btn-paste-clipboard" style="display:none;" onclick="explPasteHere()" title="Paste">📋 Paste</button>
                </div>
                <div class="file-explorer-filters">
                    <button type="button" class="fx-btn fx-filter-toggle" id="btn-filter-starred" title="Show only starred files" aria-pressed="false">☆ Starred</button>
                    <label class="fx-filter-field" title="Sort files">
                        <span class="fx-filter-label">Sort</span>
                        <select id="sort-filter" class="fx-select">
                            <option value="name_asc">Name A–Z</option>
                            <option value="name_desc">Name Z–A</option>
                            <option value="date_desc">Newest</option>
                            <option value="date_asc">Oldest</option>
                            <option value="size_desc">Largest</option>
                            <option value="size_asc">Smallest</option>
                        </select>
                    </label>
                    <div id="type-filter" class="fx-type-combo" data-value="">
                        <span class="fx-filter-label">Type</span>
                        <button type="button" class="fx-select fx-type-combo-trigger" id="type-filter-trigger" aria-haspopup="listbox" aria-expanded="false">
                            <span class="fx-type-combo-label">All Types</span>
                            <span class="fx-type-combo-caret" aria-hidden="true">▾</span>
                        </button>
                        <div class="fx-type-combo-panel" id="type-filter-panel" hidden>
                            <input type="text" id="type-filter-search" class="fx-input fx-type-combo-search" placeholder="Filter types…" autocomplete="off" spellcheck="false">
                            <ul class="fx-type-combo-list" id="type-filter-list" role="listbox"></ul>
                        </div>
                    </div>
                    <div class="fx-search-wrap">
                        <input type="search" id="search-filter" class="fx-input" placeholder="Search name or path…" autocomplete="off" spellcheck="false">
                        <button type="button" class="fx-search-clear" id="btn-clear-search" title="Clear search" hidden aria-label="Clear search">×</button>
                    </div>
                    <button type="button" class="fx-btn" id="btn-clear-filters" title="Clear search, type, and starred" hidden>Clear filters</button>
                </div>
            </div>
            <div class="file-explorer-body" id="file-explorer-body">
                <div style="text-align:center; padding: 4rem; color: var(--fx-text-muted);">Loading system files...</div>
            </div>
        </div>

        <!-- Inputs Modal Box -->
        <div class="explorer-modal" id="modal-input">
            <div class="explorer-modal-content">
                <h3 id="modal-input-title">Title</h3>
                <label id="modal-input-label">Value:</label>
                <input type="text" id="modal-input-value" class="fx-input" style="width: 100%; box-sizing: border-box;">
                <input type="hidden" id="modal-input-action">
                <input type="hidden" id="modal-input-path">
                <div class="explorer-modal-footer">
                    <button class="fx-btn modal-cancel">Cancel</button>
                    <button class="fx-btn fx-btn-primary" id="modal-input-submit">Submit</button>
                </div>
            </div>
        </div>

        <!-- Binary Upload File Modal Box -->
        <div class="explorer-modal" id="modal-upload">
            <div class="explorer-modal-content">
                <h3 id="modal-upload-title">Upload File</h3>
                <label>Select target file:</label>
                <input type="file" id="modal-upload-file" class="fx-input" style="width:100%; box-sizing: border-box; padding: 0.5rem 0;" multiple>
                <input type="hidden" id="modal-upload-path">
                <input type="hidden" id="modal-upload-is-update" value="false">
                <div class="explorer-modal-footer">
                    <button class="fx-btn modal-cancel">Cancel</button>
                    <button class="fx-btn fx-btn-primary" id="modal-upload-submit">Upload</button>
                </div>
            </div>
        </div>

        <!-- Delete confirm (mouse + checkbox only) -->
        <div class="explorer-modal" id="modal-delete" role="dialog" aria-modal="true" aria-labelledby="modal-delete-title">
            <div class="explorer-modal-content explorer-modal-delete">
                <h3 id="modal-delete-title">Delete permanently?</h3>
                <p class="fx-delete-warning" id="modal-delete-message">This cannot be undone.</p>
                <p class="fx-delete-path mono" id="modal-delete-path"></p>
                <label class="fx-delete-check" for="modal-delete-confirm-check">
                    <input type="checkbox" id="modal-delete-confirm-check" tabindex="-1">
                    <span>I understand and want to delete this item</span>
                </label>
                <input type="hidden" id="modal-delete-path-value">
                <input type="hidden" id="modal-delete-is-folder" value="false">
                <div class="explorer-modal-footer">
                    <button type="button" class="fx-btn modal-cancel" id="modal-delete-cancel" tabindex="-1">Cancel</button>
                    <button type="button" class="fx-btn fx-btn-danger" id="modal-delete-submit" disabled tabindex="-1">Delete</button>
                </div>
            </div>
        </div>
    `;

    const bodyEl = document.getElementById('file-explorer-body');
    const searchFilter = document.getElementById('search-filter');
    const typeFilter = document.getElementById('type-filter');
    const typeFilterTrigger = document.getElementById('type-filter-trigger');
    const typeFilterPanel = document.getElementById('type-filter-panel');
    const typeFilterSearch = document.getElementById('type-filter-search');
    const typeFilterList = document.getElementById('type-filter-list');
    const typeFilterLabel = typeFilterTrigger?.querySelector('.fx-type-combo-label');
    const sortFilter = document.getElementById('sort-filter');
    const btnFilterStarred = document.getElementById('btn-filter-starred');
    const btnClearSearch = document.getElementById('btn-clear-search');
    const btnClearFilters = document.getElementById('btn-clear-filters');

    /** @type {{ value: string, label: string }[]} */
    let typeFilterOptions = [{ value: '', label: 'All Types' }];

    const hasActiveFilters = () => !!(currentSearch || currentTypeFilter || filterStarred);

    const syncFilterChrome = () => {
        if (btnClearSearch) btnClearSearch.hidden = !currentSearch;
        if (btnClearFilters) btnClearFilters.hidden = !hasActiveFilters();
        if (btnFilterStarred) {
            btnFilterStarred.setAttribute('aria-pressed', filterStarred ? 'true' : 'false');
            if (filterStarred) {
                btnFilterStarred.innerHTML = '⭐ Starred';
                btnFilterStarred.classList.add('fx-btn-primary', 'is-active');
            } else {
                btnFilterStarred.innerHTML = '☆ Starred';
                btnFilterStarred.classList.remove('fx-btn-primary', 'is-active');
            }
        }
        typeFilter?.classList.toggle('has-value', !!currentTypeFilter);
        if (searchFilter && searchFilter.value !== currentSearch) {
            searchFilter.value = currentSearch || '';
        }
    };

    const clearAllFilters = ({ fetch = true } = {}) => {
        currentSearch = '';
        currentTypeFilter = '';
        filterStarred = false;
        if (typeFilter) typeFilter.dataset.value = '';
        if (typeFilterLabel) typeFilterLabel.textContent = 'All Types';
        if (searchFilter) searchFilter.value = '';
        Object.keys(childrenCache).forEach((k) => delete childrenCache[k]);
        window._lastFilesCacheStr = null;
        syncFilterChrome();
        saveFxState();
        if (fetch) fetchFiles(true);
    };

    const typeFilterDisplayLabel = (value) => {
        const v = (value || '').replace(/^\./, '').toLowerCase();
        const hit = typeFilterOptions.find((o) => o.value === v);
        return hit ? hit.label : (v ? v : 'All Types');
    };

    const applyTypeFilterValue = (value, { fetch = true } = {}) => {
        const next = (value || '').toLowerCase().replace(/^\./, '');
        currentTypeFilter = next;
        if (typeFilter) typeFilter.dataset.value = next;
        if (typeFilterLabel) typeFilterLabel.textContent = typeFilterDisplayLabel(next);
        syncFilterChrome();
        if (!fetch) return;
        Object.keys(childrenCache).forEach((k) => delete childrenCache[k]);
        window._lastFilesCacheStr = null;
        saveFxState();
        fetchFiles(true);
    };

    const renderTypeFilterList = (query = '') => {
        if (!typeFilterList) return;
        const q = String(query || '').trim().toLowerCase();
        const visible = typeFilterOptions.filter((o) => {
            if (!q) return true;
            return o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q);
        });
        if (!visible.length) {
            typeFilterList.innerHTML = `<li class="fx-type-combo-empty">No matching types</li>`;
            return;
        }
        const esc = (s) => String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/"/g, '&quot;');
        const selected = (currentTypeFilter || '').replace(/^\./, '');
        typeFilterList.innerHTML = visible.map((o) => {
            const active = o.value === selected ? ' is-active' : '';
            return `<li class="fx-type-combo-option${active}" role="option" data-value="${esc(o.value)}" aria-selected="${o.value === selected ? 'true' : 'false'}">${esc(o.label)}</li>`;
        }).join('');
    };

    const closeTypeFilter = () => {
        if (!typeFilterPanel || typeFilterPanel.hidden) return;
        typeFilterPanel.hidden = true;
        typeFilter?.classList.remove('is-open');
        if (typeFilterTrigger) typeFilterTrigger.setAttribute('aria-expanded', 'false');
        if (typeFilterSearch) typeFilterSearch.value = '';
    };

    const openTypeFilter = () => {
        if (!typeFilterPanel) return;
        typeFilterPanel.hidden = false;
        typeFilter?.classList.add('is-open');
        if (typeFilterTrigger) typeFilterTrigger.setAttribute('aria-expanded', 'true');
        renderTypeFilterList('');
        // Focus search immediately so typing filters options without an extra click
        requestAnimationFrame(() => {
            if (!typeFilterSearch) return;
            typeFilterSearch.focus({ preventScroll: true });
            typeFilterSearch.select();
        });
    };

    const toggleTypeFilter = () => {
        if (!typeFilterPanel) return;
        if (typeFilterPanel.hidden) openTypeFilter();
        else closeTypeFilter();
    };

    const saveFxState = (extra = {}) => {
        try {
            const shell = container.querySelector('.file-explorer');
            if (shell) {
                const h = Math.round(shell.getBoundingClientRect().height);
                if (h >= 560 && h <= 1400) {
                    sessionStorage.setItem(`fxShellH:${workerName}`, String(h));
                    document.documentElement.style.setProperty('--fx-shell-min-h', h + 'px');
                }
            }
            sessionStorage.setItem(fxStateKey, JSON.stringify({
                path: currentNavPath || '',
                type: currentTypeFilter || '',
                search: currentSearch || '',
                sort: currentSort || 'name_asc',
                starred: !!filterStarred,
                expanded: Array.from(expandedFolders),
                selectedPath: selectedFxPath || extra.selectedPath || '',
                scrollBody: bodyEl ? bodyEl.scrollTop : 0,
                scrollWindow: window.scrollY,
                ...extra,
            }));
        } catch (_) { /* ignore quota / private mode */ }
    };

    const loadFxState = () => {
        try {
            const raw = sessionStorage.getItem(fxStateKey);
            return raw ? JSON.parse(raw) : null;
        } catch (_) {
            return null;
        }
    };

    const applyFxState = (state) => {
        if (!state || typeof state !== 'object') return;
        if (typeof state.path === 'string') {
            currentNavPath = state.path.replace(/^\/+|\/+$/g, '');
        }
        if (typeof state.type === 'string') {
            currentTypeFilter = state.type;
        }
        if (typeof state.search === 'string') {
            currentSearch = state.search;
        }
        if (typeof state.sort === 'string') {
            currentSort = state.sort;
        }
        if (typeof state.starred === 'boolean') {
            filterStarred = state.starred;
        }
        if (Array.isArray(state.expanded)) {
            expandedFolders = new Set(state.expanded.filter(Boolean));
        }
        if (state.selectedPath) {
            selectedFxPath = String(state.selectedPath);
        }
        pendingScrollRestore = {
            scrollBody: Number(state.scrollBody) || 0,
            scrollWindow: Number(state.scrollWindow) || 0,
        };
    };

    const restoreFxScrollAndSelection = () => {
        // Prefer saved window/body scroll. Avoid scrollIntoView first — it fights scrollTo and flickers.
        if (pendingScrollRestore) {
            if (bodyEl) bodyEl.scrollTop = pendingScrollRestore.scrollBody;
            window.scrollTo(0, pendingScrollRestore.scrollWindow);
            pendingScrollRestore = null;
        } else if ((window.location.hash || '') === '#file-explorer-section') {
            const section = document.getElementById('file-explorer-section');
            if (section) section.scrollIntoView({ block: 'start', behavior: 'auto' });
        }
        if (selectedFxPath && bodyEl) {
            const needle = selectedFxPath.replace(/\\/g, '/');
            bodyEl.querySelectorAll('.file-row, .folder-row').forEach((row) => {
                const title = row.querySelector('.file-name')?.getAttribute('title') || '';
                const onclick = row.querySelector('button[title="Show Path"]')?.getAttribute('onclick') || '';
                if (title === needle || onclick.includes(`'${needle.replace(/'/g, "\\'")}'`)) {
                    row.classList.add('fx-selected');
                    row.scrollIntoView({ block: 'nearest', behavior: 'auto' });
                }
            });
            // Keep highlight in saved state, but don't re-scroll on every poll
            selectedFxPath = '';
        }
    };

    // Restore explorer location after run-script (or other) full-page redirects
    (function restoreNavFromStorageOrUrl() {
        const params = new URLSearchParams(window.location.search);
        const urlPath = params.get('fx_path');
        const saved = loadFxState();
        if (urlPath != null && urlPath !== '') {
            applyFxState({ ...(saved || {}), path: urlPath });
        } else if (saved) {
            applyFxState(saved);
        }
        if (urlPath != null) {
            params.delete('fx_path');
            const clean = `${window.location.pathname}${params.toString() ? `?${params}` : ''}${window.location.hash || '#file-explorer-section'}`;
            try { history.replaceState(null, '', clean); } catch (_) {}
        }
    })();

    // Modals Handling
    const closeModals = () => document.querySelectorAll('.explorer-modal').forEach(m => m.classList.remove('open'));
    document.querySelectorAll('.modal-cancel').forEach(btn => btn.onclick = closeModals);

    const openInputModal = (title, label, action, path, defaultValue = '') => {
        document.getElementById('modal-input-title').innerText = title;
        document.getElementById('modal-input-label').innerText = label;
        document.getElementById('modal-input-value').value = defaultValue;
        document.getElementById('modal-input-action').value = action;
        document.getElementById('modal-input-path').value = path;
        document.getElementById('modal-input').classList.add('open');
        document.getElementById('modal-input-value').focus();
    };

    const openUploadModal = (title, path, isUpdate = false) => {
        document.getElementById('modal-upload-title').innerText = title;
        document.getElementById('modal-upload-path').value = path;
        document.getElementById('modal-upload-is-update').value = isUpdate ? "true" : "false";
        document.getElementById('modal-upload-file').value = "";
        document.getElementById('modal-upload').classList.add('open');
    };

    document.getElementById('btn-create-folder-root').onclick = () => openInputModal('Create Folder', 'Folder Name:', 'create_folder', currentNavPath || '/');
    document.getElementById('btn-upload-file-root').onclick = () => openUploadModal(`Upload File to ${currentNavPath || '/'}`, currentNavPath || '/');

    const updatePasteUi = () => {
        const btn = document.getElementById('btn-paste-clipboard');
        if (!btn) return;
        if (clipboard && clipboard.path) {
            btn.style.display = '';
            btn.title = `Paste "${clipboard.name}" into current folder`;
        } else {
            btn.style.display = 'none';
        }
    };

    window.explCutItem = (path, name, isFolder) => {
        clipboard = { path, name, isFolder: !!isFolder };
        updatePasteUi();
        showFxToast(`Cut: ${name}`);
    };

    window.explPasteHere = async (destParent) => {
        if (!clipboard || !clipboard.path) return;
        const dest = (destParent === undefined || destParent === null || destParent === '')
            ? (currentNavPath || '')
            : String(destParent).replace(/^\/+|\/+$/g, '');
        if (dest === clipboard.path || String(dest).startsWith(clipboard.path + '/')) {
            alert('Cannot paste a folder into itself.');
            return;
        }
        try {
            const res = await fetch('/files/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    worker_name: workerName,
                    source_path: clipboard.path,
                    dest_parent: dest,
                    is_folder: clipboard.isFolder
                })
            });
            const data = await res.json();
            if (data.error) {
                alert(data.error);
                return;
            }
            showFxToast(`Moved ${clipboard.name}`);
            clipboard = null;
            updatePasteUi();
            Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
            window._lastFilesCacheStr = null;
            fetchFiles(true);
            setTimeout(() => fetchFiles(true), 1500);
        } catch (e) {
            alert(e);
        }
    };

    document.getElementById('modal-input-submit').onclick = async () => {
        const action = document.getElementById('modal-input-action').value;
        const path = document.getElementById('modal-input-path').value;
        const val = document.getElementById('modal-input-value').value;

        let url = '';
        let payload = { worker_name: workerName };
        if (action === 'create_folder') {
            url = '/files/create_folder';
            payload.parent_path = path;
            payload.folder_name = val;
        } else if (action === 'rename_folder') {
            url = '/files/rename_folder';
            payload.folder_path = path;
            payload.new_name = val;
        } else if (action === 'rename_file') {
            url = '/files/rename_file';
            payload.source_path = path;
            payload.new_name = val;
        }

        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.error) alert(data.error);
        } catch (e) {
            alert(e);
        }
        closeModals();
        Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
        window._lastFilesCacheStr = null;
        fetchFiles(true);
        setTimeout(() => fetchFiles(true), 2000);
    };

    document.getElementById('modal-upload-submit').onclick = async () => {
        const path = document.getElementById('modal-upload-path').value;
        const fileInput = document.getElementById('modal-upload-file');
        const isUpdate = document.getElementById('modal-upload-is-update').value === "true";

        if (!fileInput.files.length) return alert("Select a file first");

        if (isUpdate) {
            const formData = new FormData();
            formData.append('worker_name', workerName);
            formData.append('target_path', path);
            formData.append('file', fileInput.files[0]);
            try {
                const res = await fetch('/files/update', { method: 'POST', body: formData });
                const data = await res.json();
                if (data.error) alert(data.error);
            } catch (e) {
                alert(e);
            }
        } else {
            for (let i = 0; i < fileInput.files.length; i++) {
                const file = fileInput.files[i];
                const formData = new FormData();
                formData.append('worker_name', workerName);
                let targetPath = (path.endsWith('/') ? path : path + '/') + file.name;
                formData.append('target_path', targetPath);
                formData.append('file', file);
                try {
                    const res = await fetch('/files/upload', { method: 'POST', body: formData });
                    const data = await res.json();
                    if (data.error) alert(`Error for ${file.name}: ${data.error}`);
                } catch (e) {
                    alert(`Error for ${file.name}: ${e}`);
                }
            }
        }
        closeModals();
        Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
        window._lastFilesCacheStr = null;
        fetchFiles(true);
        setTimeout(() => fetchFiles(true), 2000);
    };

    const openDeleteModal = (path, isFolder) => {
        const kind = isFolder ? 'folder' : 'file';
        const name = String(path || '').replace(/\\/g, '/').split('/').filter(Boolean).pop() || path || kind;
        document.getElementById('modal-delete-title').textContent = `Delete ${kind}?`;
        document.getElementById('modal-delete-message').textContent =
            `This will permanently delete the ${kind} below. This cannot be undone.`;
        document.getElementById('modal-delete-path').textContent = name;
        document.getElementById('modal-delete-path').title = path;
        document.getElementById('modal-delete-path-value').value = path;
        document.getElementById('modal-delete-is-folder').value = isFolder ? 'true' : 'false';
        const check = document.getElementById('modal-delete-confirm-check');
        const submit = document.getElementById('modal-delete-submit');
        check.checked = false;
        submit.disabled = true;
        document.getElementById('modal-delete').classList.add('open');
    };

    const deleteFile = (path, isFolder) => openDeleteModal(path, !!isFolder);

    const runDelete = async () => {
        const check = document.getElementById('modal-delete-confirm-check');
        const submit = document.getElementById('modal-delete-submit');
        if (!check?.checked || submit?.disabled) return;
        const path = document.getElementById('modal-delete-path-value').value;
        const isFolder = document.getElementById('modal-delete-is-folder').value === 'true';
        const url = isFolder ? '/files/delete_folder' : '/files/delete';
        const payload = { worker_name: workerName };
        if (isFolder) payload.folder_path = path;
        else payload.file_path = path;

        closeModals();
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.error) alert(data.error);
        } catch (e) {
            alert(e);
        }
        Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
        window._lastFilesCacheStr = null;
        fetchFiles(true);
        setTimeout(() => fetchFiles(true), 1500);
        setTimeout(() => fetchFiles(true), 3000);
    };

    document.getElementById('modal-delete-confirm-check')?.addEventListener('change', (e) => {
        const submit = document.getElementById('modal-delete-submit');
        if (submit) submit.disabled = !e.target.checked;
    });
    // Mouse-only: block keyboard; only real pointer clicks confirm
    document.getElementById('modal-delete')?.addEventListener('keydown', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });
    document.getElementById('modal-delete-submit')?.addEventListener('click', (e) => {
        if (e.detail === 0) return; // ignore keyboard-synthesized click
        runDelete();
    });

    // Attach globals for inline HTML executions
    window.explCreateFolder = (path) => openInputModal('Create Folder', 'Folder Name:', 'create_folder', path);
    window.explRenameFolder = (path) => {
        const parts = path.replace(/\\/g, '/').split('/');
        const currentName = parts.pop();
        openInputModal('Rename Folder', 'New Name:', 'rename_folder', path, currentName);
    };
    window.explRenameFile = (path) => {
        const parts = path.replace(/\\/g, '/').split('/');
        const currentName = parts.pop();
        openInputModal('Rename File', 'New Name:', 'rename_file', path, currentName);
    };
    window.explDeleteFolder = (path) => deleteFile(path, true);
    window.explUploadFile = (path) => {
        openUploadModal(`Upload File(s) to ${path}`, path, false);
        document.getElementById('modal-upload-file').multiple = true;
    };
    window.explUpdateFile = (path) => {
        openUploadModal(`Update File ${path}`, path, true);
        document.getElementById('modal-upload-file').multiple = false;
    };
    window.explDeleteFile = (path) => deleteFile(path, false);
    window.explToggleFolder = async (path) => {
        // Chevron: expand/collapse ONLY this folder's immediate children (no recursive expand)
        if (expandedFolders.has(path)) {
            expandedFolders.delete(path);
            renderTree();
            return;
        }
        expandedFolders.add(path);
        renderTree();
        if (!childrenCache[path]) {
            loadingFolders.add(path);
            renderTree();
            try {
                const searchParams = new URLSearchParams({
                    worker_name: workerName,
                    base_path: path || '/',
                    type: currentTypeFilter || '',
                    search: '',
                    starred: 'false'
                });
                const res = await fetch(`/files/list?${searchParams}`, { cache: 'no-store' });
                const data = await res.json();
                let kids = (data.files || []).map(f => ({
                    name: f.name,
                    path: f.path,
                    type: f.type,
                    size: f.size,
                    mtime: f.mtime,
                    script_id: f.script_id,
                    can_run: !!f.can_run,
                    is_starred: f.is_starred
                }));
                // Server already keeps folders that contain matching files when type is set
                childrenCache[path] = kids;
            } catch (e) {
                childrenCache[path] = [];
            } finally {
                loadingFolders.delete(path);
            }
        }
        renderTree();
    };

    window.explToggleStar = async (path, btn) => {
        try {
            const res = await fetch('/files/star', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ worker_name: workerName, file_path: path })
            });
            const data = await res.json();
            if (data.status === 'ok') {
                const targetFile = filesCache.find(f => f.path.replace(/\\/g, '/') === path.replace(/\\/g, '/'));
                if (targetFile) targetFile.is_starred = data.starred;
                renderTree();
            } else {
                alert('Star failed: ' + (data.error || 'Unknown error from server'));
            }
        } catch (e) {
            alert('Network or server error when trying to star file: ' + e);
            console.error('Star failed', e);
        }
    };

    window.explShowPathPopup = (e, path) => {
        let existing = document.getElementById('fx-path-popup');
        if (existing) existing.remove();

        const fullPath = workerRoot + '\\' + path.split('/').join('\\');

        const popup = document.createElement('div');
        popup.id = 'fx-path-popup';
        popup.className = 'fx-path-popup';
        popup.innerHTML = `
            <div class="fx-path-popup-text" title="${escapeHtml(fullPath)}">${escapeHtml(fullPath)}</div>
            <button class="fx-icon-btn" id="fx-path-copy" title="Copy"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></button>
            <button class="fx-icon-btn" id="fx-path-close" title="Close" style="color:var(--fx-danger);">✕</button>
        `;
        document.body.appendChild(popup);

        popup.style.top = (e.clientY + 10) + 'px';
        popup.style.left = (e.clientX + 10) + 'px';

        document.getElementById('fx-path-copy').onclick = () => {
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(fullPath);
            } else {
                const textArea = document.createElement("textarea");
                textArea.value = fullPath;
                textArea.style.position = "fixed";
                textArea.style.left = "-999999px";
                textArea.style.top = "-999999px";
                document.body.appendChild(textArea);
                textArea.focus();
                textArea.select();
                try {
                    document.execCommand('copy');
                } catch (error) {
                    console.error('Copy failed', error);
                }
                textArea.remove();
            }
            popup.remove();
        };
        document.getElementById('fx-path-close').onclick = () => popup.remove();
    };

    bodyEl.addEventListener('scroll', () => {
        let existing = document.getElementById('fx-path-popup');
        if (existing) existing.remove();
    });
    
    window.addEventListener('scroll', () => {
        let existing = document.getElementById('fx-path-popup');
        if (existing) existing.remove();
    });

    let filesCache = [];

    const fetchTypes = async () => {
        try {
            const res = await fetch(`/files/types?worker_name=${workerName}`);
            const data = await res.json();
            if (data.types) {
                typeFilterOptions = [
                    { value: '', label: 'All Types' },
                    ...data.types.map((t) => ({
                        value: t === 'folder' ? 'folder' : String(t).replace(/^\./, ''),
                        label: t === 'folder' ? 'Folders' : String(t),
                    })),
                ];
                const want = (currentTypeFilter || '').replace(/^\./, '');
                const valid = typeFilterOptions.some((o) => o.value === want);
                applyTypeFilterValue(valid ? want : '', { fetch: false });
                if (!typeFilterPanel?.hidden) renderTypeFilterList(typeFilterSearch?.value || '');
            }
        } catch (e) { }
    };

    const fetchFiles = async (forceBypassCache = false) => {
        if (document.hidden && !forceBypassCache) return;
        const startTime = performance.now();
        try {
            const wantStats = forceBypassCache || !window._fsStatsLoaded;
            const searchParams = new URLSearchParams({
                worker_name: workerName,
                base_path: currentNavPath || '/',
                type: currentTypeFilter,
                search: currentSearch || '',
                starred: filterStarred ? 'true' : 'false'
            });
            if (wantStats) {
                searchParams.append('include_stats', '1');
            }
            if (forceBypassCache) {
                searchParams.append('_ts', Date.now());
                window._lastFilesCacheStr = null; // always re-render on hard refresh
            }
            // Always bypass HTTP cache so worker FS pushes show up on the next poll
            const res = await fetch(`/files/list?${searchParams}`, { cache: 'no-store' });
            const data = await res.json();
            if (Array.isArray(data.files)) {
                // Only block on empty listings for an explicit path-change sync.
                // Stale sessionStorage alone must not freeze live updates.
                const syncPending = !!window._configSyncPending;
                const syncUiActive = !!(
                    window._configSyncPending
                    || window.configSyncTimerInterval
                    || sessionStorage.getItem(syncStorageKey)
                );
                // While waiting for a path-change resync, empty listings mean "still syncing"
                // — do not clear the timer or treat as a successful idle tree.
                const earlyTreeSync = data.tree_sync || {};
                const earlyLoading = earlyTreeSync.status === 'syncing' || earlyTreeSync.status === 'uploading';
                if (syncPending && data.files.length === 0 && !(data.total_files > 0) && (earlyLoading || earlyTreeSync.status !== 'complete')) {
                    bodyEl.innerHTML = '<div style="text-align:center; padding: 4rem; color: var(--fx-text-muted);">Syncing new folder from worker... please wait.</div>';
                    startConfigSyncTimer(false);
                    renderFsStatsCard({
                        totalFiles: 0,
                        totalSize: 0,
                        loadLabel: formatFolderLoadDuration(
                            earlyTreeSync.elapsed_s != null ? Number(earlyTreeSync.elapsed_s) : clientSyncElapsedS()
                        ),
                        statusLabel: 'Loading folder…',
                        statusDone: false,
                    });
                    return;
                }

                const newDataStr = JSON.stringify(data.files);
                const isDataChanged = (window._lastFilesCacheStr !== newDataStr);
                
                if (isDataChanged || forceBypassCache || !window._lastFilesCacheStr) {
                    window._lastFilesCacheStr = newDataStr;
                    filesCache = data.files;
                    if (data.permissions) currentPerms = data.permissions;
                    if (data.worker_root) workerRoot = data.worker_root;

                    document.getElementById('btn-create-folder-root').style.display = currentPerms.can_create_folder ? '' : 'none';
                    document.getElementById('btn-upload-file-root').style.display = currentPerms.can_create_file ? '' : 'none';

                    renderTree();
                }
                
                const statsEl = document.getElementById('fs-stats-content');
                const treeSync = data.tree_sync || {};
                const totalFiles = data.total_files != null
                    ? data.total_files
                    : (window._fsStatsTotalFiles ?? filesCache.filter(f => f.type === 'file').length);
                const totalSize = data.total_size != null
                    ? data.total_size
                    : (window._fsStatsTotalSize ?? filesCache.reduce((s, f) => s + (f.type === 'file' && f.size ? f.size : 0), 0));

                if (data.total_files != null) {
                    window._fsStatsTotalFiles = data.total_files;
                    window._fsStatsTotalSize = data.total_size || 0;
                }

                const folderSyncDone = syncPending && treeSync.status === 'complete';
                const folderSyncLoading = syncPending && (
                    treeSync.status === 'syncing' || treeSync.status === 'uploading' || !treeSync.status
                );

                if (statsEl && (wantStats || data.total_files != null || syncUiActive || window._folderLoadFinished)) {
                    const endTime = performance.now();
                    const elapsedMs = endTime - startTime;
                    const fetchLabel = elapsedMs < 1000
                        ? `${Math.round(elapsedMs)} ms`
                        : `${(elapsedMs / 1000).toFixed(2)} s`;
                    if (folderSyncLoading || (syncUiActive && !folderSyncDone && !window._folderLoadFinished)) {
                        const liveS = treeSync.elapsed_s != null ? Number(treeSync.elapsed_s) : clientSyncElapsedS();
                        window._fsStatsLoadLabel = formatFolderLoadDuration(liveS);
                        renderFsStatsCard({
                            totalFiles,
                            totalSize,
                            loadLabel: window._fsStatsLoadLabel,
                            statusLabel: 'Loading folder…',
                            statusDone: false,
                        });
                    } else if (folderSyncDone) {
                        const totalS = treeSync.elapsed_s != null ? Number(treeSync.elapsed_s) : clientSyncElapsedS();
                        window._fsStatsLoadLabel = formatFolderLoadDuration(totalS);
                        window._fsStatsLoaded = true;
                        renderFsStatsCard({
                            totalFiles,
                            totalSize,
                            loadLabel: window._fsStatsLoadLabel,
                            statusLabel: 'Loading finished',
                            statusDone: true,
                        });
                    } else if (wantStats || !window._fsStatsLoaded) {
                        // First load (or explicit refresh) only — do not rewrite Load Time every poll.
                        if (data.total_files != null) {
                            window._fsStatsLoaded = true;
                            window._fsStatsLoadLabel = fetchLabel;
                        }
                        renderFsStatsCard({
                            totalFiles,
                            totalSize,
                            loadLabel: window._fsStatsLoadLabel || fetchLabel,
                            statusLabel: window._folderLoadFinished ? 'Loading finished' : '',
                            statusDone: !!window._folderLoadFinished,
                        });
                    } else if (data.total_files != null) {
                        // Totals may change from live FS sync; keep stable load label / status.
                        renderFsStatsCard({
                            totalFiles,
                            totalSize,
                            loadLabel: window._fsStatsLoadLabel || fetchLabel,
                            statusLabel: window._folderLoadFinished ? 'Loading finished' : '',
                            statusDone: !!window._folderLoadFinished,
                        });
                    }
                }

                // Path-change wait ends only when the worker finishes the full tree upload
                if (folderSyncDone) {
                    const totalS = treeSync.elapsed_s != null ? Number(treeSync.elapsed_s) : clientSyncElapsedS();
                    markConfigSyncFinished(totalS);
                } else if (folderSyncLoading) {
                    startConfigSyncTimer(false);
                }
            } else if (data.error) {
                bodyEl.innerHTML = `<div style="text-align:center; padding: 4rem; color: var(--fx-danger);">${data.error}</div>`;
                const treeSync = data.tree_sync || {};
                if (window._configSyncPending && treeSync.status === 'complete') {
                    const totalS = treeSync.elapsed_s != null ? Number(treeSync.elapsed_s) : clientSyncElapsedS();
                    markConfigSyncFinished(totalS);
                    renderFsStatsCard({
                        totalFiles: data.total_files || 0,
                        totalSize: data.total_size || 0,
                        loadLabel: formatFolderLoadDuration(totalS),
                        statusLabel: 'Loading finished',
                        statusDone: true,
                    });
                } else if (data.error.includes("not synced yet") || data.error.includes("Worker file tree not synced")) {
                    if (window._configSyncPending) {
                        bodyEl.innerHTML = '<div style="text-align:center; padding: 4rem; color: var(--fx-text-muted);">Syncing new folder from worker... please wait.</div>';
                    }
                    // Resume elapsed from persisted start (do not reset to 0 on reload)
                    startConfigSyncTimer(false);
                    renderFsStatsCard({
                        totalFiles: 0,
                        totalSize: 0,
                        loadLabel: formatFolderLoadDuration(clientSyncElapsedS()),
                        statusLabel: 'Loading folder…',
                        statusDone: false,
                    });
                }
            }
        } catch (e) {
            bodyEl.innerHTML = `<div style="text-align:center; padding: 4rem; color: var(--fx-danger);">Network Error</div>`;
        }
    };

    const buildHierarchy = (flatFiles) => {
        const root = { children: {} };
        flatFiles.forEach(file => {
            const parts = file.path.split('/');
            let current = root;
            let currentPath = '';
            for (let i = 0; i < parts.length; i++) {
                const part = parts[i];
                currentPath += (currentPath ? '/' : '') + part;
                if (!current.children[part]) {
                    current.children[part] = {
                        name: part,
                        path: currentPath,
                        type: i === parts.length - 1 ? file.type : 'folder',
                        size: file.size,
                        mtime: file.mtime,
                        script_id: file.script_id,
                        is_starred: file.is_starred,
                        children: {}
                    };
                }
                current = current.children[part];
                if (currentSearch || currentTypeFilter) {
                    expandedFolders.add(currentPath);
                }
            }
        });
        return root.children;
    };

    const formatBytes = (bytes, decimals = 2) => {
        if (!+bytes) return '--';
        const k = 1024, dm = decimals < 0 ? 0 : decimals, sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
    };

    const formatTime = (mtime) => {
        if (!mtime) return '--';
        return new Date(mtime * 1000).toLocaleString();
    };

    const escapeJsStr = (str) => {
        return String(str || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    };

    const escapeHtml = (str) => {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    };

    const getFileIcon = (filename) => {
        const ext = filename.split('.').pop().toLowerCase();
        if (['py', 'pyw'].includes(ext)) return icons.python;
        if (['js', 'jsx'].includes(ext)) return icons.javascript;
        if (['html', 'htm'].includes(ext)) return icons.html;
        if (['css', 'scss', 'sass'].includes(ext)) return icons.css;
        if (['json'].includes(ext)) return icons.json;
        if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp'].includes(ext)) return icons.image;
        if (['mp4', 'webm', 'mov', 'avi'].includes(ext)) return icons.video;
        if (['mp3', 'wav', 'ogg'].includes(ext)) return icons.audio;
        if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return icons.zip;
        if (['bat', 'ps1', 'sh'].includes(ext)) return `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6b7280" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>`;
        return icons.file;
    };

    const isTextFile = (filename) => {
        const ext = filename.split('.').pop().toLowerCase();
        const textExts = ['txt', 'py', 'java', 'log', 'md', 'csv', 'json', 'yaml', 'yml', 'xml', 'html', 'css', 'js', 'sh', 'bat', 'ps1', 'ini', 'cfg'];
        return textExts.includes(ext);
    };

    // Only types the worker execute_script path can run
    const RUNNABLE_EXTS = new Set(['py', 'pyw', 'bat', 'cmd']);
    const isRunnableScript = (filename) => {
        if (!filename || filename.indexOf('.') === -1) return false;
        const ext = filename.split('.').pop().toLowerCase();
        return RUNNABLE_EXTS.has(ext);
    };

    const sortNodes = (a, b) => {
        if (a.type !== b.type) return a.type === 'folder' ? -1 : 1;
        if (currentSort === 'name_asc') return a.name.localeCompare(b.name);
        if (currentSort === 'name_desc') return b.name.localeCompare(a.name);
        if (currentSort === 'date_desc') return (b.mtime || 0) - (a.mtime || 0);
        if (currentSort === 'date_asc') return (a.mtime || 0) - (b.mtime || 0);
        if (currentSort === 'size_desc') return (b.size || 0) - (a.size || 0);
        if (currentSort === 'size_asc') return (a.size || 0) - (b.size || 0);
        return 0;
    };

    const renderNode = (node, isRoot = false) => {
        const isFolder = node.type === 'folder';
        const showFullPath = !!currentSearch || filterStarred;
        const displayName = escapeHtml(showFullPath ? node.path : node.name);
        const safeTitle = escapeHtml(node.path || '');

        let html = '';
        if (isFolder) {
            const isExpanded = expandedFolders.has(node.path);
            const isLoading = loadingFolders.has(node.path);
            html += `
                <li>
                    <div class="folder-row" oncontextmenu="explShowContextMenu(event, 'folder', '${escapeJsStr(node.path)}', null, '${escapeJsStr(node.name)}')">
                        <div class="folder-toggle ${isExpanded ? 'is-expanded' : ''}" title="Expand folder" onclick="event.stopPropagation(); explToggleFolder('${escapeJsStr(node.path)}')">
                            ${isExpanded ? icons.chevronDown : icons.chevronRight}
                        </div>
                        <div class="file-icon" onclick="explNavigateFolder('${escapeJsStr(node.path)}')">${icons.folder}</div>
                        <div class="file-name" style="display:flex; align-items:center; gap:0.5rem;" onclick="explNavigateFolder('${escapeJsStr(node.path)}')">
                            <span>${displayName}</span>
                            <button class="fx-icon-btn" style="width:20px; height:20px;" onclick="event.stopPropagation(); explShowPathPopup(event, '${escapeJsStr(node.path)}')" title="Show Path">🔗</button>
                        </div>
                        <div class="file-mtime">${formatTime(node.mtime)}</div>
                        <div class="file-size"></div>
                    </div>
            `;
            // Immediate children only — nested folders stay collapsed unless user expands them
            if (isExpanded) {
                html += `<ul class="file-tree-ul nested">`;
                if (isLoading && !childrenCache[node.path]) {
                    html += `<li><div class="file-row empty" style="color:var(--fx-text-muted); font-style:italic; font-size:0.85rem;">Loading...</div></li>`;
                } else {
                    const kids = (childrenCache[node.path] || []).slice().sort(sortNodes);
                    if (!kids.length) {
                        html += `<li><div class="file-row empty" style="color:var(--fx-text-muted); font-style:italic; font-size:0.85rem;">Empty folder</div></li>`;
                    } else if (kids.length > 250) {
                        html += kids.slice(0, 250).map(c => renderNode(c)).join('');
                        html += `<li><div class="file-row empty" style="color:var(--fx-text-muted); font-style:italic; font-size:0.85rem;">...and ${kids.length - 250} more items</div></li>`;
                    } else {
                        html += kids.map(c => renderNode(c)).join('');
                    }
                }
                html += `</ul>`;
            }
            html += `</li>`;
        } else {
            const canRunFile = !!(currentPerms.can_run || currentPerms.is_admin || node.can_run);
            let scheduleBtn = '';
            // Schedule needs a registered script_id; ensure on click if tree is ahead of sync_scripts
            const canSchedule = node.name.toLowerCase().endsWith('.py') && canRunFile;
            if (canSchedule) {
                const sidAttr = node.script_id ? String(node.script_id) : '';
                scheduleBtn = `<button class="fx-icon-btn" style="width:20px; height:20px; color: var(--fx-primary);" onclick="event.stopPropagation(); explScheduleScript('${escapeJsStr(node.path)}', '${escapeJsStr(node.name)}', '${escapeJsStr(sidAttr)}')" title="Schedule this script">🕒</button>`;
            }
            const starred = !!node.is_starred;
            const starBtn = `<button class="fx-icon-btn" style="width:20px; height:20px;" onclick="event.stopPropagation(); explToggleStar('${escapeJsStr(node.path)}', this)" title="Star">${starred ? '⭐' : '☆'}</button>`;

            html += `
                <li>
                    <div class="file-row" oncontextmenu="explShowContextMenu(event, 'file', '${escapeJsStr(node.path)}', ${node.script_id ? `'${escapeJsStr(String(node.script_id))}'` : 'null'}, '${escapeJsStr(node.name)}', ${canRunFile ? 'true' : 'false'})">
                        <div class="file-icon" style="font-size: 1.1rem; justify-content:center;">${getFileIcon(node.name)}</div>
                        <div class="file-name" title="${safeTitle}" style="display:flex; align-items:center; gap:0.5rem;">
                            ${displayName}
                            ${starBtn}
                            ${scheduleBtn}
                            <button class="fx-icon-btn" style="width:20px; height:20px;" onclick="event.stopPropagation(); explShowPathPopup(event, '${escapeJsStr(node.path)}')" title="Show Path">🔗</button>
                        </div>
                        <div class="file-mtime">${formatTime(node.mtime)}</div>
                        <div class="file-size">${formatBytes(node.size)}</div>
                    </div>
                </li>
            `;
        }
        return html;
    };

    const renderBreadcrumb = () => {
        const parts = currentNavPath ? currentNavPath.split('/').filter(Boolean) : [];
        let html = `<button class="fx-btn" style="padding:0.2rem 0.5rem;" onclick="explNavigateFolder('')">Root</button>`;
        let acc = '';
        parts.forEach((part, idx) => {
            acc += (acc ? '/' : '') + part;
            const pathCopy = acc;
            html += ` <span style="opacity:0.5;">/</span> <button class="fx-btn" style="padding:0.2rem 0.5rem;" onclick="explNavigateFolder('${escapeJsStr(pathCopy)}')">${escapeHtml(part)}</button>`;
        });
        return `<div class="fx-breadcrumb" style="display:flex; flex-wrap:wrap; align-items:center; gap:0.25rem; margin-bottom:0.75rem;">${html}</div>`;
    };

    window.explNavigateFolder = (path) => {
        currentNavPath = (path || '').replace(/^\/+|\/+$/g, '');
        window._lastFilesCacheStr = null;
        // Reset in-view expands when navigating into a folder
        expandedFolders.clear();
        Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
        saveFxState();
        fetchFiles(true);
    };

    window.explNavigateBack = () => {
        if (!currentNavPath) return;
        const parts = currentNavPath.split('/');
        parts.pop();
        currentNavPath = parts.join('/');
        window._lastFilesCacheStr = null;
        expandedFolders.clear();
        Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
        saveFxState();
        fetchFiles(true);
    };

    window.explScheduleScript = async (filePath, scriptName, scriptId) => {
        const openScheduler = (sid, sname) => {
            const url = `/scheduler?action=create&script_id=${encodeURIComponent(sid)}&script_name=${encodeURIComponent(sname || scriptName || '')}`;
            window.open(url, '_blank');
        };
        if (scriptId) {
            openScheduler(scriptId, scriptName);
            return;
        }
        try {
            const res = await fetch('/files/ensure-script', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                cache: 'no-store',
                body: JSON.stringify({ worker_name: workerName, file_path: filePath }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.script_id) {
                alert((data && data.error) || 'Could not prepare script for scheduling.');
                return;
            }
            // Refresh list so subsequent clicks already have script_id
            window._lastFilesCacheStr = null;
            fetchFiles(true);
            openScheduler(data.script_id, data.script_name || scriptName);
        } catch (err) {
            console.error('ensure-script failed', err);
            alert('Could not prepare script for scheduling.');
        }
    };

    window.explShowContextMenu = (e, type, path, scriptId = null, name = '', canRunFile = false) => {
        e.preventDefault();
        e.stopPropagation();

        let menu = document.getElementById('fx-context-menu');
        if (!menu) {
            menu = document.createElement('div');
            menu.id = 'fx-context-menu';
            menu.className = 'fx-context-menu';
            document.body.appendChild(menu);
        }

        let html = '';
        const jsPath = escapeJsStr(path);
        const jsName = escapeJsStr(name || path.split('/').pop() || path);
        if (type === 'folder') {
            if (currentPerms.can_create_folder) html += `<button class="fx-context-menu-item" onclick="explCreateFolder('${jsPath}')">${icons.addFolder} Create Folder</button>`;
            if (currentPerms.can_create_file) html += `<button class="fx-context-menu-item" onclick="explUploadFile('${jsPath}')">${icons.upload} Upload File</button>`;
            if (clipboard && clipboard.path) html += `<button class="fx-context-menu-item" onclick="explPasteHere('${jsPath}')">📋 Paste</button>`;
            if (currentPerms.can_rename_folder) html += `<button class="fx-context-menu-item" onclick="explRenameFolder('${jsPath}')">${icons.edit} Rename</button>`;
            if (currentPerms.can_rename_folder) html += `<button class="fx-context-menu-item" onclick="explCutItem('${jsPath}', '${jsName}', true)">✂ Cut</button>`;
            if (currentPerms.can_delete_folder) html += `<button class="fx-context-menu-item danger" onclick="explDeleteFolder('${jsPath}')">${icons.trash} Delete</button>`;
        } else {
            const allowRun = isRunnableScript(name) && (currentPerms.is_admin || currentPerms.can_run || !!canRunFile);
            if (allowRun) {
                html += `
                <form action="/run-script" method="post" style="margin: 0; padding: 0;">
                    ${scriptId ? `<input type="hidden" name="script_id" value="${escapeHtml(String(scriptId))}">` : ''}
                    <input type="hidden" name="worker_name" value="${escapeHtml(workerName)}">
                    <input type="hidden" name="file_path" value="${escapeHtml(path)}">
                    <input type="hidden" name="next" value="${escapeHtml(window.location.href)}">
                    <button type="submit" class="fx-context-menu-item fx-run-script-btn" style="width: 100%;">▶ Run Script</button>
                </form>
                `;
            }
            if (isTextFile(name)) {
                const editorUrl = `/editor?worker_name=${encodeURIComponent(workerName)}&file_path=${encodeURIComponent(path)}`;
                if (currentPerms.can_edit_file) {
                    html += `<button class="fx-context-menu-item" onclick="window.open('${editorUrl}', '_blank')">✏️ Edit</button>`;
                } else {
                    // View without Edit File — editor opens read-only
                    html += `<button class="fx-context-menu-item" onclick="window.open('${editorUrl}', '_blank')">👁 View</button>`;
                }
            }
            if (currentPerms.can_rename_file) html += `<button class="fx-context-menu-item" onclick="explRenameFile('${jsPath}')">${icons.edit} Rename</button>`;
            if (currentPerms.can_rename_file) html += `<button class="fx-context-menu-item" onclick="explCutItem('${jsPath}', '${jsName}', false)">✂ Cut</button>`;
            if (currentPerms.can_update_file) html += `<button class="fx-context-menu-item" onclick="explUpdateFile('${jsPath}')">${icons.upload} Update</button>`;
            if (currentPerms.can_delete_file) html += `<button class="fx-context-menu-item danger" onclick="explDeleteFile('${jsPath}')">${icons.trash} Delete</button>`;
        }

        if (html === '') {
            menu.classList.remove('open');
            return;
        }

        menu.innerHTML = html;
        menu.style.left = `${e.clientX}px`;
        menu.style.top = `${e.clientY}px`;
        menu.classList.add('open');
    };

    document.addEventListener('click', (e) => {
        const menu = document.getElementById('fx-context-menu');
        if (menu && menu.classList.contains('open')) {
            // Hide if clicking anywhere
            menu.classList.remove('open');
        }

        const pathPopup = document.getElementById('fx-path-popup');
        if (pathPopup && !pathPopup.contains(e.target)) {
            pathPopup.remove();
        }
    });

    const renderTree = () => {
        const backBtn = document.getElementById('btn-back-nav');
        if (backBtn) {
            if (currentNavPath) {
                backBtn.style.display = 'inline-flex';
                backBtn.innerHTML = `← Back`;
            } else {
                backBtn.style.display = 'none';
            }
        }

        let displayNodes = filesCache.map(f => ({
            name: f.name,
            path: f.path,
            type: f.type,
            size: f.size,
            mtime: f.mtime,
            script_id: f.script_id,
            can_run: !!f.can_run,
            is_starred: f.is_starred
        }));

        // Starred/type/search are applied server-side via /files/list — do not re-filter here

        displayNodes.sort(sortNodes);

        let html = renderBreadcrumb();
        if (hasActiveFilters()) {
            const bits = [];
            if (filterStarred) bits.push('Starred');
            if (currentTypeFilter) bits.push(currentTypeFilter === 'folder' ? 'Folders' : currentTypeFilter);
            if (currentSearch) bits.push(`“${currentSearch}”`);
            html += `<div class="fx-filter-status">Showing ${bits.join(' · ')}${currentSearch ? ' <span class="fx-filter-hint">(all folders)</span>' : ''}</div>`;
        }
        if (!displayNodes.length) {
            let emptyMsg = 'This folder is empty.';
            if (filterStarred && !currentSearch && !currentTypeFilter) emptyMsg = 'No starred files yet. Star a file with ☆ to pin it here.';
            else if (hasActiveFilters()) emptyMsg = 'No files match the current filters.';
            html += `<div class="fx-empty-state">
                <div>${emptyMsg}</div>
                ${hasActiveFilters() ? '<button type="button" class="fx-btn fx-btn-primary" id="fx-empty-clear-filters" style="margin-top:0.75rem;">Clear filters</button>' : ''}
            </div>`;
            bodyEl.innerHTML = html;
            document.getElementById('fx-empty-clear-filters')?.addEventListener('click', () => clearAllFilters());
            if (pendingScrollRestore || selectedFxPath) {
                requestAnimationFrame(() => restoreFxScrollAndSelection());
            }
            saveFxState();
            return;
        }

        html += `<ul class="file-tree-ul root">`;
        if (displayNodes.length > 250) {
            html += displayNodes.slice(0, 250).map(n => renderNode(n, true)).join('');
            html += `<li><div class="file-row empty" style="color:var(--fx-text-muted); justify-content:center; padding:1rem; font-style:italic; font-size: 0.9rem;">...and ${displayNodes.length - 250} more items (Use the search bar above to find specific files)</div></li>`;
        } else {
            html += displayNodes.map(n => renderNode(n, true)).join('');
        }
        html += `</ul>`;
        bodyEl.innerHTML = html;
        // After run-script redirect, restore scroll/selection once tree is painted
        if (pendingScrollRestore || selectedFxPath) {
            requestAnimationFrame(() => restoreFxScrollAndSelection());
        }
        saveFxState();
    };

    // Events
    const refreshBtn = container.querySelector('#btn-refresh');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            refreshBtn.disabled = true;
            window._lastFilesCacheStr = null;
            window._fsStatsLoaded = false;
            window._fsStatsRenderSig = null;
            expandedFolders.clear();
            Object.keys(childrenCache).forEach(k => delete childrenCache[k]);
            try {
                // Ask worker to rescan current folder from disk
                await fetch('/files/refresh', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        worker_name: workerName,
                        folder_path: currentNavPath || ''
                    })
                });
                // Poll until worker push lands (typically ~1–2s)
                let updated = false;
                for (let i = 0; i < 8; i++) {
                    await new Promise(r => setTimeout(r, 400));
                    await fetchFiles(true);
                    updated = true;
                }
                showFxToast(updated ? 'File Explorer refreshed successfully' : 'Refresh requested');
            } catch (err) {
                showFxToast('Refresh failed', false);
            } finally {
                refreshBtn.disabled = false;
            }
        });
    }

    let searchTimeout = null;
    searchFilter.addEventListener('input', (e) => {
        currentSearch = e.target.value.toLowerCase();
        syncFilterChrome();
        if (searchTimeout) clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            window._lastFilesCacheStr = null;
            saveFxState();
            fetchFiles(true);
        }, 300);
    });

    btnClearSearch?.addEventListener('click', () => {
        currentSearch = '';
        if (searchFilter) searchFilter.value = '';
        syncFilterChrome();
        window._lastFilesCacheStr = null;
        saveFxState();
        fetchFiles(true);
        searchFilter?.focus();
    });

    btnClearFilters?.addEventListener('click', () => clearAllFilters());

    typeFilterTrigger?.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleTypeFilter();
    });

    typeFilterTrigger?.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            openTypeFilter();
        }
    });

    typeFilterSearch?.addEventListener('input', (e) => {
        renderTypeFilterList(e.target.value);
    });

    typeFilterSearch?.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            closeTypeFilter();
            typeFilterTrigger?.focus();
            return;
        }
        if (e.key === 'Enter') {
            e.preventDefault();
            const highlighted = typeFilterList?.querySelector('.fx-type-combo-option.is-highlight')
                || typeFilterList?.querySelector('.fx-type-combo-option');
            if (highlighted) {
                applyTypeFilterValue(highlighted.getAttribute('data-value') || '');
                closeTypeFilter();
            }
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            const opts = [...(typeFilterList?.querySelectorAll('.fx-type-combo-option') || [])];
            if (!opts.length) return;
            const idx = opts.findIndex((o) => o.classList.contains('is-highlight'));
            opts.forEach((o) => o.classList.remove('is-highlight'));
            let next = 0;
            if (e.key === 'ArrowDown') next = idx < 0 ? 0 : Math.min(opts.length - 1, idx + 1);
            else next = idx < 0 ? opts.length - 1 : Math.max(0, idx - 1);
            opts[next].classList.add('is-highlight');
            opts[next].scrollIntoView({ block: 'nearest' });
        }
    });

    typeFilterList?.addEventListener('click', (e) => {
        const opt = e.target.closest('.fx-type-combo-option');
        if (!opt) return;
        applyTypeFilterValue(opt.getAttribute('data-value') || '');
        closeTypeFilter();
    });

    document.addEventListener('mousedown', (e) => {
        if (!typeFilter || typeFilterPanel?.hidden) return;
        if (!typeFilter.contains(e.target)) closeTypeFilter();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && typeFilterPanel && !typeFilterPanel.hidden) {
            closeTypeFilter();
        }
    });

    sortFilter.addEventListener('change', (e) => {
        currentSort = e.target.value;
        saveFxState();
        renderTree();
    });

    btnFilterStarred.addEventListener('click', () => {
        filterStarred = !filterStarred;
        syncFilterChrome();
        window._lastFilesCacheStr = null;
        saveFxState();
        fetchFiles(true);
    });

    // Persist explorer folder before run-script POST (full page reload)
    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        if ((form.getAttribute('action') || '') !== '/run-script') return;
        const filePath = form.querySelector('input[name="file_path"]')?.value || '';
        selectedFxPath = filePath;
        saveFxState({ selectedPath: filePath });
        const nextInput = form.querySelector('input[name="next"]');
        if (nextInput) {
            try {
                const u = new URL(window.location.href);
                u.hash = 'file-explorer-section';
                if (currentNavPath) u.searchParams.set('fx_path', currentNavPath);
                else u.searchParams.delete('fx_path');
                nextInput.value = u.toString();
            } catch (_) {
                nextInput.value = window.location.href.split('#')[0] + '#file-explorer-section';
            }
        }
    }, true);

    window.fetchFiles = fetchFiles;
    window._explorerSetPath = (path) => {
        currentNavPath = (path || '').replace(/^\/+|\/+$/g, '');
        expandedFolders = new Set();
        window._lastFilesCacheStr = null;
        window._fsStatsLoaded = false;
        window._fsStatsRenderSig = null;
        saveFxState();
    };

    if (searchFilter && currentSearch) searchFilter.value = currentSearch;
    if (sortFilter && currentSort) sortFilter.value = currentSort;
    applyTypeFilterValue(currentTypeFilter || '', { fetch: false });
    syncFilterChrome();

    fetchTypes();
    fetchFiles();

    fetchInterval = setInterval(fetchFiles, 1500);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && typeof fetchFiles === 'function') fetchFiles();
    });
    // Persist shell height after first paint so next reload reserves the same space
    requestAnimationFrame(() => saveFxState());
});
