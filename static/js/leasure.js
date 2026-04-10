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

    btn.disabled = true;
    btn.textContent = 'Syncing...';
    btn.classList.add('syncing');

    status.innerHTML = '<div class="sync-progress-container">' +
        '<div class="progress-bar"><div class="progress-bar-fill" id="sync-progress-fill" style="width:0%"></div></div>' +
        '<p id="sync-detail" class="sync-detail"></p></div>';

    var es = new EventSource('/api/device/sync/stream?device_path=' + encodeURIComponent(path) + '&scope=' + scope);
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
            detail.innerHTML = '<strong>' + data.synced + '/' + data.total + '</strong> &middot; ' +
                data.artist + ' - ' + data.track;
        } else if (data.type === 'playlists') {
            detail.textContent = data.message;
        } else if (data.type === 'done') {
            es.close();
            btn.disabled = false;
            btn.textContent = 'SYNC TO DEVICE';
            btn.classList.remove('syncing');

            var html = '<div class="sync-result sync-result-success">' +
                '<h4>Sync Complete</h4>' +
                '<p><strong>' + data.synced + '</strong> tracks synced (' + data.size_mb + ' MB)';
            if (data.playlists) html += ' &middot; <strong>' + data.playlists + '</strong> playlists generated';
            html += '</p>';
            if (data.errors && data.errors.length > 0) {
                html += '<details><summary>' + data.errors.length + ' error(s)</summary><ul>';
                data.errors.forEach(function(err) { html += '<li><small>' + err + '</small></li>'; });
                html += '</ul></details>';
            }
            html += '</div>';
            status.innerHTML = html;

            if (Alpine.store('toast')) {
                Alpine.store('toast').add('Sync complete: ' + data.synced + ' tracks', 'success');
            }

            htmx.ajax('GET', '/api/device/files/html?device_path=' + encodeURIComponent(path), '#device-files');
            htmx.ajax('GET', '/api/device/diff/html?device_path=' + encodeURIComponent(path), '#sync-diff');
        } else if (data.type === 'error') {
            es.close();
            btn.disabled = false;
            btn.textContent = 'Start Sync';
            btn.classList.remove('syncing');
            status.innerHTML = '<div class="sync-result sync-result-error"><p>' + data.message + '</p></div>';
            if (Alpine.store('toast')) {
                Alpine.store('toast').add('Sync failed: ' + data.message, 'error');
            }
        }
    };

    es.onerror = function() {
        es.close();
        btn.disabled = false;
        btn.textContent = 'SYNC TO DEVICE';
        btn.classList.remove('syncing');
    };
}

/* ── Device Drive Selection ── */

function selectDrive(path) {
    document.getElementById('device-path').value = path;
    var enc = encodeURIComponent(path);
    htmx.ajax('GET', '/api/device/files/html?device_path=' + enc, '#device-files');
    htmx.ajax('GET', '/api/device/diff/html?device_path=' + enc, '#sync-diff');
}

/* ── Genre Graph (Sigma.js) ── */

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
        var color = (data.genres[genreKey] || {}).color || '#0066ff';
        graph.addNode(node.id, {
            label: node.label,
            x: Math.random() * 100,
            y: Math.random() * 100,
            size: 10,
            color: color,
            image: node.image,
            artist: node.artist
        });
    });

    data.edges.forEach(function(edge) {
        if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
            try {
                graph.addEdge(edge.source, edge.target, {
                    color: 'rgba(255,255,255,0.06)',
                    size: 1
                });
            } catch (e) { /* duplicate edge */ }
        }
    });

    // Run ForceAtlas2 layout
    if (window.graphologyLayoutForceAtlas2) {
        var settings = graphologyLayoutForceAtlas2.inferSettings(graph);
        settings.gravity = 1;
        graphologyLayoutForceAtlas2.assign(graph, { settings: settings, iterations: 100 });
    }

    // Render
    var renderer = new Sigma(graph, container, {
        renderLabels: true,
        labelColor: { color: '#e8e8ec' },
        labelFont: 'Inter, system-ui, sans-serif',
        labelSize: 11,
        defaultEdgeColor: 'rgba(255,255,255,0.06)',
        defaultNodeColor: '#0066ff',
        stagePadding: 40,
        minCameraRatio: 0.3,
        maxCameraRatio: 3,
    });

    // Genre legend
    var legend = document.getElementById('genre-legend');
    if (legend && data.genres) {
        var html = '';
        Object.keys(data.genres).sort().forEach(function(g) {
            var info = data.genres[g];
            html += '<span class="genre-pill" style="--pill-color:' + info.color + '">' + g + ' (' + info.count + ')</span> ';
        });
        legend.innerHTML = html;
    }

    // Click node to show info
    renderer.on('clickNode', function(e) {
        var attrs = graph.getNodeAttributes(e.node);
        if (Alpine.store('toast')) {
            Alpine.store('toast').add(attrs.artist + ' — ' + attrs.label, 'info');
        }
    });
}
