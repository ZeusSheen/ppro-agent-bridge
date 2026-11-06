/*
 * support: the update notice, opening this repository's GitHub pages, and the problem report. main.js wires them to
 * index.html; everything here is plain functions over what main.js hands in.
 *
 * Update notice: the broker's daily update check (ppro_bridge/update.py) reaches the panel in its "registered" notice
 * and in {"event": "update"} notices as {latest, url, published, newer, skipped, checkedAt} or null. cleanUpdate()
 * checks every field again (the panel trusts no URL it did not write), notice() says what to show: "0.7.1 available ·
 * View · Skip this version" while newer and not skipped. Skip sends the panel's one message of its own,
 * {"event": "skip_version", "version": "x.y.z"} (skipMessage), which the broker keeps in settings.json.
 *
 * Opening pages: only this repository's release pages (the URL the broker kept) and ISSUES_URL, through
 * uxp.shell.openExternal under the manifest's launchProcess.schemes ["https"] (UXP asks the user first). allowedUrl()
 * is the one gate: REPO_URL's /releases/ followed by plain path segments (no "." or "..", "%", "?", "#", "@",
 * backslash or space), or exactly ISSUES_URL.
 *
 * Problem report: a whitelist (SECURITY_REVIEW.md, section 1, "Problem report"): panel, broker and protocol versions,
 * build flags, Premiere's version, build and UI language, the capability flags, the connection state, the update
 * check's version, and the last errors as time, action (a spec action name), code and outcome, or a fixed label of
 * the panel's own. Never an error message, a project or sequence name, a path or a request param; the panel never has
 * the pairing token. sanitize() cuts anything path-like out of every field as a last line of defence. main.js shows
 * the whole text before it is copied.
 *
 * Copying: copyText() tries UXP's clipboard (navigator.clipboard writeText, else setContent). The manifest asks for no
 * clipboard permission - UXP offers only "read" and "readAndWrite", and reading would let the panel see whatever the
 * user copies. On 27.1 the call without it never settled (live, 6 Nov 2026), so main.js does not use it: its
 * "Select all" selects the report for Ctrl+C. Kept for a host that grants the permission.
 * Factory module.   deps: { uxp, actions }  (actions: the spec's action names, the only ones the report names)
 */
module.exports = function (deps) {
  const uxp = deps.uxp;
  const actionNames = new Set(deps.actions || []);

  const REPO_URL = "https://github.com/ZeusSheen/ppro-agent-bridge";
  const RELEASES_URL = REPO_URL + "/releases/";
  const ISSUES_URL = REPO_URL + "/issues/new";
  const VERSION_RE = /^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$/;
  const SEGMENT_RE = /^[A-Za-z0-9._~+-]{1,100}$/;
  const STAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
  const CODE_RE = /^[A-Z][A-Z0-9_]{1,39}$/;
  const FLAG_RE = /^[A-Za-z][A-Za-z0-9_]{0,40}$/;
  const OUTCOMES = ["not_committed", "committed", "partial", "unknown"];
  const MAX_ERRORS = 8;

  const isVersion = (v) => typeof v === "string" && VERSION_RE.test(v);

  /* ---------- URLs ---------- */

  function releaseUrl(url) {
    if (typeof url !== "string" || url.length > 300 || url.indexOf(RELEASES_URL) !== 0) return false;
    return url.slice(RELEASES_URL.length).split("/").every((s) => SEGMENT_RE.test(s) && s !== "." && s !== "..");
  }

  const allowedUrl = (url) => url === ISSUES_URL || releaseUrl(url);

  /* "" when the page opened, else why not. Never throws; a URL that is not allowed is not handed to UXP at all. */
  async function openUrl(url, developerText) {
    if (!allowedUrl(url)) return "refused: not a page of this repository";
    const shell = uxp ? uxp.shell : null;
    if (!shell || typeof shell.openExternal !== "function") return "this UXP build has no shell.openExternal";
    try {
      const result = await shell.openExternal(url, developerText);
      return typeof result === "string" ? result : "";
    } catch (e) {
      return (e && e.message) || String(e) || "openExternal failed";
    }
  }

  /* ---------- the update notice ---------- */

  function cleanUpdate(u) {
    if (!u || typeof u !== "object" || !isVersion(u.latest)) return null;
    return {
      latest: u.latest,
      url: releaseUrl(u.url) ? u.url : null,
      published: typeof u.published === "string" && STAMP_RE.test(u.published) ? u.published : null,
      newer: u.newer === true,
      skipped: u.skipped === true,
      checkedAt: typeof u.checkedAt === "string" && STAMP_RE.test(u.checkedAt) ? u.checkedAt : null,
    };
  }

  /* {show, version, view}: show the line while a newer release is known and not skipped; view when its page can be
     opened. */
  function notice(u) {
    const c = cleanUpdate(u);
    if (!c || !c.newer || c.skipped) return { show: false, version: null, view: false };
    return { show: true, version: c.latest, view: !!c.url };
  }

  /* The skip message for the release the notice shows, or null. */
  function skipMessage(u) {
    const n = notice(u);
    return n.show ? { event: "skip_version", version: n.version } : null;
  }

  /* ---------- recent errors (what the report lists) ---------- */

  const hhmmss = () => new Date().toTimeString().slice(0, 8);

  /* One entry for `list` (main.js keeps it): a failed request {action, code, outcome}, or a fixed label of the
     panel's own ({label}); at most MAX_ERRORS kept. Only whitelisted shapes go in. */
  function noteError(list, entry) {
    if (!Array.isArray(list) || !entry || typeof entry !== "object") return list;
    const e = { t: hhmmss() };
    if (typeof entry.label === "string") {
      e.label = labelText(entry.label);
    } else {
      e.action = actionNames.has(entry.action) ? entry.action : "unknown";
      e.code = typeof entry.code === "string" && CODE_RE.test(entry.code) ? entry.code : "UNKNOWN";
      e.outcome = OUTCOMES.indexOf(entry.outcome) >= 0 ? entry.outcome : null;
    }
    list.push(e);
    while (list.length > MAX_ERRORS) list.shift();
    return list;
  }

  /* ---------- the problem report ---------- */

  /* Anything path-like becomes "[path]": file URLs, UNC paths, drive paths, and POSIX home or volume paths. A path
     may hold spaces (a user folder named "Bob Smith"), so each one runs to the next quote, <, >, | or line end: the words
     after it go too, which is the safe side for a report. */
  function sanitize(text) {
    return String(text)
      .replace(/file:\/\/[^"'<>|\n]*/gi, "[path]")
      .replace(/\\\\[^"'<>|\n]+/g, "[path]")
      .replace(/\b[A-Za-z]:[\\/][^"'<>|\n]*/g, "[path]")
      .replace(/(^|[\s"'(=])\/(?:Users|home|Volumes|private|var|tmp|mnt|media)\/[^"'<>|\n]*/g, "$1[path]");
  }

  /* A fixed label of the panel's own, cut to plain words (a path in it is cut out first). */
  const labelText = (text) => sanitize(text).replace(/[^A-Za-z0-9 _:()\[\]-]/g, "").slice(0, 60);

  const word = (v, n) => sanitize(v === undefined || v === null || v === "" ? "?" : String(v)).slice(0, n || 60);

  /*
   * The report as text. info: {version, protocol, modules, build: {dev, packaged}, broker, hostInfo: {name, version,
   * buildNumber, uiLocale}, capabilities: {flag: true|false|"unknown"}, connection: "connected"|"connecting"|
   * "disconnected", refused: {closeCode}|null, stuck: {action, seconds}|null, denied: {reason}|false, update,
   * errors: [noteError entries], created: ISO string}. Fields not named here are never read.
   */
  function buildReport(info) {
    const i = info && typeof info === "object" ? info : {};
    const b = i.build && typeof i.build === "object" ? i.build : {};
    const h = i.hostInfo && typeof i.hostInfo === "object" ? i.hostInfo : {};
    const lines = ["### Premiere Agent Bridge problem report (from the panel)", ""];
    lines.push(`- Panel ${isVersion(i.version) ? i.version : "?"} (protocol ${word(i.protocol, 4)}, ${word(i.modules, 10)}, `
      + `${b.dev === true ? "dev" : "release"} build${b.packaged === true ? ", installed as .ccx" : ""})`);
    lines.push(`- Premiere ${word(h.version, 20)} (build ${word(h.buildNumber, 20)}, ${word(h.uiLocale, 12)})`);
    const conn = ["connected", "connecting", "disconnected"].indexOf(i.connection) >= 0 ? i.connection : "unknown";
    const refused = i.refused && typeof i.refused === "object"
      ? `, refused by the broker (close ${Number.isInteger(i.refused.closeCode) ? i.refused.closeCode : "?"})` : "";
    lines.push(`- Broker ${isVersion(i.broker) ? i.broker : "?"}: ${conn}${refused}`);
    if (i.stuck && typeof i.stuck === "object") {
      const action = actionNames.has(i.stuck.action) ? i.stuck.action : "unknown";
      lines.push(`- Stuck on ${action}` + (Number.isFinite(i.stuck.seconds) ? ` for ${Math.round(i.stuck.seconds)} s` : ""));
    }
    if (i.denied && typeof i.denied === "object") lines.push(`- Denied: this Premiere build is on the denylist (${word(i.denied.reason, 120)})`);
    const u = cleanUpdate(i.update);
    if (!u) lines.push("- Update: not known (no broker yet, the check is off, or it has not run)");
    else if (u.newer) lines.push(`- Update: ${u.latest} is available${u.skipped ? " (skipped)" : ""}`);
    else lines.push(`- Update: up to date (latest release ${u.latest})`);
    const caps = i.capabilities && typeof i.capabilities === "object" ? i.capabilities : {};
    const names = Object.keys(caps).filter((k) => FLAG_RE.test(k));
    if (names.length) {
      const off = names.filter((k) => caps[k] === false).sort();
      const yes = names.filter((k) => caps[k] === true).length;
      const unknown = names.filter((k) => caps[k] === "unknown").length;
      lines.push(`- Capabilities: ${yes} yes, ${off.length} no${off.length ? ` (${off.join(", ")})` : ""}, ${unknown} not probed yet`);
    }
    const created = typeof i.created === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(i.created)
      ? i.created.slice(0, 19) + "Z" : "?";
    lines.push(`- Created ${created}`);
    const errors = Array.isArray(i.errors) ? i.errors.slice(-MAX_ERRORS) : [];
    if (errors.length) {
      lines.push("", "Recent errors (this panel):");
      for (const e of errors) {
        const t = typeof e.t === "string" && /^\d{2}:\d{2}:\d{2}$/.test(e.t) ? e.t : "??:??:??";
        if (typeof e.label === "string") lines.push(`- ${t} ${labelText(e.label)}`);
        else {
          lines.push(`- ${t} ${actionNames.has(e.action) ? e.action : "unknown"}: `
            + `${typeof e.code === "string" && CODE_RE.test(e.code) ? e.code : "UNKNOWN"}`
            + ` (${OUTCOMES.indexOf(e.outcome) >= 0 ? e.outcome : "-"})`);
        }
      }
    }
    lines.push("", "No token, project or sequence names, paths, error messages or request params are in this report.");
    return sanitize(lines.join("\n")) + "\n";
  }

  /* ---------- copying ---------- */

  /* {ok: true} once UXP's clipboard took `text`, else {ok: false, error} (see the header: expected on Premiere). */
  async function copyText(text, clipboard) {
    if (!clipboard || typeof clipboard !== "object") return { ok: false, error: "no clipboard API" };
    try {
      if (typeof clipboard.writeText === "function") await clipboard.writeText(String(text));
      else if (typeof clipboard.setContent === "function") await clipboard.setContent({ "text/plain": String(text) });
      else return { ok: false, error: "no clipboard API" };
      return { ok: true };
    } catch (e) {
      return { ok: false, error: (e && e.message) || String(e) || "clipboard refused" };
    }
  }

  return {
    REPO_URL, RELEASES_URL, ISSUES_URL, MAX_ERRORS,
    isVersion, releaseUrl, allowedUrl, openUrl, cleanUpdate, notice, skipMessage, noteError, sanitize, buildReport,
    copyText,
  };
};
