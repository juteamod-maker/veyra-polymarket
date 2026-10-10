(function () {
    'use strict';

    const TABS = [
        {
            id: 1, slug: 'veyra',
            eyebrowKey: 'landing.tab.veyra.eyebrow',
            titleL1Key: 'landing.tab.veyra.title.l1',
            titleL2Key: 'landing.tab.veyra.title.l2',
            subKey: 'landing.tab.veyra.subtitle',
            mLabelKey: 'landing.tab.veyra.metric.label',
            mValueKey: 'landing.tab.veyra.metric.value',
            hKeys: ['landing.tab.veyra.h1', 'landing.tab.veyra.h2', 'landing.tab.veyra.h3', 'landing.tab.veyra.h4', 'landing.tab.veyra.h5'],
            ctaPrimaryKey: 'landing.tab.veyra.cta.primary',
            ctaSecondaryKey: 'landing.tab.veyra.cta.secondary',
            ctaPrimaryHref: '/preview',
            ctaSecondaryHref: 'https://veyra.trade/v2/go',
        },
        {
            id: 2, slug: 'weather',
            eyebrowKey: 'landing.tab.weather.eyebrow',
            titleL1Key: 'landing.tab.weather.title.l1',
            titleL2Key: 'landing.tab.weather.title.l2',
            subKey: 'landing.tab.weather.subtitle',
            mLabelKey: 'landing.tab.weather.metric.label',
            mValueKey: 'landing.tab.weather.metric.value',
            hKeys: ['landing.tab.weather.h1', 'landing.tab.weather.h2', 'landing.tab.weather.h3', 'landing.tab.weather.h4', 'landing.tab.weather.h5'],
            ctaPrimaryKey: 'landing.tab.weather.cta.primary',
            ctaSecondaryKey: 'landing.tab.weather.cta.secondary',
            ctaPrimaryHref: '/preview',
            ctaSecondaryHref: '/WeatherEventMap',
        },
        {
            id: 3, slug: 'sports',
            eyebrowKey: 'landing.tab.sports.eyebrow',
            titleL1Key: 'landing.tab.sports.title.l1',
            titleL2Key: 'landing.tab.sports.title.l2',
            subKey: 'landing.tab.sports.subtitle',
            mLabelKey: 'landing.tab.sports.metric.label',
            mValueKey: 'landing.tab.sports.metric.value',
            hKeys: ['landing.tab.sports.h1', 'landing.tab.sports.h2', 'landing.tab.sports.h3', 'landing.tab.sports.h4', 'landing.tab.sports.h5'],
            ctaPrimaryKey: 'landing.tab.sports.cta.primary',
            ctaSecondaryKey: 'landing.tab.sports.cta.secondary',
            ctaPrimaryHref: '/preview',
            ctaSecondaryHref: '/sportslive',
        },
        {
            id: 4, slug: 'valuelab',
            eyebrowKey: 'landing.tab.valuelab.eyebrow',
            titleL1Key: 'landing.tab.valuelab.title.l1',
            titleL2Key: 'landing.tab.valuelab.title.l2',
            subKey: 'landing.tab.valuelab.subtitle',
            mLabelKey: 'landing.tab.valuelab.metric.label',
            mValueKey: 'landing.tab.valuelab.metric.value',
            hKeys: ['landing.tab.valuelab.h1', 'landing.tab.valuelab.h2', 'landing.tab.valuelab.h3', 'landing.tab.valuelab.h4', 'landing.tab.valuelab.h5'],
            ctaPrimaryKey: 'landing.tab.valuelab.cta.primary',
            ctaSecondaryKey: 'landing.tab.valuelab.cta.secondary',
            ctaPrimaryHref: '/preview',
            ctaSecondaryHref: '/valuelab',
        },
        {
            id: 5, slug: 'copytrader',
            eyebrowKey: 'landing.tab.copytrader.eyebrow',
            titleL1Key: 'landing.tab.copytrader.title.l1',
            titleL2Key: 'landing.tab.copytrader.title.l2',
            subKey: 'landing.tab.copytrader.subtitle',
            mLabelKey: 'landing.tab.copytrader.metric.label',
            mValueKey: 'landing.tab.copytrader.metric.value',
            hKeys: ['landing.tab.copytrader.h1', 'landing.tab.copytrader.h2', 'landing.tab.copytrader.h3', 'landing.tab.copytrader.h4', 'landing.tab.copytrader.h5'],
            ctaPrimaryKey: 'landing.tab.copytrader.cta.primary',
            ctaSecondaryKey: 'landing.tab.copytrader.cta.secondary',
            ctaPrimaryHref: '/preview',
            ctaSecondaryHref: '/Copytrader',
        },
    ];

    const TOTAL = TABS.length;
    const IDLE_AUTOROTATE_MS = 10000;
    const AUTOROTATE_INTERVAL_MS = 5500;

    function $(sel, root) { return (root || document).querySelector(sel); }

    function tFn() {
        if (window.veyraI18n && typeof window.veyraI18n.t === 'function') return window.veyraI18n.t;
        return function (key, fb) { return fb != null ? fb : key; };
    }

    function pad2(n) { return n < 10 ? '0' + n : '' + n; }

    const stage = $('#lp-stage');
    const card = $('#lp-card');
    const eyebrowEl = $('.lp-card__eyebrow-text', card);
    const titleL1 = $('.lp-card__title-l1', card);
    const titleL2 = $('.lp-card__title-l2', card);
    const subEl = $('.lp-card__subtitle', card);
    const mValueEl = $('.lp-card__metric-value', card);
    const mLabelEl = $('.lp-card__metric-label', card);
    const chips = card.querySelectorAll('.lp-card__chip');
    const ctaPrimary = $('.lp-cta--primary', card);
    const ctaSecondary = $('#lp-cta-secondary', card);
    const idxNum = $('#lp-card-num');
    const dots = Array.from(document.querySelectorAll('.lp-dot'));
    const prevBtn = $('#lp-prev');
    const nextBtn = $('#lp-next');
    const langSel = $('#lp-lang');

    let current = 0;
    let isAnimating = false;
    let pendingDir = 0;
    let idleTimer = null;
    let autoTimer = null;
    let inAutoMode = false;

    // ── WAAPI animation shim ──────────────────────────────────────────────
    // T902: 移除 GSAP（GreenSock Standard Licence 非 OSI），改用 Web Animations API。
    // 保持 gsap.set / gsap.to / gsap.timeline 的最小语义，使调用点无需改动。
    function _toArray(t) {
        if (t == null) return [];
        if (Array.isArray(t)) return t;
        if (typeof t.length === 'number' && t !== window) return Array.prototype.slice.call(t);
        return [t];
    }
    function _buildTransform(p) {
        var x = p.x != null ? p.x : 0;
        var y = p.y != null ? p.y : 0;
        var scale = p.scale != null ? p.scale : 1;
        var skewY = p.skewY != null ? p.skewY : 0;
        var rotation = p.rotation != null ? p.rotation : 0;
        return 'translate3d(' + x + 'px,' + y + 'px,0) scale(' + scale + ') skewY(' + skewY + 'deg) rotate(' + rotation + 'deg)';
    }
    var _EASE_MAP = {
        'power2.out': 'cubic-bezier(0.22, 1, 0.36, 1)',
        'power3.out': 'cubic-bezier(0.16, 1, 0.3, 1)',
        'power2.in': 'cubic-bezier(0.55, 0.055, 0.675, 0.19)',
    };
    function _set(el, p) {
        var st = el.style;
        if ('x' in p || 'y' in p || 'scale' in p || 'skewY' in p || 'rotation' in p) st.transform = _buildTransform(p);
        if ('opacity' in p) st.opacity = String(p.opacity);
        if ('filter' in p) st.filter = p.filter;
    }
    function _waAnimate(el, p, dur, ease, delay, stagger, idx) {
        var kf = {};
        if ('x' in p || 'y' in p || 'scale' in p || 'skewY' in p || 'rotation' in p) kf.transform = _buildTransform(p);
        if ('opacity' in p) kf.opacity = String(p.opacity);
        if ('filter' in p) kf.filter = p.filter;
        return el.animate([kf], {
            duration: dur * 1000,
            delay: (delay + idx * stagger) * 1000,
            easing: _EASE_MAP[ease] || 'cubic-bezier(0.16, 1, 0.3, 1)',
            fill: 'forwards',
        });
    }
    var gsap = {
        set: function (targets, props) {
            _toArray(targets).forEach(function (el) { if (el) _set(el, props); });
        },
        to: function (targets, vars) {
            vars = vars || {};
            var els = _toArray(targets);
            var stagger = vars.stagger || 0;
            var dur = vars.duration != null ? vars.duration : 0.5;
            var pending = 0, done = 0;
            var complete = function () {
                if (pending > 0 && done === pending && vars.onComplete) vars.onComplete();
            };
            els.forEach(function (el, i) {
                if (!el || typeof el.animate !== 'function') return;
                pending++;
                var anim = _waAnimate(el, vars, dur, vars.ease, vars.delay || 0, stagger, i);
                var settle = function () { done++; complete(); };
                anim.onfinish = settle;
                anim.oncancel = settle;
            });
            if (pending === 0 && vars.onComplete) setTimeout(vars.onComplete, 0);
        },
        timeline: function (opts) {
            opts = opts || {};
            var defaults = opts.defaults || {};
            var pending = 0, done = 0, completed = false;
            var finish = function () {
                if (completed) return;
                completed = true;
                if (opts.onComplete) opts.onComplete();
            };
            var tl = {
                to: function (targets, vars, position) {
                    var v = {};
                    for (var k in defaults) v[k] = defaults[k];
                    for (var k2 in (vars || {})) v[k2] = vars[k2];
                    var els = _toArray(targets);
                    var pos = position != null ? position : 0;
                    var stagger = v.stagger || 0;
                    var dur = v.duration != null ? v.duration : 0.5;
                    els.forEach(function (el, i) {
                        if (!el || typeof el.animate !== 'function') return;
                        pending++;
                        var anim = _waAnimate(el, v, dur, v.ease, pos, stagger, i);
                        var settle = function () { done++; if (done === pending) finish(); };
                        anim.onfinish = settle;
                        anim.oncancel = settle;
                    });
                    return tl;
                }
            };
            // 所有 .to 同步登记后，若无动画则立即完成（与 gsap 一致）。
            Promise.resolve().then(function () { if (pending === 0) finish(); });
            return tl;
        }
    };

    const HAS_GSAP = true;

    function applyTabContent(tab) {
        const t = tFn();

        eyebrowEl.setAttribute('data-i18n', tab.eyebrowKey);
        eyebrowEl.textContent = t(tab.eyebrowKey, '');

        titleL1.setAttribute('data-i18n', tab.titleL1Key);
        titleL1.textContent = t(tab.titleL1Key, '');

        titleL2.setAttribute('data-i18n', tab.titleL2Key);
        titleL2.textContent = t(tab.titleL2Key, '');

        subEl.setAttribute('data-i18n', tab.subKey);
        subEl.textContent = t(tab.subKey, '');

        mValueEl.setAttribute('data-i18n', tab.mValueKey);
        mValueEl.textContent = t(tab.mValueKey, '');

        mLabelEl.setAttribute('data-i18n', tab.mLabelKey);
        mLabelEl.textContent = t(tab.mLabelKey, '');

        chips.forEach(function (chip, i) {
            const k = tab.hKeys[i];
            chip.setAttribute('data-i18n', k);
            chip.textContent = t(k, '');
        });

        ctaPrimary.setAttribute('data-i18n', tab.ctaPrimaryKey);
        ctaPrimary.textContent = t(tab.ctaPrimaryKey, 'Launch app');
        ctaPrimary.setAttribute('href', tab.ctaPrimaryHref);

        ctaSecondary.setAttribute('data-i18n', tab.ctaSecondaryKey);
        ctaSecondary.textContent = t(tab.ctaSecondaryKey, 'Learn more');
        ctaSecondary.setAttribute('href', tab.ctaSecondaryHref);

        if (idxNum) { idxNum.textContent = pad2(tab.id); }
        stage.setAttribute('data-tab', String(tab.id));

        dots.forEach(function (d) {
            const tgt = parseInt(d.getAttribute('data-target') || '0', 10);
            d.classList.toggle('is-active', tgt === tab.id);
        });
    }

    function playEnter(direction, rapid) {
        if (!HAS_GSAP) return;
        const cardW = card.getBoundingClientRect().width || 800;
        const offset = direction >= 0 ? cardW * 0.18 : -cardW * 0.18;
        const titleDur = rapid ? 0.42 : 0.78;
        const fadeDur = rapid ? 0.36 : 0.6;

        gsap.set([titleL1, titleL2], { opacity: 0, y: 36, skewY: 4, filter: 'blur(11px)' });
        gsap.set(eyebrowEl.parentElement, { opacity: 0, y: 14 });
        gsap.set(subEl, { opacity: 0, y: 18 });
        gsap.set(mValueEl.parentElement, { opacity: 0, y: 18, scale: 0.96 });
        gsap.set(chips, { opacity: 0, y: 12 });
        gsap.set([ctaPrimary, ctaSecondary], { opacity: 0, y: 16 });
        gsap.set(card, { x: offset, opacity: 0.6, scale: 0.985 });

        const tl = gsap.timeline({
            defaults: { ease: 'power3.out' }, onComplete: function () {
                isAnimating = false;
                if (pendingDir !== 0) {
                    const d = pendingDir; pendingDir = 0;
                    switchTo(current + d, d, true);
                }
            }
        });

        tl.to(card, { x: 0, opacity: 1, scale: 1, duration: rapid ? 0.32 : 0.55 }, 0);
        tl.to(eyebrowEl.parentElement, { opacity: 1, y: 0, duration: fadeDur }, 0.05);
        tl.to(titleL1, { opacity: 1, y: 0, skewY: 0, filter: 'blur(0px)', duration: titleDur }, 0.08);
        tl.to(titleL2, { opacity: 1, y: 0, skewY: 0, filter: 'blur(0px)', duration: titleDur }, 0.16);
        tl.to(subEl, { opacity: 1, y: 0, duration: fadeDur }, 0.22);
        tl.to(mValueEl.parentElement, { opacity: 1, y: 0, scale: 1, duration: fadeDur }, 0.28);
        tl.to(chips, { opacity: 1, y: 0, duration: fadeDur, stagger: 0.06 }, 0.34);
        tl.to([ctaPrimary, ctaSecondary], { opacity: 1, y: 0, duration: fadeDur, stagger: 0.08 }, 0.4);
    }

    function playExit(direction, rapid, onDone) {
        if (!HAS_GSAP) { onDone(); return; }
        const cardW = card.getBoundingClientRect().width || 800;
        const offset = direction >= 0 ? -cardW * 0.4 : cardW * 0.4;
        const dur = rapid ? 0.18 : 0.28;
        gsap.to(card, {
            x: offset, opacity: 0, scale: 0.97, duration: dur,
            ease: 'power2.in',
            onComplete: onDone,
        });
    }

    function switchTo(idx, direction, rapid) {
        const n = ((idx % TOTAL) + TOTAL) % TOTAL;
        if (n === current && !rapid) return;
        if (isAnimating) {
            pendingDir = direction || 1;
            return;
        }
        isAnimating = true;
        const dir = (direction == null) ? (n > current ? 1 : -1) : direction;
        playExit(dir, rapid, function () {
            current = n;
            applyTabContent(TABS[current]);
            try { if (window.veyraI18nApply) window.veyraI18nApply(document); } catch (_) { }
            playEnter(dir, rapid);
        });
    }

    function bumpIdle() {
        if (idleTimer) clearTimeout(idleTimer);
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
        inAutoMode = false;
        idleTimer = setTimeout(function () {
            inAutoMode = true;
            autoTimer = setInterval(function () {
                if (document.hidden) return;
                switchTo(current + 1, 1, false);
            }, AUTOROTATE_INTERVAL_MS);
        }, IDLE_AUTOROTATE_MS);
    }

    function attachDrag() {
        let startX = 0, startY = 0, dx = 0, dy = 0, dragging = false, locked = false;
        const LOCK = 14;
        const SWITCH = 70;

        function onDown(e) {
            try {
                if (e && e.target && typeof e.target.closest === 'function' &&
                    e.target.closest('a, button, select, input, textarea, .lp-cta, .lp-loginbtn, .lp-arrow, .lp-dot, .lp-lang, .lp-legal__pill, .lp-controls, .lp-cardfoot')) {
                    return;
                }
            } catch (_) { }
            bumpIdle();
            const p = (e.touches && e.touches[0]) || e;
            startX = p.clientX; startY = p.clientY;
            dx = 0; dy = 0;
            dragging = true; locked = false;
            card.classList.add('lp-card--dragging');
        }
        function onMove(e) {
            if (!dragging) return;
            const p = (e.touches && e.touches[0]) || e;
            dx = p.clientX - startX;
            dy = p.clientY - startY;
            if (!locked) {
                if (Math.abs(dx) > LOCK && Math.abs(dx) > Math.abs(dy)) {
                    locked = true;
                } else if (Math.abs(dy) > LOCK && Math.abs(dy) >= Math.abs(dx)) {
                    dragging = false;
                    card.classList.remove('lp-card--dragging');
                    if (HAS_GSAP) gsap.to(card, { x: 0, duration: 0.25, ease: 'power2.out' });
                    return;
                }
            }
            if (locked) {
                if (e.cancelable) e.preventDefault();
                if (HAS_GSAP) gsap.set(card, { x: dx * 0.55 });
            }
        }
        function onUp() {
            if (!dragging) return;
            card.classList.remove('lp-card--dragging');
            const wasDragging = dragging; dragging = false;
            if (!locked || !wasDragging) return;
            if (Math.abs(dx) > SWITCH) {
                const dir = dx < 0 ? 1 : -1;
                switchTo(current + dir, dir, false);
            } else {
                if (HAS_GSAP) gsap.to(card, { x: 0, duration: 0.3, ease: 'power3.out' });
            }
        }

        card.addEventListener('pointerdown', onDown);
        window.addEventListener('pointermove', onMove, { passive: false });
        window.addEventListener('pointerup', onUp);
        window.addEventListener('pointercancel', onUp);
    }

    function wireControls() {
        let lastControlTs = 0;
        let wheelAccum = 0;
        let lastWheelTs = 0;

        function stopPointerBubble(e) {
            e.stopPropagation();
        }

        function runControl(e, action) {
            const now = Date.now();
            if (now - lastControlTs < 220) {
                e.preventDefault();
                e.stopPropagation();
                return;
            }
            lastControlTs = now;
            e.preventDefault();
            e.stopPropagation();
            bumpIdle();
            action();
        }

        function onPrev(e) {
            runControl(e, function () {
                switchTo(current - 1, -1, false);
            });
        }

        function onNext(e) {
            runControl(e, function () {
                switchTo(current + 1, 1, false);
            });
        }

        function onDot(e) {
            const btn = e.currentTarget;
            runControl(e, function () {
                btn.blur();
                const tgt = parseInt(btn.getAttribute('data-target') || '1', 10) - 1;
                const dir = tgt > current ? 1 : -1;
                switchTo(tgt, dir, false);
            });
        }

        [prevBtn, nextBtn].forEach(function (btn) {
            btn.addEventListener('pointerdown', stopPointerBubble);
            btn.addEventListener('mousedown', stopPointerBubble);
        });

        dots.forEach(function (d) {
            d.addEventListener('pointerdown', stopPointerBubble);
            d.addEventListener('mousedown', stopPointerBubble);
        });

        prevBtn.addEventListener('pointerup', onPrev);
        nextBtn.addEventListener('pointerup', onNext);
        prevBtn.addEventListener('mouseup', onPrev);
        nextBtn.addEventListener('mouseup', onNext);
        prevBtn.addEventListener('touchend', onPrev, { passive: false });
        nextBtn.addEventListener('touchend', onNext, { passive: false });
        prevBtn.addEventListener('click', stopPointerBubble);
        nextBtn.addEventListener('click', stopPointerBubble);
        prevBtn.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') onPrev(e);
        });
        nextBtn.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') onNext(e);
        });

        dots.forEach(function (d) {
            d.addEventListener('pointerup', onDot);
            d.addEventListener('mouseup', onDot);
            d.addEventListener('touchend', onDot, { passive: false });
            d.addEventListener('click', stopPointerBubble);
            d.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') onDot(e);
            });
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowLeft') { bumpIdle(); switchTo(current - 1, -1, false); }
            else if (e.key === 'ArrowRight') { bumpIdle(); switchTo(current + 1, 1, false); }
        });

        const wheelTarget = card || window;

        wheelTarget.addEventListener('wheel', function (e) {
            bumpIdle();
            if (e.ctrlKey) return;
            try {
                if (e.target && typeof e.target.closest === 'function' &&
                    e.target.closest('a, button, select, input, textarea, .lp-lang, .lp-loginbtn')) {
                    return;
                }
            } catch (_) { }

            const now = Date.now();
            if (now - lastWheelTs > 180) wheelAccum = 0;

            const delta = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
            if (Math.abs(delta) < 8) return;

            wheelAccum += delta;
            if (Math.abs(wheelAccum) < 64) return;

            const dir = wheelAccum > 0 ? 1 : -1;
            wheelAccum = 0;
            lastWheelTs = now;
            if (e.cancelable) e.preventDefault();
            switchTo(current + dir, dir, false);
        }, { passive: false });

        ['mousemove', 'touchstart'].forEach(function (ev) {
            window.addEventListener(ev, bumpIdle, { passive: true });
        });
    }

    function wireLang() {
        if (!langSel) return;
        const cur = (window.veyraI18n && window.veyraI18n.getLang && window.veyraI18n.getLang())
            || document.documentElement.getAttribute('data-lang') || 'zh-CN';
        langSel.value = cur;
        langSel.addEventListener('change', function () {
            const v = langSel.value;
            if (window.veyraI18n && window.veyraI18n.setLang) {
                Promise.resolve(window.veyraI18n.setLang(v)).then(function () {
                    applyTabContent(TABS[current]);
                });
            }
        });
        document.addEventListener('i18n:changed', function () {
            applyTabContent(TABS[current]);
        });
    }

    function init() {
        if (window.veyraI18n && window.veyraI18n.init) {
            try { window.veyraI18n.init(); } catch (_) { }
        }
        if (window.veyraI18n && typeof window.veyraI18n.registerPagePack === 'function') {
            window.veyraI18n.registerPagePack('landing').then(function () {
                applyTabContent(TABS[0]);
                try { if (window.veyraI18nApply) window.veyraI18nApply(document); } catch (_) { }
            }).catch(function () {
                applyTabContent(TABS[0]);
                try { if (window.veyraI18nApply) window.veyraI18nApply(document); } catch (_) { }
            });
        } else {
            applyTabContent(TABS[0]);
            try { if (window.veyraI18nApply) window.veyraI18nApply(document); } catch (_) { }
        }
        wireControls();
        wireLang();
        attachDrag();
        if (HAS_GSAP) playEnter(1, false);
        bumpIdle();
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) bumpIdle();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
