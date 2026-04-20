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

window.formatTimestamp = window.formatTimestamp || window.SynthUtils.formatTimestamp;

window.showToast = window.showToast || function(message, isError) {
    try {
        const existing = document.getElementById('synth-toast-container');
        const container = existing || (() => {
            const el = document.createElement('div');
            el.id = 'synth-toast-container';
            el.style.position = 'fixed';
            el.style.right = '24px';
            el.style.bottom = '24px';
            el.style.zIndex = '99999';
            el.style.display = 'flex';
            el.style.flexDirection = 'column';
            el.style.gap = '8px';
            document.body.appendChild(el);
            return el;
        })();

        const toast = document.createElement('div');
        toast.textContent = String(message || '');
        toast.style.padding = '10px 14px';
        toast.style.borderRadius = '12px';
        toast.style.background = isError ? 'rgba(255, 123, 147, 0.92)' : 'rgba(24, 201, 140, 0.92)';
        toast.style.color = '#0b0b12';
        toast.style.fontWeight = '600';
        toast.style.boxShadow = '0 10px 24px rgba(0,0,0,0.35)';
        container.appendChild(toast);
        setTimeout(() => {
            try { toast.remove(); } catch (e) { /* ignore */ }
        }, 3200);
    } catch (e) { /* ignore */ }
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
