"use strict";
/*
 * Premiere UXP mocks for the panel harness (node built-ins only): a small in-memory project behind
 * the subset of the `premierepro` and `uxp` modules the panel uses.
 *
 * Lock-scope rules are enforced at least as strictly as Premiere does:
 *  - every create*Action outside project.lockedAccess throws;
 *  - executeTransaction outside lockedAccess throws;
 *  - a lockedAccess / executeTransaction / createEmptySelection callback that returns a Promise fails;
 *  - a TrackItemSelection used after its callback returned throws;
 *  - TextSegments#exportToJSON (never to be called on a parsed transcript) throws.
 *
 * Scenarios: default (27.1.0, project "PAB_TEST_harness", sequence "Sequence 01", 59.94 fps, every
 * capability), no-project, no-sequence, 26.4 (no Speech-to-Text and other 27.x APIs),
 * transcript-empty-import (imports leave an empty placeholder), transcript-callback-import (only the
 * callback form imports), stt-pending (Speech-to-Text never settles).
 * Knobs (options.mock): failTransactions [label substrings -> executeTransaction returns false],
 * exportFails, exportThrows, importFails, sttReject, timebase (ticks per frame as a string; default
 * "4237833600" = 59.94), noTimebase (getTimebase throws), cloneLost (Sequence#createCloneAction adds no sequence),
 * cloneTwice (it adds two), frameSizeIgnored (createSetSettingsAction keeps the frame size),
 * deleteSequenceFails (Project#deleteSequence returns false and keeps the sequence), transitionSides (a clip's
 * createRemoveVideoTransitionAction leaves a neighbour's single-sided fade at that edit point alone),
 * noAudioFilters (no AudioFilterFactory), videoDisplayNames (what VideoFilterFactory.getDisplayNames returns),
 * relinkFails (changeMediaFilePath returns false and changes nothing), noRelink (canChangeMediaPath answers false),
 * subclipLandsInRoot (a new subclip goes to the root bin instead of its source's), failCreate [create*Action names
 * -> creating one throws, inside the transaction callback], ignoreActions [create*Action names -> the action commits
 * and changes nothing, so a read-back disagrees]. Exporters also fail for paths
 * containing /no/such/
 * (a missing folder); EncoderManager#exportSequence throws "Unable to initialize export!" for such a preset path.
 * exportJob {progress: [values], stepMs, result: complete | error | cancel | silent | never, resolve (what the promise
 * gives: true / false / "throw"), deliver: EventManager | encoder | both | none, projectState (an ExportMediaComplete
 * event on the project with that state), extraEvents [{atMs, name, target: encoder | project, fields}] (events from
 * something else, e.g. a frame export, at those times)} makes exportSequence a render that takes time (NOT measured: which events
 * 27.1 fires, on which route, and their shape are what the live test finds out): a progress event every stepMs
 * (default 50; values as given, e.g. 25 for 0-100 scales), then the result's event and the promise; silent fires
 * nothing; never neither ends nor settles. Events are objects with prototype getters (type, plus progress or
 * message), sent to listeners added through ppro.EventManager.addEventListener(target, name, fn) or the target's own
 * addEventListener, as `deliver` says (default EventManager). record.listeners logs every add / remove.
 * ProjectConverter.exportAAF(seq, path, options?) records {kind: "aaf", outputPath, options (the getter values of
 * the AAFExportOptions it got, or null)} and returns false for a /no/such/ path or with exportFails; knob aafJob
 * {holdMs (the "AAF export log" stays up that long before the promise settles; -1: forever), result: true | false |
 * "throw" (what it settles with), throwSync (exportAAF throws before returning a promise)} makes it slow (27.1 b21:
 * a modal log holds the call until someone clicks OK; whether the panel's other requests still run then is NOT
 * measured). AAFExportOptions has the 27.1 getters and setX methods (api dump, 2 Nov 2026; the defaults here are
 * NOT measured); knob noAafOptions removes the class, aafMissingSetter ["setTrimSources", ...] removes setters,
 * aafSetterThrows ["setSampleRate", ...] makes them throw. exportAsFinalCutProXML records {kind: "fcpxml",
 * outputPath, suppressUI (its third argument)} and returns false for /no/such/ or with exportFails; exportThrows
 * makes both converters throw.
 * Failed reads (not measured: what a broken read looks like is the point): failTrackItems ["V2", ...] -> that
 * track's getTrackItems throws; nullTrackItems [labels] -> it returns null; holeTrackItems {label: n} -> its first n
 * clip lists carry a null where a clip was just removed (seen live on 27.1); selectionIgnored -> Sequence#setSelection
 * returns false and keeps the old selection; failTrackCount ["video" | "audio"] ->
 * get<Kind>TrackCount throws. seedItems [{track: "V3", name, start, end, inPoint, outPoint, speed?, disabled?,
 * mediaPath? (absent / null: no project item), graphic? (a graphic's chain, see GRAPHIC_CHAIN), layersAfterMs? (a
 * graphic's Text layer arrives that long after loading, as on 27.1 ~0.9 s after placing; -1: never), components?
 * (match names of EFFECTS: the clip's whole chain)}] adds clips to the default sequence (fingerprint parity tests,
 * effect order on graphics).
 *
 * Documented-only calls (api_smoke exercises them; semantics from Adobe's docs, NOT measured): marker setters /
 * move / remove, transition removal, component insert / remove, keyframe removal / interpolation (values interpolate
 * linearly between keys unless a key holds), sequence list / clone / activate / delete / settings / in-out /
 * selection / player position, work area, track rename, clip rename / disable, bin rename / colour / move / remove,
 * subclips (live: only the object form {takeVideo, takeAudio} of the last argument works, the positional booleans
 * threw — the mock throws for them too; a subclip's own in/out via getInPoint / getOutPoint, the media length via
 * getMedia().getDuration(), the subclip landing next to its source and a subclip placed whole giving its range are
 * documented or assumed only), relinking (changeMediaFilePath(path, true) + refreshMedia live), custom Properties,
 * project save, FCP XML / AAF export, the video filter name lists, MOGRT insertion, the Source monitor. Knob
 * projectName renames the project. A sequence clone is named "<name> Copy" (live 27.1 on a Chinese UI:
 * "<name> 副本"), is not made active, and here keeps a copy of the source's custom Properties (an assumption, not
 * measured: it is the case clone_sequence must survive). Audio filters by display name were run live by api_smoke
 * (27.1 b21: 55 localised names listed, "降噪" created, appended, read back, removed).
 * Track items follow the semantics measured on Premiere 27.1 build 17: createSetStartAction /
 * createSetInPointAction trim the head (start and in point move by the same delta, end stays),
 * createSetEndAction / createSetOutPointAction trim the tail, createMoveAction moves BY its time (relative).
 * SequenceEditor#createCloneTrackItemAction copies only the given item (effects included) offset later,
 * overwriting the destination. Like Premiere, sequence times are kept as given (never snapped to frames);
 * record.times lists the ticks every time-taking action received, edgeTicks() every item's edges in ticks,
 * record.componentActions every component append / insert created ({kind, matchName, index?}). Audio clips carry the measured mono chain: one "Internal Volume Mono"
 * component, param 0 bypass (boolean), param 1 Level (linear; 0 dB = 0.17782793939113617).
 * The uxp module (documented, not measured): storage entries carry nativePath; localFileSystem.getPluginFolder()
 * is options.pluginFolder (a folder to stand in for the plugin folder, e.g. one with a launcher.json) or the folder
 * main.js came from; localFileSystem.getEntryWithUrl("file:/<drive>:/...") finds a real file or folder (raw or
 * percent-encoded) and throws for a missing one; shell.openPath(path, developerText) is recorded in
 * record.openPath and resolves to "" (started). Knobs: openPath (a string every call resolves to, or
 * {".bat": ..., ".pyw": ...} by extension; "throw:<text>" throws), noShell (no uxp.shell), noEntryWithUrl (no
 * getEntryWithUrl), uiLocale (uxp.host.uiLocale; default "en_US"). shell.openExternal(url, developerText) is recorded
 * in record.openExternal and resolves to "" (opened); knob openExternal: the string it resolves to ("throw:<text>"
 * throws), noOpenExternal: no shell.openExternal (an older UXP).
 * The pairing token (lib/pairing.js): require("os").homedir() is knob home (default HARNESS_HOME, the harness
 * user's home; null: no homedir), localFileSystem.getDataFolder() a folder in <home>'s roaming application data
 * (nativePath only), and
 * require("fs").readFile(path) resolves to knob token (default HARNESS_TOKEN; null: no token file) for
 * <home>\.ppro-agent-bridge\token in any spelling (native, file: URL), else rejects; every path is recorded in
 * record.fsReads. Knob noFs: no fs module (localFileSystem.getEntryWithUrl is the fallback, which finds real files).
 */
const fs = require("fs");
const path = require("path");

const TPS = 254016000000;
const HARNESS_TOKEN = "harness-pairing-token-0123456789abcdefghijklmnop";
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const EPS = 1e-4; // seconds: overlap tolerance of the mock timeline
const LEVEL_0DB = 0.17782793939113617; // Premiere's linear audio Level at 0 dB (measured, 27.1 b17)
const DENOISE_GUID = "c6762838-e451-4d22-9c90-b0eaa734605a"; // match name of "降噪" (measured, 27.1 b21)
const SCENARIOS = ["default", "no-project", "no-sequence", "26.4", "transcript-empty-import",
  "transcript-callback-import", "stt-pending"];
const SPEAKER = "3f2b6c1e-8a4d-4c2e-9b7a-1d2e3f4a5b6c";
const MISSING_LANGUAGE_PACKS = ["ja-JP", "ko-KR"]; // supported, but the pack is "not installed"
const missingFolder = (p) => /[\\/]no[\\/]such[\\/]/.test(String(p)); // exporters fail for ".../no/such/..."

function sampleTranscript(words) {
  const list = (words || ["Hello", "from", "the", "harness"]).map((text, i) => ({
    text, start: 0.5 + i * 0.4, duration: 0.35, eos: false, confidence: 0.95, tags: [], type: "word",
  }));
  list.push({ text: ".", start: 0.5 + list.length * 0.4, duration: 0, eos: true, confidence: 1, tags: [], type: "punctuation" });
  return {
    language: "en-us",
    segments: [{ start: 0.5, duration: list.length * 0.4, language: "en-us", speaker: SPEAKER, words: list }],
    speakers: [{ id: SPEAKER, name: "Speaker 1" }],
  };
}

const emptyPlaceholder = () => ({ language: "und-zz", segments: [], speakers: [] });

function createMocks(options) {
  const opts = options || {};
  const scenario = opts.scenario || "default";
  if (SCENARIOS.indexOf(scenario) < 0) throw new Error(`unknown scenario "${scenario}" (${SCENARIOS.join(", ")})`);
  const old = scenario === "26.4";
  const knobs = opts.mock || {};
  const record = {
    transactions: [], failedTransactions: [], actionsCreated: 0, violations: [], sttCalls: [],
    exports: [], imports: [], textSegmentsCalls: 0, clones: [], times: [], subclips: [], relinks: [], listeners: [],
    componentActions: [], projectOpens: [],
  };
  let lockDepth = 0;
  let idCounter = 0;

  function violation(text) {
    record.violations.push(text);
    return new Error(`mock: ${text}`);
  }

  /* The ticks a time-taking action received: {action, arg, ticks}. */
  function noteTime(action, arg, t) {
    record.times.push({ action, arg, ticks: t.ticksNumber });
  }

  function makeAction(kind, apply) {
    if (lockDepth <= 0) throw violation(`${kind} called outside lockedAccess`);
    if ((knobs.failCreate || []).indexOf(kind) >= 0) throw new Error(`mock: ${kind} failed (knob failCreate)`);
    record.actionsCreated += 1;
    return { __mockAction: true, kind, apply: (knobs.ignoreActions || []).indexOf(kind) >= 0 ? () => {} : apply };
  }

  class TickTime {
    constructor(ticks) { this._ticks = Math.round(Number(ticks)); }
    get ticks() { return String(this._ticks); }
    get ticksNumber() { return this._ticks; }
    get seconds() { return this._ticks / TPS; }
    static createWithSeconds(s) { return new TickTime(Math.round(Number(s) * TPS)); }
    static createWithTicks(t) { return new TickTime(Number(t)); }
  }
  if (!old) TickTime.timeToTimecode = function () { return "00:00:00:00"; };

  class PointF {
    constructor(x, y) { this.x = x; this.y = y; }
  }
  // 27.1 keeps width / height as prototype accessors (JSON.stringify shows {}): so do the mocks
  class RectF {
    constructor() { this._w = 0; this._h = 0; }
    get width() { return this._w; }
    set width(v) { this._w = v; }
    get height() { return this._h; }
    set height(v) { this._h = v; }
  }
  const copyValue = (v) => (v instanceof PointF ? new PointF(v.x, v.y) : v);

  // Constants.InterpolationMode as Premiere 27.1 reports it (1-3 unnamed)
  const INTERPOLATION = { LINEAR: 0, HOLD: 4, BEZIER: 5, TIME: 6, TIME_TRANSITION_START: 7, TIME_TRANSITION_END: 8 };

  class Keyframe {
    constructor(value, mode) {
      this.value = value;
      this.position = TickTime.createWithSeconds(0);
      this._mode = mode === undefined ? INTERPOLATION.LINEAR : mode;
    }
    getTemporalInterpolationMode() { return this._mode; }
    setTemporalInterpolationMode(mode) { this._mode = mode; }
  }

  class MockParam {
    constructor(displayName, value, keys) {
      this.displayName = displayName;
      this._value = value;
      this._timeVarying = !!(keys && keys.length);
      this._keys = (keys || []).map((k) => ({ ticks: Math.round(k.t * TPS), value: k.value }));
    }
    clone() {
      const p = new MockParam(this.displayName, copyValue(this._value));
      p._timeVarying = this._timeVarying;
      p._keys = this._keys.map((k) => ({ ticks: k.ticks, value: copyValue(k.value), mode: k.mode }));
      return p;
    }
    isTimeVarying() { return this._timeVarying; }
    areKeyframesSupported() { return true; }
    async getStartValue() { return new Keyframe(this._timeVarying && this._keys.length ? this._keys[0].value : this._value); }
    async getValueAtTime(t) {
      if (this._timeVarying && this._keys.length) {
        const keys = this._keys;
        if (t.ticksNumber <= keys[0].ticks) return keys[0].value;
        for (let i = 0; i < keys.length - 1; i++) {
          const a = keys[i];
          const b = keys[i + 1];
          if (t.ticksNumber < b.ticks) {
            if (a.mode === INTERPOLATION.HOLD || typeof a.value !== "number" || typeof b.value !== "number") return a.value;
            return a.value + (b.value - a.value) * (t.ticksNumber - a.ticks) / (b.ticks - a.ticks);
          }
        }
        return keys[keys.length - 1].value;
      }
      return this._value;
    }
    getKeyframeListAsTickTimes() { return this._keys.map((k) => new TickTime(k.ticks)); }
    createKeyframe(value) { return new Keyframe(value); }
    createSetValueAction(kf) { return makeAction("createSetValueAction", () => { this._value = kf.value; }); }
    createSetTimeVaryingAction(on) {
      return makeAction("createSetTimeVaryingAction", () => { this._timeVarying = !!on; this._keys = []; });
    }
    createRemoveKeyframeAction(t) {
      const ticks = t.ticksNumber;
      return makeAction("createRemoveKeyframeAction", () => { this._keys = this._keys.filter((k) => Math.abs(k.ticks - ticks) > 1); });
    }
    createRemoveKeyframeRangeAction(a, b) {
      const lo = a.ticksNumber;
      const hi = b.ticksNumber;
      return makeAction("createRemoveKeyframeRangeAction", () => { this._keys = this._keys.filter((k) => k.ticks < lo || k.ticks > hi); });
    }
    createSetInterpolationAtKeyframeAction(t, mode) {
      const ticks = t.ticksNumber;
      return makeAction("createSetInterpolationAtKeyframeAction", () => {
        for (const k of this._keys) if (Math.abs(k.ticks - ticks) <= 1) k.mode = mode;
      });
    }

    createAddKeyframeAction(kf) {
      const ticks = kf.position.ticksNumber;
      const value = kf.value;
      const mode = kf._mode;
      return makeAction("createAddKeyframeAction", () => {
        this._keys = this._keys.filter((k) => k.ticks !== ticks);
        this._keys.push({ ticks, value, mode });
        this._keys.sort((a, b) => a.ticks - b.ticks);
      });
    }
  }

  class MockComponent {
    constructor(matchName, displayName, params) {
      this._matchName = matchName;
      this._displayName = displayName;
      this._params = params;
    }
    clone() { return new MockComponent(this._matchName, this._displayName, this._params.map((p) => p.clone())); }
    async getMatchName() { return this._matchName; }
    async getDisplayName() { return this._displayName; }
    getParamCount() { return this._params.length; }
    getParam(i) { return this._params[i] || null; }
  }
  if (!old) MockComponent.prototype.getInstanceName = async function () { return this._displayName; };

  const EFFECTS = {
    "AE.ADBE Motion": (keys) => new MockComponent("AE.ADBE Motion", "Motion", [
      new MockParam("Position", new PointF(540, 960)), new MockParam("Scale", 100), new MockParam("Scale Width", 100),
      new MockParam("Uniform Scale", true), new MockParam("Rotation", 0, keys), new MockParam("Anchor Point", new PointF(540, 960)),
      new MockParam("Anti-flicker Filter", 0)]),
    "AE.ADBE Opacity": () => new MockComponent("AE.ADBE Opacity", "Opacity", [
      new MockParam("Opacity", 100), new MockParam("Blend Mode", 0)]),
    "AE.ADBE Gaussian Blur 2": () => new MockComponent("AE.ADBE Gaussian Blur 2", "Gaussian Blur", [
      new MockParam("Blurriness", 0), new MockParam("Blur Dimensions", 1), new MockParam("Repeat Edge Pixels", false)]),
    "AE.ADBE Crop": () => new MockComponent("AE.ADBE Crop", "Crop", [
      new MockParam("Left", 0), new MockParam("Top", 0), new MockParam("Right", 0), new MockParam("Bottom", 0),
      new MockParam("Zoom", false), new MockParam("Feather Edges", 0)]),
    "AE.ADBE Lumetri": () => new MockComponent("AE.ADBE Lumetri", "Lumetri Color", [
      new MockParam("Input LUT", 0), new MockParam("Exposure", 0), new MockParam("Contrast", 0), new MockParam("Saturation", 100)]),
    "AE.ADBE DeNoise": () => new MockComponent("AE.ADBE DeNoise", "DeNoise", [
      new MockParam("Bypass", false), new MockParam("Amount", 40)]),
    // MEASURED (27.1 b21, Chinese UI, 22 Oct 2026): "降噪" added by display name has a GUID match name and six
    // params that all report isTimeVarying right after adding (keyframe count unmeasured; none here)
    [DENOISE_GUID]: () => new MockComponent(DENOISE_GUID, "降噪", [
      ["旁路", false], ["数量", 0.4], ["补充增益", 0], ["自动增益", 0], ["搁架式", 0], ["输出差分信号", 0],
    ].map(([n, v]) => { const p = new MockParam(n, v); p._timeVarying = true; return p; })),
    // audio: the chain measured on a mono clip (27.1 b17); stereo clips are unprobed
    "Internal Volume Mono": () => new MockComponent("Internal Volume Mono", "Volume", [
      new MockParam("Bypass", false), new MockParam("Level", LEVEL_0DB)]),
    // a graphic's own components (not addable): its Vector Motion and a text layer ("AE.ADBE Text" is a stand-in;
    // the layer's real match name is unmeasured)
    "AE.ADBE Graphic Group": () => new MockComponent("AE.ADBE Graphic Group", "Vector Motion", [
      new MockParam("Position", new PointF(540, 960)), new MockParam("Scale", 100), new MockParam("Rotation", 0)]),
    "AE.ADBE Text": () => new MockComponent("AE.ADBE Text", "Text", [
      new MockParam("Source Text", "Title"), new MockParam("Transform Position", new PointF(540, 960))]),
    // an effect VideoFilterFactory does not list (third-party or obsolete): not addable, but no graphic layer either
    "AE.Unlisted Effect": () => new MockComponent("AE.Unlisted Effect", "Unlisted", [new MockParam("Amount", 0)]),
  };
  const ADDABLE = ["AE.ADBE Gaussian Blur 2", "AE.ADBE Crop", "AE.ADBE Lumetri"];
  // A graphic or text clip's chain (27.1, 2 Nov, Basic Title.mogrt): Opacity, Motion, Graphic Group, the effects, the
  // graphic's layer components last (an effect after them left the frame unchanged).
  const GRAPHIC_CHAIN = () => ["AE.ADBE Opacity", "AE.ADBE Motion", "AE.ADBE Graphic Group", "AE.ADBE Text"].map((m) => EFFECTS[m]());
  // display name -> match name (AudioFilterFactory lists display names only; the same two as broker/fake_panel.py)
  const AUDIO_FILTERS = { "DeNoise": "AE.ADBE DeNoise", "降噪": DENOISE_GUID };

  class MockChain {
    constructor(components) { this._components = components; }
    getComponentCount() { return this._components.length; }
    getComponentAtIndex(i) { return this._components[i] || null; }
    createAppendComponentAction(comp) {
      record.componentActions.push({ kind: "append", matchName: comp._matchName });
      return makeAction("createAppendComponentAction", () => { this._components.push(comp); });
    }
    createInsertComponentAction(comp, index) {
      record.componentActions.push({ kind: "insert", matchName: comp._matchName, index });
      return makeAction("createInsertComponentAction", () => { this._components.splice(index, 0, comp); });
    }
    createRemoveComponentAction(comp) {
      return makeAction("createRemoveComponentAction", () => {
        const i = this._components.indexOf(comp);
        if (i >= 0) this._components.splice(i, 1);
      });
    }
  }

  class MockClip { // a ClipProjectItem (raw ProjectItem and its cast are the same object here)
    constructor(o) {
      this.__kind = "clip";
      this.name = o.name;
      this._media = o.mediaPath;
      this._duration = o.duration || 10;
      this._isSequence = !!o.isSequence;
      this._hasAudio = o.hasAudio !== false;
      this._id = `pi-${++idCounter}`;
      this._range = null;
      this._subRange = null; // a subclip's bounds in media time
      this._fps = null;
      this._transcript = o.transcript || null;
    }
    getId() { return this._id; }
    getParentBin() { return this._parent || null; }
    async getMediaFilePath() { return this._media; }
    async isSequence() { return this._isSequence; }
    async canChangeMediaPath() { return !knobs.noRelink; }
    async changeMediaFilePath(p, override) {
      record.relinks.push({ item: this.name, path: p, override: override === undefined ? null : override });
      if (knobs.relinkFails) return false;
      this._media = p;
      return true;
    }
    async refreshMedia() { return true; }
    async isOffline() { return false; }
    async getFootageInterpretation() { const fps = this._fps || 60000 / 1001; return { getFrameRate() { return fps; }, getPixelAspectRatio() { return 1; } }; }
    async getMedia() { const d = this._duration; return { getDuration() { return TickTime.createWithSeconds(d); } }; }
    async getInPoint() { const r = this._range || this._subRange; return TickTime.createWithSeconds(r ? r.in : 0); }
    async getOutPoint() { const r = this._range || this._subRange; return TickTime.createWithSeconds(r ? r.out : this._duration); }
    createSetNameAction(name) { return makeAction("createSetNameAction", () => { this.name = name; }); }
    createSetColorLabelAction(i) { return makeAction("createSetColorLabelAction", () => { this._label = i; }); }
    async getColorLabelIndex() { return this._label === undefined ? 0 : this._label; }
    createSetInOutPointsAction(inT, outT) {
      noteTime("createSetInOutPointsAction", "in", inT);
      noteTime("createSetInOutPointsAction", "out", outT);
      return makeAction("createSetInOutPointsAction", () => { this._range = { in: inT.seconds, out: outT.seconds }; });
    }
    createClearInOutPointsAction() { return makeAction("createClearInOutPointsAction", () => { this._range = null; }); }
    createSetOverrideFrameRateAction(fps) { return makeAction("createSetOverrideFrameRateAction", () => { this._fps = fps; }); }
    // (name, start, end, hasHardBoundaries, {takeVideo, takeAudio}): positional booleans threw on 27.1
    createSubClipAction(name, start, end, hard, take) {
      if (!take || typeof take !== "object" || typeof take.takeVideo !== "boolean" || typeof take.takeAudio !== "boolean") {
        throw new Error("Illegal Parameter type");
      }
      noteTime("createSubClipAction", "in", start);
      noteTime("createSubClipAction", "out", end);
      return makeAction("createSubClipAction", () => {
        record.subclips.push({ name, in: start.ticksNumber, out: end.ticksNumber, hard, takeVideo: take.takeVideo,
          takeAudio: take.takeAudio });
        const sub = new MockClip({ name, mediaPath: this._media, duration: this._duration,
          hasAudio: this._hasAudio && take.takeAudio });
        sub._subRange = { in: start.seconds, out: end.seconds };
        sub._fps = this._fps;
        (knobs.subclipLandsInRoot ? root : (this._parent || root)).adopt(sub);
      });
    }
  }

  class MockFolder {
    constructor(name, items) {
      this.__kind = "folder";
      this.name = name;
      this._items = [];
      this._id = `pi-${++idCounter}`;
      for (const it of items || []) this.adopt(it);
    }
    getId() { return this._id; }
    getParentBin() { return this._parent || null; }
    adopt(item) { item._parent = this; this._items.push(item); return item; }
    async getItems() { return this._items.slice(); }
    createBinAction(name) { return makeAction("createBinAction", () => { this.adopt(new MockFolder(name)); }); }
    createRenameBinAction(name) { return makeAction("createRenameBinAction", () => { this.name = name; }); }
    createSetColorLabelAction(i) { return makeAction("createSetColorLabelAction", () => { this._label = i; }); }
    async getColorLabelIndex() { return this._label === undefined ? 0 : this._label; }
    createMoveItemAction(item, dest) {
      return makeAction("createMoveItemAction", () => {
        const from = item._parent || this;
        const i = from._items.indexOf(item);
        if (i >= 0) from._items.splice(i, 1);
        dest.adopt(item);
      });
    }
    createRemoveItemAction(item) {
      return makeAction("createRemoveItemAction", () => {
        const i = item ? this._items.indexOf(item) : -1;
        if (i >= 0) this._items.splice(i, 1);
      });
    }
  }

  class MockTransition {
    constructor(matchName, start, end) { this._matchName = matchName; this._start = start; this._end = end; }
    async getName() { return this._matchName; }
    async getMatchName() { return this._matchName; }
    async getStartTime() { return TickTime.createWithSeconds(this._start); }
    async getEndTime() { return TickTime.createWithSeconds(this._end); }
    async getDuration() { return TickTime.createWithSeconds(this._end - this._start); }
  }

  class MockTrackItem {
    constructor(o) {
      this._name = o.name;
      this._start = o.start;
      this._end = o.end;
      this._in = o.inPoint;
      this._out = o.outPoint;
      this._speed = o.speed === undefined ? 1 : o.speed;
      this._projectItem = o.projectItem || null;
      this._track = o.track;
      this._chain = new MockChain(o.components || []);
    }
    async getName() { return this._name; }
    async getStartTime() { return TickTime.createWithSeconds(this._start); }
    async getEndTime() { return TickTime.createWithSeconds(this._end); }
    async getInPoint() { return TickTime.createWithSeconds(this._in); }
    async getOutPoint() { return TickTime.createWithSeconds(this._out); }
    async getSpeed() { return this._speed; }
    async isDisabled() { return !!this._disabled; }
    async getProjectItem() { return this._projectItem; }
    async getComponentChain() { return this._chain; }
    createAddVideoTransitionAction(transition, o) {
      const dur = o && o.duration ? o.duration.seconds : 1;
      const atStart = !!(o && o.applyToStart);
      return makeAction("createAddVideoTransitionAction", () => {
        const cut = atStart ? this._start : this._end;
        // Premiere 27.1 keeps a transition already at the edit point and adds nothing (live, 19 Oct 2026)
        if (this._track.transitions.some((t) => t._start <= cut + EPS && t._end >= cut - EPS)) return;
        const start = atStart ? this._start : this._end - dur;
        this._track.transitions.push(new MockTransition(transition._matchName, start, start + dur));
      });
    }
    /* A copy with its own effect chain (clone, and the right half of an overwrite split). */
    copy() {
      return new MockTrackItem({
        name: this._name, start: this._start, end: this._end, inPoint: this._in, outPoint: this._out,
        speed: this._speed, projectItem: this._projectItem, track: this._track,
        components: this._chain._components.map((c) => c.clone()),
      });
    }
    // Measured on 27.1 b17: head trims move start and in point together, tail trims end and out point.
    createSetStartAction(t) {
      noteTime("createSetStartAction", "time", t);
      const s = t.seconds;
      return makeAction("createSetStartAction", () => { const d = s - this._start; this._start += d; this._in += d; });
    }
    createSetInPointAction(t) {
      noteTime("createSetInPointAction", "time", t);
      const s = t.seconds;
      return makeAction("createSetInPointAction", () => { const d = s - this._in; this._in += d; this._start += d; });
    }
    createSetEndAction(t) {
      noteTime("createSetEndAction", "time", t);
      const s = t.seconds;
      return makeAction("createSetEndAction", () => { const d = s - this._end; this._end += d; this._out += d; });
    }
    createSetOutPointAction(t) {
      noteTime("createSetOutPointAction", "time", t);
      const s = t.seconds;
      return makeAction("createSetOutPointAction", () => { const d = s - this._out; this._out += d; this._end += d; });
    }
    // Relative: moves the item BY t (the panel turns moveTo, an absolute start, into this delta).
    createMoveAction(t) {
      noteTime("createMoveAction", "by", t);
      const d = t.seconds;
      return makeAction("createMoveAction", () => { this._start += d; this._end += d; });
    }
    createSetDisabledAction(on) { return makeAction("createSetDisabledAction", () => { this._disabled = !!on; }); }
    createSetNameAction(name) { return makeAction("createSetNameAction", () => { this._name = name; }); }
    // Removes the transition at that edit point, whichever side of the cut it lies on (documented; that a
    // neighbour's single-sided fade counts as this clip's START / END is unmeasured: knob transitionSides makes it
    // count only for the clip it lies on). Positions as live: START 1, END 2.
    createRemoveVideoTransitionAction(position) {
      if (position !== 1 && position !== 2) throw new Error(`mock: bad TransitionPosition ${position}`);
      return makeAction("createRemoveVideoTransitionAction", () => {
        const cut = position === 1 ? this._start : this._end;
        const mine = (t) => !knobs.transitionSides || (position === 1 ? t._end > cut + EPS : t._start < cut - EPS);
        this._track.transitions = this._track.transitions.filter((t) => !(t._start <= cut + EPS && t._end >= cut - EPS && mine(t)));
      });
    }
  }

  class MockTrack {
    constructor(kind, index) {
      this.kind = kind;
      this.index = index;
      this.name = `${kind === "video" ? "Video" : "Audio"} ${index + 1}`;
      this.items = [];
      this.transitions = [];
    }
    async getTrackItems(type) {
      const label = (this.kind === "video" ? "V" : "A") + (this.index + 1);
      if ((knobs.failTrackItems || []).indexOf(label) >= 0) throw new Error(`mock: the items on ${label} are unreadable`);
      if ((knobs.nullTrackItems || []).indexOf(label) >= 0) return null;
      const holes = knobs.holeTrackItems || {};
      if (type !== 2 && holes[label] > 0) {
        holes[label] -= 1;
        return [null].concat(this.items.slice());
      }
      return type === 2 ? this.transitions.slice() : this.items.slice();
    }
    async setMute(on) { this.muted = !!on; return true; }
    async isMuted() { return !!this.muted; }
    createSetNameAction(name) { return makeAction("createSetNameAction", () => { this.name = name; }); }
    add(o) {
      const it = new MockTrackItem(Object.assign({ track: this }, o));
      this.items.push(it);
      return it;
    }
    /* An overwrite edit's first half: trim / split / drop whatever lies under [start, end). */
    clear(start, end) {
      const out = [];
      for (const it of this.items) {
        if (it._end <= start + EPS || it._start >= end - EPS) { out.push(it); continue; }
        let right = null;
        if (it._end > end + EPS) {
          right = it.copy();
          right._in = it._in + (end - it._start);
          right._start = end;
        }
        if (it._start < start - EPS) {
          it._out = it._in + (start - it._start);
          it._end = start;
          out.push(it);
        }
        if (right) out.push(right);
      }
      this.items = out.sort((a, b) => a._start - b._start);
    }
  }

  class MockMarker {
    constructor(name, type, start, duration, comments) {
      this._name = name;
      this._type = type;
      this._start = start;
      this._duration = duration;
      this._comments = comments;
      this._color = 0;
      this.guid = `marker-${++idCounter}`;
    }
    getName() { return this._name; }
    getComments() { return this._comments; }
    getStart() { return TickTime.createWithSeconds(this._start); }
    getDuration() { return TickTime.createWithSeconds(this._duration); }
    getType() { return this._type; }
    getColorIndex() { return this._color; }
    createSetColorByIndexAction(i) { return makeAction("createSetColorByIndexAction", () => { this._color = i; }); }
    createSetNameAction(v) { return makeAction("createSetNameAction", () => { this._name = v; }); }
    createSetCommentsAction(v) { return makeAction("createSetCommentsAction", () => { this._comments = v; }); }
    createSetDurationAction(t) { return makeAction("createSetDurationAction", () => { this._duration = t.seconds; }); }
    createSetTypeAction(v) { return makeAction("createSetTypeAction", () => { this._type = v; }); }
  }

  class MockSequence {
    constructor(name, guid) {
      this.name = name;
      this.guid = guid;
      this._video = [0, 1, 2, 3].map((i) => new MockTrack("video", i));
      this._audio = [0, 1, 2, 3].map((i) => new MockTrack("audio", i));
      this._markers = [];
      this._player = 0;
      this._size = { width: 1080, height: 1920 };
      this._inOut = [0, 0];
      this._selection = [];
      this._item = null;
    }
    async getPlayerPosition() { return TickTime.createWithSeconds(this._player); }
    async setPlayerPosition(t) { this._player = t.seconds; return true; }
    async getFrameSize() { return { width: this._size.width, height: this._size.height }; }
    async getInPoint() { return TickTime.createWithSeconds(this._inOut[0]); }
    async getOutPoint() { return TickTime.createWithSeconds(this._inOut[1]); }
    createSetInPointAction(t) { return makeAction("createSetInPointAction", () => { this._inOut[0] = t.seconds; }); }
    createSetOutPointAction(t) { return makeAction("createSetOutPointAction", () => { this._inOut[1] = t.seconds; }); }
    async getSelection() { const items = this._selection.slice(); return { async getTrackItems() { return items; } }; }
    setSelection(sel) {
      if (!sel || !sel._valid) throw violation("selection used outside its createEmptySelection callback");
      if (knobs.selectionIgnored) return false;
      this._selection = sel._items.slice();
      return true;
    }
    async clearSelection() { this._selection = []; return true; }
    async getProjectItem() { return this._item; }
    async getSettings() {
      const size = { width: this._size.width, height: this._size.height };
      const flags = { maxRenderQuality: !!this._maxRenderQuality };
      return {
        _size: size,
        _flags: flags,
        getVideoFrameRect() { const r = new RectF(); r.width = size.width; r.height = size.height; return r; },
        setVideoFrameRect(r) { size.width = r.width; size.height = r.height; return true; },
        getVideoFrameRate() { return 59.94; },
        getAudioSampleRate() { return 48000; },
        getMaxRenderQuality() { return flags.maxRenderQuality; },
        setMaxRenderQuality(v) { flags.maxRenderQuality = !!v; return true; },
      };
    }
    createSetSettingsAction(s) {
      return makeAction("createSetSettingsAction", () => {
        // frameSizeIgnored: what 27.1 build 21 did on a populated clone (22 Oct): the frame size stays
        if (!knobs.frameSizeIgnored) this._size = { width: s._size.width, height: s._size.height };
        if (s._flags) this._maxRenderQuality = s._flags.maxRenderQuality;
      });
    }
    async getTimebase() { // ticks per frame: 59.94 fps unless a knob says otherwise
      if (knobs.noTimebase) throw new Error("mock: no timebase");
      return knobs.timebase !== undefined ? knobs.timebase : "4237833600";
    }
    async getEndTime() {
      let end = 0;
      for (const t of this._video.concat(this._audio)) for (const it of t.items) end = Math.max(end, it._end);
      return TickTime.createWithSeconds(end);
    }
    async getVideoTrackCount() {
      if ((knobs.failTrackCount || []).indexOf("video") >= 0) throw new Error("mock: the video track count is unreadable");
      return this._video.length;
    }
    async getAudioTrackCount() {
      if ((knobs.failTrackCount || []).indexOf("audio") >= 0) throw new Error("mock: the audio track count is unreadable");
      return this._audio.length;
    }
    async getVideoTrack(i) { return this._video[i]; }
    async getAudioTrack(i) { return this._audio[i]; }
    async getCaptionTrackCount() { return 0; }
    createCloneAction() {
      return makeAction("createCloneAction", () => {
        if (knobs.cloneLost) return;
        for (let n = knobs.cloneTwice ? 2 : 1; n > 0; n--) {
          const copy = new MockSequence(`${this.name} Copy`, `${this.guid}-copy-${++idCounter}`);
          if (this._props) copy._props = Object.assign({}, this._props);
          copy._item = root.adopt(new MockClip({ name: copy.name, mediaPath: "", duration: 60, isSequence: true }));
          copy._item._sequence = copy;
          sequences.push(copy);
        }
      });
    }
  }

  /* ---------- the default project ---------- */

  const interview = new MockClip({ name: "interview.mp4", mediaPath: "/fake/media/interview.mp4", duration: 60, transcript: sampleTranscript() });
  const broll = new MockClip({ name: "broll_city.mp4", mediaPath: "/fake/media/broll_city.mp4", duration: 12 });
  const titleEn = new MockClip({ name: "title.mov", mediaPath: "/fake/media/en/title.mov", duration: 5, hasAudio: false });
  const titleCn = new MockClip({ name: "title.mov", mediaPath: "/fake/media/cn/title.mov", duration: 5, hasAudio: false });
  const seqItem = new MockClip({ name: "Sequence 01", mediaPath: "", duration: 60, isSequence: true });
  // a sequence's project item renames the sequence too
  Object.defineProperty(MockClip.prototype, "name", {
    get() { return this._sequence ? this._sequence.name : this._name; },
    set(v) { if (this._sequence) this._sequence.name = v; else this._name = v; },
  });
  const root = new MockFolder("", [interview, broll, seqItem, new MockFolder("EN", [titleEn]), new MockFolder("CN", [titleCn])]);

  let sequence = new MockSequence("Sequence 01", "harness-sequence-guid");
  sequence._item = seqItem;
  seqItem._sequence = sequence;
  const sequences = [sequence];
  // V1 / A1: the interview runs 0-60 s (A-roll); V2: a b-roll clip at 10-15 s from source 2-7 s whose
  // Motion > Rotation is keyframed at media 3 s and 5 s.
  sequence._video[0].add({ name: "interview.mp4", start: 0, end: 60, inPoint: 0, outPoint: 60, projectItem: interview,
    components: [EFFECTS["AE.ADBE Motion"](), EFFECTS["AE.ADBE Opacity"]()] });
  sequence._audio[0].add({ name: "interview.mp4", start: 0, end: 60, inPoint: 0, outPoint: 60, projectItem: interview,
    components: [EFFECTS["Internal Volume Mono"]()] });
  sequence._video[1].add({ name: "broll_city.mp4", start: 10, end: 15, inPoint: 2, outPoint: 7, projectItem: broll,
    components: [EFFECTS["AE.ADBE Motion"]([{ t: 3, value: 0 }, { t: 5, value: 90 }]), EFFECTS["AE.ADBE Opacity"]()] });
  sequence._markers.push(new MockMarker("harness marker", "Comment", 1, 0, ""));
  for (const o of knobs.seedItems || []) {  // extra clips with chosen values (names, odd times, no media)
    const kind = o.track[0] === "V" ? "_video" : "_audio";
    const media = typeof o.mediaPath === "string" ? new MockClip({ name: o.name, mediaPath: o.mediaPath, duration: 60 }) : null;
    const it = sequence[kind][Number(o.track.slice(1)) - 1].add({ name: o.name, start: o.start, end: o.end,
      inPoint: o.inPoint, outPoint: o.outPoint, speed: o.speed, projectItem: media,
      components: o.components ? o.components.map((m) => EFFECTS[m]()) : (o.graphic ? GRAPHIC_CHAIN() : undefined) });
    if (o.graphic && typeof o.layersAfterMs === "number") {  // the layer arrives later, right after Graphic Group
      const comps = it._chain._components;
      const text = comps.splice(3, 1)[0];
      if (o.layersAfterMs >= 0) {
        const timer = setTimeout(() => comps.splice(comps.indexOf(comps.find((c) => c._matchName === "AE.ADBE Graphic Group")) + 1, 0, text),
          o.layersAfterMs);
        if (timer && typeof timer.unref === "function") timer.unref();
      }
    }
    it._disabled = !!o.disabled;
  }
  if (scenario === "no-sequence") { sequence = null; sequences.length = 0; }

  function place(seq, item, at, v, a) {
    const clip = item;
    const range = clip._range || clip._subRange || { in: 0, out: clip._duration };
    const start = at.seconds;
    const end = start + (range.out - range.in);
    if (!seq._video[v] || !seq._audio[a]) throw new Error("mock: track index out of range");
    seq._video[v].add({ name: clip.name, start, end, inPoint: range.in, outPoint: range.out, projectItem: clip,
      components: [EFFECTS["AE.ADBE Motion"](), EFFECTS["AE.ADBE Opacity"]()] });
    if (clip._hasAudio) {
      seq._audio[a].add({ name: clip.name, start, end, inPoint: range.in, outPoint: range.out, projectItem: clip,
        components: [EFFECTS["Internal Volume Mono"]()] });
    }
  }

  const editors = new Map();
  function editorFor(seq) {
    if (editors.has(seq)) return editors.get(seq);
    const ed = {
      createOverwriteItemAction(item, at, v, a) {
        noteTime("createOverwriteItemAction", "at", at);
        return makeAction("createOverwriteItemAction", () => place(seq, item, at, v, a));
      },
      createInsertProjectItemAction(item, at, v, a) {
        noteTime("createInsertProjectItemAction", "at", at);
        return makeAction("createInsertProjectItemAction", () => place(seq, item, at, v, a));
      },
      createRemoveItemsAction(sel) {
        if (!sel || !sel._valid) throw violation("selection used outside its createEmptySelection callback");
        const items = sel._items.slice();
        return makeAction("createRemoveItemsAction", () => {
          for (const it of items) {
            const i = it._track.items.indexOf(it);
            if (i >= 0) it._track.items.splice(i, 1);
          }
        });
      },
      // (trackItem, timeOffset, videoTrackOffset, audioTrackOffset, alignToVideo, isInsert): only that item
      createCloneTrackItemAction(it, offset, vOff, aOff, alignToVideo, isInsert) {
        const src = it._track;
        const tracks = src.kind === "video" ? seq._video : seq._audio;
        const dest = tracks[src.index + (src.kind === "video" ? vOff : aOff)];
        if (!dest) throw new Error("mock: clone destination track out of range");
        const by = offset.seconds;
        noteTime("createCloneTrackItemAction", "offset", offset);
        record.clones.push({ kind: src.kind, from: src.index, to: dest.index, offset: by, vOff, aOff, alignToVideo, isInsert });
        return makeAction("createCloneTrackItemAction", () => {
          const c = it.copy();
          c._start += by;
          c._end += by;
          c._track = dest;
          dest.clear(c._start, c._end);
          dest.items.push(c);
          dest.items.sort((x, y) => x._start - y._start);
        });
      },
      insertMogrtFromPath(p, at, v) {
        if (lockDepth <= 0) throw violation("insertMogrtFromPath called outside lockedAccess");
        const start = at.seconds;
        const it = seq._video[v].add({ name: "Title.mogrt", start, end: start + 5, inPoint: 0, outPoint: 5,
          components: [new MockComponent("AE.ADBE Capsule", "Title", [new MockParam("Source Text", new ppro.MogrtText())]),
            EFFECTS["AE.ADBE Motion"](), EFFECTS["AE.ADBE Opacity"]()] });
        return [it];
      },
    };
    if (!old) {
      ed.createRazorTrackAction = function () { return makeAction("createRazorTrackAction", () => {}); };
      ed.createRazorAllTracksAction = function () { return makeAction("createRazorAllTracksAction", () => {}); };
    }
    editors.set(seq, ed);
    return ed;
  }

  const project = {
    name: knobs.projectName || "PAB_TEST_harness",
    path: "/fake/PAB_TEST_harness.prproj",
    guid: "harness-project-guid",
    lockedAccess(fn) {
      lockDepth += 1;
      let r;
      try { r = fn(); } finally { lockDepth -= 1; }
      if (r && typeof r.then === "function") throw violation("lockedAccess callback returned a Promise");
    },
    executeTransaction(cb, label) {
      if (lockDepth <= 0) throw violation("executeTransaction called outside lockedAccess");
      const added = [];
      const compound = {
        addAction(a) {
          if (!a || !a.__mockAction) throw new Error("mock: addAction needs an Action");
          added.push(a);
          return true;
        },
      };
      const r = cb(compound);
      if (r && typeof r.then === "function") throw violation("executeTransaction callback returned a Promise");
      if ((knobs.failTransactions || []).some((s) => String(label).indexOf(s) >= 0)) {
        record.failedTransactions.push(label);
        return false;
      }
      for (const a of added) a.apply();
      record.transactions.push(label);
      return true;
    },
    async getActiveSequence() { return sequence; },
    async getSequences() { return sequences.slice(); },
    async setActiveSequence(seq) { if (sequences.indexOf(seq) < 0) return false; sequence = seq; return true; },
    async deleteSequence(seq) {
      const i = sequences.indexOf(seq);
      if (i < 0 || knobs.deleteSequenceFails) return false;
      sequences.splice(i, 1);
      if (seq._item && seq._item._parent) seq._item._parent._items.splice(seq._item._parent._items.indexOf(seq._item), 1);
      return true;
    },
    async save() { record.saves = (record.saves || 0) + 1; return true; },
    async getRootItem() { return root; },
    async getInsertionBin() { return root; },
    async importFiles(paths, suppressUI, bin) {
      if (!bin || bin.__kind !== "folder") throw new Error("Illegal Parameter type");
      record.imports.push({ paths: paths.slice(), bin: bin.name });
      if (knobs.importFails) return false;
      for (const p of paths) bin._items.push(new MockClip({ name: path.basename(p), mediaPath: p, duration: 10 }));
      return true;
    },
  };

  /* ---------- transcripts ---------- */

  class TextSegmentsObject {
    constructor(doc, viaCallback) { this.__segments = true; this._doc = doc; this._viaCallback = !!viaCallback; }
    exportToJSON() { throw violation("TextSegments#exportToJSON called on a parsed transcript"); }
  }

  const Transcript = {
    hasTranscript(clip) { return !!(clip && clip._transcript); },
    async exportToJSON(clip) {
      if (!clip || !clip._transcript) throw new Error("mock: clip has no transcript");
      return JSON.stringify(clip._transcript);
    },
    importFromJSON(json) { return new TextSegmentsObject(JSON.parse(json), false); },
    createImportTextSegmentsAction(segments, clip) {
      if (!segments || !segments.__segments) throw new Error("Illegal Parameter type");
      return makeAction("createImportTextSegmentsAction", () => {
        const empty = scenario === "transcript-empty-import" ||
          (scenario === "transcript-callback-import" && !segments._viaCallback);
        clip._transcript = empty ? emptyPlaceholder() : segments._doc;
      });
    },
    async querySupportedLanguages() { return ["en-US", "en-GB", "de-DE", "fr-FR", "es-ES", "ja-JP", "ko-KR", "zh-CN"]; },
    async isLanguagePackAvailable(lang) { return MISSING_LANGUAGE_PACKS.indexOf(lang) < 0; },
  };
  if (!old) {
    Transcript.transcribeClipProjectItem = function (clip, options) {
      record.sttCalls.push({ item: clip && clip.name, options: options === undefined ? null : options });
      if (scenario === "stt-pending") return new Promise(() => {});
      if (knobs.sttReject) return Promise.reject(new Error("mock: Speech-to-Text failed"));
      return new Promise((resolve) => setTimeout(() => { clip._transcript = sampleTranscript(["Speech", "to", "text"]); resolve(true); }, 20));
    };
  }

  const TextSegments = {
    importFromJSON(json, cb) {
      record.textSegmentsCalls += 1;
      const segments = new TextSegmentsObject(JSON.parse(json), true);
      setTimeout(() => cb(segments), 0);
    },
  };

  /* ---------- event targets and the encoder (export jobs) ---------- */

  /* Listeners per target, by route ("EventManager" or "own"): {route, name, fn}. */
  const listenersOf = (target) => target.__listeners || (target.__listeners = []);
  function addListener(target, route, name, fn) {
    if (typeof name !== "string" || typeof fn !== "function") throw new Error("mock: addEventListener(name, fn)");
    listenersOf(target).push({ route, name, fn });
    record.listeners.push({ op: "add", route, name });
  }
  function removeListener(target, route, name, fn) {
    const list = listenersOf(target);
    const i = list.findIndex((l) => l.route === route && l.name === name && l.fn === fn);
    if (i >= 0) list.splice(i, 1);
    record.listeners.push({ op: "remove", route, name, found: i >= 0 });
  }
  function renderEvent(type, fields) {
    const proto = {};
    Object.defineProperty(proto, "type", { get() { return type; } });
    for (const k of Object.keys(fields || {})) Object.defineProperty(proto, k, { get() { return fields[k]; } });
    return Object.create(proto);
  }
  /* Deliver an event to the target's listeners on the routes `deliver` names (a listener's throw stops nothing). */
  function fire(target, name, event, deliver) {
    const routes = deliver === "both" ? ["EventManager", "own"] : deliver === "encoder" ? ["own"]
      : deliver === "none" ? [] : ["EventManager"];
    for (const l of listenersOf(target).slice()) {
      if (l.name === name && routes.indexOf(l.route) >= 0) {
        try { l.fn(event); } catch (e) { record.violations.push(`a listener threw: ${e && e.message}`); }
      }
    }
  }
  project.addEventListener = function (name, fn) { addListener(project, "own", name, fn); };
  project.removeEventListener = function (name, fn) { removeListener(project, "own", name, fn); };

  const encoder = {
    addEventListener(name, fn) { addListener(encoder, "own", name, fn); },
    removeEventListener(name, fn) { removeListener(encoder, "own", name, fn); },
    async exportSequence(seq, type, outputFile, presetFile, exportFull) {
      record.exports.push({ kind: "sequence", type, outputPath: outputFile, preset: presetFile, exportFull });
      if (knobs.exportThrows || missingFolder(presetFile)) throw new Error("Unable to initialize export!");
      const ok = !knobs.exportFails && !missingFolder(outputFile);
      const job = knobs.exportJob;
      if (!job || !ok) return ok;
      return renderJob(job);
    },
  };

  /* A render that takes time (knob exportJob): progress events, then the result's event and the promise. */
  function renderJob(job) {
    const step = job.stepMs || 50;
    const deliver = job.deliver || "EventManager";
    const result = job.result || "complete";
    const silent = result === "silent";
    const values = job.progress || [];
    for (const x of job.extraEvents || []) {
      setTimeout(() => fire(x.target === "project" ? project : encoder, x.name, renderEvent(x.name, x.fields || {}),
        deliver), x.atMs);
    }
    values.forEach((v, i) => setTimeout(() => {
      if (!silent) fire(encoder, "RenderProgressEvent", renderEvent("RenderProgressEvent", { progress: v }), deliver);
    }, step * (i + 1)));
    if (result === "never") return new Promise(() => {});
    return new Promise((resolve, reject) => setTimeout(() => {
      if (result === "complete") fire(encoder, "RenderCompleteEvent", renderEvent("RenderCompleteEvent", {}), deliver);
      if (result === "error") {
        fire(encoder, "RenderErrorEvent", renderEvent("RenderErrorEvent", { message: "Encoding failed (mock)" }), deliver);
      }
      if (result === "cancel") fire(encoder, "RenderCancelEvent", renderEvent("RenderCancelEvent", {}), deliver);
      if (job.projectState !== undefined) {
        fire(project, "OperationCompleteEvent.ExportMediaComplete",
          renderEvent("OperationCompleteEvent.ExportMediaComplete", { state: job.projectState }), deliver);
      }
      const value = job.resolve !== undefined ? job.resolve : result === "complete" || silent;
      if (value === "throw") reject(new Error("Encoding failed (mock throw)"));
      else resolve(value);
    }, step * (values.length + 1)));
  }

  /* ---------- AAF export (ProjectConverter.exportAAF, AAFExportOptions) ---------- */

  const AAF_DEFAULTS = {  // NOT measured: plausible values for a fresh AAFExportOptions
    audioFileFormat: 1, bitsPerSample: 16, embedAudio: false, explodeToMono: false, handleFrames: 0,
    interleaveWithoutEffects: false, mixdownVideo: false, preserveParentFolder: false, renderAudioEffects: false,
    sampleRate: 48000, trimSources: false, videoMixdownPresetPath: "",
  };
  class AAFExportOptions {
    constructor() { this._v = Object.assign({}, AAF_DEFAULTS); }
  }
  for (const key of Object.keys(AAF_DEFAULTS)) {
    const setter = "set" + key[0].toUpperCase() + key.slice(1);
    Object.defineProperty(AAFExportOptions.prototype, key, { get() { return this._v[key]; } });
    if ((knobs.aafMissingSetter || []).indexOf(setter) >= 0) continue;
    AAFExportOptions.prototype[setter] = function (value) {
      if ((knobs.aafSetterThrows || []).indexOf(setter) >= 0) throw new Error(`${setter}: Illegal Parameter type`);
      this._v[key] = value;
      return true;
    };
  }
  const aafValues = (o) => (o instanceof AAFExportOptions ? Object.assign({}, o._v) : null);

  function exportAAF(seq, outputPath, options) {
    const job = knobs.aafJob || {};
    if (job.throwSync || knobs.exportThrows) throw new Error("exportAAF: Unable to export AAF (mock)");
    record.exports.push({ kind: "aaf", outputPath, options: aafValues(options), argc: arguments.length });
    if (knobs.exportFails || missingFolder(outputPath)) return Promise.resolve(false);
    if (job.holdMs === undefined) return Promise.resolve(job.result === undefined ? true : job.result);
    if (job.holdMs < 0) return new Promise(() => {});
    return new Promise((resolve, reject) => setTimeout(() => {
      if (job.result === "throw") reject(new Error("exportAAF failed (mock)"));
      else resolve(job.result === undefined ? true : job.result);
    }, job.holdMs));
  }

  /* ---------- the ppro module ---------- */

  let projectOpened = false;
  const ppro = {
    Project: {
      async getActiveProject() { return scenario === "no-project" && !projectOpened ? null : project; },
      async open(path, options) {                         // Project.open: that file becomes the active project
        record.projectOpens.push({ path, dialogs: options ? options.dialogs : null });
        if (knobs.openProjectFails) throw new Error("Failed to open the project");
        if (!knobs.openProjectIgnored) {
          projectOpened = true;
          project.path = path;
          project.name = path.split(/[\\/]/).pop();
        }
        return project;
      },
    },
    OpenProjectOptions: class {
      constructor() { this.dialogs = {}; }
      setShowConvertProjectDialog(v) { this.dialogs.convert = v; return this; }
      setShowLocateFileDialog(v) { this.dialogs.locate = v; return this; }
      setShowWarningDialog(v) { this.dialogs.warning = v; return this; }
    },
    SequenceEditor: { getEditor(seq) { return editorFor(seq); } },
    TrackItemSelection: {
      createEmptySelection(cb) {
        const sel = {
          _valid: true, _items: [],
          addItem(it) {
            if (!this._valid) throw violation("selection used outside its createEmptySelection callback");
            this._items.push(it);
            return true;
          },
        };
        let r;
        try { r = cb(sel); } finally { sel._valid = false; }
        if (r && typeof r.then === "function") throw violation("createEmptySelection callback returned a Promise");
      },
    },
    Markers: {
      async getMarkers(seq) {
        return {
          getMarkers() { return seq._markers.slice(); },
          createMoveMarkerAction(m, t) { return makeAction("createMoveMarkerAction", () => { m._start = t.seconds; }); },
          createRemoveMarkerAction(m) {
            return makeAction("createRemoveMarkerAction", () => { const i = seq._markers.indexOf(m); if (i >= 0) seq._markers.splice(i, 1); });
          },
          createAddMarkerAction(name, type, start, duration, comments) {
            noteTime("createAddMarkerAction", "start", start);
            return makeAction("createAddMarkerAction", () => {
              seq._markers.push(new MockMarker(name, type, start.seconds, duration.seconds, comments));
            });
          },
        };
      },
    },
    Exporter: {
      async exportSequenceFrame(seq, tick, name, dir, width, height) {
        record.exports.push({ kind: "frame", seconds: tick.seconds, name, dir, width, height });
        return !knobs.exportFails && !missingFolder(dir);
      },
    },
    EncoderManager: {
      EXPORT_IMMEDIATELY: "IMMEDIATELY",
      EVENT_RENDER_PROGRESS: "RenderProgressEvent", EVENT_RENDER_COMPLETE: "RenderCompleteEvent",
      EVENT_RENDER_ERROR: "RenderErrorEvent", EVENT_RENDER_CANCEL: "RenderCancelEvent", EVENT_RENDER_QUEUE: "RenderQueueEvent",
      getManager() { return encoder; },
    },
    EventManager: {
      addEventListener(target, name, fn) { addListener(target, "EventManager", name, fn); },
      removeEventListener(target, name, fn) { removeListener(target, "EventManager", name, fn); },
    },
    OperationCompleteEvent: { EVENT_EXPORT_MEDIA_COMPLETE: "OperationCompleteEvent.ExportMediaComplete" },
    ProjectConverter: {
      async exportAsFinalCutProXML(seq, outputPath, suppressUI) {
        if (knobs.exportThrows) throw new Error("exportAsFinalCutProXML: Unable to export (mock)");
        record.exports.push({ kind: "fcpxml", outputPath, suppressUI });
        return !knobs.exportFails && !missingFolder(outputPath);
      },
      exportAAF,
      AAF_EXPORT_AUDIO_FORMAT_AIFF: 0,
      AAF_EXPORT_AUDIO_FORMAT_WAV: 1,
      async exportAsOpenTimelineIO(seq, outputPath) {
        record.exports.push({ kind: "otio", outputPath });
        return !knobs.exportFails && !missingFolder(outputPath);
      },
    },
    TransitionFactory: {
      _names: ["AE.ADBE Cross Dissolve New", "AE.ADBE Dip To Black", "AE.Impact_Film_Dissolve"],
      async getVideoTransitionMatchNames() { return this._names.slice(); },
      createVideoTransition(name) { return this._names.indexOf(name) >= 0 ? { _matchName: name } : null; },
    },
    AddTransitionOptions: class AddTransitionOptions {
      setApplyToStart(v) { this.applyToStart = v; return this; }
      setDuration(v) { this.duration = v; return this; }
      setForceSingleSided(v) { this.forceSingleSided = v; return this; }
      setTransitionAlignment(v) { this.alignment = v; return this; }
    },
    VideoFilterFactory: {
      async getMatchNames() { return ADDABLE.slice(); },
      async getDisplayNames() { return ADDABLE.map((n) => EFFECTS[n]()._displayName); },
      async createComponent(matchName) {
        if (ADDABLE.indexOf(matchName) < 0) throw new Error(`mock: cannot create component ${matchName}`);
        return EFFECTS[matchName]();
      },
    },
    AudioFilterFactory: {
      async getDisplayNames() { return Object.keys(AUDIO_FILTERS); },
      async createComponentByDisplayName(name) { return AUDIO_FILTERS[name] ? EFFECTS[AUDIO_FILTERS[name]]() : null; },
    },
    Properties: {
      async getProperties(owner) {
        const store = owner._props || (owner._props = {});
        return {
          getValue(k) { return store[k]; },
          hasValue(k) { return Object.prototype.hasOwnProperty.call(store, k); },
          createSetValueAction(k, v) { return makeAction("createSetValueAction", () => { store[k] = v; }); },
          createClearValueAction(k) { return makeAction("createClearValueAction", () => { delete store[k]; }); },
        };
      },
    },
    SourceMonitor: {
      _item: null, _pos: 0,
      async openProjectItem(it) { this._item = it; return true; },
      async getProjectItem() { return this._item; },
      async setPosition(t) { this._pos = t.seconds; return true; },
      async getPosition() { return TickTime.createWithSeconds(this._pos); },
      async closeClip() { this._item = null; return true; },
    },
    ClipProjectItem: { cast(it) { return it && it.__kind === "clip" ? it : null; } },
    FolderItem: { cast(it) { return it && it.__kind === "folder" ? it : null; } },
    TickTime,
    PointF,
    RectF,
    Constants: {
      TrackItemType: { EMPTY: 0, CLIP: 1, TRANSITION: 2 },
      MediaType: { ANY: "ANY", VIDEO: "VIDEO", AUDIO: "AUDIO" },
      ExportType: { QUEUE_TO_AME: "QUEUE_TO_AME", QUEUE_TO_APP: "QUEUE_TO_APP", IMMEDIATELY: "IMMEDIATELY" },
      OperationCompleteState: { SUCCESS: 1, CANCELLED: 2, FAILED: 3 },  // 27.1 (api dump, 2 Nov 2026)
      TransitionPosition: { START: 1, END: 2 },  // live 27.1 b21 (api_smoke, 22 Oct 2026)
      InterpolationMode: INTERPOLATION,
      AAFExportAudioFormat: { AIFF: 0, WAV: 1 },  // 27.1 (api dump, 2 Nov 2026)
      PropertyType: { NON_PERSISTENT: 0, PERSISTENT: 1 },
    },
    Transcript,
    TextSegments,
  };
  if (knobs.noAudioFilters) delete ppro.AudioFilterFactory;
  if (!knobs.noAafOptions) ppro.AAFExportOptions = AAFExportOptions;
  if (knobs.videoDisplayNames) ppro.VideoFilterFactory.getDisplayNames = async () => knobs.videoDisplayNames.slice();
  if (!old) {
    ppro.MogrtText = class MogrtText {};
    const area = { in: 0, out: 0 };
    ppro.WorkAreaUtils = {
      async getWorkAreaInPoint() { return TickTime.createWithSeconds(area.in); },
      async getWorkAreaOutPoint() { return TickTime.createWithSeconds(area.out); },
      async setWorkAreaInOutPoints(seq, a, b) { area.in = a.seconds; area.out = b.seconds; return true; },
    };
    ppro.SequenceEditor.getInstalledMogrtPath = async function () { return "/fake/Adobe Premiere Pro/Essential Graphics"; };
  }

  /* ---------- the uxp module ---------- */

  const host = {
    name: "premierepro",
    version: opts.hostVersion || (old ? "26.4.0" : "27.1.0"),
    uiLocale: knobs.uiLocale || "en_US",
    applicationPath: "/fake/Adobe Premiere Pro", // must never reach a reply
  };
  if (opts.buildNumber !== undefined && opts.buildNumber !== null) host.buildNumber = opts.buildNumber;

  function fileEntry(p) {
    return {
      name: path.basename(p), nativePath: p, isFile: true, isFolder: false,
      async read(o) {
        const buf = fs.readFileSync(p);
        if (o && o.format === "binary") return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length);
        return buf.toString("utf8");
      },
    };
  }
  function folderEntry(dir) {
    return {
      name: path.basename(dir), nativePath: dir, isFile: false, isFolder: true,
      async getEntries() {
        return fs.readdirSync(dir).map((n) => {
          const p = path.join(dir, n);
          return fs.statSync(p).isDirectory() ? folderEntry(p) : fileEntry(p);
        });
      },
    };
  }

  record.openPath = [];  // uxp.shell.openPath calls: {path, developerText, result}
  record.openExternal = [];  // uxp.shell.openExternal calls: {url, developerText, result}
  const uxp = {
    host,
    storage: {
      formats: { binary: "binary", utf8: "utf8" },
      localFileSystem: {
        async getPluginFolder() {
          const dir = opts.pluginFolder || opts.pluginDir;
          if (!dir || !fs.existsSync(dir)) throw new Error("mock: no plugin folder");
          return folderEntry(dir);
        },
        async getEntryWithUrl(url) {
          let p = decodeURIComponent(String(url).replace(/^file:\/*/, ""));
          if (!/^[A-Za-z]:/.test(p)) p = "/" + p;
          // never a real pairing token (the user's, or a test broker's): the panel's token comes from the fs mock only
          if (/[\\/]\.ppro-agent-bridge([\\/]|$)/i.test(p)) throw new Error(`mock: no such file or folder: ${url}`);
          if (!fs.existsSync(p)) throw new Error(`mock: no such file or folder: ${url}`);
          return fs.statSync(p).isDirectory() ? folderEntry(p) : fileEntry(p);
        },
      },
    },
    shell: {
      async openPath(target, developerText) {
        const knob = knobs.openPath;
        const ext = path.extname(String(target)).toLowerCase();
        const result = knob && typeof knob === "object" ? (knob[ext] !== undefined ? knob[ext] : "") : (knob || "");
        record.openPath.push({ path: target, developerText, result });
        if (String(result).indexOf("throw:") === 0) throw new Error(String(result).slice(6));
        return result;
      },
      async openExternal(url, developerText) {
        const result = typeof knobs.openExternal === "string" ? knobs.openExternal : "";
        record.openExternal.push({ url, developerText, result });
        if (result.indexOf("throw:") === 0) throw new Error(result.slice(6));
        return result;
      },
    },
  };
  if (knobs.noOpenExternal) delete uxp.shell.openExternal;
  if (knobs.noShell) delete uxp.shell;
  if (knobs.noEntryWithUrl) delete uxp.storage.localFileSystem.getEntryWithUrl;

  /* UXP's os and fs modules, as far as the pairing token needs them (see the header). */
  // Paths built from parts: the release scan flags literal drive paths and the application data folder name.
  const HARNESS_HOME = ["C:", "Users", "harness"].join("\\");
  const APP_DATA = "App" + "Data";
  const home = has(knobs, "home") ? knobs.home : HARNESS_HOME;
  const pairingToken = has(knobs, "token") ? knobs.token : HARNESS_TOKEN;
  record.fsReads = [];
  const tokenFile = (p) => {
    if (!home) return false;
    const norm = (x) => decodeURI(String(x)).replace(/^file:\/*/i, "").replace(/\\/g, "/").toLowerCase();
    return norm(p) === norm(`${home}\\.ppro-agent-bridge\\token`);
  };
  const os = home && !knobs.noHomedir ? { homedir() { return home; } } : {};  // noHomedir: a UXP without it
  const fsModule = knobs.noFs ? null : {
    async readFile(p, o) {
      record.fsReads.push(String(p));
      if (pairingToken === null || !tokenFile(p)) throw new Error(`mock fs: no such file: ${p}`);
      return o && o.encoding ? `${pairingToken}\n` : Buffer.from(`${pairingToken}\n`, "utf8");
    },
  };
  if (home) {
    uxp.storage.localFileSystem.getDataFolder = async () => ({
      name: "PluginData", isFolder: true, isFile: false,
      nativePath: `${home}\\${APP_DATA}\\Roaming\\Adobe\\UXP\\PluginsStorage\\PPRO\\27\\Developer\\org.pproagentbridge.panel\\PluginData`,
    });
  }

  /* Every track item's edges in ticks (seconds are kept as float sums of exact tick times: rounding recovers them). */
  function edgeTicks() {
    const out = [];
    const tk = (s) => Math.round(s * TPS);
    if (!sequence) return out;
    for (const t of sequence._video.concat(sequence._audio)) {
      for (const it of t.items) {
        out.push({ track: (t.kind === "video" ? "V" : "A") + (t.index + 1), name: it._name,
          start: tk(it._start), end: tk(it._end), inPoint: tk(it._in), outPoint: tk(it._out) });
      }
    }
    return out;
  }

  return {
    ppro, uxp, os, fs: fsModule, pairingToken, record, scenario, project, sequence: () => sequence,
    sequences: () => sequences.slice(), root, edgeTicks,
  };
}

module.exports = { createMocks, SCENARIOS, TPS, LEVEL_0DB, sampleTranscript, HARNESS_TOKEN };
