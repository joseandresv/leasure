/* Leasure — THE DECK (rhythm-deck song selector)
 *
 * A vertical DDR-style roulette (right column of the music cabinet) drives the
 * PERSISTENT left-rail readout + Three.js device screen. The wheel is swapped
 * per tab (deck / recent / albums / playlists / artists) into #music-results;
 * the readout (.deck-readout) and stage (.deck-screen-img) live in the cabinet
 * and stay put. Each banner carries data-* for the readout text and a hidden
 * <template class="deck-banner-db"> action (download / OPEN) that is cloned into
 * the readout's .deck-dbstate on selection.
 */
(function () {
    'use strict';

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const REVEAL_MARGIN = 12;

    // Scroll the wheel only: scrollIntoView also scrolls the panel body and drags
    // the docked Walker with it.
    function centerInWheel(wheel, banner, smooth) {
        wheel.scrollTo({
            top: banner.offsetTop - (wheel.clientHeight - banner.offsetHeight) / 2,
            behavior: smooth && !reducedMotion.matches ? 'smooth' : 'auto',
        });
    }

    // The wheel can be taller than the visible part of the deck, and near either end
    // it cannot centre the selection, so scroll the deck just enough to show it.
    function revealInDeck(deck, wheel, banner) {
        const maxTop = wheel.scrollHeight - wheel.clientHeight;
        const wheelTop = Math.max(0, Math.min(maxTop, banner.offsetTop - (wheel.clientHeight - banner.offsetHeight) / 2));
        const top = wheel.getBoundingClientRect().top + banner.offsetTop - wheelTop;
        const bottom = top + banner.offsetHeight;
        const view = deck.getBoundingClientRect();
        const viewTop = Math.max(view.top, 0) + REVEAL_MARGIN;
        const viewBottom = Math.min(view.bottom, window.innerHeight) - REVEAL_MARGIN;
        let delta = 0;
        if (bottom > viewBottom) delta = bottom - viewBottom;
        else if (top < viewTop) delta = top - viewTop;
        if (delta) deck.scrollBy({ top: delta, behavior: reducedMotion.matches ? 'auto' : 'smooth' });
    }

    function setText(deck, sel, txt) {
        const val = (txt && txt.length) ? txt : '—';
        deck.querySelectorAll(sel).forEach((el) => { el.textContent = val; });
    }

    /* Drive the neon accent for the WHOLE UI from the selected art via Vibrant. */
    function applyAccent(img) {
        if (!window.Vibrant || !img || !img.src) return;
        const run = () => {
            try {
                const v = new Vibrant(img, 16);
                const sw = v.swatches();
                const s = sw.Vibrant || sw.LightVibrant || sw.Muted || sw.DarkVibrant;
                if (s) {
                    const [r, g, b] = s.getRgb().map(Math.round);
                    const root = document.documentElement;
                    root.style.setProperty('--album-accent', `rgb(${r},${g},${b})`);
                    root.style.setProperty('--album-accent-soft', `rgba(${r},${g},${b},0.32)`);
                    root.style.setProperty('--album-accent-faint', `rgba(${r},${g},${b},0.12)`);
                    root.style.setProperty('--album-glow-color', `rgba(${r},${g},${b},0.12)`);
                    document.body.classList.add('album-lit');
                    if (window.leasureScene && window.leasureScene.setAccentLight) {
                        window.leasureScene.setAccentLight(r, g, b);
                    }
                }
            } catch (e) { /* CORS/decode — keep default */ }
            // Vibrant leaks a scratch <canvas> onto <body> each run — remove it.
            document.querySelectorAll('body > canvas').forEach((c) => c.remove());
        };
        if (img.complete && img.naturalWidth > 0) run();
        else img.addEventListener('load', run, { once: true });
    }

    function updateDisplay(deck, banner) {
        const d = banner.dataset;
        const img = deck.querySelector('.deck-screen-img');
        const fallback = deck.querySelector('.menu-stage-fallback');

        if (img) {
            if (d.image) {
                if (img.getAttribute('src') !== d.image) img.setAttribute('src', d.image);
                img.style.display = 'block';
                if (fallback) fallback.style.display = 'none';
            } else {
                img.removeAttribute('src');
                img.style.display = 'none';
                if (fallback) fallback.style.display = '';
            }
        }

        setText(deck, '.deck-title', d.title);
        setText(deck, '.deck-artist', d.artist);
        setText(deck, '.deck-album', d.album + (d.year ? '  ·  ' + d.year : ''));
        setText(deck, '.deck-genre', d.genre);
        setText(deck, '.deck-year', d.year);
        setText(deck, '.deck-duration', d.duration);
        setText(deck, '.deck-trk', d.trk);
        const srcEl = deck.querySelector('.deck-source');
        if (srcEl) srcEl.textContent = (d.source && d.source.length) ? d.source : '—';

        // Inject the per-banner action payload (download / OPEN / badges)
        const dbWrap = deck.querySelector('.deck-dbstate');
        const tpl = banner.querySelector('.deck-banner-db');
        if (dbWrap && tpl) {
            dbWrap.replaceChildren(tpl.content.cloneNode(true));
            if (window.htmx) htmx.process(dbWrap);
        }

        applyAccent(img);

        if (window.leasureScene && window.leasureScene.setDeviceArt) {
            window.leasureScene.setDeviceArt(d.image || '');
        }
    }

    // The deck outlives every wheel swapped into it, so it gets one key listener that
    // steps whichever wheel bound last.
    function bindDeckKeys(deck) {
        if (deck.dataset.deckKeysBound) return;
        deck.dataset.deckKeysBound = '1';
        deck.addEventListener('keydown', (e) => {
            if (!deck.deckStep) return;
            if (e.key === 'ArrowDown' || e.key === 'ArrowRight') { deck.deckStep(1); e.preventDefault(); }
            else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') { deck.deckStep(-1); e.preventDefault(); }
        });
    }

    function bindWheel(wheel) {
        if (!wheel || wheel.dataset.deckBound) return;
        const deck = wheel.closest('.rhythm-deck');
        if (!deck) return;
        const banners = Array.from(wheel.querySelectorAll('.deck-banner'));
        if (!banners.length) return;
        wheel.dataset.deckBound = '1';

        let current = -1;

        function select(i, scroll) {
            i = Math.max(0, Math.min(banners.length - 1, i));
            if (i === current) return;
            current = i;
            banners.forEach((b, j) => {
                b.classList.toggle('is-center', j === i);
                b.setAttribute('aria-selected', j === i ? 'true' : 'false');
            });
            updateDisplay(deck, banners[i]);
            if (scroll) centerInWheel(wheel, banners[i], true);
        }

        function nearestToCenter() {
            const wr = wheel.getBoundingClientRect();
            const cy = wr.top + wr.height / 2;
            let best = 0, bestD = Infinity;
            banners.forEach((b, j) => {
                const r = b.getBoundingClientRect();
                const dd = Math.abs((r.top + r.height / 2) - cy);
                if (dd < bestD) { bestD = dd; best = j; }
            });
            return best;
        }

        let raf;
        wheel.addEventListener('scroll', () => {
            if (raf) cancelAnimationFrame(raf);
            raf = requestAnimationFrame(() => select(nearestToCenter(), false));
        }, { passive: true });

        banners.forEach((b, j) => b.addEventListener('click', (e) => {
            // Don't hijack clicks on the action buttons inside a banner template clone
            if (e.target.closest('.deck-dl-btn, .deck-open-btn')) return;
            select(j, true);
        }));

        // Focus follows the selection so the ring never stays on a banner left behind;
        // the wheel does its own scrolling, so focusing must not scroll the panel.
        deck.deckStep = (delta) => {
            if (!wheel.isConnected) return;
            const hadFocus = banners.includes(document.activeElement);
            select(current + delta, true);
            revealInDeck(deck, wheel, banners[current]);
            if (hadFocus) banners[current].focus({ preventScroll: true });
        };
        bindDeckKeys(deck);

        select(0, false);
        requestAnimationFrame(() => centerInWheel(wheel, banners[0], false));
    }

    function initDeck(root) {
        const scope = (root && root.querySelectorAll) ? root : document;
        if (root && root.classList && root.classList.contains('deck-wheel')) bindWheel(root);
        scope.querySelectorAll('.deck-wheel').forEach(bindWheel);
    }

    // A sub-tab swap rolls the new list in: a tab to the right enters from below,
    // one to the left from above.
    let lastTabIndex = 0;
    document.addEventListener('htmx:afterSwap', (e) => {
        const target = e.detail.target;
        const elt = e.detail.requestConfig && e.detail.requestConfig.elt;
        const nav = elt && elt.closest && elt.closest('.tab-nav');
        if (!target || target.id !== 'music-results' || !nav) return;
        const i = Array.from(nav.querySelectorAll('button')).indexOf(elt);
        const dir = i >= lastTabIndex ? 'up' : 'down';
        lastTabIndex = i;
        if (reducedMotion.matches) return;
        target.classList.remove('roll-in-up', 'roll-in-down');
        void target.offsetWidth;  // restart the animation on a repeat click
        target.classList.add('roll-in-' + dir);
    });
    document.addEventListener('animationend', (e) => {
        const p = e.target.parentElement;
        if (p && p.id === 'music-results') p.classList.remove('roll-in-up', 'roll-in-down');
    });

    document.addEventListener('htmx:afterSettle', (e) => initDeck(e.detail.target));
    document.addEventListener('DOMContentLoaded', () => initDeck());

    window.leasureDeck = { init: initDeck };
})();
