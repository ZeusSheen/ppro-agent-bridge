/*
 * hostapi: load Premiere's UXP API (the "premierepro" module) and say how that went.
 *
 * Why: on Premiere Pro (Beta) 27.1 builds 46 and 48 (14 and 16 Nov 2026) require("premierepro") throws
 *   TypeError: Cannot read properties of undefined (reading 'prototype')
 * inside UXP's own middleware (uxp://uxp-internal/middleware/index.js, under _loadCoreModule), for every plugin: a
 * test plugin with no permissions and one line of code fails the same way. The host's class table lists
 * CaptionDataClipTrackItem with merge: ["TrackItem"], a class that does not exist, and that one entry aborts the
 * whole module. A panel that dies there shows nothing but its static page.
 *
 * The middleware walks the class table with Map.prototype.forEach in the plugin's own realm. So when the plain
 * load fails, the load runs once more while Map.prototype.forEach skips, and records, each entry whose callback
 * throws; forEach is put back before this returns, whatever happens. Live on build 48: one entry skipped
 * (CaptionDataClipTrackItem), 77 exports, project / sequence / tracks / clips read as on build 41; only the items
 * of a caption track cannot be read (the class that failed; the bridge reads the caption track count only).
 *
 * loadHostApi() -> {api, skipped, error}
 *   api      the module; null when it does not load even so, or loads without a class the bridge needs (NEEDED)
 *   skipped  [{name, error}]: the entries the second try left out ([] after a plain load)
 *   error    null after a plain load, else what the plain load threw (and why `api` is null, when it is)
 */
module.exports = function (deps) {
  const load = deps.load;  // loads the "premierepro" module: main.js passes it, a lib module requires nothing
  const NEEDED = ["Project", "Constants", "SequenceEditor", "TickTime"];
  const MAX_SKIPPED = 20;  // more than a few skipped classes is not a host this panel should drive
  const text = (e) => String((e && e.message) || e).slice(0, 300);

  function missing(api) {
    return NEEDED.filter((name) => !api || api[name] === undefined || api[name] === null);
  }

  function loadHostApi() {
    let first;
    try {
      const api = load();
      const gone = missing(api);
      if (!gone.length) return { api, skipped: [], error: null };
      return { api: null, skipped: [], error: `the module loaded without ${gone.join(", ")}` };
    } catch (e) {
      first = text(e);
    }
    const skipped = [];
    let tooMany = false;
    const proto = Map.prototype;
    const original = proto.forEach;
    proto.forEach = function forEachSkippingFailures(callback, thisArg) {
      return original.call(this, function guarded(value, key, map) {
        try {
          return callback.call(thisArg, value, key, map);
        } catch (e) {
          if (skipped.length < MAX_SKIPPED) skipped.push({ name: String(key).slice(0, 80), error: text(e) });
          else tooMany = true;
          return undefined;
        }
      });
    };
    let api = null;
    let second = null;
    try {
      api = load();
    } catch (e) {
      second = text(e);
    } finally {
      proto.forEach = original;
    }
    if (second !== null) return { api: null, skipped, error: second === first ? first : `${first}; then ${second}` };
    if (tooMany) return { api: null, skipped, error: `${first}; more than ${MAX_SKIPPED} classes fail to load` };
    const gone = missing(api);
    if (gone.length) return { api: null, skipped, error: `${first}; it loads only without ${gone.join(", ")}` };
    return { api, skipped, error: first };
  }

  return { loadHostApi, NEEDED };
};
