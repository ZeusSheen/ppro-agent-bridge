/*
 * project: the project panel tree (walk, name/bin matching, bins) and the Speech-to-Text job table.
 * Every helper takes the project it works on; nothing re-fetches the active project mid-handler.
 * Factory module.   deps: { ppro, core }
 */
module.exports = function (deps) {
  const ppro = deps.ppro;
  const core = deps.core;
  const BridgeError = core.BridgeError;
  const tryGet = core.tryGet;

  /* Walk the project panel tree. Returns {item, clip, binPath} entries: `item` is
   * the raw ProjectItem (what sequence editor actions expect), `clip` is the
   * ClipProjectItem cast (what transcript / in-out actions expect), `binPath` is
   * "" for the root or "Bin/Sub bin". */
  async function walkProject(project) {
    const root = await project.getRootItem();
    const queue = (await root.getItems()).map((item) => ({ item, binPath: "" }));
    const found = [];
    for (let i = 0; i < queue.length; i++) {
      const { item, binPath } = queue[i];
      const clip = ppro.ClipProjectItem.cast(item);
      if (clip) {
        found.push({ item, clip, binPath });
      } else {
        const folder = ppro.FolderItem.cast(item);
        if (folder) {
          const path = binPath ? `${binPath}/${folder.name}` : folder.name;
          for (const sub of await folder.getItems()) queue.push({ item: sub, binPath: path });
        }
      }
    }
    return found;
  }

  /* Project items called `name`, narrowed to one bin when binPath is given ("" = root). */
  function matchClips(entries, name, binPath) {
    return entries.filter((e) => e.clip.name === name && (typeof binPath !== "string" || e.binPath === binPath));
  }

  const itemRef = (e) => (e.binPath ? `${e.binPath}/` : "") + e.clip.name;

  /* The one item to act on. Same-named items pointing at different media are ambiguous:
   * placing the wrong one (e.g. the other language's render) would still "verify". */
  async function resolveClip(project, name, binPath, entries) {
    const all = entries || (await walkProject(project));
    const matches = matchClips(all, name, binPath);
    if (!matches.length) {
      const where = typeof binPath === "string" ? ` in bin "${binPath}"` : "";
      throw new BridgeError("NOT_FOUND", `No clip named "${name}"${where}. Clips: ` + all.map(itemRef).join(" | "));
    }
    if (matches.length > 1) {
      const paths = new Set();
      for (const m of matches) paths.add(await tryGet(() => m.clip.getMediaFilePath()));
      if (paths.size > 1) {
        throw new BridgeError("AMBIGUOUS", `${matches.length} project items are named "${name}" with different media (` +
          matches.map(itemRef).join(", ") + "). Name one with its bin path.");
      }
    }
    return matches[0];
  }

  /* Find a top-level bin by name, creating it if missing (one labelled undo step). */
  async function resolveBin(project, binName, ctx) {
    const root = await project.getRootItem();
    const find = async () => {
      for (const it of await root.getItems()) {
        const f = ppro.FolderItem.cast(it);
        if (f && f.name === binName) return f;
      }
      return null;
    };
    let bin = await find();
    if (!bin) {
      core.runTransaction(project, `Agent: create bin "${binName}"`, () => root.createBinAction(binName, false), ctx);
      bin = await find();
      if (!bin) throw new BridgeError("INTERNAL", `Created bin "${binName}" but could not find it afterwards.`);
    }
    return bin;
  }

  /* A stable key for one project item: its id where the build has getId, else bin path + media. */
  async function clipKey(entry) {
    const id = core.isFn(entry.item, "getId") ? await tryGet(() => entry.item.getId()) : null;
    if (id !== null && id !== undefined && id !== "") return `id:${id}`;
    return `ref:${itemRef(entry)}|${await tryGet(() => entry.clip.getMediaFilePath(), "")}`;
  }

  /* Speech-to-Text jobs started by transcribe_clip (lost on panel reload). */
  function createJobTable() {
    const list = [];
    let counter = 0;
    return {
      add(fields) {
        counter += 1;
        const job = Object.assign({
          jobId: `stt-${Date.now().toString(36)}-${counter}`,
          state: "running", error: null, startedAt: Date.now(), finishedAt: null,
        }, fields);
        list.push(job);
        return job;
      },
      remove(job) {
        const i = list.indexOf(job);
        if (i >= 0) list.splice(i, 1);
      },
      runningFor(key) {
        for (const j of list) if (j.key === key && j.state === "running") return j;
        return null;
      },
      all() { return list.slice(); },
    };
  }

  return { walkProject, matchClips, itemRef, resolveClip, resolveBin, clipKey, jobs: createJobTable() };
};
