/* clouds.js: cut-paper cloud banks between the acts of the 1776 story ("拨云见下一幕").
   Each side of the sheet has three layers of grey paper with a scalloped, scissor-cut cloud edge facing the
   centre. Closed, the two back layers overlap in the middle and the sheet is covered edge to edge. Parting
   pushes every layer outward and scales it up about the sheet's centre, front layers further and faster
   than back ones, like a camera moving through the clouds.

   Classic script; uses the global `gsap` (only for set/close/part). Exposes window.Clouds:

     const clouds = Clouds.mount(sheetEl);   // appends <div class="clouds" aria-hidden="true"> to the sheet
     clouds.set(true | false)                // instant: covered / parked outside (the default after mount)
     clouds.close(tl, at, dur = 0.6)         // closing tweens on timeline `tl` at position `at`
     clouds.part(tl, at, dur = 0.8)          // parting tweens
     clouds.destroy()                        // remove DOM, observer and tweens

   Only transforms are tweened (xPercent, yPercent, scale) and only on the stable .cloud-layer wrappers, each
   exactly the size of the sheet, so xPercent is a share of the sheet's width. The scallop geometry is built
   in the sheet's real pixel size and rebuilt on resize (only the path `d` changes), so tweens already in a
   timeline stay valid. Every tween is a fromTo with immediateRender: false: scrubbing either way, or jumping,
   always lands on the right state. */
(function () {
  'use strict';

  var STYLE_ID = 'clouds-css';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var TAU = Math.PI * 2;

  /* Tones: a little lighter and darker than the paper. Far layers are lighter, near layers darker
     (on both papers), so depth reads from tone alone; a faint hairline separates same-tone overlaps. */
  var CSS = [
    '.clouds{position:absolute;top:0;right:0;bottom:0;left:0;z-index:2;pointer-events:none;overflow:hidden;',
    'contain:layout paint style;',
    '--cloud-1:#3a3b3f;--cloud-1b:#36373b;--cloud-2:#313236;--cloud-3:#26272a;',
    '--cloud-line:var(--ink-soft,#8a8c91);--cloud-line-opacity:.32}',
    'html[data-paper="light"] .clouds{--cloud-1:#dadbdd;--cloud-1b:#d5d6d8;--cloud-2:#c3c4c7;--cloud-3:#b6b7ba;',
    '--cloud-line-opacity:.34}',
    '.clouds .cloud-layer{position:absolute;top:0;right:0;bottom:0;left:0;transform-origin:50% 50%;',
    'will-change:transform}',
    '.clouds svg{position:absolute;top:0;left:0;width:100%;height:100%;display:block;overflow:visible}',
    '.clouds .cloud-fill{stroke:none;fill:var(--cloud-2)}',
    '.clouds.is-narrow .cloud-layer[data-depth="1"]{visibility:hidden}',
    '.clouds .cloud-layer[data-depth="0"] .cloud-fill{fill:var(--cloud-1)}',
    '.clouds .cloud-layer[data-depth="0"][data-side="right"] .cloud-fill{fill:var(--cloud-1b)}',
    '.clouds .cloud-layer[data-depth="2"] .cloud-fill{fill:var(--cloud-3)}',
    '.clouds .cloud-edge{fill:none;stroke:var(--cloud-line);stroke-opacity:var(--cloud-line-opacity);',
    'stroke-width:1px;stroke-linejoin:round;vector-effect:non-scaling-stroke}'
  ].join('');

  /* Layers in paint order (back first). side -1 = left, +1 = right. Local x is measured from the side's
     own outer edge toward the centre. Each edge is a row of puffs (lobes) stacked down the sheet, each puff
     cut into scallops.
     base:  the edge's innermost line (share of the sheet width); puffs stick out up to base + lobe;
     lobe:  tallest puff (share of width); bump: scallop size factor (near = bigger);
     cap:   largest scallop chord (share of width), which bounds how far a scallop pokes past its puff.
     The back pair always overlaps in the middle: both bases sit OVERLAP past the centre line.
     The right back layer is the bottom sheet; it only shows as the band between the left back layer's edge
     and the right mid layer, so that band is kept wide (a thin band squeezed between two scalloped edges
     facing each other reads as a row of thorns). */
  var OVERLAP = 0.035;
  var LAYERS = [
    { side: 1, depth: 0, seed: 0x5eed02, base: 0.5 + OVERLAP, lobe: 0.08, bump: 0.85, cap: 0.14 },
    { side: -1, depth: 0, seed: 0x5eed01, base: 0.5 + OVERLAP, lobe: 0.08, bump: 0.85, cap: 0.14 },
    { side: -1, depth: 1, seed: 0x5eed03, base: 0.19, lobe: 0.15, bump: 1.0, cap: 0.18 },
    { side: 1, depth: 1, seed: 0x5eed04, base: 0.13, lobe: 0.14, bump: 1.0, cap: 0.18 },
    { side: -1, depth: 2, seed: 0x5eed05, base: 0.05, lobe: 0.17, bump: 1.2, cap: 0.20,
      narrow: { base: 0.08, lobe: 0.2 } },
    { side: 1, depth: 2, seed: 0x5eed06, base: 0.04, lobe: 0.14, bump: 1.2, cap: 0.20,
      narrow: { base: 0.07, lobe: 0.18 } }
  ];
  /* On a narrow sheet (a phone) six bands would read as stripes: the mid layers are hidden there and the
     front layers reach a little further. */
  var NARROW = 560;

  /* Motion by depth (x, y in % of the sheet; signs follow the side). An edge never pokes past
     base + lobe + jitter + the tallest scallop (0.67 x cap / 2): about 0.68 (back), 0.42 (mid), 0.36 (front)
     of the width (measured: 0.65, 0.37, 0.32), so these end states are off the sheet for any sheet shape.
     Back = only just clear, so its reveal fills the tween; near layers travel further and faster. */
  var PARTED = [ // after part()
    { x: 72, y: 2, s: 1.08 },
    { x: 90, y: -3, s: 1.18 },
    { x: 112, y: 4, s: 1.3 }
  ];
  var ENTER = [ // where close() brings them in from: a little smaller, so they grow as they arrive
    { x: 76, y: -2, s: 0.99 },
    { x: 94, y: 3, s: 0.97 },
    { x: 116, y: -4, s: 0.95 }
  ];
  var LAG = 0.05; // per depth step, share of dur: part moves near layers first, close brings far ones first

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }

  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function f1(n) { return (Math.round(n * 10) / 10).toString(); }

  /* One layer's outline in sheet pixels: a block from outside the sheet up to a scalloped edge.
     1. puffs: a row of overlapping circles down the sheet (they meet in wide notches, never thorns);
     2. cusps: points spaced along that line by arc length (small, medium and big steps, a little jitter
        toward the centre);
     3. between cusps, circular arcs from a semicircle to a shallower bump, bulging toward the centre. */
  function outline(spec, W, H) {
    var rnd = mulberry32(spec.seed);
    var u = clamp(Math.sqrt(W * H) / 8, 36, 120) * spec.bump;   // scallop unit: round on any sheet shape
    var padX = Math.max(0.14 * W, 48);
    var padY = 0.16 * H + u;                                     // room for scale < 1 and vertical drift
    var o = W < NARROW && spec.narrow ? spec.narrow : spec;
    var base = o.base * W;
    var hMax = o.lobe * W;
    var jit = Math.min(0.1 * u, 0.012 * W);
    var capC = spec.cap * W;
    var y0 = -padY - u, y1 = H + padY + u;

    // 1. puffs: overlapping circles down the sheet; each pokes h past the base line
    var puffs = [];
    var R = 0, cy = y0 - rnd() * u;
    for (var g = 0; g < 200 && cy < y1 + R; g++) {
      var h = hMax * (0.35 + 0.65 * rnd());
      R = Math.max(0.9 * u, h * (1.1 + 0.8 * rnd()));
      if (puffs.length) cy += (puffs[puffs.length - 1].R + R) * (0.5 + 0.22 * rnd());
      puffs.push({ cy: cy, cx: base + h - R, R: R });
    }
    function contour(y) {
      var x = base;
      for (var i = 0; i < puffs.length; i++) {
        var P = puffs[i], dy = y - P.cy;
        if (dy * dy < P.R * P.R) x = Math.max(x, P.cx + Math.sqrt(P.R * P.R - dy * dy));
      }
      return x;
    }

    // the puff line, sampled every 2px, with its running length
    var line = [], acc = 0;
    for (var y = y0; y <= y1; y += 2) {
      var x = contour(y);
      if (line.length) {
        var q = line[line.length - 1];
        acc += Math.sqrt((x - q[0]) * (x - q[0]) + (y - q[1]) * (y - q[1]));
      }
      line.push([x, y, acc]);
    }

    // 2. cusps by arc length
    var pts = [[line[0][0], line[0][1]]];
    var sAt = 0, j = 0;
    for (var guard = 0; guard < 600; guard++) {
      var k = rnd(), c;
      if (k < 0.2) c = 0.3 + 0.2 * rnd();          // small
      else if (k < 0.8) c = 0.6 + 0.5 * rnd();     // medium
      else c = 1.2 + 0.5 * rnd();                  // big
      sAt += Math.min(c * u, capC);
      if (sAt >= acc) break;
      while (j < line.length - 2 && line[j + 1][2] < sAt) j++;
      var a = line[j], b = line[j + 1];
      var f = (sAt - a[2]) / Math.max(1e-6, b[2] - a[2]);
      pts.push([a[0] + (b[0] - a[0]) * f + rnd() * jit, a[1] + (b[1] - a[1]) * f]);
    }
    var last = line[line.length - 1];
    pts.push([last[0], last[1]]);

    // 3. arcs
    var right = spec.side > 0;
    var sweep = right ? 0 : 1;                     // walking down, bulge toward the centre
    function X(v) { return right ? W - v : v; }

    var d = 'M' + f1(X(-padX)) + ',' + f1(pts[0][1]) + 'L' + f1(X(pts[0][0])) + ',' + f1(pts[0][1]);
    for (var i = 1; i < pts.length; i++) {
      var dx = pts[i][0] - pts[i - 1][0], dy = pts[i][1] - pts[i - 1][1];
      var r = Math.sqrt(dx * dx + dy * dy) / 2 * (1.08 + 0.32 * rnd());   // cusps open 44-92 deg
      d += 'A' + f1(r) + ',' + f1(r) + ' 0 0 ' + sweep + ' ' + f1(X(pts[i][0])) + ',' + f1(pts[i][1]);
    }
    d += 'L' + f1(X(-padX)) + ',' + f1(pts[pts.length - 1][1]) + 'Z';
    return d;
  }

  var styleUsers = 0;
  function injectStyle() {
    styleUsers++;
    if (document.getElementById(STYLE_ID)) return;
    var st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }
  function releaseStyle() {
    styleUsers = Math.max(0, styleUsers - 1);
    if (styleUsers) return;
    var st = document.getElementById(STYLE_ID);
    if (st && st.parentNode) st.parentNode.removeChild(st);
  }

  function assign(a, b) { for (var k in b) if (Object.prototype.hasOwnProperty.call(b, k)) a[k] = b[k]; return a; }

  function mount(sheetEl) {
    if (!sheetEl || sheetEl.nodeType !== 1) return null;
    injectStyle();

    var root = document.createElement('div');
    root.className = 'clouds';
    root.setAttribute('aria-hidden', 'true');

    var layers = LAYERS.map(function (spec) {
      var el = document.createElement('div');
      el.className = 'cloud-layer';
      el.setAttribute('data-side', spec.side < 0 ? 'left' : 'right');
      el.setAttribute('data-depth', String(spec.depth));
      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('preserveAspectRatio', 'none');   // a one-frame size lag stretches, never uncovers
      svg.setAttribute('focusable', 'false');
      var fill = document.createElementNS(SVG_NS, 'path');
      fill.setAttribute('class', 'cloud-fill');
      var edge = document.createElementNS(SVG_NS, 'path');
      edge.setAttribute('class', 'cloud-edge');
      svg.appendChild(fill);
      svg.appendChild(edge);
      el.appendChild(svg);
      root.appendChild(el);
      return { spec: spec, el: el, svg: svg, fill: fill, edge: edge };
    });
    var els = layers.map(function (L) { return L.el; });

    sheetEl.appendChild(root);

    var W = 0, H = 0;
    function rebuild(force) {
      var w = root.clientWidth, h = root.clientHeight;
      if (!w || !h) return;
      if (!force && Math.abs(w - W) < 0.5 && Math.abs(h - H) < 0.5) return;
      W = w; H = h;
      root.classList.toggle('is-narrow', W < NARROW);
      layers.forEach(function (L) {
        var d = outline(L.spec, W, H);
        L.svg.setAttribute('viewBox', '0 0 ' + f1(W) + ' ' + f1(H));
        L.fill.setAttribute('d', d);
        L.edge.setAttribute('d', d);
      });
    }
    rebuild(true);

    var ro = null;
    function onWinResize() { rebuild(false); }
    if (typeof ResizeObserver === 'function') {
      ro = new ResizeObserver(function () { rebuild(false); });
      ro.observe(root);
    } else {
      window.addEventListener('resize', onWinResize);
    }

    function closedVars() { return { xPercent: 0, yPercent: 0, scale: 1 }; }
    function stateVars(L, table) {
      var m = table[L.spec.depth], s = L.spec.side;
      return { xPercent: s * m.x, yPercent: s * m.y, scale: m.s };
    }
    function cssTransform(v) {
      return 'translate(' + v.xPercent + '%,' + v.yPercent + '%) scale(' + v.scale + ')';
    }

    function set(covered) {
      layers.forEach(function (L) {
        var v = covered ? closedVars() : stateVars(L, PARTED);
        if (window.gsap) window.gsap.set(L.el, v);
        else L.el.style.transform = cssTransform(v);
      });
    }

    // builds the tweens in a child timeline so `at` can be any GSAP position (number, label, "+=0.2")
    function addPhase(tl, at, dur, kind) {
      var gsap = window.gsap;
      if (!gsap) return tl;                                // no gsap, no timeline: nothing to add
      dur = typeof dur === 'number' && dur > 0 ? dur : (kind === 'close' ? 0.6 : 0.8);
      var sub = gsap.timeline();
      layers.forEach(function (L) {
        var depth = L.spec.depth;
        var steps = kind === 'part' ? (2 - depth) : depth;  // part: back starts last; close: front starts last
        var off = steps * LAG * dur;
        var from = kind === 'part' ? closedVars() : stateVars(L, ENTER);
        var to = kind === 'part' ? stateVars(L, PARTED) : closedVars();
        sub.fromTo(L.el, from, assign(to, {
          duration: dur - off,
          ease: kind === 'part' ? 'power3.out' : 'power2.inOut',
          immediateRender: false,
          overwrite: false
        }), off);
      });
      if (tl && typeof tl.add === 'function') {
        tl.add(sub, at == null ? '>' : at);
        return tl;
      }
      return sub; // no timeline given: it simply plays now
    }

    var api = {
      el: root,
      layers: els,
      set: set,
      close: function (tl, at, dur) { return addPhase(tl, at, dur === undefined ? 0.6 : dur, 'close'); },
      part: function (tl, at, dur) { return addPhase(tl, at, dur === undefined ? 0.8 : dur, 'part'); },
      rebuild: function () { rebuild(true); },
      destroy: function () {
        if (ro) ro.disconnect(); else window.removeEventListener('resize', onWinResize);
        if (window.gsap) window.gsap.killTweensOf(els);
        if (root.parentNode) root.parentNode.removeChild(root);
        releaseStyle();
        api.destroy = function () {};
      }
    };

    set(false);
    return api;
  }

  window.Clouds = { mount: mount };
})();
