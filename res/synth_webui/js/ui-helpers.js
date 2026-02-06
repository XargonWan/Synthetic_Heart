// ui-helpers.js — UI compatibility helpers and window/resize utilities

// Reset window positions to sensible defaults (no-op if not needed)
function resetWindowPositions(forceConfirm) {
    try {
        // Prefer a simple DOM-level fallback that moves a handful of known windows
        const topbar = (document.querySelector('header.top-bar') && document.querySelector('header.top-bar').getBoundingClientRect()) ? Math.ceil(document.querySelector('header.top-bar').getBoundingClientRect().height) : 0;
        // If a manager is present, try to reposition primary windows below the topbar
        if (typeof window.SynthWindowManager !== 'undefined' && window.SynthWindowManager) {
            const mgr = window.SynthWindowManager;
            const ids = ['chat', 'debug', 'archive'];
            let offsetX = 24;
            const viewportH = window.innerHeight || document.documentElement.clientHeight || 0;
            const margin = 24;
            ids.forEach((id) => {
                try {
                    const wb = mgr.get(id);
                    if (wb && typeof wb.move === 'function') {
                        let y = Math.max(topbar, 80);
                        if (id === 'chat') {
                            let height = 320;
                            try {
                                const winEl = wb.window || wb.dom || wb.g || null;
                                if (winEl && winEl.getBoundingClientRect) {
                                    const rect = winEl.getBoundingClientRect();
                                    height = rect.height || height;
                                } else if (typeof wb.height === 'number') {
                                    height = wb.height;
                                }
                            } catch (e) { /* ignore */ }
                            if (viewportH) {
                                y = Math.max(topbar, Math.round(viewportH - height - margin));
                            }
                        }
                        try { wb.move(offsetX, y); } catch (e) { /* ignore */ }
                        offsetX += 48;
                    }
                } catch (e) { /* ignore per-window errors */ }
            });
            return;
        }
        // Otherwise call generic applyDefaultWindowPositions if available
        if (typeof window.applyDefaultWindowPositions === 'function') {
            try { window.applyDefaultWindowPositions(); } catch (e) { /* ignore */ }
        }
    } catch (e) { /* ignore */ }
}

// Provide a small helper to add card collapsers (compatibility)
function addCardCollapsers() {
    try {
        // No-op: the real implementation lives in VRM/Animation modules
        return;
    } catch (e) { /* ignore */ }
}

// Expose a fallback applyDefaultWindowPositions that triggers resize helpers
function applyDefaultWindowPositions() {
    try {
        // Try to call createResizeHandlesForElement if available (vrm-viewer has it)
        if (typeof createResizeHandlesForElement === 'function') {
            // Find all card elements and ensure resize handles
            const cards = document.querySelectorAll('.card');
            for (const c of cards) {
                try { createResizeHandlesForElement(c); } catch (e) { /* ignore */ }
            }
        }
    } catch (e) { /* ignore */ }
}

// Expose to global scope for legacy callers
window.resetWindowPositions = window.resetWindowPositions || resetWindowPositions;
window.addCardCollapsers = window.addCardCollapsers || addCardCollapsers;
window.applyDefaultWindowPositions = window.applyDefaultWindowPositions || applyDefaultWindowPositions;

// Note: this file is intended to be loaded as a regular script (not necessarily an ES module).
// If module semantics are required, load it via <script type="module"> and add exports there.

