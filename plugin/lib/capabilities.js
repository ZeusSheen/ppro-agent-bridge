/*
 * capabilities: what this Premiere build can do, which host it is, whether it is denied, and the
 * panel's code hash.
 *  - static flags: probed once at load with typeof on ppro.*
 *  - instance flags: probed lazily on real instances (SequenceEditor of the active sequence, its first
 *    track / clip / component, a marker, the root bin, a project item), with typeof only, inside
 *    lockedAccess; cached per project + sequence name; never while stuck or denied; re-probed on
 *    ping {probe: true}. No getters are swept and no Action is created while probing.
 *  - host table: {denied: [{match: "27.1.*", builds?: "a-b", reason}], quirks: {}}, evaluated at load
 *    before connecting; empty in the release, overridable via globalThis.__agentBridgeHostTable.
 *  - codeHash: FNV-1a 32 over index.html, main.js, manifest.json and lib/*.js of the plugin folder.
 * Flag names, order and probe kinds mirror ppro_bridge/actions.json "capabilities".
 * Factory module.   deps: { ppro, uxp, core, timeline, hostTable?, log? }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const uxp = deps.uxp;
  const core = deps.core;
  const timeline = deps.timeline;
  const log = deps.log || function () {};
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;

  const host = (uxp && uxp.host) || null;
  const DEFAULT_HOST_TABLE = { denied: [], quirks: {} };
  const hostTable = core.isPlainObject(deps.hostTable) ? deps.hostTable : DEFAULT_HOST_TABLE;

  const FLAGS = [
    ["razor", "instance"],
    ["razorAllTracks", "instance"],
    ["transcribe", "static"],
    ["transcriptLanguages", "static"],
    ["mogrtInsert", "instance"],
    ["mogrtText", "static"],
    ["sequenceGuid", "instance"],
    ["markerGuid", "instance"],
    ["projectItemId", "instance"],
    ["subclip", "instance"],
    ["sequenceClone", "instance"],
    ["cloneTrackItem", "instance"],
    ["trackItemTrim", "instance"],
    ["trackItemDisable", "instance"],
    ["trackMute", "instance"],
    ["removeProjectItem", "instance"],
    ["workArea", "static"],
    ["timecode", "static"],
    ["componentInstanceName", "instance"],
  ];

  const defined = (v) => v !== undefined && v !== null;
  /* true / false for a method on a real instance; "unknown" when there is no instance to look at. */
  function method(obj, name) {
    if (!defined(obj)) return "unknown";
    try { return typeof obj[name] === "function"; } catch (e) { return "unknown"; }
  }
  function methods(obj, names) {
    if (!defined(obj)) return "unknown";
    for (const n of names) if (method(obj, n) !== true) return false;
    return true;
  }
  /* One named property (never a sweep): present when `in` says so or it reads as defined. */
  function property(obj, name) {
    if (!defined(obj)) return "unknown";
    try { return (name in obj) || obj[name] !== undefined; } catch (e) { return "unknown"; }
  }

  const STATIC = {
    transcribe: () => core.isFn(ppro.Transcript, "transcribeClipProjectItem"),
    transcriptLanguages: () => core.isFn(ppro.Transcript, "querySupportedLanguages"),
    mogrtText: () => defined(ppro.MogrtText),
    workArea: () => defined(ppro.WorkAreaUtils),
    timecode: () => core.isFn(ppro.TickTime, "timeToTimecode"),
  };

  const INSTANCE = {
    razor: (i) => method(i.editor, "createRazorTrackAction"),
    razorAllTracks: (i) => method(i.editor, "createRazorAllTracksAction"),
    mogrtInsert: (i) => method(i.editor, "insertMogrtFromPath"),
    sequenceGuid: (i) => property(i.sequence, "guid"),
    markerGuid: (i) => property(i.marker, "guid"),
    projectItemId: (i) => method(i.projectItem, "getId"),
    subclip: (i) => method(i.clip, "createSubClipAction"),
    sequenceClone: (i) => method(i.sequence, "createCloneAction"),
    cloneTrackItem: (i) => method(i.editor, "createCloneTrackItemAction"),
    trackItemTrim: (i) => methods(i.trackItem, ["createSetInPointAction", "createSetOutPointAction",
      "createSetStartAction", "createSetEndAction", "createMoveAction"]),
    trackItemDisable: (i) => method(i.trackItem, "createSetDisabledAction"),
    trackMute: (i) => method(i.videoTrack, "setMute"),
    removeProjectItem: (i) => method(i.root, "createRemoveItemAction"),
    componentInstanceName: (i) => method(i.component, "getInstanceName"),
  };

  const statics = {};
  for (const [name, kind] of FLAGS) {
    if (kind !== "static") continue;
    try { statics[name] = !!STATIC[name](); } catch (e) { statics[name] = "unknown"; }
  }

  /* ---------- host ---------- */

  const hostString = () => (host ? `${host.name} ${host.version}` : "unknown");

  /* name, version, uiLocale (+ buildNumber where the host has one). Never the application path. */
  function hostInfo() {
    const info = { name: null, version: null, uiLocale: null };
    if (!host) return info;
    for (const k of ["name", "version", "uiLocale"]) {
      const v = tryGetSync(() => host[k]);
      info[k] = defined(v) ? v : null;
    }
    if ("buildNumber" in host) {
      const b = tryGetSync(() => host.buildNumber);
      info.buildNumber = defined(b) ? b : null;
    }
    return info;
  }

  function globToRegExp(glob) {
    let re = "^";
    for (const ch of String(glob)) {
      if (ch === "*") re += ".*";
      else if (ch === "?") re += ".";
      else re += ch.replace(/[\\^$.+()[\]{}|]/g, "\\$&");
    }
    return new RegExp(re + "$");
  }

  function numericBuild() {
    if (!host || !("buildNumber" in host)) return null;
    const raw = tryGetSync(() => host.buildNumber);
    if (!defined(raw) || String(raw).trim() === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  /* "a-b" (inclusive) or "n"; needs a numeric build number (unknown -> no match). */
  function buildsMatch(builds, n) {
    if (n === null) return false;
    const m = /^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$/.exec(String(builds));
    if (!m) return false;
    const lo = Number(m[1]);
    const hi = m[2] !== undefined ? Number(m[2]) : lo;
    return n >= lo && n <= hi;
  }

  function evaluateHost(table) {
    const list = table && Array.isArray(table.denied) ? table.denied : [];
    const version = host ? String(tryGetSync(() => host.version, "")) : "";
    const build = numericBuild();
    for (const d of list) {
      if (!d || typeof d.match !== "string") continue;
      if (!globToRegExp(d.match).test(version)) continue;
      if (defined(d.builds) && !buildsMatch(d.builds, build)) continue;
      const out = { match: d.match };
      if (defined(d.builds)) out.builds = String(d.builds);
      out.reason = d.reason ? String(d.reason) : "This Premiere build is on the panel's denylist.";
      return out;
    }
    return false;
  }

  const denied = evaluateHost(hostTable);

  /* ---------- instance probe ---------- */

  const cache = {};
  const cacheOrder = [];
  let currentKey = null;

  /* Real instances to look at (reads only; no project walk). */
  async function gather(project, sequence) {
    const inst = { project, sequence };
    const root = await tryGet(() => project.getRootItem());
    inst.root = root;
    const items = root ? ((await tryGet(() => root.getItems(), [])) || []) : [];
    inst.projectItem = items.length ? items[0] : null;
    inst.clip = null;
    const folders = [];
    for (const it of items) {
      const c = tryGetSync(() => ppro.ClipProjectItem.cast(it));
      if (c) { inst.clip = c; break; }
      const f = tryGetSync(() => ppro.FolderItem.cast(it));
      if (f && folders.length < 20) folders.push(f);
    }
    for (let i = 0; !inst.clip && i < folders.length; i++) {
      for (const it of (await tryGet(() => folders[i].getItems(), [])) || []) {
        const c = tryGetSync(() => ppro.ClipProjectItem.cast(it));
        if (c) { inst.clip = c; break; }
      }
    }
    if (sequence) {
      inst.editor = tryGetSync(() => ppro.SequenceEditor.getEditor(sequence));
      const count = await timeline.trackCount(sequence, "video");
      for (let i = 0; i < count && !inst.trackItem; i++) {
        const track = await tryGet(() => sequence.getVideoTrack(i));
        if (i === 0) inst.videoTrack = track;
        const clips = track ? await timeline.clipItems(track) : [];
        if (clips.length) inst.trackItem = clips[0];
      }
      if (inst.trackItem) {
        const chain = await tryGet(() => inst.trackItem.getComponentChain());
        if (chain && (tryGetSync(() => chain.getComponentCount(), 0) || 0) > 0) {
          inst.component = tryGetSync(() => chain.getComponentAtIndex(0));
        }
      }
      const markers = await tryGet(() => ppro.Markers.getMarkers(sequence));
      const list = markers ? (tryGetSync(() => markers.getMarkers(), []) || []) : [];
      inst.marker = list.length ? list[0] : null;
    }
    return inst;
  }

  function runInstanceProbes(inst) {
    const flags = {};
    const run = () => {
      for (const [name, kind] of FLAGS) {
        if (kind !== "instance") continue;
        let v;
        try { v = INSTANCE[name](inst); } catch (e) { v = "unknown"; }
        flags[name] = v;
      }
    };
    if (inst.project && core.isFn(inst.project, "lockedAccess")) inst.project.lockedAccess(run);
    else run();
    return flags;
  }

  /* Probe the active project + sequence unless cached (force re-probes). Skipped while stuck or denied. */
  async function ensureProbe(opts) {
    const o = opts || {};
    if (o.stuck || o.denied) return false;
    const project = await tryGet(() => ppro.Project.getActiveProject());
    if (!project) { currentKey = null; return false; }
    const sequence = await tryGet(() => project.getActiveSequence());
    let key;
    try {
      key = `${String(project.name)}\u0000${sequence ? String(sequence.name) : ""}`;
    } catch (e) {
      key = "?";
    }
    if (!o.force && cache[key]) { currentKey = key; return true; }
    let flags;
    try {
      flags = runInstanceProbes(await gather(project, sequence));
    } catch (e) {
      log(`Capability probe failed: ${core.errorMessage(e)}`, "err");
      return false;
    }
    if (!cache[key]) cacheOrder.push(key);
    cache[key] = { flags, probedAt: Date.now() };
    while (cacheOrder.length > 20) delete cache[cacheOrder.shift()];
    currentKey = key;
    return true;
  }

  /* All flags in spec order: statics, plus the current instance probe (else "unknown"). */
  function snapshot() {
    const entry = currentKey ? cache[currentKey] : null;
    const flags = {};
    for (const [name, kind] of FLAGS) {
      if (kind === "static") flags[name] = statics[name];
      else flags[name] = entry && core.hasOwn(entry.flags, name) ? entry.flags[name] : "unknown";
    }
    return { flags, probedAt: entry ? entry.probedAt : null };
  }

  function flag(name) {
    const flags = snapshot().flags;
    return core.hasOwn(flags, name) ? flags[name] : "unknown";
  }

  /* ---------- codeHash ---------- */

  const HASH_FILES = ["index.html", "main.js", "manifest.json"];
  const ZERO = [0];

  function utf8Bytes(s) {
    const out = [];
    for (const ch of String(s)) {
      let c = ch.codePointAt(0);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
      else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
      else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    }
    return out;
  }

  function fnv(h, bytes) {
    for (let i = 0; i < bytes.length; i++) {
      h ^= bytes[i];
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
  }

  async function computeCodeHash() {
    const storage = uxp && uxp.storage;
    const lfs = storage && storage.localFileSystem;
    if (!lfs || !core.isFn(lfs, "getPluginFolder")) return null;
    const binary = storage.formats && storage.formats.binary;
    const folder = await lfs.getPluginFolder();
    const top = Array.from((await folder.getEntries()) || []);
    const files = [];
    for (const name of HASH_FILES) {
      const e = top.find((x) => x && x.name === name && x.isFile);
      if (!e) return null;
      files.push({ rel: name, entry: e });
    }
    const lib = top.find((x) => x && x.name === "lib" && x.isFolder);
    if (lib) {
      for (const e of Array.from((await lib.getEntries()) || [])) {
        if (e && e.isFile && /\.js$/.test(e.name)) files.push({ rel: `lib/${e.name}`, entry: e });
      }
    }
    files.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
    let h = 0x811c9dc5;
    for (const f of files) {
      const data = await f.entry.read({ format: binary });
      if (data === null || data === undefined || typeof data === "string") return null;
      h = fnv(h, utf8Bytes(f.rel));
      h = fnv(h, ZERO);
      h = fnv(h, new Uint8Array(data));
      h = fnv(h, ZERO);
    }
    return ("00000000" + h.toString(16)).slice(-8);
  }

  let hashPromise = null;
  let hashValue; // undefined until computed; null when it could not be computed

  function startCodeHash() {
    if (!hashPromise) {
      hashPromise = Promise.resolve().then(computeCodeHash).then(
        (v) => { hashValue = v || null; return hashValue; },
        (e) => { hashValue = null; log(`codeHash unavailable: ${core.errorMessage(e)}`, "err"); return null; });
    }
    return hashPromise;
  }

  /* The code hash, waiting at most `ms` for the first computation; null when unavailable. */
  async function codeHashWithin(ms) {
    if (hashValue !== undefined) return hashValue;
    if (!(ms > 0)) { startCodeHash(); return null; }  // not computed yet: don't wait
    let timer;
    const late = new Promise((resolve) => { timer = setTimeout(() => resolve(null), ms); });
    const v = await Promise.race([startCodeHash(), late]);
    clearTimeout(timer);
    return v;
  }

  return {
    FLAGS, DEFAULT_HOST_TABLE, denied, hostString, hostInfo, evaluateHost, globToRegExp,
    ensureProbe, snapshot, flag, startCodeHash, codeHashWithin,
  };
};
