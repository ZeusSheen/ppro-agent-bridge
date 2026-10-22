/*
 * effects: component chains (read for inspect_items, write for set_params / set_scale),
 * param value conversion and the keyframe time-base conversion (media / clip / sequence -> media ticks).
 * Keyframe positions are media time; keyframes are read with getKeyframeListAsTickTimes only.
 * Factory module.   deps: { ppro, core }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const BridgeError = core.BridgeError;
  const secs = core.secs;
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;
  const TPS = core.TICKS_PER_SECOND;

  // Keyframe times are converted in integer ticks; 9 h of media is the limit (ticks stay exact
  // integers far below 2^53), and no input may exceed ~9.7 h before the conversion.
  const MAX_MEDIA_SECONDS = 32400;
  const MAX_INPUT_SECONDS = 35000;
  const TIME_BASES = ["media", "clip", "sequence"];

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const isPointArray = (v) => Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === "number");
  const isPointObject = (v) => v && typeof v === "object" && typeof v.x === "number" && typeof v.y === "number";
  const isColorObject = (v) => v && typeof v === "object" && typeof v.red === "number";

  /* Validate a requested param value (no Premiere objects yet): {value: v} is unwrapped, [x, y] / {x, y}
   * become a point; numbers, strings, booleans and colour objects pass. Anything else -> BAD_PARAMS. */
  function normalizeValue(v, where) {
    if (v && typeof v === "object" && !Array.isArray(v) && !isPointObject(v) && !isColorObject(v)
        && core.hasOwn(v, "value")) {
      return normalizeValue(v.value, where);
    }
    if (isPointArray(v)) return { point: [v[0], v[1]] };
    if (isPointObject(v)) return { point: [v.x, v.y] };
    if (isColorObject(v)) return { plain: v };
    if (typeof v === "number" || typeof v === "string" || typeof v === "boolean") return { plain: v };
    throw new BridgeError("BAD_PARAMS", `${where || "value"}: unsupported value ${JSON.stringify(v)} ` +
      "(number, string, boolean, [x, y], {x, y} or a colour)");
  }

  /* Inverse of plainValue, from a normalizeValue result: a point becomes a PointF. */
  function toParamValue(n) {
    return n.point ? new ppro.PointF(n.point[0], n.point[1]) : n.plain;
  }

  /* JSON-safe copy of a param value (numbers, strings, bools, PointF, Color). */
  function plainValue(v) {
    if (v === null || v === undefined) return null;
    if (typeof v !== "object") return v;
    if (typeof v.x === "number" && typeof v.y === "number") return { x: v.x, y: v.y };
    if (typeof v.red === "number") return { red: v.red, green: v.green, blue: v.blue, alpha: v.alpha };
    if (v.value !== undefined) return plainValue(v.value);
    try { return JSON.parse(JSON.stringify(v)); } catch (e) { return String(v); }
  }

  /* Match names of a chain's components, in chain order. */
  async function chainMatchNames(chain) {
    const out = [];
    const n = tryGetSync(() => chain.getComponentCount(), 0) || 0;
    for (let i = 0; i < n; i++) {
      const comp = chain.getComponentAtIndex(i);
      out.push(comp ? await tryGet(() => comp.getMatchName()) : null);
    }
    return out;
  }

  /* Chain indexes of the instances of one match name, in chain order (occurrence n = [n - 1]). */
  function indexesOf(names, matchName) {
    const out = [];
    for (let i = 0; i < names.length; i++) if (names[i] === matchName) out.push(i);
    return out;
  }

  /* The clip's effect chain: components (with occurrence) -> params -> value / keyframes (media time). */
  async function describeChain(trackItem, maxKf) {
    const chain = await tryGet(() => trackItem.getComponentChain());
    if (!chain) return null;
    const comps = [];
    const seen = {};
    const n = tryGetSync(() => chain.getComponentCount(), 0);
    for (let i = 0; i < n; i++) {
      const comp = chain.getComponentAtIndex(i);
      const matchName = await tryGet(() => comp.getMatchName());
      const key = String(matchName);
      seen[key] = (seen[key] || 0) + 1;
      const c = {
        index: i,
        matchName,
        occurrence: seen[key],
        displayName: await tryGet(() => comp.getDisplayName()),
        params: [],
      };
      const pc = tryGetSync(() => comp.getParamCount(), 0);
      for (let k = 0; k < pc; k++) {
        const param = comp.getParam(k);
        if (!param) continue;
        const keyed = !!tryGetSync(() => param.isTimeVarying(), false);
        const startKf = await tryGet(() => param.getStartValue());
        const pr = {
          index: k,
          name: param.displayName,
          value: plainValue(startKf ? startKf.value : await tryGet(() => param.getValueAtTime(ppro.TickTime.createWithSeconds(0)))),
          keyframed: keyed,
        };
        if (keyed) {
          pr.timeBase = "media";
          const times = tryGetSync(() => param.getKeyframeListAsTickTimes(), []) || [];
          pr.keyframes = [];
          for (const t of times.slice(0, maxKf)) {
            pr.keyframes.push({ t: secs(t), value: plainValue(await tryGet(() => param.getValueAtTime(t))) });
          }
          if (times.length > maxKf) pr.keyframesTruncated = times.length;
        }
        c.params.push(pr);
      }
      comps.push(c);
    }
    return comps;
  }

  /* Motion's Scale param. Display names are localized, so match the component by
   * match name and the param by known names, falling back to index 1 (Position, Scale, ...). */
  const SCALE_NAMES = ["Scale", "缩放", "縮放", "スケール", "Skalierung", "Échelle", "Escala", "Scala", "Масштаб", "비율 조정"];
  async function findMotionScale(trackItem) {
    const chain = await tryGet(() => trackItem.getComponentChain());
    if (!chain) return null;
    const n = tryGetSync(() => chain.getComponentCount(), 0);
    for (let i = 0; i < n; i++) {
      const comp = chain.getComponentAtIndex(i);
      if ((await tryGet(() => comp.getMatchName())) !== "AE.ADBE Motion") continue;
      const pc = tryGetSync(() => comp.getParamCount(), 0);
      for (let p = 0; p < pc; p++) {
        const param = comp.getParam(p);
        if (param && SCALE_NAMES.includes(param.displayName)) return { comp, param };
      }
      const fallback = pc > 1 ? comp.getParam(1) : null;
      return fallback ? { comp, param: fallback } : null;
    }
    return null;
  }

  const toTicks = (s) => Math.round(s * TPS);

  function speedIsOne(speed) {
    const n = typeof speed === "string" ? Number(speed) : speed;
    return isNum(n) && Math.abs(n - 1) < 1e-9;
  }

  /*
   * One keyframe time -> media ticks.
   *   media: t;  clip: inPoint + t;  sequence: inPoint + (t - start)   (all in integer ticks)
   * clip / sequence need speed 1 (UNSUPPORTED otherwise); media time beyond 9 h -> BAD_PARAMS;
   * outside [inPoint - half a sequence frame, outPoint + half a frame] -> KEYFRAME_OUT_OF_RANGE.
   * o: { timeBase, t, start, inPoint, outPoint, speed, frameTicks }
   */
  function convertKeyframeTime(o) {
    const where = o.where ? `${o.where}: ` : "";
    if (TIME_BASES.indexOf(o.timeBase) < 0) {
      throw new BridgeError("BAD_PARAMS", `${where}timeBase must be one of media, clip, sequence`);
    }
    if (!isNum(o.t)) throw new BridgeError("BAD_PARAMS", `${where}keyframe t must be a number`);
    if (!isNum(o.inPoint) || !isNum(o.outPoint) || (o.timeBase === "sequence" && !isNum(o.start))) {
      throw new BridgeError("INTERNAL", `${where}could not read the clip's start / in / out point`);
    }
    if (o.timeBase !== "media" && !speedIsOne(o.speed)) {
      throw new BridgeError("UNSUPPORTED", `${where}timeBase "${o.timeBase}" needs a clip at speed 1 (speed ${o.speed}); use timeBase "media"`);
    }
    for (const v of [o.t, o.inPoint, o.timeBase === "sequence" ? o.start : 0]) {
      if (Math.abs(v) > MAX_INPUT_SECONDS) throw new BridgeError("BAD_PARAMS", `${where}time ${v}s is beyond the 9 h limit`);
    }
    const inT = toTicks(o.inPoint);
    const outT = toTicks(o.outPoint);
    const tT = toTicks(o.t);
    let media;
    if (o.timeBase === "media") media = tT;
    else if (o.timeBase === "clip") media = inT + tT;
    else media = inT + (tT - toTicks(o.start));
    if (media > MAX_MEDIA_SECONDS * TPS) {
      throw new BridgeError("BAD_PARAMS", `${where}keyframe at media ${media / TPS}s is beyond the 9 h limit`);
    }
    const half = isNum(o.frameTicks) && o.frameTicks > 0 ? Math.round(o.frameTicks / 2) : 0;
    if (media < inT - half || media > outT + half) {
      throw new BridgeError("KEYFRAME_OUT_OF_RANGE", `${where}keyframe ${o.timeBase} ${o.t}s is media ${media / TPS}s, ` +
        `outside the clip's [${o.inPoint}, ${o.outPoint}]s`);
    }
    return { mediaTicks: media, mediaSeconds: media / TPS };
  }

  const tickTimeFromTicks = core.tickTimeFromTicks;

  return {
    MAX_MEDIA_SECONDS, TIME_BASES, SCALE_NAMES,
    normalizeValue, toParamValue, plainValue, chainMatchNames, indexesOf, describeChain, findMotionScale,
    speedIsOne, convertKeyframeTime, tickTimeFromTicks,
  };
};
