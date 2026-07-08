// pip-window.mjs
// Espelle il canvas del VRM (Synth) in una finestra Document Picture-in-Picture
// separata, trasparente e trascinabile a livello di sistema operativo.
//
// Approccio: spostiamo il nodo DOM esistente (#vrm-canvas) nella finestra PiP,
// così il renderer WebGL e l'intero contesto three.js sopravvivono al move
// (nessuna ricreazione, nessuna perdita di stato animazione/espressione/audio).
// Alla chiusura della finestra PiP riportiamo il canvas nella WebUI.
//
// Il drag/resize avviene sul chrome nativo della finestra PiP: non tocca il
// canvas, quindi non produce alcun effetto "tocco sul modello".
//
// Compatibilità: Document Picture-in-Picture è disponibile solo su browser
// desktop basati su Chromium (Chrome/Edge/Opera). Sugli altri il pulsante
// resta nascosto.

(function initPipWindow() {
    'use strict';

    const SUPPORTED = 'documentPictureInPicture' in window;

    function ready(fn) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', fn, { once: true });
        } else {
            fn();
        }
    }

    ready(() => {
        const btn = document.getElementById('pip-toggle');
        if (!btn) {
            return;
        }

        // Se il browser non supporta Document PiP, il pulsante resta nascosto.
        if (!SUPPORTED) {
            btn.style.display = 'none';
            return;
        }

        btn.style.display = 'flex';

        // Sposta il pulsante PiP nello stesso stack in basso a destra dei
        // pulsanti chat/debug (#synth-minimized-stack), con lo stesso stile
        // condiviso (.synth-dock-btn). Così tutti i pulsanti flottanti sono
        // impilati e coerenti. Se lo stack non esiste ancora (creato lazy da
        // main.js), lo si attende brevemente con un piccolo retry.
        function moveButtonToDock() {
            try {
                const dock = document.getElementById('synth-minimized-stack');
                if (!dock) return false;
                // Rimuove lo stile flottante standalone e adotta quello del dock.
                btn.classList.remove('pip-toggle-btn');
                btn.classList.add('synth-dock-btn');
                btn.style.position = 'static';
                btn.style.top = '';
                btn.style.right = '';
                btn.style.bottom = '';
                btn.style.left = '';
                btn.style.zIndex = '';
                btn.style.display = 'flex';
                // Inserisce il pulsante prima dell'announcer (se presente) così
                // resta in cima allo stack visivo ma dentro il contenitore.
                const announcer = dock.querySelector('.synth-dock-announcer');
                if (announcer) {
                    dock.insertBefore(btn, announcer);
                } else {
                    dock.appendChild(btn);
                }
                return true;
            } catch (e) {
                console.warn('[pip-window] impossibile spostare il pulsante nel dock:', e);
                return false;
            }
        }
        if (!moveButtonToDock()) {
            let tries = 0;
            const retry = setInterval(() => {
                tries += 1;
                if (moveButtonToDock() || tries > 40) {
                    clearInterval(retry);
                }
            }, 150);
        }

        const placeholder = document.getElementById('pip-placeholder');

        // Popola il nome del Synth nel placeholder (come nel loading overlay).
        try {
            const nameEl = document.getElementById('pip-placeholder-name');
            if (nameEl) {
                nameEl.textContent =
                    (window.__SYNTH_CONFIG && window.__SYNTH_CONFIG.SYNTH_NAME) || 'SyntH';
            }
        } catch (e) { /* ignore */ }

        let pipWindow = null;
        // Segnaposto DOM per ricordare dove reinserire il canvas al ritorno.
        let anchor = null;
        let movedNode = null;
        // Posa camera della WebUI principale, salvata all'apertura del PiP e
        // ripristinata alla chiusura (il PiP indietreggia la camera per il
        // framing portrait a corpo intero).
        let savedCameraPose = null;

        function resizeRenderer() {
            try {
                if (typeof window.resizeVRMRenderer === 'function') {
                    window.resizeVRMRenderer();
                }
                // Adatta esplicitamente il framing all'aspect della finestra PiP
                // (portrait) così l'intero corpo del modello resta visibile.
                if (typeof window.fitVRMFramingForAspect === 'function') {
                    window.fitVRMFramingForAspect();
                }
            } catch (e) {
                console.warn('[pip-window] resizeVRMRenderer failed:', e);
            }
        }

        function setActive(active) {
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            if (placeholder) {
                placeholder.style.display = active ? 'flex' : 'none';
            }
        }

        // Riporta il canvas nella WebUI e ripulisce lo stato.
        function restoreCanvas() {
            // Rimuove lo scheduler PiP: il render loop torna al rAF della
            // pagina principale.
            try { window.__synthRafScheduler = null; } catch (e) { /* ignore */ }
            // La voce torna a suonare dalla finestra principale.
            try { window.__synthAudioWindow = null; } catch (e) { /* ignore */ }
            if (movedNode && anchor && anchor.parentNode) {
                anchor.parentNode.insertBefore(movedNode, anchor);
                anchor.remove();
            }
            anchor = null;
            movedNode = null;
            pipWindow = null;
            setActive(false);
            // Ripristina la posa camera che l'utente aveva nella WebUI (lo
            // zoom/rotazione), sovrascritta dal framing portrait del PiP.
            const poseToRestore = savedCameraPose;
            savedCameraPose = null;
            // Riavvia il render loop sulla finestra principale. L'ultimo frame
            // prima della chiusura era stato schedulato sul requestAnimationFrame
            // della finestra PiP (ora morto), quindi senza questo kick la scena
            // resterebbe congelata nella WebUI.
            try {
                if (typeof window.kickVRMRenderLoop === 'function') {
                    window.kickVRMRenderLoop();
                }
            } catch (e) { /* ignore */ }
            // Attende un frame perché il layout della WebUI si ripristini.
            requestAnimationFrame(() => {
                resizeRenderer();
                try {
                    if (poseToRestore && typeof window.restoreVRMCameraPose === 'function') {
                        window.restoreVRMCameraPose(poseToRestore);
                    }
                } catch (e) { /* ignore */ }
                // Secondo kick di sicurezza dopo il ripristino del layout.
                try {
                    if (typeof window.kickVRMRenderLoop === 'function') {
                        window.kickVRMRenderLoop();
                    }
                } catch (e) { /* ignore */ }
            });
        }

        async function openPip() {
            const canvas = document.getElementById('vrm-canvas');
            if (!canvas) {
                console.warn('[pip-window] #vrm-canvas non trovato.');
                return;
            }

            // Salva la posa camera corrente della WebUI per ripristinarla alla
            // chiusura del PiP.
            try {
                if (typeof window.saveVRMCameraPose === 'function') {
                    savedCameraPose = window.saveVRMCameraPose();
                }
            } catch (e) { savedCameraPose = null; }

            try {
                pipWindow = await window.documentPictureInPicture.requestWindow({
                    width: 360,
                    height: 640,
                });
            } catch (e) {
                console.warn('[pip-window] requestWindow rifiutata:', e);
                pipWindow = null;
                return;
            }

            // Replica lo sfondo del Synth (lo stesso della WebUI principale):
            // il canvas three.js è trasparente (alpha:true), quindi lo sfondo
            // scuro/gradiente proviene dal CSS del <body>. Nel PiP dobbiamo
            // riprodurlo, altrimenti la finestra mostra il bianco di default.
            // Leggiamo lo sfondo calcolato del body principale così restiamo
            // sempre coerenti con il tema/accent corrente.
            let pageBackground = '';
            let pageBgColor = '';
            try {
                const bodyStyle = window.getComputedStyle(document.body);
                pageBackground = bodyStyle.backgroundImage || '';
                pageBgColor = bodyStyle.backgroundColor || '';
            } catch (e) { /* ignore */ }
            // Fallback allineato al tema di default (--bg: #0d0d16).
            if (!pageBgColor || pageBgColor === 'rgba(0, 0, 0, 0)') {
                pageBgColor = '#0d0d16';
            }
            const bgImageDecl =
                pageBackground && pageBackground !== 'none'
                    ? `background-image: ${pageBackground};`
                    : '';

            // Stile minimale: sfondo del Synth, canvas a piena finestra.
            const style = pipWindow.document.createElement('style');
            style.textContent = `
                html, body {
                    margin: 0;
                    padding: 0;
                    width: 100%;
                    height: 100%;
                    overflow: hidden;
                    background-color: ${pageBgColor};
                    ${bgImageDecl}
                    background-repeat: no-repeat;
                    background-attachment: fixed;
                }
                #vrm-canvas {
                    display: block;
                    width: 100% !important;
                    height: 100% !important;
                    background: transparent;
                }
            `;
            pipWindow.document.head.appendChild(style);

            // Lascia un segnaposto per sapere dove reinserire il canvas dopo.
            anchor = document.createComment('vrm-canvas-anchor');
            canvas.parentNode.insertBefore(anchor, canvas);

            movedNode = canvas;
            pipWindow.document.body.appendChild(canvas);
            setActive(true);

            // Guida il render loop con il requestAnimationFrame della finestra
            // PiP. La finestra PiP è sempre visibile a schermo, quindi il suo
            // rAF continua a scattare a pieno frame rate anche quando la tab
            // principale (opener) perde il focus o viene nascosta — così il
            // rendering dell'avatar prosegue con il browser in background.
            try {
                const pw = pipWindow;
                window.__synthRafScheduler = (cb) => pw.requestAnimationFrame(cb);
            } catch (e) {
                console.warn('[pip-window] impossibile installare lo scheduler rAF PiP:', e);
            }

            // Instrada la voce del Synth in modo che esca dalla finestra PiP
            // (la bocca dell'avatar è lì). chat-window.mjs crea Audio +
            // AudioContext da questa finestra al prossimo tts-play.
            try {
                window.__synthAudioWindow = pipWindow;
            } catch (e) {
                console.warn('[pip-window] impossibile instradare l\'audio nel PiP:', e);
            }

            // Adatta il renderer alle dimensioni della finestra PiP.
            requestAnimationFrame(() => resizeRenderer());

            // La chiusura della finestra PiP vale come "ritorno Synth".
            pipWindow.addEventListener('pagehide', restoreCanvas, { once: true });
            // Adatta il renderer quando l'utente ridimensiona la finestra.
            pipWindow.addEventListener('resize', resizeRenderer);
        }

        btn.addEventListener('click', () => {
            if (pipWindow) {
                // Toggle: se la finestra è aperta, chiudila (→ ritorno Synth).
                try {
                    pipWindow.close();
                } catch (e) {
                    console.warn('[pip-window] close failed:', e);
                    restoreCanvas();
                }
            } else {
                openPip();
            }
        });
    });
})();
