/* engine.js: a Newcomen atmospheric engine as an ink line drawing, and its motion.
   window.Engine.mount(figureEl) -> engine (see CONTRACT.md, "engine.js API").
   Classic script, no modules. Uses gsap.ticker for the time loop when gsap is there, else requestAnimationFrame.

   The drawing (viewBox 0 0 600 420), side view:
   - brick furnace + haystack boiler, lower left; the open-topped cylinder above it with the piston inside;
     the regulator valve on the short steam pipe between them, its hand lever pointing left (where the boy stands)
   - the great beam on its gudgeon on top of the engine-house wall, an arch head at each end (radius R about the
     gudgeon), so both chains hang straight down: the piston moves by R*theta, the pump rod by -R*theta
   - the pump rod goes down a mine shaft below the ground line, on the right
   Motion: theta = A * run * sin(wt), one stroke 2.4 s. The valve is open while the piston rises (cos(wt) > 0)
   and shut while it falls, switching near the turning points; lever raised = open, a plate inside turns 90 deg.

   Draw-on: Chrome restarts the dash pattern at every subpath, so every part that should draw itself on is one
   continuous subpath where it matters: textures are serpentines (course after course, joints as out-and-back
   teeth, the turns hidden under the main outline), chains go out along one side of the links and back. */
(function () {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';

  // ------------------------------------------------------------------ geometry (viewBox units)
  var MAIN = 2.4;        // main ink weight
  var SOFT = 1.5;        // brickwork, ground, shaft, inner walls, straps
  var BRACE = 1.8;       // arch-head braces
  var CHAIN_W = 1.6;
  var STRING_W = 1.7;
  var LEADER_W = 1.1;

  var P = { x: 415, y: 84 };   // the gudgeon (beam pivot)
  var R = 150;                 // chain pitch line on the arch heads = lever arm of both chains
  var FACE = R - 3;            // the arch timber's outer face: the chain links lie on it
  var ARCH_IN = R - 10;        // its inner face (the beam ends butt against it)
  var CX = P.x - R;            // 265: cylinder axis, piston chain
  var PX = P.x + R;            // 565: pump rod
  var AMP = 0.125;             // beam amplitude at run 1 (rad, ~7.2 deg): stroke +-18.75
  var PERIOD = 2.4;            // one up-and-down stroke (s)
  var OMEGA = 2 * Math.PI / PERIOD;
  var V = { x: CX, y: 233 };   // valve spindle
  var LEVER_LEN = 124;         // spindle to the tip ring
  var LEVER_SHUT = -4;         // deg (SVG rotate of a lever pointing left): tip a little down
  var LEVER_OPEN = 18;         // deg: tip raised
  var SWITCH_K = 0.25;         // valve switches while |cos(wt)| < K: ~0.2 s around each turning point
  var GROUND = 364;
  var FURN_TOP = 334;
  var WALL_TOP = 106;

  // where the string is tied: the top end of the left arch head (piston side of the beam)
  var TIE = archPoint(-1, (FACE + ARCH_IN) / 2, 20);
  var SAG_MIN = 0.6, SAG_MAX = 9;   // string sag (units at mid-string): taut .. most slack

  // labels: anchor in viewBox units (converted to % of the figure), align per CONTRACT.
  // "a boy" / "gone to play" sit in the row under the lever, left of the steam pipe: "gone to play" stays clear
  // of everything after story.js slides it 48px right. Leaders marked `track` follow their moving target.
  var NOTE_SPECS = {
    boiler:   { text: 'boiler', x: 197, y: 314, align: 'right' },
    cylinder: { text: 'cylinder', x: 228, y: 160, align: 'right' },
    piston:   { text: 'piston', x: 299, y: 171, align: 'left', leader: 'M296 171 H281' },
    beam:     { text: 'beam', x: 460, y: 48, align: 'center' },
    valve:    { text: 'valve', x: 285, y: 230, align: 'left' },
    pump:     { text: 'to the mine pump', x: 543, y: 392, align: 'right', leader: 'M547 392 H558' },
    boy:      { text: 'a boy', x: 160, y: 266, align: 'right', leader: 'track' },
    play:     { text: 'gone to play', x: 160, y: 266, align: 'right' },
    string:   { text: 'string', x: 186, y: 120, align: 'right', leader: 'track' },
    state:    { text: 'shut', x: 281, y: 266, align: 'left', state: true }
  };
  var NOTE_ORDER = ['boiler', 'cylinder', 'piston', 'beam', 'valve', 'pump', 'boy', 'play', 'string', 'state'];
  var BOY_LEADER_FROM = { x: 146, y: 252 };
  var STRING_LEADER_FROM = { x: 190, y: 120 };

  // ------------------------------------------------------------------ small helpers
  function f(n) { return Math.round(n * 100) / 100; }
  function clamp01(n) { n = +n; return n > 1 ? 1 : n > 0 ? n : 0; }
  function smoothstep(a, b, x) {
    var t = clamp01((x - a) / (b - a));
    return t * t * (3 - 2 * t);
  }
  function rad(deg) { return deg * Math.PI / 180; }

  // a point on an arch head: side -1 = left (piston), +1 = right (pump); phi = degrees above the horizontal
  function archPoint(side, r, phi) {
    return { x: P.x + side * r * Math.cos(rad(phi)), y: P.y - r * Math.sin(rad(phi)) };
  }
  // rotate a point about the gudgeon by theta (rad, SVG sense: + lifts the left end)
  function rotP(pt, theta) {
    var c = Math.cos(theta), s = Math.sin(theta), x = pt.x - P.x, y = pt.y - P.y;
    return { x: P.x + x * c - y * s, y: P.y + x * s + y * c };
  }
  function circle(cx, cy, r) {
    return 'M' + f(cx + r) + ' ' + f(cy) + ' A' + r + ' ' + r + ' 0 1 1 ' + f(cx - r) + ' ' + f(cy) +
      ' A' + r + ' ' + r + ' 0 1 1 ' + f(cx + r) + ' ' + f(cy);
  }
  // beam top / bottom edge at x (the beam is two quadratic edges, thicker at the gudgeon)
  function beamTop(x) { var t = (x - 275) / 280; return 78 - 12 * t + 12 * t * t; }
  function beamBottom(x) { var t = (x - 275) / 280; return 90 + 12 * t - 12 * t * t; }

  // ------------------------------------------------------------------ the parts (static path data)

  // courses of brick or stone as ONE path: each course is run alternately left->right and right->left, its
  // joints are out-and-back teeth; `turn(prev, next, dir)` joins consecutive courses (hidden under an outline);
  // `via(dir)` (optional) detours around an opening, teeth are split before/after it at x = c.viaX
  function courses(list, turn) {
    var d = '', dir = 1;
    list.forEach(function (c, i) {
      var a = dir > 0 ? c.x0 : c.x1, b = dir > 0 ? c.x1 : c.x0;
      d += i === 0 ? 'M' + f(a) + ' ' + f(c.y) : turn(list[i - 1], c, dir);
      var teeth = [];
      (c.up || []).forEach(function (x) { teeth.push([x, c.upTo]); });
      (c.down || []).forEach(function (x) { teeth.push([x, c.downTo]); });
      teeth.sort(function (p, q) { return dir * (p[0] - q[0]); });
      var tooth = function (t) { d += ' H' + f(t[0]) + ' V' + f(t[1]) + ' V' + f(c.y); };
      if (c.via) {
        teeth.filter(function (t) { return dir * (t[0] - c.viaX) < 0; }).forEach(tooth);
        d += c.via(dir);
        teeth.filter(function (t) { return dir * (t[0] - c.viaX) > 0; }).forEach(tooth);
      } else {
        teeth.forEach(tooth);
      }
      d += ' H' + f(b);
      dir = -dir;
    });
    return d;
  }
  function joints(x0, x1, first, step, skip) {
    var out = [];
    for (var x = x0 + first; x < x1 - 3; x += step) if (!skip || !skip(x)) out.push(x);
    return out;
  }

  function groundPath() {
    var d = 'M16 ' + GROUND;
    for (var x = 30; x < 545; x += 26) d += ' H' + x + ' l-6 7 l6 -7';
    return d + ' H552 M578 ' + GROUND + ' H592';
  }
  function shaftPath() { return 'M552 ' + GROUND + ' V420 M578 ' + GROUND + ' V420'; }

  // the engine-house (bob) wall: battered masonry with a coping slab; the beam's bearing sits on it
  function wallL(y) { return 383 + (GROUND - y) * 6 / (GROUND - WALL_TOP); }
  function wallR(y) { return 447 - (GROUND - y) * 6 / (GROUND - WALL_TOP); }
  function wallPath() {
    return 'M383 ' + GROUND + ' L389 ' + WALL_TOP + ' H384 V100 H446 V' + WALL_TOP + ' H441 L447 ' + GROUND;
  }
  function wallStonesPath() {
    var list = [], prev = WALL_TOP, k = 0;
    for (var y = WALL_TOP + 16; y < GROUND - 6; y += 16, k++) {
      var mid = (prev + y) / 2;
      list.push({ y: y, x0: wallL(y), x1: wallR(y), up: joints(wallL(mid), wallR(mid), k % 2 ? 14 : 27, 26), upTo: prev });
      prev = y;
    }
    var last = list[list.length - 1], lm = (last.y + GROUND) / 2;
    last.down = joints(wallL(lm), wallR(lm), k % 2 ? 14 : 27, 26);
    last.downTo = GROUND;
    return courses(list, function (a, b, dir) {
      return ' L' + f(dir > 0 ? wallL(b.y) : wallR(b.y)) + ' ' + f(b.y);
    });
  }

  // furnace: a brick box with an arched fire door (centre 265,352, r 10)
  function furnacePath() {
    return 'M195 ' + GROUND + ' V' + FURN_TOP + ' H335 V' + GROUND +
      ' M255 ' + GROUND + ' V352 A10 10 0 0 1 275 352 V' + GROUND;
  }
  function furnaceBricksPath() {
    var nearDoor = function (x) { return x > 251 && x < 279; };
    var list = [
      { y: 344, x0: 195, x1: 335, up: joints(195, 335, 20, 20, nearDoor), upTo: FURN_TOP, viaX: 265,
        via: function (dir) { return dir > 0 ? ' H259 A10 10 0 0 1 271 344' : ' H271 A10 10 0 0 0 259 344'; } },
      { y: 354, x0: 195, x1: 335, up: joints(195, 335, 10, 20, nearDoor), upTo: 344,
        down: joints(195, 335, 20, 20, nearDoor), downTo: GROUND, viaX: 265,
        via: function (dir) {
          return dir > 0 ? ' H255 V352 A10 10 0 0 1 275 352 V354' : ' H275 V352 A10 10 0 0 0 255 352 V354';
        } }
    ];
    return courses(list, function (a, b, dir) { return ' L' + (dir > 0 ? 195 : 335) + ' ' + b.y; });
  }
  function boilerPath() {
    return 'M207 ' + FURN_TOP + ' C200 311 219 288 265 288 C311 288 330 311 323 ' + FURN_TOP;
  }
  function pipePath() {
    return 'M261 213 V221.5 H258 H272 H269 V213 M258 244.5 H261 V288.2 H269 V244.5 H272';
  }
  function cylinderPath() {
    return 'M243 134 H236 H239 V208 H233 V213 H297 V208 H291 V134 H294 H287 H291 V208 H239';
  }
  function floorPath() {
    return 'M297 208 H' + f(wallL(208)) + ' L' + f(wallL(213)) + ' 213 H297';
  }
  function beamPath() {
    return 'M275 78 Q415 72 555 78 L555 90 Q415 96 275 90 Z';
  }
  function beamStrapsPath() {
    return [300, 342, 488, 530].map(function (x) {
      return 'M' + x + ' ' + f(beamTop(x)) + ' V' + f(beamBottom(x));
    }).join(' ');
  }
  // arch head: a curved timber whose outer face (radius R) the chain wraps, braced to the beam; one subpath
  function archPath(side) {
    var o1 = archPoint(side, FACE, 20), o2 = archPoint(side, FACE, -10);
    var i1 = archPoint(side, ARCH_IN, 20), i2 = archPoint(side, ARCH_IN, -10);
    var up = side < 0 ? 1 : 0; // arc sweep from the lower end up to the upper end
    return 'M' + f(i2.x) + ' ' + f(i2.y) + ' L' + f(o2.x) + ' ' + f(o2.y) +
      ' A' + FACE + ' ' + FACE + ' 0 0 ' + up + ' ' + f(o1.x) + ' ' + f(o1.y) + ' L' + f(i1.x) + ' ' + f(i1.y) +
      ' A' + ARCH_IN + ' ' + ARCH_IN + ' 0 0 ' + (1 - up) + ' ' + f(i2.x) + ' ' + f(i2.y);
  }
  function archBracesPath(side) {
    var i1 = archPoint(side, ARCH_IN, 20), i2 = archPoint(side, ARCH_IN, -10);
    var top = side < 0 ? 335 : 495, bot = side < 0 ? 312 : 518;
    return 'M' + top + ' ' + f(beamTop(top)) + ' L' + f(i1.x) + ' ' + f(i1.y) +
      ' M' + bot + ' ' + f(beamBottom(bot)) + ' L' + f(i2.x) + ' ' + f(i2.y);
  }

  // a hanging chain from y0 toward y1, links alternately face-on (oval) and edge-on (bar), as ONE subpath:
  // out along the left halves and the bars, back along the right halves (the bars are retraced)
  function chainPath(x, y0, y1) {
    var dir = y1 < y0 ? -1 : 1, pitch = 7, ry = 4.3, rx = 2.3, cs = [];
    for (var c = y0 + dir * (ry - 1); dir < 0 ? c + ry > y1 : c - ry < y1; c += dir * pitch) cs.push(c);
    var sOut = dir < 0 ? 1 : 0, sBack = sOut; // both passes turn the same way: out via the left, back via the right
    var d = 'M' + x + ' ' + f(cs[0] - dir * ry);
    cs.forEach(function (c, k) {
      if (k % 2 === 0) d += ' L' + x + ' ' + f(c - dir * ry) + ' A' + rx + ' ' + ry + ' 0 0 ' + sOut + ' ' + x + ' ' + f(c + dir * ry);
      else d += ' L' + x + ' ' + f(c + dir * ry);
    });
    for (var k = cs.length - 1; k >= 0; k--) {
      var c2 = cs[k];
      if (k % 2 === 0) d += ' L' + x + ' ' + f(c2 + dir * ry) + ' A' + rx + ' ' + ry + ' 0 0 ' + sBack + ' ' + x + ' ' + f(c2 - dir * ry);
      else d += ' L' + x + ' ' + f(c2 - dir * ry);
    }
    return d;
  }

  // x of a quadratic Bezier where it crosses height y (null if it does not)
  function quadXAtY(p0, c, p1, y) {
    var a = p0.y - 2 * c.y + p1.y, b = 2 * (c.y - p0.y), k = p0.y - y, ts = [];
    if (Math.abs(a) < 1e-6) { if (Math.abs(b) > 1e-9) ts.push(-k / b); }
    else {
      var disc = b * b - 4 * a * k;
      if (disc >= 0) { var r = Math.sqrt(disc); ts.push((-b + r) / (2 * a), (-b - r) / (2 * a)); }
    }
    for (var i = 0; i < ts.length; i++) {
      var t = ts[i];
      if (t >= 0 && t <= 1) return (1 - t) * (1 - t) * p0.x + 2 * t * (1 - t) * c.x + t * t * p1.x;
    }
    return null;
  }

  // ------------------------------------------------------------------ DOM / loop helpers
  function svgEl(tag, attrs, parent) {
    var el = document.createElementNS(NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(el);
    return el;
  }

  function addTick(fn) {
    var g = window.gsap;
    if (g && g.ticker && typeof g.ticker.add === 'function') {
      var wrapped = function (time, deltaMs) { fn(deltaMs / 1000); };
      g.ticker.add(wrapped);
      return function () { g.ticker.remove(wrapped); };
    }
    var raf = 0, last = 0, alive = true;
    var loop = function (now) {
      if (!alive) return;
      var dt = last ? (now - last) / 1000 : 0;
      last = now;
      fn(dt);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return function () { alive = false; cancelAnimationFrame(raf); };
  }

  var uid = 0;

  // ------------------------------------------------------------------ mount
  function mount(target) {
    var fig = typeof target === 'string' ? document.querySelector(target) : target;
    if (!fig || !fig.appendChild) throw new Error('Engine.mount: figure element not found');
    if (fig.__engine && typeof fig.__engine.destroy === 'function') fig.__engine.destroy();

    var id = 'eng' + (++uid);
    try {
      if (window.getComputedStyle && getComputedStyle(fig).position === 'static') fig.style.position = 'relative';
    } catch (e) { /* ignore */ }

    var svg = svgEl('svg', {
      viewBox: '0 0 600 420', width: '100%', 'class': 'engine-svg', focusable: 'false', 'aria-hidden': 'true',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round'
    });
    var defs = svgEl('defs', null, svg);
    // the chains pay out over the arch heads: hide what is above the gudgeon line (and below the sheet's edge)
    var clip = svgEl('clipPath', { id: id + '-below-beam' }, defs);
    svgEl('rect', { x: 0, y: P.y, width: 600, height: 420 - P.y }, clip);

    var byPart = {};
    function part(name, d, cls, width, parent) {
      var el = svgEl('path', {
        d: d, 'class': cls, 'stroke-width': width, pathLength: 1,
        'stroke-dasharray': '1 2', 'stroke-dashoffset': 0, 'data-part': name
      }, parent);
      byPart[name] = el;
      return el;
    }

    // DOM order = stacking (textures under their outlines); drawing order is the drawables list further down
    var gStatic = svgEl('g', { 'class': 'engine-static' }, svg);
    part('ground', groundPath(), 'ink-soft', SOFT, gStatic);
    part('shaft', shaftPath(), 'ink-soft', SOFT, gStatic);
    part('wall-stones', wallStonesPath(), 'ink-soft', SOFT, gStatic);
    part('wall', wallPath(), 'ink', MAIN, gStatic);
    part('bearing', 'M402 100 L405 96 H425 L428 100', 'ink', MAIN, gStatic);
    part('furnace-bricks', furnaceBricksPath(), 'ink-soft', SOFT, gStatic);
    part('furnace', furnacePath(), 'ink', MAIN, gStatic);
    part('boiler', boilerPath(), 'ink', MAIN, gStatic);
    part('pipe', pipePath(), 'ink', MAIN, gStatic);
    part('valve', circle(V.x, V.y, 11), 'ink', MAIN, gStatic);
    part('cylinder-inner', 'M243 134 V208 M287 134 V208', 'ink-soft', SOFT, gStatic);
    part('cylinder', cylinderPath(), 'ink', MAIN, gStatic);
    part('floor', floorPath(), 'ink', MAIN, gStatic);

    // piston, rod and chain move together; the chain is clipped where it meets the arch head
    var gPistonClip = svgEl('g', { 'clip-path': 'url(#' + id + '-below-beam)' }, svg);
    var gPiston = svgEl('g', { 'class': 'engine-piston' }, gPistonClip);
    svgEl('rect', { x: 244, y: 166, width: 42, height: 10, 'class': 'paper-fill' }, gPiston);
    part('piston', 'M244 166 H286 V176 H244 Z', 'ink', MAIN, gPiston);
    part('piston-rod', 'M265 166 V126 H261 H269', 'ink', MAIN, gPiston);
    part('piston-chain', chainPath(CX, 126, P.y - 24), 'ink', CHAIN_W, gPiston);

    // pump chain and rod, down the shaft (clipped at the gudgeon line and at the bottom edge)
    var gPumpClip = svgEl('g', { 'clip-path': 'url(#' + id + '-below-beam)' }, svg);
    var gPump = svgEl('g', { 'class': 'engine-pump' }, gPumpClip);
    part('pump-chain', chainPath(PX, 130, P.y - 24), 'ink', CHAIN_W, gPump);
    part('pump', 'M558 130 H572 H569 V445 M561 130 V445', 'ink', MAIN, gPump);

    // the great beam with its arch heads
    var gBeam = svgEl('g', { 'class': 'engine-beam' }, svg);
    part('arch-braces', archBracesPath(-1) + ' ' + archBracesPath(1), 'ink', BRACE, gBeam);
    part('beam', beamPath(), 'ink', MAIN, gBeam);
    part('beam-straps', beamStrapsPath(), 'ink-soft', SOFT, gBeam);
    part('arch-left', archPath(-1), 'ink', MAIN, gBeam);
    part('arch-right', archPath(1), 'ink', MAIN, gBeam);
    part('pivot', circle(P.x, P.y, 3.2), 'ink', MAIN, svg);

    // the valve plate (turns 90 deg) and the hand lever, both on the valve spindle
    var gPlate = svgEl('g', { 'class': 'engine-plate' }, svg);
    part('valve-plate', 'M257 ' + V.y + ' H273 ' + circle(V.x, V.y, 1.5), 'ink', 2.2, gPlate);
    var gLever = svgEl('g', { 'class': 'engine-lever' }, svg);
    var tipX = V.x - LEVER_LEN;
    part('lever', 'M253 ' + V.y + ' H' + f(tipX + 2.6) + ' A2.6 2.6 0 1 0 ' + f(tipX - 2.6) + ' ' + V.y +
      ' A2.6 2.6 0 1 0 ' + f(tipX + 2.6) + ' ' + V.y, 'ink', MAIN, gLever);

    // the string (not a drawable: its draw-on is state.string)
    var stringEl = svgEl('path', {
      d: 'M0 0', 'class': 'ink engine-string', 'stroke-width': STRING_W, pathLength: 1,
      'stroke-dasharray': '1 2', 'stroke-dashoffset': 1, 'data-part': 'string'
    }, svg);

    // the boy's hand: an ink dot on the lever tip
    var hand = svgEl('g', { 'class': 'engine-hand' }, svg);
    svgEl('circle', { cx: 0, cy: 0, r: 5.5, 'class': 'ink-fill' }, hand);

    // leader lines and labels
    var gLeaders = svgEl('g', { 'class': 'engine-leaders' }, svg);
    fig.appendChild(svg);

    var notes = {};
    var stateSpan = null, boyLeader = null, stringLeader = null;
    NOTE_ORDER.forEach(function (name) {
      var s = NOTE_SPECS[name];
      var div = document.createElement('div');
      div.className = 'engine-note' + (s.state ? ' is-state' : '');
      div.setAttribute('data-note', name);
      div.setAttribute('data-align', s.align);
      div.style.left = f(s.x / 6) + '%';
      div.style.top = f(s.y / 4.2) + '%';
      var span = document.createElement('span');
      span.textContent = s.text;
      div.appendChild(span);
      fig.appendChild(div);
      var els = [div];
      if (s.leader) {
        var ld = svgEl('path', {
          d: s.leader === 'track' ? '' : s.leader, 'class': 'ink engine-leader', 'stroke-width': LEADER_W,
          'data-note': name
        }, gLeaders);
        els.push(ld);
        if (name === 'boy') boyLeader = ld;
        if (name === 'string') stringLeader = ld;
      }
      if (s.state) stateSpan = span;
      notes[name] = els;
    });

    // drawing order: ground and house first, moving parts last
    var drawables = ['ground', 'shaft', 'wall', 'wall-stones', 'furnace', 'furnace-bricks', 'boiler',
      'pipe', 'valve', 'cylinder', 'cylinder-inner', 'floor', 'bearing', 'beam', 'beam-straps', 'pivot', 'arch-left',
      'arch-right', 'arch-braces', 'piston', 'piston-rod', 'piston-chain', 'pump-chain', 'pump', 'valve-plate',
      'lever'
    ].map(function (n) { return byPart[n]; });

    // ---------------------------------------------------------------- kinematics
    function leverTip(v) {
      var a = rad(LEVER_SHUT + (LEVER_OPEN - LEVER_SHUT) * v);
      return { x: V.x - LEVER_LEN * Math.cos(a), y: V.y - LEVER_LEN * Math.sin(a) };
    }
    // valve opening 0..1: open while the piston rises (cos > 0), eased in as the engine starts
    function valveAt(psi, run) {
      return smoothstep(-SWITCH_K, SWITCH_K, Math.cos(psi)) * smoothstep(0.04, 0.3, run);
    }
    // the string's length: the farthest apart the tie and the tip get over a full stroke
    var dMax = 0, dMin = Infinity;
    for (var i = 0; i < 720; i++) {
      var psi = i / 720 * 2 * Math.PI;
      var b = rotP(TIE, AMP * Math.sin(psi)), t = leverTip(valveAt(psi, 1));
      var dist = Math.hypot(b.x - t.x, b.y - t.y);
      if (dist > dMax) dMax = dist;
      if (dist < dMin) dMin = dist;
    }
    var slackRange = Math.max(1, dMax - dMin);

    // ---------------------------------------------------------------- state, loop, render
    var S = { run: 0, string: 0 };
    var time = 0;          // engine time (s): advances only while started, slower while run is low
    var running = false;
    var removeTick = null;
    var pending = 0;
    var valveOpen = false;
    var destroyed = false;

    function requestRender() {
      if (pending || destroyed) return;
      var raf = window.requestAnimationFrame || function (cb) { return setTimeout(cb, 16); };
      pending = raf(function () { pending = 0; if (!running) render(); });
    }

    var state = {};
    ['run', 'string'].forEach(function (k) {
      Object.defineProperty(state, k, {
        enumerable: true,
        get: function () { return S[k]; },
        set: function (v) { S[k] = +v || 0; if (!running) requestRender(); }
      });
    });

    function render() {
      if (destroyed) return;
      var run = clamp01(S.run), str = clamp01(S.string);
      var psi = OMEGA * time;
      var theta = AMP * run * Math.sin(psi);
      var lift = R * theta; // piston up by lift, pump rod down by lift

      gBeam.setAttribute('transform', 'rotate(' + f(theta * 180 / Math.PI) + ' ' + P.x + ' ' + P.y + ')');
      gPiston.setAttribute('transform', 'translate(0 ' + f(-lift) + ')');
      gPump.setAttribute('transform', 'translate(0 ' + f(lift) + ')');

      var v = valveAt(psi, run);
      var a = LEVER_SHUT + (LEVER_OPEN - LEVER_SHUT) * v;
      gLever.setAttribute('transform', 'rotate(' + f(a) + ' ' + V.x + ' ' + V.y + ')');
      gPlate.setAttribute('transform', 'rotate(' + f(90 * v) + ' ' + V.x + ' ' + V.y + ')');

      var tip = leverTip(v);
      hand.setAttribute('transform', 'translate(' + f(tip.x) + ' ' + f(tip.y) + ')');
      if (boyLeader) {
        var hx = tip.x - BOY_LEADER_FROM.x, hy = tip.y - BOY_LEADER_FROM.y, hl = Math.hypot(hx, hy) || 1;
        var len = hl - 8; // end just short of the dot; no stub when the hand is right above the label
        boyLeader.setAttribute('d', len < 6 ? '' : 'M' + BOY_LEADER_FROM.x + ' ' + BOY_LEADER_FROM.y + ' L' +
          f(BOY_LEADER_FROM.x + hx * len / hl) + ' ' + f(BOY_LEADER_FROM.y + hy * len / hl));
      }

      // the string: tip -> tie on the beam, a quadratic curve that sags (downward side) when slack
      var b = rotP(TIE, theta);
      var dx = b.x - tip.x, dy = b.y - tip.y, d = Math.hypot(dx, dy) || 1;
      var slack = clamp01((dMax - d) / slackRange);
      var h = SAG_MIN + (SAG_MAX - SAG_MIN) * slack;
      var nx = -dy / d, ny = dx / d;
      if (ny < 0) { nx = -nx; ny = -ny; }
      var c = { x: (tip.x + b.x) / 2 + nx * 2 * h, y: (tip.y + b.y) / 2 + ny * 2 * h };
      stringEl.setAttribute('d', 'M' + f(tip.x) + ' ' + f(tip.y) + ' Q' + f(c.x) + ' ' + f(c.y) + ' ' + f(b.x) + ' ' + f(b.y));
      stringEl.setAttribute('stroke-dashoffset', f(1 - str));
      stringEl.style.visibility = str > 0 ? '' : 'hidden';
      if (stringLeader) {
        var sx = quadXAtY(tip, c, b, STRING_LEADER_FROM.y);
        var end = sx == null ? STRING_LEADER_FROM.x + 8 : Math.max(STRING_LEADER_FROM.x + 4, sx - 4);
        stringLeader.setAttribute('d', 'M' + STRING_LEADER_FROM.x + ' ' + STRING_LEADER_FROM.y + ' H' + f(end));
      }

      var open = v > 0.5;
      if (open !== valveOpen && stateSpan) stateSpan.textContent = open ? 'open' : 'shut';
      valveOpen = open;
      api.valveOpen = open;
    }

    function tick(dt) {
      if (!(dt > 0)) dt = 0;
      if (dt > 0.1) dt = 0.1; // after a stall, do not jump
      var run = clamp01(S.run);
      if (run > 0.0001) time += dt * (0.35 + 0.65 * run); // the engine winds up as run grows
      render();
    }

    function start() {
      if (running || destroyed) return api;
      running = true;
      removeTick = addTick(tick);
      return api;
    }
    function stop() {
      if (!running) return api;
      running = false;
      if (removeTick) removeTick();
      removeTick = null;
      return api;
    }
    function destroy() {
      stop();
      destroyed = true;
      if (svg.parentNode) svg.parentNode.removeChild(svg);
      NOTE_ORDER.forEach(function (n) {
        var div = notes[n] && notes[n][0];
        if (div && div.parentNode) div.parentNode.removeChild(div);
      });
      if (fig.__engine === api) fig.__engine = null;
    }

    var api = {
      state: state,
      start: start,
      stop: stop,
      render: render,
      drawables: drawables,
      notes: notes,
      hand: hand,
      valveOpen: false,
      destroy: destroy,
      svg: svg,
      figure: fig
    };
    // debug / scrubbing: engine time in seconds (setting it renders a frame when the loop is off)
    Object.defineProperty(api, 'time', {
      enumerable: true,
      get: function () { return time; },
      set: function (t) { time = +t || 0; if (!running) render(); }
    });
    Object.defineProperty(api, 'running', { enumerable: true, get: function () { return running; } });

    fig.__engine = api;
    render();
    return api;
  }

  window.Engine = { mount: mount, period: PERIOD };
})();
