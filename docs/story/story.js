/* story.js: the choreography of the 1776 story, which floats over the page in #story-overlay.
   - scroll version (prefers-reduced-motion: no-preference): one GSAP timeline scrubbed by a ScrollTrigger on
     the overlay's own scroller (.story-scroller); .story-stage is sticky inside the tall .story-track (CSS, no
     pin). Snaps to the beat labels, so one flick of the wheel or one swipe moves to the next beat. Acts are
     revealed through paper clouds (window.Clouds) or, without it, by crossfades.
   - static version (reduced motion, or gsap / Engine missing): a stacked article, the engine drawn once.
   - the highlighter: one bar per line of span.hl, all driven by one progress value.
   Classic script, runs after gsap, ScrollTrigger, engine.js, clouds.js and overlay.js. It never throws. */
(function () {
  'use strict';

  var story = document.querySelector('.story');
  if (!story) return;

  var overlay = document.getElementById('story-overlay');
  var scroller = story.closest ? story.closest('.story-scroller') : null;
  var track = story.querySelector('.story-track');
  var stage = story.querySelector('.story-stage');
  var figure = story.querySelector('.engine-figure');
  var quoteP = story.querySelector('.quote p');
  var hl = quoteP ? quoteP.querySelector('.hl') : null;

  // the notes that name a part of the engine; each fades in when its part (and the part's pieces) land
  var PART_NOTES = ['boiler', 'cylinder', 'piston', 'beam', 'valve', 'pump'];
  // scroll length of the track per timeline unit (svh), on top of the 100svh the sticky stage needs
  var SVH_PER_UNIT = 35;

  function warn(msg, err) {
    try { if (window.console && console.warn) console.warn('[story] ' + msg, err || ''); } catch (e) { /* ignore */ }
  }

  function toArr(x) {
    var out = [];
    (function add(v) {
      if (!v) return;
      if (v.nodeType === 1) { out.push(v); return; }
      if (typeof v.length === 'number' && typeof v !== 'string') {
        for (var i = 0; i < v.length; i++) add(v[i]);
      }
    })(x);
    return out;
  }

  function overlayOpen() { return !overlay || !overlay.hidden; }

  // ---------------------------------------------------------------- engine (mounted once, shared by both versions)

  var engine = null;
  var engineTried = false;

  function getEngine() {
    if (!engineTried) {
      engineTried = true;
      try {
        if (figure && window.Engine && typeof window.Engine.mount === 'function') {
          engine = window.Engine.mount(figure) || null;
        }
      } catch (err) {
        engine = null;
        warn('Engine.mount failed', err);
      }
    }
    return engine;
  }

  function call(obj, name) {
    try {
      if (obj && typeof obj[name] === 'function') obj[name]();
    } catch (err) {
      warn('engine.' + name + '() failed', err);
    }
  }

  function noteEls(name) {
    return engine && engine.notes ? toArr(engine.notes[name]) : [];
  }

  // the inner span of a note's HTML label (the only thing story.js ever transforms on a note)
  function noteSpan(name) {
    var els = noteEls(name);
    for (var i = 0; i < els.length; i++) {
      if (els[i].classList && els[i].classList.contains('engine-note')) {
        var s = els[i].querySelector('span');
        if (s) return s;
      }
    }
    return figure ? figure.querySelector('.engine-note[data-note="' + name + '"] > span') : null;
  }

  // ---------------------------------------------------------------- static version

  function buildStatic() {
    var restore = [];
    function keep(el, prop, value) {
      restore.push([el, prop, el.style[prop]]);
      el.style[prop] = value;
    }

    story.classList.remove('live');
    var eng = getEngine();
    if (eng) {
      call(eng, 'stop');
      if (eng.state) {
        eng.state.run = 0;
        eng.state.string = 1;
      }
      toArr(eng.drawables).forEach(function (el) { keep(el, 'strokeDashoffset', '0'); });
      // no boy at the valve (the string does his job); "gone to play" is never shown; of the part names
      // only "valve" stays, as in the last scroll beat ("cylinder" would cross the drawn string)
      ['boy', 'play', 'boiler', 'cylinder', 'piston', 'beam', 'pump'].reduce(function (els, name) {
        return els.concat(noteEls(name));
      }, toArr(eng.hand)).forEach(function (el) {
        keep(el, 'opacity', '0');
        keep(el, 'visibility', 'hidden');
      });
      call(eng, 'render');
    }

    return function undoStatic() {
      for (var i = restore.length - 1; i >= 0; i--) restore[i][0].style[restore[i][1]] = restore[i][2];
      restore.length = 0;
    };
  }

  function safeStatic() {
    try {
      return buildStatic();
    } catch (err) {
      warn('static version failed', err);
      return null;
    }
  }

  // ---------------------------------------------------------------- highlighter

  // Measures the lines of span.hl and keeps one .hl-bar per line inside .hl-bars. All bars are driven by
  // one progress value (0..1): each bar owns a slice of it proportional to its width, so the sweep runs
  // line after line at one speed, however many lines the current width makes.
  function Highlighter() {
    this.p = 0;
    this.box = null;
    this.bars = [];
    this.segs = [];
  }

  Highlighter.prototype.measure = function () {
    if (!quoteP || !hl) return [];
    var range = document.createRange();
    range.selectNodeContents(hl);
    var rects = range.getClientRects();
    if (range.detach) range.detach();

    var box = quoteP.getBoundingClientRect();
    // undo any scale on the paragraph or its ancestors (the bars live in its own, untransformed box)
    var sx = quoteP.offsetWidth ? box.width / quoteP.offsetWidth : 1;
    var sy = quoteP.offsetHeight ? box.height / quoteP.offsetHeight : 1;
    if (!sx) sx = 1;
    if (!sy) sy = 1;
    var ox = box.left + quoteP.clientLeft * sx;
    var oy = box.top + quoteP.clientTop * sy;

    var lines = [];
    for (var i = 0; i < rects.length; i++) {
      var r = rects[i];
      if (r.width < 0.5 || r.height < 0.5) continue;
      var x0 = (r.left - ox) / sx, x1 = (r.right - ox) / sx;
      var y0 = (r.top - oy) / sy, y1 = (r.bottom - oy) / sy;
      var line = null;
      for (var j = 0; j < lines.length; j++) {
        var L = lines[j];
        var overlap = Math.min(L.y1, y1) - Math.max(L.y0, y0);
        if (overlap > 0.5 * Math.min(L.y1 - L.y0, y1 - y0)) { line = L; break; }
      }
      if (line) {
        line.x0 = Math.min(line.x0, x0); line.x1 = Math.max(line.x1, x1);
        line.y0 = Math.min(line.y0, y0); line.y1 = Math.max(line.y1, y1);
      } else {
        lines.push({ x0: x0, x1: x1, y0: y0, y1: y1 });
      }
    }
    lines.sort(function (a, b) { return (a.y0 - b.y0) || (a.x0 - b.x0); });
    // a marker overshoots the words a little (the static highlight pads 0.14em too)
    var pad = (parseFloat(getComputedStyle(quoteP).fontSize) || 0) * 0.1;
    lines.forEach(function (l) { l.x0 -= pad; l.x1 += pad; });
    return lines;
  };

  Highlighter.prototype.build = function () {
    if (!quoteP || !hl) return;
    if (!this.box) {
      this.box = document.createElement('span');
      this.box.className = 'hl-bars';
      this.box.setAttribute('aria-hidden', 'true');
      quoteP.appendChild(this.box);
    }
    var lines = this.measure();
    while (this.bars.length > lines.length) this.box.removeChild(this.bars.pop());
    while (this.bars.length < lines.length) {
      var b = document.createElement('span');
      b.className = 'hl-bar';
      b.style.transformOrigin = '0 50%';
      this.box.appendChild(b);
      this.bars.push(b);
    }
    var total = 0;
    lines.forEach(function (l) { total += l.x1 - l.x0; });
    var acc = 0;
    var bars = this.bars;
    this.segs = lines.map(function (l, i) {
      var w = l.x1 - l.x0;
      var st = bars[i].style;
      st.left = l.x0.toFixed(2) + 'px';
      st.top = l.y0.toFixed(2) + 'px';
      st.width = w.toFixed(2) + 'px';
      st.height = (l.y1 - l.y0).toFixed(2) + 'px';
      var seg = { el: bars[i], from: total ? acc / total : 0, share: total ? w / total : 0 };
      acc += w;
      return seg;
    });
    this.apply();
  };

  Highlighter.prototype.apply = function () {
    var p = this.p;
    for (var i = 0; i < this.segs.length; i++) {
      var s = this.segs[i];
      var v = s.share > 0 ? (p - s.from) / s.share : (p > s.from ? 1 : 0);
      v = v < 0 ? 0 : v > 1 ? 1 : v;
      s.el.style.transform = 'scaleX(' + v.toFixed(4) + ')';
    }
  };

  Highlighter.prototype.destroy = function () {
    if (this.box && this.box.parentNode) this.box.parentNode.removeChild(this.box);
    this.box = null;
    this.bars = [];
    this.segs = [];
  };

  // ---------------------------------------------------------------- scroll version

  // useClouds: reveal each act through window.Clouds (paper clouds part) instead of crossfading the acts.
  // Returns { undo, sync }: undo reverts everything; sync re-measures and jumps the story to the scroll position.
  function buildScroll(useClouds) {
    var eng = getEngine();
    if (!eng || !eng.state) throw new Error('no engine');

    var cleanups = [];
    function runCleanups() {
      while (cleanups.length) {
        try { cleanups.pop()(); } catch (err) { warn('cleanup failed', err); }
      }
    }

    var st = null;
    var ctx = gsap.context(function () {});
    try {
      ctx.add(function () {
        // .live first: it changes the layout everything below measures
        story.classList.add('live');
        cleanups.push(function () { story.classList.remove('live'); });

        var q = function (sel) { return story.querySelector(sel); };
        var sheet = q('.sheet');
        var stamp = q('.stamp');
        var facts = toArr(story.querySelectorAll('.stamp .fact'));
        var scene = q('.scene');
        var kicker = q('.kicker');
        var cap = {};
        toArr(story.querySelectorAll('.cap')).forEach(function (el) { cap[el.getAttribute('data-cap')] = el; });
        var caps = [cap[2], cap[3], cap[4]].filter(Boolean);
        var quote = q('.quote');
        var end = q('.end');

        var drawables = toArr(eng.drawables);
        var hand = toArr(eng.hand);
        var allNotes = [];
        Object.keys(eng.notes || {}).forEach(function (k) { allNotes = allNotes.concat(noteEls(k)); });
        var render = function () { call(eng, 'render'); };

        var clouds = null;
        if (useClouds) {
          clouds = window.Clouds.mount(sheet);
          if (!clouds) throw new Error('Clouds.mount returned nothing');
          cleanups.push(function () { clouds.destroy(); });
        }

        // ---- initial states (gsap.set inside the context, so a matchMedia switch reverts them)
        var set = function (targets, vars) {
          var a = toArr(targets);
          if (a.length) gsap.set(a, vars);
        };
        set(facts, { opacity: 0, y: 14 });
        set(scene, { opacity: 0 });
        set(quote, clouds ? { opacity: 0 } : { opacity: 0, y: 14 });
        // the end card holds a button: keep it out of the tab order until it shows (autoAlpha = visibility too)
        set(end, clouds ? { autoAlpha: 0 } : { autoAlpha: 0, y: 14 });
        set([kicker].concat(caps), { opacity: 0, y: 14 });
        // dash 1 + gap 2 (pathLength 1): at offset 1 the whole path sits in the gap, no round-cap dot at either end
        set(drawables, { strokeDasharray: '1 2', strokeDashoffset: 1 });
        set(allNotes.concat(hand), { opacity: 0 });   // "gone to play" stays hidden for good
        PART_NOTES.concat(['boy', 'state', 'string']).forEach(function (n) {
          var s = noteSpan(n);
          if (s) gsap.set(s, { y: 4 });
        });
        gsap.set(eng.state, { run: 0, string: 0 });
        render();
        if (clouds) clouds.set(true);   // the story opens under the clouds

        var hlr = new Highlighter();
        cleanups.push(function () { hlr.destroy(); });

        var tl = gsap.timeline({ paused: true, defaults: { ease: 'power3.out', duration: 0.5 } });
        var t = 0;
        // a rest point: a short hold with the beat's label in the middle (snapping lands here)
        function rest(name) {
          t += 0.1;
          tl.addLabel(name, t);
          t += 0.1;
        }
        // a note's label and its leader line fade in; the label's span rises 4px
        function noteIn(name, at, dur) {
          var els = noteEls(name);
          if (!els.length) return;
          tl.to(els, { opacity: 1, duration: dur || 0.25 }, at);
          var s = noteSpan(name);
          if (s) tl.to(s, { y: 0, duration: dur || 0.25 }, at);
        }
        function notesOut(names, at) {
          var els = [];
          names.forEach(function (n) { els = els.concat(noteEls(n)); });
          if (els.length) tl.to(els, { opacity: 0, duration: 0.25 }, at);
        }
        // from one act to the next: clouds close, the acts swap underneath, the clouds part with a push-in
        // (or a crossfade without clouds). Moves t to where the new act is fully open; `onOpen(at)` places the
        // new act's own first moves.
        function actChange(from, toEl, toVisible, onOpen) {
          var at = t, open;
          if (clouds) {
            clouds.close(tl, at, 0.45);
            if (from) tl.set(from, { opacity: 0 }, at + 0.5);
            if (toEl) tl.set(toEl, toVisible({ scale: 1.04 }), at + 0.5);
            clouds.part(tl, at + 0.55, 0.55);
            if (toEl) tl.to(toEl, { scale: 1, duration: 0.65 }, at + 0.55);
            open = at + 1.2;
            if (onOpen) onOpen(at + 0.65);
          } else {
            if (from) tl.to(from, { opacity: 0, y: -12, duration: 0.3 }, at);
            if (toEl) tl.to(toEl, toVisible({ y: 0, duration: 0.35 }), at + 0.2);
            open = at + 0.55;
            if (onOpen) onOpen(at + 0.25);
          }
          t = open;
        }
        var opacity1 = function (v) { v.opacity = 1; return v; };
        var autoAlpha1 = function (v) { v.autoAlpha = 1; return v; };

        // ---- start: covered by the clouds (or the bare year without them)
        tl.addLabel('start', 0);
        t = 0.1;

        // ---- act A (.stamp): the clouds part on the year; fact 1, then fact 2 rise in
        if (clouds) {
          clouds.part(tl, t, 0.55);
          if (facts[0]) tl.to(facts[0], { opacity: 1, y: 0, duration: 0.4 }, t + 0.4);
          if (facts[1]) tl.to(facts[1], { opacity: 1, y: 0, duration: 0.4 }, t + 0.55);
          t += 0.95;
        } else {
          if (facts[0]) tl.to(facts[0], { opacity: 1, y: 0, duration: 0.4 }, t);
          if (facts[1]) tl.to(facts[1], { opacity: 1, y: 0, duration: 0.4 }, t + 0.15);
          t += 0.55;
        }
        rest('year');

        // ---- act B (.scene): kicker and caption 2 come in as it opens, then the engine draws itself
        var drawAt = 0;
        actChange(stamp, scene, opacity1, function (at) {
          if (kicker) tl.to(kicker, { opacity: 1, y: 0, duration: 0.35 }, at);
          if (cap[2]) tl.to(cap[2], { opacity: 1, y: 0, duration: 0.35 }, at + 0.05);
          drawAt = at + 0.15;
        });
        var DRAW = 1.2;
        var n = drawables.length;
        var d = n > 1 ? Math.min(0.5, DRAW * 0.3) : DRAW;
        var step = n > 1 ? (DRAW - d) / (n - 1) : 0;
        var partEnd = {};
        drawables.forEach(function (el, i) {
          var at = drawAt + i * step;
          tl.to(el, { strokeDashoffset: 0, duration: d, ease: 'power2.inOut' }, at);
          // a note belongs to its part and the part's pieces: "valve" lands with "valve-plate", "pump" with "pump-chain"
          var part = el.getAttribute('data-part') || '';
          PART_NOTES.forEach(function (name) {
            if (part === name || part.indexOf(name + '-') === 0) partEnd[name] = Math.max(partEnd[name] || 0, at + d);
          });
        });
        PART_NOTES.forEach(function (name) {
          var at = partEnd[name] != null ? Math.min(partEnd[name] - 0.12, drawAt + DRAW - 0.1) : drawAt + DRAW - 0.2;
          noteIn(name, at);
        });
        t = Math.max(t, drawAt + DRAW);
        rest('engine');

        // ---- caption 2 at work: the engine runs; part notes out but "valve"; open/shut, the hand and "a boy" in
        tl.to(eng.state, { run: 1, duration: 0.35, ease: 'power1.inOut', onUpdate: render }, t);
        notesOut(PART_NOTES.filter(function (x) { return x !== 'valve'; }), t + 0.05);
        noteIn('state', t + 0.15);
        if (hand.length) tl.to(hand, { opacity: 1, duration: 0.3 }, t + 0.25);
        noteIn('boy', t + 0.25, 0.3);
        t += 0.55;
        rest('boy');

        // ---- caption 3: "…by tying a string from the handle of the valve…": the string draws itself
        if (cap[2]) tl.to(cap[2], { opacity: 0, y: -10, duration: 0.2 }, t);
        if (cap[3]) tl.to(cap[3], { opacity: 1, y: 0, duration: 0.3 }, t + 0.15);
        tl.to(eng.state, { string: 1, duration: 0.5, ease: 'power2.inOut', onUpdate: render }, t + 0.25);
        noteIn('string', t + 0.65);
        t += 0.9;
        rest('string');

        // ---- caption 4: "…without his assistance": the hand and "a boy" go; the valve keeps working
        if (cap[3]) tl.to(cap[3], { opacity: 0, y: -10, duration: 0.2 }, t);
        if (cap[4]) tl.to(cap[4], { opacity: 1, y: 0, duration: 0.3 }, t + 0.15);
        notesOut(['boy'], t + 0.25);
        if (hand.length) tl.to(hand, { opacity: 0, duration: 0.3 }, t + 0.25);
        t += 0.55;
        rest('liberty');

        // ---- act C (.quote), then the highlighter once it is open
        actChange(scene, quote, opacity1, null);
        rest('quote');
        tl.to(hlr, { p: 1, duration: 0.7, ease: 'power1.inOut', onUpdate: function () { hlr.apply(); } }, t);
        t += 0.7;
        rest('marked');

        // ---- act D (.end): one line and the way back to the page; the last label is the timeline's end
        actChange(quote, end, autoAlpha1, null);
        tl.addLabel('end', t);
        if (tl.duration() < t) tl.to({}, { duration: t - tl.duration() }, tl.duration());

        // ---- the scroll length follows the timeline
        if (track) {
          var oldH = track.style.height;
          var h = Math.round(100 + t * SVH_PER_UNIT);
          track.style.height = h + 'vh';
          track.style.height = h + 'svh';   // ignored where svh is not supported
          cleanups.push(function () { track.style.height = oldH; });
        }

        st = ScrollTrigger.create({
          animation: tl,
          scroller: scroller || undefined,
          trigger: track || story,
          start: 'top top',
          end: track ? 'bottom bottom' : function () { return '+=' + Math.round(window.innerHeight * t * SVH_PER_UNIT / 100); },
          pin: track ? false : stage,
          scrub: 0.4,
          invalidateOnRefresh: true,
          // inertia off: the snap goes to the next label from where the scroll stopped, so a flick moves one beat
          // (?nosnap turns it off, for screenshots of frames between beats)
          snap: /[?&]nosnap\b/.test(location.search) ? undefined
            : { snapTo: 'labelsDirectional', duration: { min: 0.2, max: 0.6 }, delay: 0.05, ease: 'power3.out', inertia: false }
        });
        story.__storyTimeline = tl;   // debug handle (tests, the console)
        cleanups.push(function () { story.__storyTimeline = null; });

        // the engine's time loop runs only while the story is on screen and the overlay open
        ScrollTrigger.create({
          scroller: scroller || undefined,
          trigger: track || story,
          start: 'top bottom',
          end: 'bottom top',
          onToggle: function (self) { call(eng, self.isActive && overlayOpen() ? 'start' : 'stop'); }
        });
        cleanups.push(function () { call(eng, 'stop'); });

        // highlighter bars: measured now, again after every refresh and on resize (line breaks change)
        hlr.build();
        var onRefresh = function () { if (overlayOpen()) hlr.build(); };
        ScrollTrigger.addEventListener('refresh', onRefresh);
        cleanups.push(function () { ScrollTrigger.removeEventListener('refresh', onRefresh); });
        var raf = 0;
        var onResize = function () {
          if (raf) return;
          raf = requestAnimationFrame(function () { raf = 0; if (overlayOpen()) hlr.build(); });
        };
        window.addEventListener('resize', onResize);
        cleanups.push(function () {
          window.removeEventListener('resize', onResize);
          if (raf) cancelAnimationFrame(raf);
          raf = 0;
        });
      });
    } catch (err) {
      runCleanups();
      try { ctx.revert(); } catch (e) { /* ignore */ }
      throw err;
    }

    return {
      undo: function () {
        runCleanups();
        ctx.revert();
      },
      // after the overlay opens: measure again and land on the scroll position at once (no scrub catch-up)
      sync: function () {
        ScrollTrigger.refresh();
        if (!st || !st.animation) return;
        var tw = st.getTween ? st.getTween() : null;
        if (tw) tw.progress(1);
        st.animation.progress(st.progress);
      }
    };
  }

  // ---------------------------------------------------------------- start

  function init() {
    var g = window.gsap, ST = window.ScrollTrigger;
    var mq = function (m) { return !!(window.matchMedia && window.matchMedia(m).matches); };
    var supported = mq('(prefers-reduced-motion: no-preference)') || mq('(prefers-reduced-motion: reduce)');
    if (!g || !ST || typeof g.matchMedia !== 'function' || typeof g.context !== 'function' || !supported || !getEngine()) {
      safeStatic();
      return;
    }

    g.registerPlugin(ST);
    ST.config({ ignoreMobileResize: true });

    var mm = g.matchMedia();
    mm.add('(prefers-reduced-motion: no-preference)', function () {
      var built = null;
      var dead = false;

      function build() {
        if (built || dead) return;
        if (window.Clouds && typeof window.Clouds.mount === 'function') {
          try {
            built = buildScroll(true);
          } catch (err) {
            warn('clouds failed; crossfading the acts instead', err);
          }
        }
        if (!built) {
          try {
            built = buildScroll(false);
          } catch (err) {
            warn('scroll version failed; showing the static one', err);
            var undoStatic = safeStatic();
            built = { undo: function () { if (undoStatic) undoStatic(); }, sync: function () {} };
          }
        }
      }
      function onOpen() {
        try {
          if (!built) build();
          if (built) built.sync();
          call(engine, 'start');
        } catch (err) {
          warn('opening the story failed', err);
        }
      }
      function onClose() { call(engine, 'stop'); }

      // build now if the overlay is open (overlay.js opens it before story.js runs), else on the first open
      if (overlayOpen()) build();
      if (overlay) {
        overlay.addEventListener('storyoverlay:open', onOpen);
        overlay.addEventListener('storyoverlay:close', onClose);
      }
      return function () {
        dead = true;
        if (overlay) {
          overlay.removeEventListener('storyoverlay:open', onOpen);
          overlay.removeEventListener('storyoverlay:close', onClose);
        }
        try { if (built) built.undo(); } catch (err) { warn('reverting the scroll version failed', err); }
        built = null;
      };
    });
    mm.add('(prefers-reduced-motion: reduce)', function () {
      var undo = safeStatic();
      return function () {
        try { if (undo) undo(); } catch (err) { warn('reverting the static version failed', err); }
      };
    });

    if (document.fonts && document.fonts.ready && typeof document.fonts.ready.then === 'function') {
      document.fonts.ready.then(function () { if (overlayOpen()) ST.refresh(); }, function () {});
    }
  }

  try {
    init();
  } catch (err) {
    warn('story failed to start; showing the static version', err);
    story.classList.remove('live');
    safeStatic();
  }
})();
