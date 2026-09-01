(function () {
    function qs(sel, root) { return (root || document).querySelector(sel); }
    function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

    var createOverlay = qs('#users-create-overlay');
    var editOverlay = qs('#users-edit-overlay');

    function openOverlay(el) {
        if (!el) return;
        el.classList.add('is-open');
        el.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
    }

    function closeOverlay(el) {
        if (!el) return;
        el.classList.remove('is-open');
        el.setAttribute('aria-hidden', 'true');
        if (!qs('.users-overlay.is-open')) {
            document.body.style.overflow = '';
        }
    }

    function moveToBody(el) {
        if (el && el.parentNode !== document.body) document.body.appendChild(el);
    }

    moveToBody(createOverlay);
    moveToBody(editOverlay);

    /* Search + filter */
    function applyFilters() {
        var q = ((qs('#users-search') && qs('#users-search').value) || '').toLowerCase().trim();
        var role = (qs('#users-filter-role') && qs('#users-filter-role').value) || 'all';
        var status = (qs('#users-filter-status') && qs('#users-filter-status').value) || 'all';
        var visible = 0;

        qsa('.users-row').forEach(function (row) {
            var hay = (row.getAttribute('data-search') || '').toLowerCase();
            var rowRole = row.getAttribute('data-role') || '';
            var rowStatus = row.getAttribute('data-status') || '';
            var ok =
                (!q || hay.indexOf(q) !== -1) &&
                (role === 'all' || rowRole === role) &&
                (status === 'all' || rowStatus === status);
            row.style.display = ok ? '' : 'none';
            if (ok) visible += 1;
        });

        var empty = qs('#users-filter-empty');
        var table = qs('#users-table');
        if (empty) empty.style.display = visible ? 'none' : 'block';
        if (table) table.style.display = visible ? '' : 'none';

        var count = qs('#users-visible-count');
        if (count) count.textContent = String(visible);
    }

    ['users-search', 'users-filter-role', 'users-filter-status'].forEach(function (id) {
        var el = qs('#' + id);
        if (!el) return;
        el.addEventListener('input', applyFilters);
        el.addEventListener('change', applyFilters);
    });

    /* Create drawer */
    var openCreateBtn = qs('#users-open-create');
    if (openCreateBtn) {
        openCreateBtn.addEventListener('click', function () {
            openOverlay(createOverlay);
            var first = qs('#create-username');
            if (first) setTimeout(function () { first.focus(); }, 50);
        });
    }

    qsa('[data-close-create]').forEach(function (btn) {
        btn.addEventListener('click', function () { closeOverlay(createOverlay); });
    });
    if (createOverlay) {
        createOverlay.addEventListener('click', function (e) {
            if (e.target === createOverlay) closeOverlay(createOverlay);
        });
    }

    /* Edit drawer */
    function fillEditDrawer(btn) {
        var form = qs('#users-edit-form');
        if (!form || !btn) return;

        var userId = btn.getAttribute('data-user-id');
        form.action = '/users/' + userId + '/edit';

        qs('#edit-username').value = btn.getAttribute('data-username') || '';
        qs('#edit-ip').value = btn.getAttribute('data-ip') || '';
        qs('#edit-password').value = '';

        var role = btn.getAttribute('data-role') || 'user';
        var isPrimary = btn.getAttribute('data-primary') === '1';
        var isSelf = btn.getAttribute('data-self') === '1';
        var disabled = btn.getAttribute('data-disabled') === '1';

        var roleSelectWrap = qs('#edit-role-select-wrap');
        var roleLockedWrap = qs('#edit-role-locked-wrap');
        var roleSelect = qs('#edit-role');
        var roleValue = qs('#edit-role-value');

        if (role === 'admin') {
            if (roleSelectWrap) roleSelectWrap.style.display = 'none';
            if (roleLockedWrap) roleLockedWrap.style.display = '';
            if (roleValue) roleValue.value = 'admin';
        } else {
            if (roleSelectWrap) roleSelectWrap.style.display = '';
            if (roleLockedWrap) roleLockedWrap.style.display = 'none';
            if (roleSelect) roleSelect.value = 'user';
            if (roleValue) roleValue.value = 'user';
        }

        var disableWrap = qs('#edit-disable-wrap');
        var disableNote = qs('#edit-disable-note');
        var disableCb = qs('#edit-is-disabled');

        if (isPrimary || isSelf) {
            if (disableWrap) disableWrap.style.display = 'none';
            if (disableNote) {
                disableNote.style.display = '';
                disableNote.textContent = isPrimary
                    ? 'Primary admin cannot be disabled.'
                    : 'You cannot disable your own account.';
            }
            if (disableCb) {
                disableCb.checked = false;
                disableCb.disabled = true;
            }
        } else {
            if (disableWrap) disableWrap.style.display = '';
            if (disableNote) disableNote.style.display = 'none';
            if (disableCb) {
                disableCb.disabled = false;
                disableCb.checked = disabled;
            }
        }

        var title = qs('#users-edit-title');
        if (title) title.textContent = 'Edit ' + (btn.getAttribute('data-username') || 'user');
    }

    document.addEventListener('click', function (e) {
        var editBtn = e.target.closest ? e.target.closest('.js-edit-user') : null;
        if (editBtn) {
            e.preventDefault();
            fillEditDrawer(editBtn);
            openOverlay(editOverlay);
            var first = qs('#edit-username');
            if (first) setTimeout(function () { first.focus(); }, 50);
        }
    });

    qsa('[data-close-edit]').forEach(function (btn) {
        btn.addEventListener('click', function () { closeOverlay(editOverlay); });
    });
    if (editOverlay) {
        editOverlay.addEventListener('click', function (e) {
            if (e.target === editOverlay) closeOverlay(editOverlay);
        });
    }

    var roleSelect = qs('#edit-role');
    if (roleSelect) {
        roleSelect.addEventListener('change', function () {
            var roleValue = qs('#edit-role-value');
            if (roleValue) roleValue.value = roleSelect.value;
        });
    }

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (editOverlay && editOverlay.classList.contains('is-open')) {
            closeOverlay(editOverlay);
            return;
        }
        if (createOverlay && createOverlay.classList.contains('is-open')) {
            closeOverlay(createOverlay);
        }
    });

    applyFilters();
})();
