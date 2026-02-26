(function () {
    const state = {
        general: [],
        advanced: [],
        renderGrouped: null,
        configGeneralListEl: null,
        configAdvancedListEl: null,
        statusEl: null,
        bound: false
    };

    const normalize = (val) => (val === null || val === undefined) ? '' : String(val).toLowerCase();

    const buildHaystack = (item) => {
        return [
            item.key,
            item.label,
            item.description,
            item.component_label,
            item.component,
            item.component_description,
            item.group,
            item.group_label
        ].map(normalize).join(' ');
    };

    const filterList = (list, query) => {
        const terms = normalize(query).split(/\s+/).filter(Boolean);
        if (!terms.length) return list;
        return list.filter((item) => {
            const haystack = buildHaystack(item);
            return terms.every((term) => haystack.includes(term));
        });
    };

    const applyFilter = (queryRaw) => {
        if (!state.renderGrouped || !state.configGeneralListEl || !state.configAdvancedListEl) return;
        const filteredGeneral = filterList(state.general, queryRaw);
        const filteredAdvanced = filterList(state.advanced, queryRaw);
        state.renderGrouped(filteredGeneral, state.configGeneralListEl);
        state.renderGrouped(filteredAdvanced, state.configAdvancedListEl);
        if (state.statusEl) {
            if (queryRaw && String(queryRaw).trim()) {
                const total = state.general.length + state.advanced.length;
                const shown = filteredGeneral.length + filteredAdvanced.length;
                state.statusEl.textContent = `Showing ${shown} of ${total} settings`;
            } else {
                state.statusEl.textContent = '';
            }
        }
    };

    const bindConfigSearch = (opts) => {
        state.general = Array.isArray(opts.general) ? opts.general : [];
        state.advanced = Array.isArray(opts.advanced) ? opts.advanced : [];
        state.renderGrouped = opts.renderGrouped;
        state.configGeneralListEl = opts.configGeneralListEl;
        state.configAdvancedListEl = opts.configAdvancedListEl;
        state.statusEl = opts.statusEl || null;

        const inputEl = document.getElementById('config-search');
        const clearEl = document.getElementById('config-search-clear');

        if (!state.bound && inputEl) {
            state.bound = true;
            const triggerFilter = () => applyFilter(inputEl.value);
            inputEl.addEventListener('input', triggerFilter);
            inputEl.addEventListener('search', triggerFilter);
            if (clearEl) {
                clearEl.addEventListener('click', () => {
                    inputEl.value = '';
                    triggerFilter();
                    try { inputEl.focus(); } catch (e) { /* ignore */ }
                });
            }
        }

        applyFilter(inputEl ? inputEl.value : '');
    };

    window.SynthSettings = window.SynthSettings || {};
    window.SynthSettings.bindConfigSearch = bindConfigSearch;
    window.SynthSettings.applyConfigSearch = applyFilter;
})();
