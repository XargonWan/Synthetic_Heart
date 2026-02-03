(function(){
'use strict';

window.SynthUtils = window.SynthUtils || {};

window.SynthUtils.debounce = function(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
};

window.SynthUtils.escapeHtml = function(text) {
    if (text === undefined || text === null) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
};

window.SynthUtils.formatTimestamp = function(isoString) {
    if (!isoString) return 'Unknown time';
    try {
        let normalized = String(isoString).trim();
        if (!normalized.endsWith('Z') && !/[+-]\d{2}:\d{2}$/.test(normalized)) normalized += 'Z';
        const parsed = new Date(normalized);
        if (Number.isNaN(parsed.getTime())) return isoString;
        const localTZ = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
        return new Intl.DateTimeFormat('it-IT', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false, timeZone: localTZ, timeZoneName: 'short'
        }).format(parsed);
    } catch (e) {
        return isoString;
    }
};

window.SynthUtils.formatJsonBlock = function(value) {
    if (value === undefined || value === null || value === '') return '';
    try {
        const normalized = typeof value === 'string' ? JSON.parse(value) : value;
        return window.SynthUtils.escapeHtml(JSON.stringify(normalized, null, 2));
    } catch (e) {
        return window.SynthUtils.escapeHtml(String(value));
    }
};

})();
