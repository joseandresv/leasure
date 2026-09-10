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

    const RADIUS = 900;
    const ANGLE_STEP = 360 / PANEL_NAMES.length;

    // The HiFi Walker is a persistent hero: it lives in the Deck's album-cover slot
    // while Music is active, and docks to the top-left corner on every other tab.
    // Each tab rotates it to a distinct face so its pose reflects where you are.
    // The Walker is now a centerpiece on every menu, so it always faces forward
    // enough to keep its screen visible — each tab just gets a distinct lean so
    // its pose still reflects where you are.
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

            const angle = ANGLE_STEP * i;
            panel.style.transform = `rotateY(${angle}deg) translateZ(${RADIUS}px) scale(0.5)`;

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

        // Restore 3D transform
        const angle = ANGLE_STEP * idx;
        panel.style.position = 'absolute';
        // Clear the pop-out centering so the stylesheet's --panel-w/--panel-h
        // offsets take over again — the panel size is viewport-relative.
        panel.style.left = '';
        panel.style.top = '';
        panel.style.transform = `rotateY(${angle}deg) translateZ(${RADIUS}px) scale(0.5)`;
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

        glRenderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
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

        window.addEventListener('resize', () => { placeDevice(); resizeRenderer(); });
    }

    /* ─── Device placement: Deck hero ⇄ corner dock ─── */

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

    function placeDevice(instant) {
        const canvas = document.getElementById('device-canvas');
        if (!canvas) return;

        // The Walker docks into the ACTIVE panel's stage bezel (.device-dock) — the
        // Deck's album slot on Music, the menu-stage bezel on every other menu.
        let slotRect = null, slotEl = null;
        if (panelPopped) {
            const panel = document.getElementById('panel-' + activePanel);
            if (panel) {
                slotEl = panel.querySelector('.device-dock');
                if (slotEl) {
                    const r = slotEl.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) slotRect = r;
                }
            }
        }

        // The stage bezel sits at the same screen position on every menu, so when
        // the new dock rect matches the last one we skip the glide entirely — the
        // Walker simply stays put as you move between menus.
        const near = (a, b) => a && b &&
            Math.abs(a.left - b.left) < 2 && Math.abs(a.top - b.top) < 2 &&
            Math.abs(a.width - b.width) < 2 && Math.abs(a.height - b.height) < 2;
        const still = slotRect && near(slotRect, lastDock);
        canvas.style.transition = (still || instant) ? 'none'
            : 'top 500ms cubic-bezier(.2,.7,.3,1), left 500ms cubic-bezier(.2,.7,.3,1), width 500ms cubic-bezier(.2,.7,.3,1), height 500ms cubic-bezier(.2,.7,.3,1), opacity 500ms ease';
        canvas.style.bottom = 'auto';
        canvas.style.transform = 'none';
        canvas.style.zIndex = '15';  // in front of panels (overlay is z-index 10)

        // Clear any previous mounted host (deck or menu-stage)
        document.querySelectorAll('.device-mounted').forEach(e => e.classList.remove('device-mounted'));

        if (slotRect) {
            // Dock into the stage bezel — fill it and flag the host as mounted so
            // the flat backdrop/fallback gives way to the floating 3D Walker.
            canvas.style.left = slotRect.left + 'px';
            canvas.style.top = slotRect.top + 'px';
            canvas.style.width = slotRect.width + 'px';
            canvas.style.height = slotRect.height + 'px';
            canvas.style.opacity = '1';
            lastDock = { left: slotRect.left, top: slotRect.top, width: slotRect.width, height: slotRect.height };
            clipToScroller(canvas, slotRect, slotEl);
            const host = slotEl.closest('.rhythm-deck') || slotEl.closest('.menu-stage') || slotEl;
            if (host) host.classList.add('device-mounted');
        } else if (activePanel === 'music') {
            unclipDevice(canvas);
            // Music sub-tabs (Albums/Playlists/Artists) have no bezel — big hero dock.
            const h = 300, w = 216;
            canvas.style.left = '32px';
            canvas.style.top = Math.max(72, Math.round(window.innerHeight / 2 - h / 2)) + 'px';
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
            canvas.style.opacity = '1';
        } else {
            unclipDevice(canvas);
            // Fallback corner dock (e.g. before a panel's stage has laid out)
            canvas.style.left = '28px';
            canvas.style.top = '28px';
            canvas.style.width = '168px';
            canvas.style.height = '232px';
            canvas.style.opacity = '0.96';
        }
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

        // Pop current active panel back into 3D ring
        if (panelPopped) {
            popInPanel(activePanel);
        }

        activePanel = name;
        isRotating = true;
        targetAngle = -ANGLE_STEP * idx;

        // Rotate the device to this tab's face, and start it flying to its new home
        const face = PANEL_FACE[name] || PANEL_FACE.home;
        targetRY = face.ry;
        targetRX = face.rx;
        placeDevice();

        // Update nav buttons
        document.querySelectorAll('.nav-sphere-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.panel === name);
        });

        // Show the active panel; neighbours flank it faintly for the carousel depth.
        PANEL_NAMES.forEach((n, i) => {
            const el = document.getElementById('panel-' + n);
            if (!el) return;
            el.style.pointerEvents = 'none';
            el.inert = n !== name;
            if (n === name) {
                el.style.opacity = '1';
                el.style.visibility = 'visible';
            } else {
                const dist = Math.min(Math.abs(i - idx), PANEL_NAMES.length - Math.abs(i - idx));
                if (dist === 1) {
                    el.style.opacity = '0.12';
                    el.style.visibility = 'visible';
                } else {
                    el.style.opacity = '0';
                    el.style.visibility = 'hidden';
                }
            }
        });

        if (instant) {
            currentAngle = targetAngle;
            applyRotation();
            isRotating = false;
            popOutPanel(name);
        }
    }

    function applyRotation() {
        const ring = document.getElementById('carousel-ring');
        if (ring) ring.style.transform = `rotateY(${currentAngle}deg)`;
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
            const idle = Math.sin(tick * 0.012) * 0.06;
            device.rotation.y += ((targetRY + idle) - device.rotation.y) * 0.08;
            device.rotation.x += (targetRX - device.rotation.x) * 0.08;
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

    function setDeviceArt(url) {
        if (!THREE || !screenMat) return;
        if (!url) {
            screenMat.map = null;
            screenMat.color.setHex(0x161616);
            screenMat.emissive.setHex(0xff8c00);
            screenMat.emissiveIntensity = 0.5;
            screenMat.needsUpdate = true;
            return;
        }
        const loader = new THREE.TextureLoader();
        loader.setCrossOrigin('anonymous');
        loader.load(
            url,
            (tex) => {
                screenMat.map = tex;
                screenMat.color.setHex(0xffffff);
                screenMat.emissive.setHex(0xffffff);
                screenMat.emissiveIntensity = 0.4;  // make the art glow on the screen
                screenMat.needsUpdate = true;
            },
            undefined,
            () => { /* CORS/load failure — keep the default amber screen */ }
        );
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
