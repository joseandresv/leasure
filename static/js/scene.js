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
    let isRotating = false;
    let panelPopped = false;

    const THREE = window.THREE;
    let scene, camera, glRenderer, device;

    const RADIUS = 900;
    const ANGLE_STEP = 360 / PANEL_NAMES.length;

    function init() {
        buildCarousel();
        buildDevice();
        setupNavigation();
        navigateToPanel('home', true);
        animate();
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
        panel.style.left = '-540px';
        panel.style.top = '-420px';
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
        camera.position.set(0, 10, 200);
        camera.lookAt(0, 0, 0);

        glRenderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
        glRenderer.setSize(canvas.clientWidth, canvas.clientHeight);
        glRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        glRenderer.setClearColor(0x000000, 0);

        scene.add(new THREE.AmbientLight(0x606060, 2));
        const pl = new THREE.PointLight(0xff8c00, 2, 600);
        pl.position.set(50, 100, 150); scene.add(pl);
        const pl2 = new THREE.PointLight(0x4488ff, 1, 600);
        pl2.position.set(-50, -50, -100); scene.add(pl2);

        device = new THREE.Group();
        const bodyMat = new THREE.MeshStandardMaterial({ color: 0x3a3a40, metalness: 0.85, roughness: 0.25 });
        device.add(new THREE.Mesh(new THREE.BoxGeometry(60, 100, 15), bodyMat));
        const screenMat = new THREE.MeshStandardMaterial({ color: 0x111111, emissive: 0xff8c00, emissiveIntensity: 0.2 });
        const scr = new THREE.Mesh(new THREE.PlaneGeometry(40, 25), screenMat);
        scr.position.set(0, 20, 8); device.add(scr);
        const bezMat = new THREE.MeshStandardMaterial({ color: 0x222222, metalness: 0.9, roughness: 0.3 });
        const bez = new THREE.Mesh(new THREE.PlaneGeometry(44, 29), bezMat);
        bez.position.set(0, 20, 7.8); device.add(bez);
        const discMat = new THREE.MeshStandardMaterial({ color: 0x0a0a0a, metalness: 0.3, roughness: 0.8 });
        const disc = new THREE.Mesh(new THREE.CircleGeometry(15, 32), discMat);
        disc.position.set(0, -15, 8); device.add(disc);
        const ringMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a, metalness: 0.5, roughness: 0.6, side: THREE.DoubleSide });
        const rng = new THREE.Mesh(new THREE.RingGeometry(6, 14, 32), ringMat);
        rng.position.set(0, -15, 8.1); device.add(rng);
        const spMat = new THREE.MeshStandardMaterial({ color: 0x888888, metalness: 0.9, roughness: 0.2 });
        const sp = new THREE.Mesh(new THREE.CircleGeometry(1.5, 16), spMat);
        sp.position.set(0, -15, 8.2); device.add(sp);
        const btnMat = new THREE.MeshStandardMaterial({ color: 0x555555, metalness: 0.8, roughness: 0.3 });
        const btnGeo = new THREE.CylinderGeometry(3, 3, 2, 16);
        [[-15, -38, 8], [0, -38, 8], [15, -38, 8]].forEach(([x, y, z]) => {
            const b = new THREE.Mesh(btnGeo, btnMat); b.rotation.x = Math.PI / 2;
            b.position.set(x, y, z); device.add(b);
        });
        const ledMat = new THREE.MeshStandardMaterial({ color: 0xff8c00, emissive: 0xff8c00, emissiveIntensity: 0.8 });
        const led = new THREE.Mesh(new THREE.CircleGeometry(1, 16), ledMat);
        led.position.set(22, 43, 8); device.add(led);
        scene.add(device);

        window.addEventListener('resize', () => {
            if (!canvas.parentElement) return;
            camera.aspect = canvas.clientWidth / canvas.clientHeight;
            camera.updateProjectionMatrix();
            glRenderer.setSize(canvas.clientWidth, canvas.clientHeight);
        });
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

        // Update nav buttons
        document.querySelectorAll('.nav-sphere-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.panel === name);
        });

        // Show/hide panels in ring
        PANEL_NAMES.forEach((n, i) => {
            const el = document.getElementById('panel-' + n);
            if (!el) return;
            el.style.pointerEvents = 'none';
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
            if (Math.abs(diff) > 0.3) {
                currentAngle += diff * 0.08;
                applyRotation();
            } else {
                // Rotation settled — snap and pop out the active panel
                currentAngle = targetAngle;
                applyRotation();
                isRotating = false;
                popOutPanel(activePanel);
            }
        }

        if (device) device.rotation.y += 0.003;
        if (glRenderer && scene && camera) glRenderer.render(scene, camera);
    }

    /* ─── Public API ─── */

    window.leasureScene = { init, navigateToPanel, getActivePanel: () => activePanel };

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
