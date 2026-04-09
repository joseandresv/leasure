// Leasure client-side utilities

// Format milliseconds as mm:ss
function formatDuration(ms) {
    if (!ms) return '--:--';
    const s = Math.floor(ms / 1000);
    const min = Math.floor(s / 60);
    const sec = s % 60;
    return `${min}:${sec.toString().padStart(2, '0')}`;
}

// Format bytes as human-readable
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

// HTMX event hooks
document.addEventListener('htmx:responseError', function(event) {
    console.error('HTMX error:', event.detail);
});
