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
 * "4237833600" = 59.94), noTimebase (getTimebase throws). Exporters also fail for paths containing /no/such/
 * (a missing folder); EncoderManager#exportSequence throws "Unable to initialize export!" for such a preset path.
 *
 * Documented-only calls (api_smoke exercises them; semantics from Adobe's docs, NOT measured): marker setters /
 * move / remove, transition removal, component insert / remove, keyframe removal / interpolation (values interpolate
 * linearly between keys unless a key holds), sequence list / clone / activate / delete / settings / in-out /
 * selection / player position, work area, track rename, clip rename / disable, bin rename / colour / move / remove,
 * subclips, relinking, custom Properties, project save, FCP XML / AAF export, filter name lists, audio filters by
 * display name, MOGRT insertion, the Source monitor. Knob projectName renames the project.
 * Track items follow the semantics measured on Premiere 27.1 build 17: createSetStartAction /
 * createSetInPointAction trim the head (start and in point move by the same delta, end stays),
 * createSetEndAction / createSetOutPointAction trim the tail, createMoveAction moves BY its time (relative).
 * SequenceEditor#createCloneTrackItemAction copies only the given item (effects included) offset later,
 * overwriting the destination. Like Premiere, sequence times are kept as given (never snapped to frames);
 * record.times lists the ticks every time-taking action received, edgeTicks() every item's edges in ticks. Audio clips carry the measured mono chain: one "Internal Volume Mono"
 * component, param 0 bypass (boolean), param 1 Level (linear; 0 dB = 0.17782793939113617).
 */
const fs = require("fs");
const path = require("path");

const TPS = 254016000000;
const EPS = 1e-4; // seconds: overlap tolerance of the mock timeline
const LEVEL_0DB = 0.17782793939113617; // Premiere's linear audio Level at 0 dB (measured, 27.1 b17)
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
    exports: [], imports: [], textSegmentsCalls: 0, clones: [], times: [],
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
    record.actionsCreated += 1;
    return { __mockAction: true, kind, apply };
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
  const copyValue = (v) => (v instanceof PointF ? new PointF(v.x, v.y) : v);

  class Keyframe {
    constructor(value) { this.value = value; this.position = TickTime.createWithSeconds(0); }
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
      p._keys = this._keys.map((k) => ({ ticks: k.ticks, value: copyValue(k.value) }));
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
            if (a.mode === "HOLD" || typeof a.value !== "number" || typeof b.value !== "number") return a.value;
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
      return makeAction("createAddKeyframeAction", () => {
        this._keys = this._keys.filter((k) => k.ticks !== ticks);
        this._keys.push({ ticks, value });
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
    // audio: the chain measured on a mono clip (27.1 b17); stereo clips are unprobed
    "Internal Volume Mono": () => new MockComponent("Internal Volume Mono", "Volume", [
      new MockParam("Bypass", false), new MockParam("Level", LEVEL_0DB)]),
  };
  const ADDABLE = ["AE.ADBE Gaussian Blur 2", "AE.ADBE Crop", "AE.ADBE Lumetri"];
  const AUDIO_FILTERS = { "DeNoise": "AE.ADBE DeNoise" };

  class MockChain {
    constructor(components) { this._components = components; }
    getComponentCount() { return this._components.length; }
    getComponentAtIndex(i) { return this._components[i] || null; }
    createAppendComponentAction(comp) {
      return makeAction("createAppendComponentAction", () => { this._components.push(comp); });
    }
    createInsertComponentAction(comp, index) {
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
      this._fps = null;
      this._transcript = o.transcript || null;
    }
    getId() { return this._id; }
    getParentBin() { return this._parent || null; }
    async getMediaFilePath() { return this._media; }
    async isSequence() { return this._isSequence; }
    async canChangeMediaPath() { return true; }
    async changeMediaFilePath(p) { this._media = p; return true; }
    async refreshMedia() { return true; }
    async isOffline() { return false; }
    async getFootageInterpretation() { const fps = this._fps || 59.94; return { getFrameRate() { return fps; }, getPixelAspectRatio() { return 1; } }; }
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
    createSubClipAction(name, start, end) {
      return makeAction("createSubClipAction", () => {
        const sub = new MockClip({ name, mediaPath: this._media, duration: end.seconds - start.seconds });
        (this._parent || root).adopt(sub);
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
    createRemoveVideoTransitionAction(position) {
      return makeAction("createRemoveVideoTransitionAction", () => {
        const cut = position === 0 ? this._start : this._end;
        this._track.transitions = this._track.transitions.filter((t) => !(t._start <= cut + EPS && t._end >= cut - EPS));
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
    async getTrackItems(type) { return type === 2 ? this.transitions.slice() : this.items.slice(); }
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
      this._selection = sel._items.slice();
      return true;
    }
    async clearSelection() { this._selection = []; return true; }
    async getProjectItem() { return this._item; }
    async getSettings() {
      const size = { width: this._size.width, height: this._size.height };
      return {
        _size: size,
        getVideoFrameRect() { return { width: size.width, height: size.height }; },
        setVideoFrameRect(r) { size.width = r.width; size.height = r.height; return true; },
        getVideoFrameRate() { return 59.94; },
        getAudioSampleRate() { return 48000; },
      };
    }
    createSetSettingsAction(s) {
      return makeAction("createSetSettingsAction", () => { this._size = { width: s._size.width, height: s._size.height }; });
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
    async getVideoTrackCount() { return this._video.length; }
    async getAudioTrackCount() { return this._audio.length; }
    async getVideoTrack(i) { return this._video[i]; }
    async getAudioTrack(i) { return this._audio[i]; }
    async getCaptionTrackCount() { return 0; }
    createCloneAction() {
      return makeAction("createCloneAction", () => {
        const copy = new MockSequence(`${this.name} Copy`, `${this.guid}-copy-${++idCounter}`);
        copy._item = root.adopt(new MockClip({ name: copy.name, mediaPath: "", duration: 60, isSequence: true }));
        copy._item._sequence = copy;
        sequences.push(copy);
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
  if (scenario === "no-sequence") { sequence = null; sequences.length = 0; }

  function place(seq, item, at, v, a) {
    const clip = item;
    const range = clip._range || { in: 0, out: clip._duration };
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
      if (i < 0) return false;
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

  /* ---------- the ppro module ---------- */

  const ppro = {
    Project: { async getActiveProject() { return scenario === "no-project" ? null : project; } },
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
      getManager() {
        return {
          async exportSequence(seq, type, outputFile, presetFile, exportFull) {
            record.exports.push({ kind: "sequence", type, outputPath: outputFile, preset: presetFile, exportFull });
            if (knobs.exportThrows || missingFolder(presetFile)) throw new Error("Unable to initialize export!");
            return !knobs.exportFails && !missingFolder(outputFile);
          },
        };
      },
    },
    ProjectConverter: {
      async exportAsFinalCutProXML(seq, outputPath) { record.exports.push({ kind: "fcpxml", outputPath }); return !missingFolder(outputPath); },
      async exportAAF(seq, outputPath) { record.exports.push({ kind: "aaf", outputPath }); return !missingFolder(outputPath); },
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
    Constants: {
      TrackItemType: { EMPTY: 0, CLIP: 1, TRANSITION: 2 },
      MediaType: { ANY: "ANY", VIDEO: "VIDEO", AUDIO: "AUDIO" },
      ExportType: { QUEUE_TO_AME: "QUEUE_TO_AME", QUEUE_TO_APP: "QUEUE_TO_APP", IMMEDIATELY: "IMMEDIATELY" },
      TransitionPosition: { START: 0, END: 1 },
      InterpolationMode: { LINEAR: "LINEAR", HOLD: "HOLD", BEZIER: "BEZIER" },
      PropertyType: { NON_PERSISTENT: 0, PERSISTENT: 1 },
    },
    Transcript,
    TextSegments,
  };
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
    uiLocale: "en_US",
    applicationPath: "/fake/Adobe Premiere Pro", // must never reach a reply
  };
  if (opts.buildNumber !== undefined && opts.buildNumber !== null) host.buildNumber = opts.buildNumber;

  function fileEntry(p) {
    return {
      name: path.basename(p), isFile: true, isFolder: false,
      async read(o) {
        const buf = fs.readFileSync(p);
        if (o && o.format === "binary") return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length);
        return buf.toString("utf8");
      },
    };
  }
  function folderEntry(dir) {
    return {
      name: path.basename(dir), isFile: false, isFolder: true,
      async getEntries() {
        return fs.readdirSync(dir).map((n) => {
          const p = path.join(dir, n);
          return fs.statSync(p).isDirectory() ? folderEntry(p) : fileEntry(p);
        });
      },
    };
  }

  const uxp = {
    host,
    storage: {
      formats: { binary: "binary", utf8: "utf8" },
      localFileSystem: {
        async getPluginFolder() {
          if (!opts.pluginDir || !fs.existsSync(opts.pluginDir)) throw new Error("mock: no plugin folder");
          return folderEntry(opts.pluginDir);
        },
      },
    },
  };

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

  return { ppro, uxp, record, scenario, project, sequence: () => sequence, sequences: () => sequences.slice(), root, edgeTicks };
}

module.exports = { createMocks, SCENARIOS, TPS, LEVEL_0DB, sampleTranscript };
