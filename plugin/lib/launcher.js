/*
 * launcher: start the broker from the panel (the "Start bridge" button in main.js calls launch(); nothing here runs
 * without that click).
 *
 * The launcher is a file UXP opens with uxp.shell.openPath under the manifest's launchProcess permission. UXP asks
 * the user once (the user may let it remember); openPath resolves to "" when Windows started the file, else to an
 * error string. Which file:
 *  - `launcher.json` in the plugin folder, {"path": "<absolute path>"}: scripts/build_ccx.py writes it into a .ccx
 *    (the path of start_bridge.bat in the checkout that built the package);
 *  - else start_bridge.bat next to the plugin folder: the repo layout that UXP Developer Tool loads (plugin/).
 * When UXP will not open a .bat, start_bridge.pyw beside it is tried: pythonw runs it without a console, and it opens
 * start_bridge.bat in a console window of its own (the broker window users keep open). UXP's error texts are not
 * known, so any failure of the .bat tries the .pyw: a user who declined the first consent dialog gets a second one.
 * A launcher that is not there is reported without calling openPath when its folder can be listed.
 * Factory module.   deps: { uxp, core, log }
 */
module.exports = function (deps) {
  const uxp = deps.uxp;
  const core = deps.core;
  const log = deps.log || (() => {});

  const LAUNCHER_JSON = "launcher.json";
  const DEV_LAUNCHER = "start_bridge.bat";
  const HINT = "To start it by hand, double-click start_bridge.bat in the bridge folder.";
  // The consent dialog's text (openPath's developerText), in the host's UI language as UXP asks.
  const REASON = {
    en: "Agent Bridge starts its broker: a console window opens. Keep it open while you edit.",
    zh: "Agent Bridge 将启动 broker：会打开一个命令行窗口，编辑期间请保持打开。",
  };

  /* ---------- paths (Windows and POSIX spellings; UXP gives native paths) ---------- */

  const isAbsolute = (p) => /^[A-Za-z]:[\\/]/.test(p) || /^\\\\[^\\]/.test(p) || /^\//.test(p);
  const sepOf = (p) => (p.indexOf("\\") >= 0 ? "\\" : "/");

  function parentDir(p) {
    const s = String(p).replace(/[\\/]+$/, "");
    const i = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
    return i > 0 ? s.slice(0, i) : null;
  }

  function baseName(p) {
    const s = String(p).replace(/[\\/]+$/, "");
    return s.slice(Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/")) + 1);
  }

  /* file: URLs for getEntryWithUrl, as written and percent-encoded (which one a build takes is not measured). */
  function fileUrls(p) {
    const fwd = String(p).replace(/\\/g, "/");
    const url = "file:" + (fwd.charAt(0) === "/" ? fwd : "/" + fwd);
    const encoded = encodeURI(url);
    return encoded === url ? [url] : [url, encoded];
  }

  const fileSystem = () => (uxp && uxp.storage ? uxp.storage.localFileSystem : null);

  /* true / false when the launcher's folder can be listed; null when this build cannot tell (openPath will). */
  async function exists(p) {
    const lfs = fileSystem();
    const dir = parentDir(p);
    if (!dir || !core.isFn(lfs, "getEntryWithUrl")) return null;
    let folder = null;
    for (const url of fileUrls(dir)) {
      try {
        folder = await lfs.getEntryWithUrl(url);
        if (folder) break;
      } catch (e) { /* the next spelling, or unknown */ }
    }
    if (!folder || !core.isFn(folder, "getEntries")) return null;
    try {
      const want = baseName(p).toLowerCase();
      return Array.from((await folder.getEntries()) || [])
        .some((e) => e && e.isFile && String(e.name).toLowerCase() === want);
    } catch (e) {
      return null;
    }
  }

  /* ---------- which launcher ---------- */

  /* {path, source} or {error, source?}: launcher.json in the plugin folder, else ../start_bridge.bat. */
  async function resolve() {
    const lfs = fileSystem();
    if (!core.isFn(lfs, "getPluginFolder")) return { error: "this UXP build has no localFileSystem.getPluginFolder" };
    const folder = await lfs.getPluginFolder();
    const dir = folder && typeof folder.nativePath === "string" && folder.nativePath ? folder.nativePath : null;
    const entries = core.isFn(folder, "getEntries") ? Array.from((await folder.getEntries()) || []) : [];
    const json = entries.find((e) => e && e.isFile && e.name === LAUNCHER_JSON);
    if (json) {
      const where = dir ? `${LAUNCHER_JSON} in ${dir}` : LAUNCHER_JSON;
      let doc;
      try {
        doc = JSON.parse(String(await json.read()).replace(/^﻿/, ""));
      } catch (e) {
        return { error: `${where} is not JSON (${core.errorMessage(e)})`, source: LAUNCHER_JSON };
      }
      const p = doc && typeof doc === "object" ? doc.path : undefined;
      if (typeof p !== "string" || !isAbsolute(p)) {
        const error = `${where} has no absolute "path" (expected {"path": "<absolute path>"})`;
        return { error, source: LAUNCHER_JSON };
      }
      return { path: p, source: LAUNCHER_JSON };
    }
    const parent = dir ? parentDir(dir) : null;
    if (!parent) return { error: `no ${LAUNCHER_JSON} in the plugin folder, and its location is unknown` };
    return { path: parent + sepOf(dir) + DEV_LAUNCHER, source: "next to the plugin folder" };
  }

  function reasonText() {
    const locale = String((uxp && uxp.host && uxp.host.uiLocale) || "");
    return /^zh/i.test(locale) ? REASON.zh : REASON.en;
  }

  /* "" when Windows started the file, else UXP's error text. */
  async function openPath(p) {
    const shell = uxp ? uxp.shell : null;
    if (!core.isFn(shell, "openPath")) return "this UXP build has no shell.openPath";
    try {
      const result = await shell.openPath(p, reasonText());
      return typeof result === "string" ? result : "";
    } catch (e) {
      return core.errorMessage(e) || "openPath failed";
    }
  }

  /* ---------- the click ---------- */

  /*
   * Open the launcher. Never throws; logs every step to the panel log. Returns
   * {ok: true, path, source, tried} or {ok: false, message, source?, tried}; tried = [{path, error}] in order
   * (error "" = started, "not found" = not opened because its folder listing lacks it).
   */
  async function launch() {
    let target;
    try {
      target = await resolve();
    } catch (e) {
      target = { error: `cannot read the plugin folder (${core.errorMessage(e)})` };
    }
    const tried = [];
    if (target.error) {
      const fix = target.source === LAUNCHER_JSON ? " Rebuild and reinstall the package (scripts/build_ccx.py)." : "";
      const message = `Start bridge: ${target.error}.${fix} ${HINT}`;
      log(message, "err");
      return { ok: false, message, source: target.source, tried };
    }
    const candidates = [target.path];
    if (/\.bat$/i.test(target.path)) candidates.push(target.path.replace(/\.bat$/i, ".pyw"));
    for (const p of candidates) {
      if ((await exists(p)) === false) {
        tried.push({ path: p, error: "not found" });
        continue;
      }
      log(`Start bridge: opening ${p} (${target.source}) ...`, "cmd");
      const error = await openPath(p);
      tried.push({ path: p, error });
      if (!error) {
        log(`Start bridge: Windows started ${baseName(p)}; waiting for the broker ...`, "ok");
        return { ok: true, path: p, source: target.source, tried };
      }
      log(`Start bridge: UXP could not open ${p}: ${error}`, "err");
    }
    let message;
    if (tried.length && tried.every((t) => t.error === "not found")) {
      const fix = target.source === LAUNCHER_JSON ? ` The path comes from ${LAUNCHER_JSON}: rebuild and reinstall the `
        + "package (scripts/build_ccx.py) if the bridge folder moved." : "";
      message = `Start bridge: no launcher at ${candidates.join(" or ")}.${fix} ${HINT}`;
    } else {
      message = `Start bridge: the broker was not started. ${HINT}`;
    }
    log(message, "err");
    return { ok: false, message, source: target.source, tried };
  }

  return { launch, resolve, exists, parentDir, baseName, fileUrls, reasonText, LAUNCHER_JSON, DEV_LAUNCHER };
};
