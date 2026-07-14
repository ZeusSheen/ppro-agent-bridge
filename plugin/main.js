/*
 * Agent Bridge — UXP panel for Premiere Pro (>= 25.6)
 *
 * Role: dumb executor. Connects OUT to a local broker (UXP plugins cannot
 * listen on sockets), receives whitelisted JSON commands, runs them against
 * the Premiere UXP DOM, returns JSON results.
 *
 * Security posture:
 *  - Command whitelist only. There is deliberately NO eval / runScript action.
 *  - Broker is expected to bind 127.0.0.1 only.
 *
 * API usage patterns verified against AdobeDocs/uxp-premiere-pro-samples.
 */

const ppro = require("premierepro");

const BROKER_URL = "ws://127.0.0.1:8017";
const RECONNECT_MS = 3000;

let ws = null;
let reconnectTimer = null;

/* ---------- tiny UI helpers ---------- */

const dot = document.getElementById("dot");
const statusText = document.getElementById("statusText");
const logEl = document.getElementById("log");

function setStatus(state, text) {
  dot.className = state; // "", "connecting", "connected"
  statusText.textContent = text;
}

function log(msg, cls) {
  const line = document.createElement("div");
  if (cls) line.className = cls;
  const t = new Date().toTimeString().slice(0, 8);
  line.textContent = `[${t}] ${msg}`;
  logEl.appendChild(line);
  while (logEl.childNodes.length > 300) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}

/* ---------- command handlers (the whitelist) ---------- */

async function requireProject() {
  const project = await ppro.Project.getActiveProject();
  if (!project) throw new Error("No active project open in Premiere.");
  return project;
}

async function requireSequence() {
  const project = await requireProject();
  const sequence = await project.getActiveSequence();
  if (!sequence) throw new Error("Project has no active sequence.");
  return { project, sequence };
}

const handlers = {
  /* Liveness check. */
  async ping() {
    const uxp = require("uxp");
    return {
      pong: true,
      host: uxp.host ? `${uxp.host.name} ${uxp.host.version}` : "unknown",
      plugin: "ppro-agent-bridge 0.1.0",
    };
  },

  /* Basic project info — the Phase 1 acceptance test. */
  async get_project() {
    const project = await requireProject();
    const out = { name: project.name };
    try { out.path = project.path; } catch (e) { out.path = null; }
    try {
      const seq = await project.getActiveSequence();
      out.activeSequence = seq ? seq.name : null;
    } catch (e) {
      out.activeSequence = null;
    }
    return out;
  },

  /* Active sequence info incl. player position in seconds. */
  async get_sequence() {
    const { sequence } = await requireSequence();
    const out = { name: sequence.name };
    try {
      const pos = await sequence.getPlayerPosition(); // TickTime
      out.playerPositionSeconds = pos ? pos.seconds : null;
    } catch (e) {
      out.playerPositionSeconds = null;
    }
    return out;
  },

  /*
   * Export one frame of the active sequence as an image.
   * params: { dir (required), name = "frame.png",
   *           atSeconds = player position, width = 640, height = 360 }
   *
   * Known Adobe bug (confirmed in official sample, comment reads
   * "We do double extension"): output may land as <name>.png even when
   * <name> already ends in .png. We return both candidate paths and let
   * the CLI resolve which one exists.
   */
  async export_frame(params) {
    if (!params || !params.dir) throw new Error("export_frame requires params.dir");
    const { sequence } = await requireSequence();

    const name = params.name || "frame.png";
    const width = params.width || 640;
    const height = params.height || 360;

    let tick;
    if (typeof params.atSeconds === "number") {
      tick = ppro.TickTime.createWithSeconds(params.atSeconds);
    } else {
      tick = await sequence.getPlayerPosition();
    }

    const ok = await ppro.Exporter.exportSequenceFrame(
      sequence, tick, name, params.dir, width, height
    );
    if (!ok) throw new Error("exportSequenceFrame returned false");

    return {
      exported: true,
      dir: params.dir,
      candidates: [name, name + ".png"], // double-extension bug tolerance
      width, height,
      atSeconds: typeof params.atSeconds === "number" ? params.atSeconds : "playerPosition",
    };
  },

  /*
   * Export the active sequence as OpenTimelineIO — the structure layer.
   * params: { outputPath (required, full file path ending .otio) }
   */
  async export_otio(params) {
    if (!params || !params.outputPath) throw new Error("export_otio requires params.outputPath");
    const { sequence } = await requireSequence();
    const ok = await ppro.ProjectConverter.exportAsOpenTimelineIO(
      sequence, params.outputPath
    );
    if (!ok) throw new Error("exportAsOpenTimelineIO returned false");
    return { exported: true, outputPath: params.outputPath };
  },

  /*
   * List every clip in the project and whether it carries a transcript.
   * Walks the project panel tree (folders included), same traversal as
   * Adobe's official sample.
   */
  async list_transcripts() {
    const entries = await findClips();
    const out = [];
    for (const e of entries) {
      let has = false;
      try { has = ppro.Transcript.hasTranscript(e.clip); } catch (err) { /* leave false */ }
      out.push({ name: e.clip.name, hasTranscript: has });
    }
    return { clips: out };
  },

  /*
   * Export a clip's transcript as Adobe Transcript JSON — the semantic layer.
   * params: { itemName? }  If omitted, uses the first clip that has one.
   * Returns the raw JSON string; the CLI writes it to disk.
   */
  async export_transcript(params) {
    const entries = await findClips();
    let target = null;

    if (params && params.itemName) {
      const hit = entries.find((e) => e.clip.name === params.itemName) || null;
      if (!hit) {
        throw new Error(
          `No clip named "${params.itemName}". Clips: ` +
          entries.map((e) => e.clip.name).join(" | ")
        );
      }
      if (!ppro.Transcript.hasTranscript(hit.clip)) {
        throw new Error(`Clip "${params.itemName}" has no transcript. Run Speech to Text in the Text panel first.`);
      }
      target = hit.clip;
    } else {
      for (const e of entries) {
        try {
          if (ppro.Transcript.hasTranscript(e.clip)) { target = e.clip; break; }
        } catch (err) { /* skip */ }
      }
      if (!target) throw new Error("No clip in this project has a transcript. Run Speech to Text in the Text panel first.");
    }

    const transcriptJson = await ppro.Transcript.exportToJSON(target);
    if (!transcriptJson) throw new Error("exportToJSON returned nothing.");
    return { itemName: target.name, transcriptJson: String(transcriptJson) };
  },

  /*
   * Import an Adobe-format transcript JSON onto a clip (e.g. produced by
   * local Whisper via transcribe.py). params: { itemName, transcriptJson }
   */
  async import_transcript(params) {
    if (!params || !params.itemName || !params.transcriptJson) {
      throw new Error("import_transcript requires params.itemName and params.transcriptJson");
    }
    const project = await requireProject();
    const entries = await findClips();
    const hit = entries.find((e) => e.clip.name === params.itemName);
    if (!hit) {
      throw new Error(`No clip named "${params.itemName}". Clips: ` + entries.map((e) => e.clip.name).join(" | "));
    }
    let success = false;
    project.lockedAccess(() => {
      success = project.executeTransaction((compoundAction) => {
        const action = ppro.Transcript.createImportTextSegmentsAction(
          ppro.Transcript.importFromJSON(params.transcriptJson),
          hit.clip
        );
        compoundAction.addAction(action);
      }, `Import transcript onto ${params.itemName}`);
    });
    if (!success) throw new Error("Import transaction returned false.");
    return { imported: true, itemName: params.itemName };
  },

  /* ============ Phase 3: write operations ============ */

  /*
   * Import media files into the project root.
   * params: { paths: [absolute file paths] }
   */
  async import_media(params) {
    if (!params || !Array.isArray(params.paths) || params.paths.length === 0) {
      throw new Error("import_media requires params.paths (non-empty array).");
    }
    const project = await requireProject();
    const ok = await project.importFiles(
      params.paths,
      true,       // suppressUI
      undefined,  // project root bin
      false       // importAsNumberedStills
    );
    if (!ok) throw new Error("importFiles returned false.");
    return { imported: true, count: params.paths.length };
  },

  /*
   * Place a clip (or a source range of it — a "sentence") on the timeline.
   * params: {
   *   itemName (required)      — project item name, exact match
   *   atSeconds = 0            — sequence time to place at
   *   videoTrack = 0, audioTrack = 0   (0-based: 0 = V1/A1)
   *   mode = "overwrite" | "insert"
   *   inSeconds?, outSeconds?  — source range; omit both to place whole clip
   *   undoLabel?               — name shown in Premiere's undo history
   * }
   * With in/out given, runs three transactions: set source in/out →
   * place → clear in/out. Each is one undo step, labelled.
   */
  async insert_clip(params) {
    if (!params || !params.itemName) throw new Error("insert_clip requires params.itemName");
    const project = await requireProject();
    const sequence = await project.getActiveSequence();
    if (!sequence) throw new Error("No active sequence.");

    const entries = await findClips();
    const hit = entries.find((e) => e.clip.name === params.itemName);
    if (!hit) {
      throw new Error(`No clip named "${params.itemName}". Clips: ` + entries.map((e) => e.clip.name).join(" | "));
    }

    const atSeconds = params.atSeconds || 0;
    const vTrack = params.videoTrack || 0;
    const aTrack = params.audioTrack || 0;
    const mode = params.mode === "insert" ? "insert" : "overwrite";
    const label = params.undoLabel || `Claude: place ${params.itemName} @${atSeconds}s`;
    const hasRange = typeof params.inSeconds === "number" && typeof params.outSeconds === "number";

    if (hasRange && params.outSeconds <= params.inSeconds) {
      throw new Error("outSeconds must be greater than inSeconds.");
    }

    let success = false;

    const step = (name, fn) => {
      try { return fn(); }
      catch (e) { throw new Error(`[${name}] ${e && e.message ? e.message : e}`); }
    };

    if (hasRange) {
      let rangeOk = false;
      step("set source in/out", () => {
        project.lockedAccess(() => {
          rangeOk = project.executeTransaction((compoundAction) => {
            compoundAction.addAction(hit.clip.createSetInOutPointsAction(
              ppro.TickTime.createWithSeconds(params.inSeconds),
              ppro.TickTime.createWithSeconds(params.outSeconds)
            ));
          }, label + " [1/3 set range]");
        });
      });
      if (!rangeOk) throw new Error("[set source in/out] transaction returned false.");
    }

    const sequenceEditor = ppro.SequenceEditor.getEditor(sequence);
    step("place on timeline", () => {
      project.lockedAccess(() => {
        success = project.executeTransaction((compoundAction) => {
          const at = ppro.TickTime.createWithSeconds(atSeconds);
          const action = (mode === "insert")
            ? sequenceEditor.createInsertProjectItemAction(hit.item, at, vTrack, aTrack, true)
            : sequenceEditor.createOverwriteItemAction(hit.item, at, vTrack, aTrack);
          compoundAction.addAction(action);
        }, label + (hasRange ? " [2/3 place]" : ""));
      });
    });

    if (hasRange) {
      step("clear source in/out", () => {
        project.lockedAccess(() => {
          project.executeTransaction((compoundAction) => {
            compoundAction.addAction(hit.clip.createClearInOutPointsAction());
          }, label + " [3/3 clear range]");
        });
      });
    }

    if (!success) throw new Error("Placing the clip failed (transaction returned false).");
    return {
      placed: true, itemName: params.itemName, mode,
      atSeconds, videoTrack: vTrack, audioTrack: aTrack,
      sourceRange: hasRange ? [params.inSeconds, params.outSeconds] : "whole clip",
      undoLabel: label,
    };
  },
};

/* Walk the project panel tree. Returns {item, clip} pairs: `item` is the
 * raw ProjectItem (what sequence editor actions expect), `clip` is the
 * ClipProjectItem cast (what transcript / in-out actions expect). */
async function findClips() {
  const project = await requireProject();
  const root = await project.getRootItem();
  const queue = await root.getItems();
  const found = [];
  for (let i = 0; i < queue.length; i++) {
    const clip = ppro.ClipProjectItem.cast(queue[i]);
    if (clip) {
      found.push({ item: queue[i], clip });
    } else {
      const folder = ppro.FolderItem.cast(queue[i]);
      if (folder) {
        const sub = await folder.getItems();
        queue.push(...sub);
      }
    }
  }
  return found;
}

/* ---------- message plumbing ---------- */

async function handleMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch (e) {
    log(`Bad JSON from broker: ${raw}`, "err");
    return;
  }
  if (!msg.id || !msg.action) return;

  log(`<- ${msg.action}`, "cmd");
  const reply = { id: msg.id };

  const handler = handlers[msg.action];
  if (!handler) {
    reply.ok = false;
    reply.error = `Unknown action "${msg.action}". Whitelist: ${Object.keys(handlers).join(", ")}`;
  } else {
    try {
      reply.ok = true;
      reply.data = await handler(msg.params || {});
    } catch (e) {
      reply.ok = false;
      reply.error = String(e && e.message ? e.message : e);
    }
  }

  log(reply.ok ? `-> ${msg.action} ok` : `-> ${msg.action} FAILED: ${reply.error}`, reply.ok ? "ok" : "err");
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(reply));
  }
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  setStatus("connecting", `Connecting to ${BROKER_URL} ...`);

  try {
    ws = new WebSocket(BROKER_URL);
  } catch (e) {
    setStatus("", "WebSocket unavailable — check manifest network permission");
    log(String(e), "err");
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    setStatus("connected", "Connected to broker");
    log("Connected. Registering as panel.", "ok");
    ws.send(JSON.stringify({ role: "panel" }));
  };

  ws.onmessage = (evt) => handleMessage(evt.data);

  ws.onclose = () => {
    setStatus("", "Disconnected — broker not running?");
    scheduleReconnect();
  };

  ws.onerror = () => {
    /* onclose follows; nothing extra to do */
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

document.getElementById("reconnectBtn").addEventListener("click", () => {
  if (ws) { try { ws.close(); } catch (e) { /* noop */ } }
  ws = null;
  connect();
});

log("Agent Bridge panel loaded. Whitelist: " + Object.keys(handlers).join(", "));
connect();
