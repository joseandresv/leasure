/* Leasure — shared JavaScript module */

/* ── Utilities ── */

function formatDuration(ms) {
    if (!ms) return '--:--';
    const s = Math.floor(ms / 1000);
    const min = Math.floor(s / 60);
    const sec = s % 60;
    return `${min}:${sec.toString().padStart(2, '0')}`;
}

function formatSize(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    let size = bytes;
    while (size >= 1024 && i < units.length - 1) {
        size /= 1024;
        i++;
    }
    return `${size.toFixed(1)} ${units[i]}`;
}

/* ── Toast Notification System (Alpine.js store) ── */

document.addEventListener('alpine:init', () => {
    Alpine.store('toast', {
        items: [],
        add(message, type = 'info') {
            const id = Date.now();
            this.items.push({ id, message, type });
            setTimeout(() => this.remove(id), 4000);
        },
        remove(id) {
            this.items = this.items.filter(t => t.id !== id);
        }
    });
});

/* ── HTMX Event Hooks ── */

// Show toast on download responses via custom header
document.addEventListener('htmx:afterRequest', function(event) {
    const xhr = event.detail.xhr;
    if (!xhr) return;
    const toast = xhr.getResponseHeader('HX-Trigger-After-Swap');
    if (toast) {
        try {
            const data = JSON.parse(toast);
            if (data.showToast) {
                Alpine.store('toast').add(data.showToast.message, data.showToast.type);
            }
        } catch (e) { /* ignore */ }
    }
});

// Log HTMX errors
document.addEventListener('htmx:responseError', function(event) {
    console.error('HTMX error:', event.detail);
    if (Alpine.store('toast')) {
        Alpine.store('toast').add('Request failed', 'error');
    }
});

// Add loading class to buttons during HTMX requests
document.addEventListener('htmx:beforeRequest', function(event) {
    const el = event.detail.elt;
    if (el.classList && el.classList.contains('dl-btn')) {
        if (!el.dataset.dlLabel) el.dataset.dlLabel = el.textContent.trim();
        el.disabled = true;
        el.textContent = '...';
    }
});

// A failed download POST is not swapped, so the button would stay dead at '...'
['htmx:afterRequest', 'htmx:responseError', 'htmx:sendError'].forEach(function(name) {
    document.addEventListener(name, function(event) {
        if (event.detail.successful) return;
        const el = event.detail.elt;
        if (!el || !el.classList || !el.classList.contains('dl-btn')) return;
        el.disabled = false;
        if (el.dataset.dlLabel) el.textContent = el.dataset.dlLabel;
    });
});

/* ── Detail modals ── */

function setDetailNote(body, text) {
    const note = document.createElement('p');
    note.className = 'detail-note';
    note.textContent = text;
    body.replaceChildren(note);
}

// An opener clears the modal before showing it, so the previous record never stands
// in for the one still loading. htmx has already sent its request by the time this
// delegated listener runs.
document.addEventListener('click', function(event) {
    const opener = event.target.closest && event.target.closest('[data-opens-detail]');
    if (!opener) return;
    const body = document.querySelector(opener.getAttribute('hx-target'));
    const modal = body && body.closest('dialog');
    if (!modal) return;
    setDetailNote(body, opener.dataset.opensDetail || 'Loading…');
    if (!modal.open) modal.showModal();
});

['htmx:responseError', 'htmx:sendError'].forEach(function(name) {
    document.addEventListener(name, function(event) {
        const body = event.detail.target;
        if (!body || !body.classList.contains('modal-body')) return;
        setDetailNote(body, name === 'htmx:sendError'
            ? 'Leasure did not answer. Check that it is still running, then try again.'
            : 'This could not be loaded. Close this window and try again.');
    });
});

/* ── Cover art ── */

// Covers on CDNs that send no CORS header fail to load as WebGL textures or
// crossorigin images; the server re-serves them from our origin. Hosts known to
// send none go through it on the first try, others only after a failure.
const NO_CORS_COVER_HOSTS = new Set(['www.gstatic.com']);

function absoluteUrl(url) {
    try { return new URL(url); } catch (e) { return null; }
}

function coverProxyUrl(url) {
    const parsed = absoluteUrl(url);
    if (!parsed || !/^https?:$/.test(parsed.protocol) || parsed.origin === location.origin) return null;
    return '/api/art/proxy?url=' + encodeURIComponent(parsed.href);
}

function coverLoadUrl(url) {
    const parsed = absoluteUrl(url);
    return (parsed && NO_CORS_COVER_HOSTS.has(parsed.hostname) && coverProxyUrl(url)) || url;
}

/* ── Album Art Color Extraction (Vibrant.js) ── */

// Vibrant.js 1.0.0 appends a scratch <canvas> to <body> for sampling and never
// removes it. Sweep those leaks after each use (the device canvas is nested in
// #scene-container, so direct-child body canvases are all Vibrant's).
function sweepVibrantCanvases() {
    document.querySelectorAll('body > canvas').forEach(c => c.remove());
}

function extractAlbumColor(imgElement) {
    if (!window.Vibrant) return;
    try {
        const vibrant = new Vibrant(imgElement, 16);
        const swatches = vibrant.swatches();
        const swatch = swatches.Vibrant || swatches.Muted || swatches.DarkVibrant;
        if (swatch) {
            const [r, g, b] = swatch.getRgb();
            const card = imgElement.closest('.album-card') || imgElement.closest('.album-hero');
            if (card) {
                card.style.setProperty('--album-accent', `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`);
                card.style.setProperty('--album-accent-glow', `rgba(${Math.round(r)},${Math.round(g)},${Math.round(b)},0.2)`);
            }
        }
    } catch (e) { /* CORS or decode error — use default accent */ }
    finally { sweepVibrantCanvases(); }
}

function initColorExtraction(root) {
    const target = root || document;
    target.querySelectorAll('img[data-vibrant]').forEach(img => {
        if (img.dataset.vibrantDone) return;
        img.dataset.vibrantDone = '1';
        if (img.complete && img.naturalWidth > 0) {
            extractAlbumColor(img);
        } else {
            img.addEventListener('load', () => extractAlbumColor(img), { once: true });
        }
    });
}

// Run color extraction after HTMX swaps new content
document.addEventListener('htmx:afterSettle', function(event) {
    initColorExtraction(event.detail.target);
});

// Run on initial page load
document.addEventListener('DOMContentLoaded', function() {
    initColorExtraction();
});

/* ── Device Sync SSE Handler ── */

// Loose comparison (trailing separator, drive-letter case) so a path the server
// would accept is never blocked here.
function isDetectedDrive(candidate) {
    var wanted = String(candidate).replace(/[\\/]+$/, '').toLowerCase();
    return (window.leasureDrives || []).some(function(p) {
        return String(p).replace(/[\\/]+$/, '').toLowerCase() === wanted;
    });
}

// Strings are plain text, {n} are counts to emphasise; shared by the result box and the toast.
function syncCountSegments(data, hasErrors) {
    var segs;
    if (!data.copied && !data.skipped) {
        segs = [hasErrors ? 'Nothing copied' : 'No downloaded tracks in this scope'];
    } else {
        segs = data.copied ? [{ n: data.copied }, ' copied (' + data.size_mb + ' MB)']
            : [hasErrors ? 'Nothing copied — ' : 'Nothing new — '];
        if (data.copied && data.skipped) segs.push(' · ');
        if (data.skipped) segs.push({ n: data.skipped }, ' already on the card');
    }
    if (data.playlists) segs.push(' · ', { n: data.playlists }, ' playlists generated');
    return segs;
}

var SYNC_ERRORS_SHOWN = 50;

function syncErrorDetails(errors, label, open) {
    var details = document.createElement('details');
    details.open = open;
    var summary = document.createElement('summary');
    summary.textContent = label;
    details.appendChild(summary);
    var ul = document.createElement('ul');
    var shown = errors.slice(0, SYNC_ERRORS_SHOWN);
    if (errors.length > shown.length) shown.push('… and ' + (errors.length - shown.length) + ' more');
    shown.forEach(function(err) {
        var li = document.createElement('li');
        var small = document.createElement('small');
        small.textContent = err;
        li.appendChild(small);
        ul.appendChild(li);
    });
    details.appendChild(ul);
    return details;
}

function startSync(e) {
    e.preventDefault();
    var path = document.getElementById('device-path').value;
    var scope = document.querySelector('input[name="scope"]:checked').value;
    var btn = document.getElementById('sync-btn');
    var status = document.getElementById('sync-status');

    function resetButton() {
        btn.disabled = false;
        btn.textContent = 'SYNC TO DEVICE';
        btn.classList.remove('syncing');
    }
    // kind 'error' = the sync was attempted and failed, 'warning' = it was refused
    // before anything ran or its outcome is unknown, so the toast must not claim a failure.
    function showNotice(message, kind, heading) {
        resetButton();
        status.textContent = '';
        var box = document.createElement('div');
        box.className = 'sync-result';
        if (kind === 'error') {
            box.classList.add('sync-result-error');
        } else {
            box.classList.add('sync-result-warning');
        }
        if (heading) {
            var h4 = document.createElement('h4');
            h4.textContent = heading;
            box.appendChild(h4);
        }
        var p = document.createElement('p');
        p.textContent = message;
        box.appendChild(p);
        status.appendChild(box);
        if (Alpine.store('toast')) {
            Alpine.store('toast').add(kind === 'error' ? 'Sync failed: ' + message : message, kind);
        }
    }
    function showError(message) {
        showNotice(message, 'error', 'Sync failed');
    }

    // The server is the source of truth and answers an unknown target with 400;
    // this only spares the browser that failed request for the usual typo. Drives
    // are undefined until a scan has run — then fall through to the server.
    if (window.leasureDrives && !isDetectedDrive(path)) {
        showNotice('Not a detected drive — click Scan and pick the H2.', 'warning');
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Syncing...';
    btn.classList.add('syncing');

    status.innerHTML = '<div class="sync-progress-container">' +
        '<div class="progress-bar"><div class="progress-bar-fill" id="sync-progress-fill" style="width:0%"></div></div>' +
        '<p id="sync-detail" class="sync-detail"></p></div>';

    // The sync is created with a POST (a cross-site page cannot forge that from a
    // plain navigation), then its progress is streamed by job id.
    var body = new URLSearchParams();
    body.set('device_path', path);
    body.set('scope', scope);
    fetch('/api/device/sync/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString()
    }).then(function(resp) {
        return resp.json().then(function(data) {
            if (!resp.ok) { throw new Error(data.detail || ('HTTP ' + resp.status)); }
            return data;
        });
    }).then(function(job) {
        streamSync(job.job_id);
    }).catch(function(err) {
        showError(err.message || 'Could not start sync');
    });

    function streamSync(jobId) {
        var es = new EventSource('/api/device/sync/stream/' + encodeURIComponent(jobId));
        var total = 0;

        es.onmessage = function(event) {
            var data = JSON.parse(event.data);
            var fill = document.getElementById('sync-progress-fill');
            var detail = document.getElementById('sync-detail');

            if (data.type === 'start') {
                total = data.total;
                detail.textContent = 'Starting sync of ' + data.total + ' tracks...';
            } else if (data.type === 'progress') {
                var pct = total > 0 ? Math.round((data.current / total) * 100) : 0;
                fill.style.width = pct + '%';
                // Provider metadata goes in as text, never as markup.
                detail.textContent = '';
                var strong = document.createElement('strong');
                strong.textContent = data.synced + '/' + data.total;
                detail.appendChild(strong);
                detail.appendChild(document.createTextNode(' · ' + data.artist + ' - ' + data.track));
            } else if (data.type === 'playlists') {
                detail.textContent = data.message;
            } else if (data.type === 'done') {
                es.close();
                resetButton();

                var errors = data.errors || [];
                var kind = data.total === 0 ? 'info'
                    : !errors.length ? 'success' : (data.synced ? 'warning' : 'error');
                var errorLabel = errors.length + (errors.length === 1 ? ' error' : ' errors');
                var heading = {
                    info: 'Nothing to sync', success: 'Sync Complete',
                    warning: 'Sync finished with ' + errorLabel, error: 'Sync failed'
                }[kind];
                var segs = syncCountSegments(data, errors.length > 0);

                status.textContent = '';
                var box = document.createElement('div');
                box.className = 'sync-result' + (kind === 'info' ? '' : ' sync-result-' + kind);
                var h4 = document.createElement('h4');
                h4.textContent = heading;
                box.appendChild(h4);
                var p = document.createElement('p');
                segs.forEach(function(seg) {
                    if (typeof seg === 'string') {
                        p.appendChild(document.createTextNode(seg));
                    } else {
                        var n = document.createElement('strong');
                        n.textContent = seg.n;
                        p.appendChild(n);
                    }
                });
                box.appendChild(p);
                if (errors.length > 0) {
                    box.appendChild(syncErrorDetails(errors, errorLabel, !data.copied));
                }
                status.appendChild(box);

                if (Alpine.store('toast')) {
                    var countsText = segs.map(function(seg) { return typeof seg === 'string' ? seg : seg.n; }).join('');
                    var toastText = kind === 'info' ? heading
                        : heading + ': ' + (kind === 'error' ? errorLabel + ', nothing copied' : countsText);
                    Alpine.store('toast').add(toastText, kind);
                }

                htmx.ajax('GET', '/api/device/files/html?device_path=' + encodeURIComponent(path), '#device-files');
                htmx.ajax('GET', '/api/device/diff/html?device_path=' + encodeURIComponent(path), '#sync-diff');
            } else if (data.type === 'error') {
                es.close();
                showError(data.message);
            }
        };

        es.onerror = function() {
            es.close();
            showNotice('Connection to the sync stream was lost. The sync keeps running on the server — refresh the Device page to see the result.', 'warning');
        };
    }
}

/* ── Device Drive Selection ── */

function selectDrive(path) {
    document.getElementById('device-path').value = path;
    var enc = encodeURIComponent(path);
    htmx.ajax('GET', '/api/device/files/html?device_path=' + enc, '#device-files');
    htmx.ajax('GET', '/api/device/diff/html?device_path=' + enc, '#sync-diff');
}

// After an eject /mnt/<letter> is an empty folder; a sync target still pointing
// there must not survive.
document.addEventListener('htmx:afterSwap', function(event) {
    var detail = event.detail;
    if (!detail.pathInfo || detail.pathInfo.requestPath !== '/api/device/eject') return;
    var result = detail.target && detail.target.querySelector('[data-mount-status]');
    if (!result || result.dataset.mountStatus !== 'unmounted') return;
    var letter = String(detail.requestConfig.formData.get('letter') || '').trim().toLowerCase();
    var input = document.getElementById('device-path');
    if (!letter || !input) return;
    if (input.value.trim().replace(/\/+$/, '').toLowerCase() === '/mnt/' + letter) clearSyncTarget();
});

function clearSyncTarget() {
    document.getElementById('device-path').value = '';
    showDevicePlaceholder('sync-diff', 'Select a drive to see what will be synced');
    showDevicePlaceholder('device-files', 'Select a drive above to browse music on device');
}

// A rescan settles what the last eject said: a card that shows up mounted again (by
// hand, from PowerShell) loses its "safe to remove" note, which also lets automount
// run again, and a target the scan no longer lists is dropped as after an eject.
document.addEventListener('htmx:afterSettle', function(event) {
    var id = event.detail.target && event.detail.target.id;
    if (id === 'device-list') {
        var input = document.getElementById('device-path');
        if (input && input.value.trim() && window.leasureDrives && !isDetectedDrive(input.value.trim())) clearSyncTarget();
        clearEjectNoteIf(function(letter) { return isDetectedDrive('/mnt/' + letter); });
    } else if (id === 'removable-list') {
        clearEjectNoteIf(function(letter) {
            return !!document.querySelector('#removable-list .drive-row[data-state="mounted"][data-letter="' + letter + '"]');
        });
    }
});

function clearEjectNoteIf(isMounted) {
    var note = document.querySelector('#mount-result [data-mount-status="unmounted"][data-mount-letter]');
    var letter = note && note.dataset.mountLetter;
    if (letter && /^[a-z]$/.test(letter) && isMounted(letter)) document.getElementById('mount-result').replaceChildren();
}

function showDevicePlaceholder(id, text) {
    var box = document.getElementById(id);
    if (!box) return;
    var note = document.createElement('small');
    note.style.color = 'var(--text-muted)';
    note.textContent = text;
    var p = document.createElement('p');
    p.appendChild(note);
    box.replaceChildren(p);
}

// A diff or file list requested for a drive that is no longer the target (ejected
// meanwhile) must not repaint the views the eject just cleared.
document.addEventListener('htmx:beforeSwap', function(event) {
    var path = event.detail.pathInfo && event.detail.pathInfo.requestPath;
    if (!path || !/^\/api\/device\/(diff|files)\/html\?/.test(path)) return;
    var input = document.getElementById('device-path');
    var wanted = new URL(path, window.location.href).searchParams.get('device_path');
    if (input && wanted !== input.value) event.detail.shouldSwap = false;
});

// Eject only unmounts inside WSL, so Windows still reports the card: automount would
// mount it straight back. While the eject message is showing, rescans do not mount.
function leasureJustEjected() {
    return !!document.querySelector('#mount-result [data-mount-status="unmounted"]');
}

// A rescan mounts or finds the card again, so the last mount/eject outcome is stale.
document.addEventListener('htmx:beforeRequest', function(event) {
    if (!event.detail.elt.hasAttribute('data-automount')) return;
    var result = document.getElementById('mount-result');
    if (result) result.replaceChildren();
});

/* ── Genre Graph (Sigma.js) ── */

var GRAPH_LABEL = {
    font: 'Orbitron, JetBrains Mono, system-ui, sans-serif',
    weight: '600',
    size: 11,
    nodeSize: 10
};

// Room the widest label needs to the right of its node, capped so the margin never
// eats more than a quarter of the frame.
function labelStagePadding(labels, container) {
    var ctx = document.createElement('canvas').getContext('2d');
    if (!ctx) return 40;
    ctx.font = GRAPH_LABEL.weight + ' ' + GRAPH_LABEL.size + 'px ' + GRAPH_LABEL.font;
    var widest = labels.reduce(function(w, label) {
        return Math.max(w, ctx.measureText(label).width);
    }, 0);
    return Math.round(Math.min(GRAPH_LABEL.nodeSize + 6 + widest, container.clientHeight / 4));
}

// Album titles repeat across artists ("Greatest Hits"); name the artist only on the
// nodes that would otherwise be indistinguishable.
function graphLabels(nodes) {
    var titleCount = Object.create(null);
    nodes.forEach(function(node) {
        var title = node.label || '';
        titleCount[title] = (titleCount[title] || 0) + 1;
    });
    return nodes.map(function(node) {
        var title = node.label || '';
        return (titleCount[title] > 1 && node.artist) ? title + ' — ' + node.artist : title;
    });
}

function initGraph(event) {
    var container = document.getElementById('genre-graph');
    if (!container || !window.graphology || !window.Sigma) return;

    var data;
    try {
        data = JSON.parse(event.detail.xhr.responseText);
    } catch (e) { return; }

    if (!data.nodes || data.nodes.length < 2) {
        container.innerHTML = '<p class="graph-empty">Add more music to see genre connections.</p>';
        return;
    }

    var graph = new graphology.Graph();
    var labels = graphLabels(data.nodes);

    data.nodes.forEach(function(node, i) {
        var genreKey = (node.genres[0] || '').toLowerCase();
        var color = (data.genres[genreKey] || {}).color || '#38d6ff';
        graph.addNode(node.id, {
            label: labels[i],
            title: node.label,
            x: Math.random() * 100,
            y: Math.random() * 100,
            size: GRAPH_LABEL.nodeSize,
            color: color,
            image: node.image,
            artist: node.artist
        });
    });

    data.edges.forEach(function(edge) {
        if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
            try {
                graph.addEdge(edge.source, edge.target, {
                    color: 'rgba(56,214,255,0.12)',
                    size: 1
                });
            } catch (e) { /* duplicate edge */ }
        }
    });

    // Run ForceAtlas2 layout
    var forceAtlas2 = window.graphologyLibrary && window.graphologyLibrary.layoutForceAtlas2;
    if (forceAtlas2) {
        var settings = forceAtlas2.inferSettings(graph);
        settings.gravity = 1;
        forceAtlas2.assign(graph, { settings: settings, iterations: 100 });
    }

    // Render. Sigma frames node *centres* and draws each label to the right of its
    // node, so the right-most label runs past the canvas unless the stage padding —
    // a pixel margin kept on every edge — covers the widest label. Rescaling the
    // layout coordinates would not help: Sigma re-normalises them to the node extent.
    var renderer = new Sigma(graph, container, {
        renderLabels: true,
        labelColor: { color: '#cfe6ff' },
        labelFont: GRAPH_LABEL.font,
        labelWeight: GRAPH_LABEL.weight,
        labelSize: GRAPH_LABEL.size,
        defaultEdgeColor: 'rgba(56,214,255,0.12)',
        defaultNodeColor: '#38d6ff',
        stagePadding: labelStagePadding(labels, container),
        minCameraRatio: 0.3,
        maxCameraRatio: 3,
    });

    // Genre legend
    var legend = document.getElementById('genre-legend');
    if (legend && data.genres) {
        legend.replaceChildren();
        Object.keys(data.genres).sort().forEach(function(g) {
            var info = data.genres[g];
            var pill = document.createElement('span');
            pill.className = 'genre-pill';
            // genre names come from provider tags; only a literal hex colour may reach CSS
            if (/^#[0-9a-f]{3,8}$/i.test(info.color || '')) {
                pill.style.setProperty('--pill-color', info.color);
            }
            pill.textContent = g + ' (' + info.count + ')';
            legend.appendChild(pill);
        });
    }

    // Click node to show info
    renderer.on('clickNode', function(e) {
        var attrs = graph.getNodeAttributes(e.node);
        if (Alpine.store('toast')) {
            Alpine.store('toast').add(attrs.artist + ' — ' + attrs.title, 'info');
        }
    });
}
