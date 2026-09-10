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
        el.disabled = true;
        el.textContent = '...';
    }
});

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
    // before anything ran, so the toast must not claim a failure.
    function showNotice(message, kind) {
        resetButton();
        status.textContent = '';
        var box = document.createElement('div');
        box.className = 'sync-result';
        if (kind === 'error') {
            box.classList.add('sync-result-error');
        } else {
            box.classList.add('sync-result-warning');
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
        showNotice(message, 'error');
    }

    // Loose comparison (trailing separator, drive-letter case) so a path the server
    // would accept is never blocked here.
    function isDetectedDrive(candidate) {
        var wanted = String(candidate).replace(/[\\/]+$/, '').toLowerCase();
        return window.leasureDrives.some(function(p) {
            return String(p).replace(/[\\/]+$/, '').toLowerCase() === wanted;
        });
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

                status.textContent = '';
                var box = document.createElement('div');
                box.className = 'sync-result sync-result-success';
                var h4 = document.createElement('h4');
                h4.textContent = 'Sync Complete';
                box.appendChild(h4);
                var p = document.createElement('p');
                var n = document.createElement('strong');
                n.textContent = data.synced;
                p.appendChild(n);
                p.appendChild(document.createTextNode(' tracks synced (' + data.size_mb + ' MB)'));
                if (data.playlists) {
                    p.appendChild(document.createTextNode(' · '));
                    var pl = document.createElement('strong');
                    pl.textContent = data.playlists;
                    p.appendChild(pl);
                    p.appendChild(document.createTextNode(' playlists generated'));
                }
                box.appendChild(p);
                if (data.errors && data.errors.length > 0) {
                    var details = document.createElement('details');
                    var summary = document.createElement('summary');
                    summary.textContent = data.errors.length + ' error(s)';
                    details.appendChild(summary);
                    var ul = document.createElement('ul');
                    data.errors.forEach(function(err) {
                        var li = document.createElement('li');
                        var small = document.createElement('small');
                        small.textContent = err;
                        li.appendChild(small);
                        ul.appendChild(li);
                    });
                    details.appendChild(ul);
                    box.appendChild(details);
                }
                status.appendChild(box);

                if (Alpine.store('toast')) {
                    Alpine.store('toast').add('Sync complete: ' + data.synced + ' tracks', 'success');
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
            showError('Connection to the sync stream was lost. The sync keeps running on the server — refresh the Device page to see the result.');
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

/* ── Genre Graph (Sigma.js) ── */

var GRAPH_LABEL = {
    font: 'Orbitron, JetBrains Mono, system-ui, sans-serif',
    weight: '600',
    size: 11,
    nodeSize: 10
};

// Room the widest label needs to the right of its node, capped so the margin never
// eats more than a quarter of the frame.
function labelStagePadding(nodes, container) {
    var ctx = document.createElement('canvas').getContext('2d');
    if (!ctx) return 40;
    ctx.font = GRAPH_LABEL.weight + ' ' + GRAPH_LABEL.size + 'px ' + GRAPH_LABEL.font;
    var widest = nodes.reduce(function(w, node) {
        return Math.max(w, ctx.measureText(node.label || '').width);
    }, 0);
    return Math.round(Math.min(GRAPH_LABEL.nodeSize + 6 + widest, container.clientHeight / 4));
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

    data.nodes.forEach(function(node) {
        var genreKey = (node.genres[0] || '').toLowerCase();
        var color = (data.genres[genreKey] || {}).color || '#38d6ff';
        graph.addNode(node.id, {
            label: node.label,
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
        stagePadding: labelStagePadding(data.nodes, container),
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
            Alpine.store('toast').add(attrs.artist + ' — ' + attrs.label, 'info');
        }
    });
}
