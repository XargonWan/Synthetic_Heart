/*
 * theme.js — take the WebUI's colours.
 *
 * This page is served from the Core's own origin (the extension has web.origin), so it
 * can read the WebUI page that frames it. It copies the theme variables once and again
 * whenever the WebUI changes them; with no WebUI around (the page opened on its own) the
 * defaults written in the stylesheet apply.
 */
(function () {
    'use strict';
    var NAMES = [
        '--bg', '--surface', '--surface-alt', '--panel-bg', '--border', '--border-strong',
        '--accent', '--accent-dark', '--accent-soft', '--accent-contrast',
        '--accent-r', '--accent-g', '--accent-b',
        '--text', '--text-soft', '--success', '--warn', '--error'
    ];

    function copy() {
        var parent;
        try {
            parent = window.parent !== window ? window.parent : null;
            if (!parent || !parent.document) { return; }
            var style = parent.getComputedStyle(parent.document.documentElement);
            var body = parent.getComputedStyle(parent.document.body);
            NAMES.forEach(function (name) {
                var value = (style.getPropertyValue(name) || body.getPropertyValue(name) || '').trim();
                if (value) {
                    document.documentElement.style.setProperty(name, value);
                }
            });
        } catch (e) {
            // Not same-origin or no parent: keep the defaults.
        }
    }

    copy();
    setInterval(copy, 3000);
})();
