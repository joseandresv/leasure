/**
 * Leasure 3D Scene
 * - CSS 3D carousel for rotation transitions
 * - Active panel pops out of 3D context into 2D overlay (fixes pointer events)
 * - Three.js WebGL for decorative device model
 */
(function () {
    'use strict';

    const PANEL_NAMES = ['home', 'music', 'library', 'downloads', 'device'];
    let activePanel = 'home';
    let currentAngle = 0;
    let targetAngle = 0;
    let angleVel = 0;   // weighted-spring momentum for the carousel ring
    let isRotating = false;
    let panelPopped = false;

    const THREE = window.THREE;
    let scene, camera, glRenderer, device, screenMat, keyLight;

    // A drum, not a ring: faces 30deg apart on R=900 (chord 2*900*sin15 = 466px >= 420px
    // per scaled face), so menus roll in rail order and never wrap.
    const RADIUS = 900;
    const ANGLE_STEP = 30;
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    const faceTransform = i => `rotateX(${-ANGLE_STEP * i}deg) translateZ(${RADIUS}px) scale(0.5)`;

    // The Walker holds one dock on every menu; each tab only gives it a distinct
    // lean, kept small enough that its screen stays visible.
    const PANEL_FACE = {
        home:      { ry: 0.00,  rx: 0.00 },
        music:     { ry: -0.30, rx: 0.05 },
        library:   { ry: 0.34,  rx: 0.02 },
        downloads: { ry: -0.42, rx: 0.06 },
        device:    { ry: 0.20,  rx: -0.05 },
    };
    let targetRY = 0, targetRX = 0;
    let lastCW = 0, lastCH = 0, tick = 0;
    let lastDock = null;  // last docked rect — used to keep the H2 still across menus

    function init() {
        buildCarousel();
        trackPanelBodyScroll();
        buildDevice();
        setupNavigation();
        navigateToPanel('home', true);
        animate();

        // When the Deck (or any panel) swaps in new content, re-anchor the device
        document.addEventListener('htmx:afterSettle', () => placeDevice());
    }

    /* ─── CSS 3D Carousel ─── */

    function buildCarousel() {
        const container = document.getElementById('scene-container');
        if (!container) return;

        const stage = document.createElement('div');
        stage.id = 'carousel-stage';
        container.appendChild(stage);

        const ring = document.createElement('div');
        ring.id = 'carousel-ring';
        stage.appendChild(ring);

        PANEL_NAMES.forEach((name, i) => {
            const template = document.getElementById('tpl-' + name);
            if (!template) return;

            const panel = document.createElement('div');
            panel.className = 'carousel-panel';
            panel.id = 'panel-' + name;
            panel.dataset.index = i;
            // Off-screen panels stay out of the tab order and the a11y tree until
            // navigateToPanel activates one.
            panel.inert = true;
            panel.appendChild(template.content.cloneNode(true));

            panel.style.transform = faceTransform(i);

            ring.appendChild(panel);
        });

        // 2D overlay for the active panel (outside 3D context)
        const overlay = document.createElement('div');
        overlay.id = 'panel-overlay';
        container.appendChild(overlay);
    }

    function initFrameworks() {
        PANEL_NAMES.forEach(name => {
            const el = document.getElementById('panel-' + name);
            if (!el) return;
            if (window.htmx) htmx.process(el);
            if (window.Alpine && window.Alpine.initTree) {
                try { Alpine.initTree(el); } catch (e) { /* already initialized */ }
            }
        });
    }

    /* ─── Panel Pop-out: move active panel to 2D overlay ─── */

    function popOutPanel(name) {
        if (panelPopped) return;
        panelPopped = true;

        const panel = document.getElementById('panel-' + name);
        const overlay = document.getElementById('panel-overlay');
        if (!panel || !overlay) return;

        // Move panel from 3D ring to 2D overlay
        overlay.appendChild(panel);

        // Override 3D transform with 2D centering (no scale — in 3D the
        // perspective*scale combo equaled 1.0, so native size matches)
        panel.style.position = 'absolute';
        panel.style.left = '50%';
        panel.style.top = '50%';
        panel.style.transform = 'translate(-50%, -50%)';
        panel.style.opacity = '1';
        panel.style.visibility = 'visible';
        panel.style.pointerEvents = 'auto';

        // Re-process HTMX
        if (window.htmx) htmx.process(panel);
        document.body.dispatchEvent(new CustomEvent('leasure:panel-shown', { detail: { name } }));

        // Now that the panel is in its final 2D position, anchor the device to its slot
        placeDevice();
        setTimeout(() => placeDevice(), 80);
    }

    function popInPanel(name) {
        if (!panelPopped) return;
        panelPopped = false;

        const panel = document.getElementById('panel-' + name);
        const ring = document.getElementById('carousel-ring');
        if (!panel || !ring) return;

        const idx = PANEL_NAMES.indexOf(name);
        if (idx < 0) return;

        // Move panel back to 3D ring
        ring.appendChild(panel);

        panel.style.position = 'absolute';
        // Clear the pop-out centering so the stylesheet's --panel-w/--panel-h
        // offsets take over again — the panel size is viewport-relative.
        panel.style.left = '';
        panel.style.top = '';
        panel.style.transform = faceTransform(idx);
        panel.style.pointerEvents = 'none';
    }

    /* ─── Three.js Device Model ─── */

    function buildDevice() {
        if (!THREE) return;
        const canvas = document.getElementById('device-canvas');
        if (!canvas) return;

        scene = new THREE.Scene();
        camera = new THREE.PerspectiveCamera(40, canvas.clientWidth / canvas.clientHeight, 1, 1000);
        camera.position.set(0, 6, 200);
        camera.lookAt(0, 6, 0);  // favor the enlarged album screen

        try {
            glRenderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
        } catch (e) {
            // No WebGL (no GPU, remote desktop): the bezels keep their flat fallback.
            return;
        }
        glRenderer.setSize(canvas.clientWidth, canvas.clientHeight);
        glRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        glRenderer.setClearColor(0x000000, 0);

        scene.add(new THREE.AmbientLight(0x8595bb, 2.4));
        keyLight = new THREE.PointLight(0xff8c00, 2, 600);
        keyLight.position.set(50, 100, 150); scene.add(keyLight);
        const pl2 = new THREE.PointLight(0x4488ff, 1, 600);
        pl2.position.set(-50, -50, -100); scene.add(pl2);

        device = new THREE.Group();
        const bodyMat = new THREE.MeshStandardMaterial({ color: 0x3a3a40, metalness: 0.85, roughness: 0.25 });
        device.add(new THREE.Mesh(new THREE.BoxGeometry(64, 104, 14), bodyMat));
        // Large SQUARE screen up top — sized for album-cover viewing
        const bezMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a, metalness: 0.9, roughness: 0.3 });
        const bez = new THREE.Mesh(new THREE.PlaneGeometry(54, 54), bezMat);
        bez.position.set(0, 16, 7.2); device.add(bez);
        screenMat = new THREE.MeshStandardMaterial({ color: 0x161616, emissive: 0xff8c00, emissiveIntensity: 0.5 });
        const scr = new THREE.Mesh(new THREE.PlaneGeometry(48, 48), screenMat);
        scr.position.set(0, 16, 7.4); device.add(scr);
        // Jog disc + controls below the screen
        const discMat = new THREE.MeshStandardMaterial({ color: 0x0a0a0a, metalness: 0.3, roughness: 0.8 });
        const disc = new THREE.Mesh(new THREE.CircleGeometry(11, 32), discMat);
        disc.position.set(0, -34, 7.4); device.add(disc);
        const ringMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a, metalness: 0.5, roughness: 0.6, side: THREE.DoubleSide });
        const rng = new THREE.Mesh(new THREE.RingGeometry(4.5, 10, 32), ringMat);
        rng.position.set(0, -34, 7.5); device.add(rng);
        const spMat = new THREE.MeshStandardMaterial({ color: 0x888888, metalness: 0.9, roughness: 0.2 });
        const sp = new THREE.Mesh(new THREE.CircleGeometry(1.4, 16), spMat);
        sp.position.set(0, -34, 7.6); device.add(sp);
        const btnMat = new THREE.MeshStandardMaterial({ color: 0x555555, metalness: 0.8, roughness: 0.3 });
        const btnGeo = new THREE.CylinderGeometry(2.6, 2.6, 2, 16);
        [[-23, -34, 7.4], [23, -34, 7.4]].forEach(([x, y, z]) => {
            const b = new THREE.Mesh(btnGeo, btnMat); b.rotation.x = Math.PI / 2;
            b.position.set(x, y, z); device.add(b);
        });
        const ledMat = new THREE.MeshStandardMaterial({ color: 0xff8c00, emissive: 0xff8c00, emissiveIntensity: 0.8 });
        const led = new THREE.Mesh(new THREE.CircleGeometry(1, 16), ledMat);
        led.position.set(25, 47, 7.4); device.add(led);
        scene.add(device);

        window.addEventListener('resize', () => { placeDevice(true); resizeRenderer(); });
    }

    /* ─── Device placement: dock into the active bezel ─── */

    function resizeRenderer() {
        const canvas = document.getElementById('device-canvas');
        if (!canvas || !glRenderer || !camera) return;
        const w = canvas.clientWidth, h = canvas.clientHeight;
        if (w > 0 && h > 0) {
            camera.aspect = w / h;
            camera.updateProjectionMatrix();
            glRenderer.setSize(w, h, false);  // false: keep the CSS size we control
        }
    }

    // The panel frame clips and its body (.menu-deck / .rhythm-deck) is the only
    // scroller, so the docked bezel moves under the viewport-positioned canvas.
    // Re-anchor on scroll, instantly — a glide would trail behind the bezel.
    function trackPanelBodyScroll() {
        let queued = false;
        const onScroll = () => {
            if (queued) return;
            queued = true;
            requestAnimationFrame(() => { queued = false; placeDevice(true); });
        };
        document.querySelectorAll('.carousel-panel > .menu-deck, .carousel-panel > .rhythm-deck')
            .forEach(body => {
                if (body.dataset.deviceScrollBound) return;
                body.dataset.deviceScrollBound = '1';
                body.addEventListener('scroll', onScroll, { passive: true });
            });
    }

    // The panel body's own top mask band, read from CSS so the two cannot drift.
    const BODY_FADE = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue('--body-fade')) || 10;

    function unclipDevice(canvas) {
        canvas.style.clipPath = 'none';
        canvas.style.maskImage = 'none';
        canvas.style.webkitMaskImage = 'none';
    }

    // The canvas floats above the panels, so it is not clipped by the panel frame:
    // when the bezel scrolls past the body's edge, clip the Walker to match. The top
    // edge fades over the same band as the body's mask so the Walker dissolves under
    // the pinned masthead instead of being cut by it.
    function clipToScroller(canvas, slotRect, slotEl) {
        const panel = slotEl.closest('.carousel-panel');
        const body = panel && panel.querySelector(':scope > .menu-deck, :scope > .rhythm-deck');
        if (!body) { unclipDevice(canvas); return; }
        const view = body.getBoundingClientRect();
        const cut = n => Math.max(0, Math.round(n)) + 'px';
        canvas.style.clipPath = `inset(0 ${cut(slotRect.right - view.right)} ` +
            `${cut(slotRect.bottom - view.bottom)} ${cut(view.left - slotRect.left)})`;
        const fadeStart = Math.round(view.top - slotRect.top);
        const fade = fadeStart + BODY_FADE <= 0 ? 'none'
            : `linear-gradient(to bottom, transparent ${fadeStart}px, #000 ${fadeStart + BODY_FADE}px)`;
        canvas.style.maskImage = fade;
        canvas.style.webkitMaskImage = fade;
    }

    const near = (a, b) => a && b &&
        Math.abs(a.left - b.left) < 1 && Math.abs(a.top - b.top) < 1 &&
        Math.abs(a.width - b.width) < 1 && Math.abs(a.height - b.height) < 1;

    // The Walker docks into the active panel's .device-dock. Mid-roll, or with no
    // measurable bezel, it holds its last dock: there is no fallback position.
    function placeDevice(instant) {
        const canvas = document.getElementById('device-canvas');
        if (!canvas || !glRenderer || !panelPopped) return;
        const panel = document.getElementById('panel-' + activePanel);
        const slotEl = panel && panel.querySelector('.device-dock');
        const r = slotEl && slotEl.getBoundingClientRect();
        if (!r || r.width <= 20 || r.height <= 20) return;

        const glide = !instant && !reducedMotion.matches && lastDock && !near(r, lastDock);
        const t = '200ms var(--ease-relay)';
        canvas.style.transition = glide ? `top ${t}, left ${t}, width ${t}, height ${t}` : 'none';
        canvas.style.bottom = 'auto';
        canvas.style.transform = 'none';
        canvas.style.zIndex = '15';  // in front of panels (overlay is z-index 10)
        canvas.style.left = r.left + 'px';
        canvas.style.top = r.top + 'px';
        canvas.style.width = r.width + 'px';
        canvas.style.height = r.height + 'px';
        canvas.style.opacity = '1';
        lastDock = { left: r.left, top: r.top, width: r.width, height: r.height };
        clipToScroller(canvas, r, slotEl);
        // Every bezel shows mounted glass, so faces rolling past the fixed Walker
        // never flash the flat fallback.
        document.querySelectorAll('.device-dock').forEach(d => {
            const host = d.closest('.rhythm-deck') || d.closest('.menu-stage');
            if (host) host.classList.add('device-mounted');
        });
        resizeRenderer();
    }

    /* ─── Navigation ─── */

    function setupNavigation() {
        document.querySelectorAll('.nav-sphere-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const target = btn.dataset.panel;
                if (target && target !== activePanel) navigateToPanel(target);
            });
        });
    }

    function navigateToPanel(name, instant) {
        const idx = PANEL_NAMES.indexOf(name);
        if (idx < 0) return;

        // Every menu leaves at its top, so every face of the drum carries its bezel
        // at the one dock rect and the Walker holds still for the whole roll.
        if (panelPopped) {
            const body = document.querySelector(`#panel-${activePanel} > .menu-deck, #panel-${activePanel} > .rhythm-deck`);
            if (body && body.scrollTop) { body.scrollTop = 0; placeDevice(true); }
            popInPanel(activePanel);
        }

        activePanel = name;
        isRotating = true;
        targetAngle = ANGLE_STEP * idx;

        const face = PANEL_FACE[name] || PANEL_FACE.home;
        targetRY = face.ry;
        targetRX = face.rx;

        // Update nav buttons
        document.querySelectorAll('.nav-sphere-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.panel === name);
        });

        PANEL_NAMES.forEach(n => {
            const el = document.getElementById('panel-' + n);
            if (!el) return;
            el.style.pointerEvents = 'none';
            el.inert = n !== name;
        });

        if (instant || reducedMotion.matches) {
            angleVel = 0;
            currentAngle = targetAngle;
            applyRotation();
            isRotating = false;
            popOutPanel(name);
        }
    }

    function applyRotation() {
        const ring = document.getElementById('carousel-ring');
        if (!ring) return;
        ring.style.transform = `rotateX(${currentAngle}deg)`;
        // The arriving face brightens as it rolls to the front; neighbours keep a faint ghost.
        PANEL_NAMES.forEach((n, i) => {
            const el = document.getElementById('panel-' + n);
            if (!el || el.parentElement !== ring) return;
            const d = Math.abs(currentAngle - ANGLE_STEP * i) / ANGLE_STEP;
            const o = d < 1 ? 1 - 0.88 * d : Math.max(0, 0.12 * (2 - d));
            el.style.opacity = o.toFixed(3);
            el.style.visibility = o > 0 ? 'visible' : 'hidden';
        });
    }

    /* ─── Animation Loop ─── */

    function animate() {
        requestAnimationFrame(animate);

        if (isRotating) {
            const diff = targetAngle - currentAngle;
            if (Math.abs(diff) > 0.3 || Math.abs(angleVel) > 0.15) {
                // Weighted spring: the ring has mass — it loads up, swings through,
                // then friction bleeds the momentum so it settles into the detent
                // with a faint overshoot instead of gliding to a soft stop.
                angleVel += diff * 0.02;
                angleVel *= 0.78;
                currentAngle += angleVel;
                applyRotation();
            } else {
                // Rotation settled — snap and pop out the active panel
                currentAngle = targetAngle;
                angleVel = 0;
                applyRotation();
                isRotating = false;
                popOutPanel(activePanel);
            }
        }

        if (device) {
            tick++;
            // Ease toward the current tab's face, plus a gentle idle drift so it feels alive
            const still = reducedMotion.matches;
            const idle = still ? 0 : Math.sin(tick * 0.012) * 0.06;
            const ease = still ? 1 : 0.08;
            device.rotation.y += ((targetRY + idle) - device.rotation.y) * ease;
            device.rotation.x += (targetRX - device.rotation.x) * ease;
        }

        // Keep the WebGL buffer matched to the canvas as it flies/resizes between slots
        const canvas = document.getElementById('device-canvas');
        if (canvas && (canvas.clientWidth !== lastCW || canvas.clientHeight !== lastCH)) {
            lastCW = canvas.clientWidth;
            lastCH = canvas.clientHeight;
            resizeRenderer();
        }

        if (glRenderer && scene && camera) glRenderer.render(scene, camera);
    }

    /* ─── Device Screen Texture (rhythm-deck "now showing") ─── */

    // Bumped on every selection so a slow cover cannot land after a newer one.
    let artRequest = 0;

    // A cover is shown as pure emission over a black base: lit by the scene's bright
    // ambient light, light covers clipped to white. The idle screen stays amber.
    function setScreenMap(tex) {
        if (screenMat.emissiveMap && screenMat.emissiveMap !== tex) screenMat.emissiveMap.dispose();
        screenMat.emissiveMap = tex;
        screenMat.color.setHex(tex ? 0x000000 : 0x161616);
        screenMat.emissive.setHex(tex ? 0xffffff : 0xff8c00);
        screenMat.emissiveIntensity = tex ? 1 : 0.5;
        screenMat.needsUpdate = true;
    }

    function loadScreenArt(url, request, mayProxy) {
        const loader = new THREE.TextureLoader();
        loader.setCrossOrigin('anonymous');
        loader.load(
            url,
            (tex) => {
                if (request === artRequest) setScreenMap(tex);
                else tex.dispose();
            },
            undefined,
            () => {
                if (request !== artRequest) return;
                const proxied = mayProxy && window.coverProxyUrl && coverProxyUrl(url);
                if (proxied) loadScreenArt(proxied, request, false);
                else setScreenMap(null);
            }
        );
    }

    function setDeviceArt(url) {
        if (!THREE || !screenMat) return;
        const request = ++artRequest;
        if (!url) { setScreenMap(null); return; }
        const first = window.coverLoadUrl ? coverLoadUrl(url) : url;
        loadScreenArt(first, request, first === url);
    }

    /* Tint the device's key light toward the selected album color */
    function setAccentLight(r, g, b) {
        if (keyLight) keyLight.color.setRGB(r / 255, g / 255, b / 255);
    }

    /* ─── Public API ─── */

    window.leasureScene = { init, navigateToPanel, getActivePanel: () => activePanel, setDeviceArt, setAccentLight };

    function boot() {
        init();
        setTimeout(initFrameworks, 200);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 50));
    } else {
        setTimeout(boot, 50);
    }
})();
