/*
 * radio.js — the Radio window: station status and the synth's remarks on air.
 *
 * Data comes from this extension's own routes (api/data, api/audio?id=N); relative URLs,
 * so it works under /x/<id>/ whatever the Core's address. Every value from the server is
 * put in with textContent, never as markup.
 */
(function () {
    'use strict';

    var root = document.getElementById('root');

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) { node.className = className; }
        if (text !== undefined && text !== null) { node.textContent = text; }
        return node;
    }

    function when(stamp) {
        if (!stamp) { return '—'; }
        var date = new Date(/(Z|[+-]\d\d:\d\d)$/.test(stamp) ? stamp : stamp + 'Z');
        return isNaN(date.getTime()) ? stamp : date.toLocaleString();
    }

    function setup() {
        var card = el('div', 'setup');
        card.appendChild(el('h3', '', '📻 AzuraCast Radio DJ'));
        card.appendChild(el('p', '', 'Synth can be the AI DJ of your AzuraCast station, speaking between songs.'));
        var steps = el('ol');
        [
            'Open this extension\'s settings (Plugins).',
            'Fill in the AzuraCast Base URL, API Key and Station ID.',
            'Give the WebDJ streamer username and password of the station.'
        ].forEach(function (line) { steps.appendChild(el('li', '', line)); });
        card.appendChild(steps);
        return card;
    }

    function entry(activity) {
        var box = el('div', 'entry');
        var meta = el('div', 'meta');
        meta.appendChild(el('span', '', '📻 ' + when(activity.timestamp)));
        meta.appendChild(el('span', 'badge', activity.style || 'transition'));
        if (activity.status && activity.status !== 'success') {
            meta.appendChild(el('span', 'badge bad', activity.status));
        }
        box.appendChild(meta);
        box.appendChild(el('div', 'track', (activity.track_artist || '?') + ' — ' + (activity.track_title || '?')));
        box.appendChild(el('div', 'banter', activity.banter_text || '—'));
        if (activity.audio_url) {
            var audio = el('audio');
            audio.controls = true;
            audio.preload = 'none';
            audio.src = activity.audio_url;
            box.appendChild(audio);
        }
        return box;
    }

    function view(data) {
        var frag = document.createDocumentFragment();
        var bar = el('div', 'bar');
        bar.appendChild(el('span', 'dot' + (data.online ? ' online' : '')));
        bar.appendChild(el('strong', '', data.station_name || 'Radio Host'));
        var parts = [
            data.online ? 'Connected' : 'Disconnected',
            data.language || 'English',
            data.schedule_description,
            'Poll: ' + data.poll_interval + 's',
            data.intermission > 1 ? 'Every ' + data.intermission + ' songs' : 'Every song'
        ].filter(Boolean);
        bar.appendChild(el('span', '', '· ' + parts.join(' · ')));
        var reload = el('button', 'grow', '↺ Reload');
        reload.type = 'button';
        reload.addEventListener('click', load);
        bar.appendChild(reload);
        frag.appendChild(bar);
        if (data.activities && data.activities.length) {
            data.activities.forEach(function (a) { frag.appendChild(entry(a)); });
        } else {
            frag.appendChild(el('div', 'empty', 'No radio activity yet. Wait for a track change.'));
        }
        return frag;
    }

    function show(node) {
        root.textContent = '';
        root.appendChild(node);
    }

    function load() {
        fetch('api/data', { headers: { 'Accept': 'application/json' } })
            .then(function (resp) {
                if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
                return resp.json();
            })
            .then(function (data) { show(data.configured ? view(data) : setup()); })
            .catch(function () { show(el('div', 'empty', 'Could not load radio activity.')); });
    }

    load();
    setInterval(function () {
        // Do not rebuild the page under a clip that is playing.
        var playing = Array.prototype.some.call(root.querySelectorAll('audio'), function (a) { return !a.paused; });
        if (!playing) { load(); }
    }, 30000);
})();
