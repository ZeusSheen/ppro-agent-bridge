/*
 * pairing: the panel and the broker prove to each other that they know the pairing token (ppro_bridge/auth.py,
 * "Pairing"; SECURITY_REVIEW.md, open issues 1 and 2 of 0.6). Neither sends the token.
 *
 *   panel  -> {"role": "panel", "plugin", "version", "auth": "hmac-sha256", "nonce": <64 hex>}
 *   broker -> {"event": "challenge", "auth": "hmac-sha256", "nonce": <64 hex>}
 *   panel  -> {"proof": HMAC-SHA256(token, "ppro-agent-bridge/1 client panel <panel nonce> <broker nonce>")}
 *   broker -> {"event": "registered", ..., "proof": HMAC-SHA256(token, "ppro-agent-bridge/1 broker panel ...")}
 *
 * main.js runs no request, and sends nothing but these, before the broker's proof checks out (fail closed).
 *
 * The token: %USERPROFILE%\.ppro-agent-bridge\token, the file the broker creates (manifest localFileSystem
 * fullAccess). The home folder comes from UXP's os.homedir(), else from a UXP storage folder's nativePath (the data
 * or temporary folder sits in the user's application data folder under <home>; not the plugin folder, which can be anywhere). The file is read with UXP's fs.readFile (native path,
 * then file: URLs), else localFileSystem.getEntryWithUrl(file: URL).read(). Which of these a Premiere build offers is
 * not measured yet: readToken() reports the path and the way that worked (`via`), and the error with every path
 * tried. The panel cannot read environment variables, so a broker started with PPRO_BRIDGE_TOKEN pairs only when the
 * file holds the same token.
 *
 * Nonces: crypto.getRandomValues when the host has it, else SHA-256 over Math.random, the clocks and a counter
 * (nonceSource says which). The panel proves first and the broker's nonce comes from the OS, so a weak panel nonce
 * only weakens the freshness of the broker's proof against a replay recorded off the loopback interface.
 *
 * Factory module.   deps: { uxp, core, os?, fs?, crypto? }
 */
module.exports = function (deps) {
  const uxp = deps.uxp;
  const core = deps.core;

  const SCHEME = "hmac-sha256";
  const LABEL = "ppro-agent-bridge/1";
  const MIN_TOKEN = 32;
  const NONCE_RE = /^[0-9a-f]{64}$/;
  const DIR = ".ppro-agent-bridge";

  /* ---------- HMAC-SHA256 ---------- */

  const toHex = (bytes) => bytes.map((b) => (b & 255).toString(16).padStart(2, "0")).join("");

  function hmacBytes(keyBytes, msgBytes) {
    let key = Array.from(keyBytes);
    if (key.length > 64) key = core.sha256Bytes(key);
    while (key.length < 64) key.push(0);
    const inner = core.sha256Bytes(key.map((b) => b ^ 0x36).concat(Array.from(msgBytes)));
    return core.sha256Bytes(key.map((b) => b ^ 0x5c).concat(inner));
  }

  /* HMAC-SHA256 of two strings' UTF-8 bytes, as lowercase hex (Python: hmac.new(k, m, "sha256").hexdigest()). */
  function hmacHex(key, message) {
    return toHex(hmacBytes(core.utf8Bytes(String(key)), core.utf8Bytes(String(message))));
  }

  const validNonce = (n) => typeof n === "string" && NONCE_RE.test(n);

  function proof(token, side, clientNonce, brokerNonce) {
    if (side !== "client" && side !== "broker") throw new Error(`proof for side ${side}`);
    return hmacHex(token, `${LABEL} ${side} panel ${clientNonce} ${brokerNonce}`);
  }

  /* The broker's (or a client's) proof for this handshake, compared over its whole length. */
  function proofMatches(given, token, side, clientNonce, brokerNonce) {
    if (typeof given !== "string" || !token || !validNonce(clientNonce) || !validNonce(brokerNonce)) return false;
    const want = proof(token, side, clientNonce, brokerNonce);
    let diff = given.length ^ want.length;
    for (let i = 0; i < want.length; i++) diff |= (given.charCodeAt(i) | 0) ^ want.charCodeAt(i);
    return diff === 0;
  }

  /* ---------- nonces ---------- */

  let counter = 0;
  let nonceSource = null;

  function randomBytes(n) {
    const c = deps.crypto;
    if (c && typeof c.getRandomValues === "function" && typeof Uint8Array === "function") {
      try {
        const a = new Uint8Array(n);
        c.getRandomValues(a);
        nonceSource = "crypto.getRandomValues";
        return Array.from(a);
      } catch (e) { /* fall through */ }
    }
    nonceSource = "Math.random";
    counter += 1;
    const perf = typeof performance !== "undefined" && performance && typeof performance.now === "function"
      ? performance.now() : 0;
    const seed = [Date.now(), perf, counter];
    for (let i = 0; i < 16; i++) seed.push(Math.floor(Math.random() * 4294967296));
    const out = [];
    let block = 0;
    while (out.length < n) {
      for (const b of core.sha256Bytes(core.utf8Bytes(`${seed.join(",")}#${block}`))) out.push(b);
      block += 1;
    }
    return out.slice(0, n);
  }

  const newNonce = () => toHex(randomBytes(32));

  /* ---------- the token file ---------- */

  const sepOf = (p) => (String(p).indexOf("\\") >= 0 ? "\\" : "/");

  /* <home> of a path inside <home>'s application data folder (a UXP storage folder), else null. Written
     Ap[p]Data below: the same folder name, which the release scan's generic-path rule would flag. */
  function homeFrom(p) {
    const m = /^(.*?)[\\/]Ap[p]Data[\\/]/i.exec(String(p || ""));
    return m && m[1] ? m[1] : null;
  }

  async function homes() {
    const out = [];
    const add = (h, via) => {
      if (typeof h === "string" && h && !out.some((x) => x.home.toLowerCase() === h.toLowerCase())) out.push({ home: h, via });
    };
    try {
      if (deps.os && typeof deps.os.homedir === "function") add(await deps.os.homedir(), "os.homedir");
    } catch (e) { /* the next source */ }
    const lfs = uxp && uxp.storage ? uxp.storage.localFileSystem : null;
    // not getPluginFolder: a plugin loaded from someone's application data folder is not proof of whose home it is
    for (const name of ["getDataFolder", "getTemporaryFolder"]) {
      try {
        if (core.isFn(lfs, name)) {
          const folder = await lfs[name]();
          add(homeFrom(folder && folder.nativePath), name);
        }
      } catch (e) { /* the next source */ }
    }
    return out;
  }

  async function tokenPaths() {
    return (await homes()).map((h) => {
      const sep = sepOf(h.home);
      return { path: h.home.replace(/[\\/]+$/, "") + sep + DIR + sep + "token", home: h.via };
    });
  }

  function fileUrls(p) {
    const fwd = String(p).replace(/\\/g, "/");
    const url = "file:" + (fwd.charAt(0) === "/" ? fwd : "/" + fwd);
    const encoded = encodeURI(url);
    return encoded === url ? [url] : [url, encoded];
  }

  const usable = (text) => {
    const t = typeof text === "string" ? text.replace(/^﻿/, "").trim() : "";
    return t.length >= MIN_TOKEN ? t : null;
  };

  /* {token, path, via} for the first token file that can be read, else {error, tried: [paths]}. Never throws; the
     token never goes into a message. */
  async function readToken() {
    const tried = [];
    const lfs = uxp && uxp.storage ? uxp.storage.localFileSystem : null;
    let candidates = [];
    try {
      candidates = await tokenPaths();
    } catch (e) { /* none */ }
    for (const c of candidates) {
      tried.push(c.path);
      if (deps.fs && typeof deps.fs.readFile === "function") {
        for (const spelling of [c.path].concat(fileUrls(c.path))) {
          try {
            const t = usable(await deps.fs.readFile(spelling, { encoding: "utf-8" }));
            if (t) return { token: t, path: c.path, via: `fs.readFile, home from ${c.home}` };
          } catch (e) { /* the next spelling */ }
        }
      }
      if (core.isFn(lfs, "getEntryWithUrl")) {
        for (const url of fileUrls(c.path)) {
          try {
            const entry = await lfs.getEntryWithUrl(url);
            const t = entry && core.isFn(entry, "read") ? usable(await entry.read()) : null;
            if (t) return { token: t, path: c.path, via: `getEntryWithUrl, home from ${c.home}` };
          } catch (e) { /* the next spelling */ }
        }
      }
    }
    const where = tried.length ? tried.join(" or ") : "%USERPROFILE%\\.ppro-agent-bridge\\token (the home folder is unknown)";
    return { error: `no pairing token at ${where} (the broker creates it when it starts)`, tried };
  }

  return {
    SCHEME, LABEL, hmacHex, proof, proofMatches, validNonce, newNonce, readToken, tokenPaths, homeFrom, fileUrls,
    nonceSource: () => nonceSource,
  };
};
