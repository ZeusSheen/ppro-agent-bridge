/*
 * probe: probe_api, a read-only inventory of the UXP API this Premiere build offers. The `premierepro`
 * module's exports (classes with their static and prototype members, namespaces, values), the `uxp`
 * module's exports, and the members of real instances (the active project, sequence, editor, tracks,
 * clips, component chains, markers, bins, the encoder manager), because a host object may only show its
 * methods on an instance.
 * Members are read through property descriptors: a getter is listed, never called (only the module's
 * own exports are read, which is how every handler reaches ppro.Project & co.). Instances are listed
 * inside lockedAccess, like the capability probe; no Action is ever created.
 * Member notation: "name(2)" a method and its arity, "name{get}" / "name{get,set}" an accessor,
 * "name:type" a data property.
 * Factory module.   deps: { ppro, uxp, core, timeline, hostInfo }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const uxp = deps.uxp;
  const core = deps.core;
  const timeline = deps.timeline;
  const tryGet = core.tryGet;
  const tryGetSync = core.tryGetSync;

  const SKIP = ["constructor", "__proto__", "caller", "callee", "arguments"];
  const FN_SKIP = ["length", "name", "prototype", "caller", "arguments"];
  const ROOTS = [Object.prototype, Function.prototype];

  function ownNames(obj) {
    let names = [];
    try { names = Object.getOwnPropertyNames(obj); } catch (e) { names = []; }
    try { for (const k in obj) if (names.indexOf(k) < 0) names.push(k); } catch (e) { /* host object */ }
    return names;
  }

  function describe(name, d) {
    if (!d) return null;
    if (typeof d.get === "function" || typeof d.set === "function") {
      return `${name}{${[d.get ? "get" : null, d.set ? "set" : null].filter(Boolean).join(",")}}`;
    }
    const v = d.value;
    if (typeof v === "function") return `${name}(${typeof v.length === "number" ? v.length : "?"})`;
    return `${name}:${v === null ? "null" : Array.isArray(v) ? "array" : typeof v}`;
  }

  /* Object.prototype / Function.prototype of any realm (host objects may come from another one). */
  function isRoot(p) {
    if (ROOTS.indexOf(p) >= 0) return true;
    const own = (k) => { try { return Object.prototype.hasOwnProperty.call(p, k); } catch (e) { return false; } };
    if (own("hasOwnProperty") && own("__defineGetter__")) return true;
    return typeof p === "function" && own("apply") && own("bind") && own("call");
  }

  /* Own members of obj and of its prototype chain (Object/Function prototypes excluded), sorted. */
  function members(obj, opts) {
    const o = opts || {};
    const skip = SKIP.concat(o.skip || []);
    const seen = new Set();
    const out = [];
    let p = obj;
    for (let depth = 0; p && !isRoot(p) && depth < 8; depth++) {
      for (const name of ownNames(p)) {
        if (seen.has(name) || skip.indexOf(name) >= 0) continue;
        seen.add(name);
        let d = null;
        try { d = Object.getOwnPropertyDescriptor(p, name); } catch (e) { d = null; }
        const text = describe(name, d) || `${name}:?`;
        out.push(text);
      }
      if (o.own) break;
      try { p = Object.getPrototypeOf(p); } catch (e) { p = null; }
    }
    out.sort();
    return o.max && out.length > o.max ? out.slice(0, o.max).concat([`...${out.length - o.max} more`]) : out;
  }

  /* A namespace's nested enums (Constants.TrackItemType -> ["CLIP=1", ...]); without `values` only types. */
  function enumValues(obj, max, values) {
    const out = [];
    for (const k of ownNames(obj)) {
      let d = null;
      try { d = Object.getOwnPropertyDescriptor(obj, k); } catch (e) { d = null; }
      if (!d || !("value" in d)) { out.push(`${k}{get}`); continue; }
      const v = d.value;
      out.push(v !== null && typeof v === "object" ? `${k}:object` : values ? `${k}=${String(v)}` : `${k}:${typeof v}`);
      if (out.length >= max) break;
    }
    return out.sort();
  }

  function exportInfo(value, max, values) {
    if (typeof value === "function") {
      const proto = tryGetSync(() => value.prototype);
      const protoMembers = proto && typeof proto === "object" ? members(proto, { max }) : [];
      return {
        type: protoMembers.length ? "class" : "function",
        arity: typeof value.length === "number" ? value.length : null,
        static: members(value, { own: true, skip: FN_SKIP, max }),
        proto: protoMembers,
      };
    }
    if (value !== null && typeof value === "object") {
      const info = { type: "object", members: members(value, { max }) };
      const nested = {};
      for (const k of ownNames(value)) {
        let d = null;
        try { d = Object.getOwnPropertyDescriptor(value, k); } catch (e) { d = null; }
        const v = d && "value" in d ? d.value : undefined;
        if (v !== null && typeof v === "object" && !Array.isArray(v)) nested[k] = enumValues(v, max, values);
      }
      if (Object.keys(nested).length) info.nested = nested;
      return info;
    }
    return { type: value === null ? "null" : typeof value, value: values && typeof value !== "object" ? value : null };
  }

  /* Every export of a module: read (module exports are how the panel reaches the API), then described.
   * Primitive values only with `values` (premierepro's enums); uxp's host object holds the application path. */
  function moduleExports(mod, max, problems, label, values) {
    const out = {};
    if (!mod) return out;
    for (const name of ownNames(mod).sort()) {
      if (SKIP.indexOf(name) >= 0) continue;
      let value;
      try { value = mod[name]; } catch (e) { problems.push(`${label}.${name}: ${core.errorMessage(e)}`); continue; }
      try { out[name] = exportInfo(value, max, values); } catch (e) { problems.push(`${label}.${name}: ${core.errorMessage(e)}`); }
    }
    return out;
  }

  /* Real objects to list members of (reads only). */
  async function gatherInstances(project, sequence, problems) {
    const inst = { project };
    const note = (what, e) => problems.push(`instance ${what}: ${core.errorMessage(e)}`);
    try { inst.rootFolder = await project.getRootItem(); } catch (e) { note("rootFolder", e); }
    if (inst.rootFolder) {
      const queue = ((await tryGet(() => inst.rootFolder.getItems(), [])) || []).slice(0, 200);
      for (let i = 0; i < queue.length && !(inst.clipProjectItem && inst.folderItem); i++) {
        const c = tryGetSync(() => ppro.ClipProjectItem.cast(queue[i]));
        if (c && !inst.clipProjectItem) { inst.clipProjectItem = c; inst.projectItem = queue[i]; continue; }
        const f = tryGetSync(() => ppro.FolderItem.cast(queue[i]));
        if (f && !inst.folderItem) inst.folderItem = f;
      }
    }
    if (sequence) {
      inst.sequence = sequence;
      inst.sequenceEditor = tryGetSync(() => ppro.SequenceEditor.getEditor(sequence));
      for (const kind of ["video", "audio"]) {
        const count = await timeline.trackCount(sequence, kind);
        for (let i = 0; i < count; i++) {
          const track = await tryGet(() => (kind === "video" ? sequence.getVideoTrack(i) : sequence.getAudioTrack(i)));
          if (i === 0 && track) inst[`${kind}Track`] = track;
          const items = track ? await timeline.clipItems(track) : [];
          if (items.length) { inst[`${kind}ClipTrackItem`] = items[0]; break; }
        }
        const clip = inst[`${kind}ClipTrackItem`];
        if (clip) {
          const chain = await tryGet(() => clip.getComponentChain());
          if (chain) inst[`${kind}ComponentChain`] = chain;
          const n = chain ? (tryGetSync(() => chain.getComponentCount(), 0) || 0) : 0;
          const comp = n ? tryGetSync(() => chain.getComponentAtIndex(0)) : null;
          if (comp) {
            inst[`${kind}Component`] = comp;
            const param = (tryGetSync(() => comp.getParamCount(), 0) || 0) > 0 ? tryGetSync(() => comp.getParam(0)) : null;
            if (param) inst[`${kind}ComponentParam`] = param;
          }
        }
      }
      try { inst.markers = await ppro.Markers.getMarkers(sequence); } catch (e) { note("markers", e); }
      const list = inst.markers ? (tryGetSync(() => inst.markers.getMarkers(), []) || []) : [];
      if (list.length) inst.marker = list[0];
      const settings = await tryGet(() => sequence.getSettings());
      if (settings) inst.sequenceSettings = settings;
    }
    const manager = tryGetSync(() => ppro.EncoderManager.getManager());
    if (manager) inst.encoderManager = manager;
    return inst;
  }

  return {
    /* params: { instances = true, maxMembers = 300 } */
    async probe_api(params, ctx) {
      const max = params.maxMembers || 300;
      const problems = [];
      const premierepro = moduleExports(ppro, max, problems, "premierepro", true);
      const out = { host: deps.hostInfo ? deps.hostInfo() : null, premierepro };
      out.uxp = moduleExports(uxp, max, problems, "uxp", false);
      let instanceCount = 0;
      if (params.instances !== false) {
        const project = await core.requireProject(ctx);
        const sequence = await tryGet(() => project.getActiveSequence());
        const inst = await gatherInstances(project, sequence, problems);
        const listed = {};
        const list = () => {
          for (const name of Object.keys(inst)) {
            try { listed[name] = members(inst[name], { max }); } catch (e) { problems.push(`instance ${name}: ${core.errorMessage(e)}`); }
          }
        };
        if (core.isFn(project, "lockedAccess")) project.lockedAccess(list);
        else list();
        out.instances = listed;
        instanceCount = Object.keys(listed).length;
      } else {
        await core.checkGuard(ctx);
      }
      const memberCount = (info) => (info.static || []).length + (info.proto || []).length + (info.members || []).length;
      out.counts = {
        exported: Object.keys(premierepro).length,
        members: Object.values(premierepro).reduce((n, info) => n + memberCount(info), 0),
        instances: instanceCount,
      };
      out.problems = problems;
      return out;
    },
  };
};
