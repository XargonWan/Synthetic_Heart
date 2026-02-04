// ui-helpers.js — UI compatibility helpers and window/resize utilities

// Reset window positions to sensible defaults (no-op if not needed)
function resetWindowPositions(forceConfirm) {
    try {
        // Best-effort: if applyDefaultWindowPositions is available, call it
        if (typeof window.applyDefaultWindowPositions === 'function') {
            try { window.applyDefaultWindowPositions(); } catch (e) { /* ignore */ }
            return;
        }
        // Otherwise provide a safe no-op fallback
        return;
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

export { resetWindowPositions, addCardCollapsers, applyDefaultWindowPositions };
