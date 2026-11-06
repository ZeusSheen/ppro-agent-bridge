"""The pairing token: one secret per Windows user that the broker, its controllers and the panel all know.

The broker reads it from the token file at start, or creates the file (exclusive create, so two brokers starting
at once agree on one token). The CLI, scripts using ppro_bridge.transport, the MCP server and the Premiere panel
read the same file, so pairing needs no setup. Browsers are already refused by the broker's Origin check; the token
keeps out other local programs and other users that cannot read this user's profile.

  PPRO_BRIDGE_TOKEN       the token itself (wins over the file; at least 32 characters). The panel cannot read
                          environment variables: it pairs only with a broker whose token is the file's.
  PPRO_BRIDGE_TOKEN_FILE  where the file is (default ~/.ppro-agent-bridge/token,
                          %USERPROFILE%\\.ppro-agent-bridge\\token on Windows)

Pairing (0.7, SECURITY_REVIEW.md section 1, open issues 1 and 2 of 0.6): the token itself never crosses the wire.
Each side proves it knows the token with an HMAC-SHA256 over both sides' nonces, the client first:

  client -> {"role": "panel"|"controller", ..., "auth": "hmac-sha256", "nonce": <64 hex>}
  broker -> {"event": "challenge", "auth": "hmac-sha256", "nonce": <64 hex>}
  client -> {"proof": proof(token, "client", role, client nonce, broker nonce)}
  broker -> {"event": "registered" (panel) | "authenticated" (controller), ...,
             "proof": proof(token, "broker", role, client nonce, broker nonce)}

The client sends nothing else (no request, no reply) before the broker's proof checks out, and the broker answers
nothing but the challenge before the client's does: a program that holds the port while no broker runs learns
neither the token nor a request, and a program that connects to the broker learns no proof it could replay. The side
and the role are part of the signed text, so no proof is valid for another direction or role. proof() and
proof_matches() need no OpenSSL (the builtin SHA-256: a CLI call stays fast).

Stdlib only: the broker imports this.
"""

import os
from pathlib import Path

TOKEN_ENV = "PPRO_BRIDGE_TOKEN"
TOKEN_FILE_ENV = "PPRO_BRIDGE_TOKEN_FILE"
MIN_LENGTH = 32
SCHEME = "hmac-sha256"          # the hello's "auth"
PROOF_LABEL = "ppro-agent-bridge/1"
NONCE_HEX = 64                  # 32 random bytes as lowercase hex
SIDES = ("client", "broker")
ROLES = ("panel", "controller")


class TokenError(Exception):
    """The token file cannot be read or created, or holds something too short to be a token."""


def data_dir() -> Path:
    """The bridge's per-user folder: ~/.ppro-agent-bridge (%USERPROFILE%\\.ppro-agent-bridge on Windows).

    Not %LOCALAPPDATA%: Windows gives packaged (MSIX) apps such as the Claude desktop app a private copy of it, so a
    CLI started from one would read another token than a broker started by double-click.
    """
    return Path.home() / ".ppro-agent-bridge"


def token_path() -> Path:
    return Path(os.environ.get(TOKEN_FILE_ENV) or data_dir() / "token")


def _valid(token: str | None) -> str | None:
    token = (token or "").strip()
    return token if len(token) >= MIN_LENGTH else None


def read_token() -> str | None:
    """The token a client sends: PPRO_BRIDGE_TOKEN, else the token file; None when neither holds a valid one."""
    if os.environ.get(TOKEN_ENV):
        return _valid(os.environ[TOKEN_ENV])
    try:
        return _valid(token_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


ACL_ERRORS: list[str] = []  # what restrict_to_owner could not do in ensure_token / the audit log (the broker logs it)


_SID = []  # this user's SID once asked


def _user_sid() -> str | None:
    """This Windows user's SID (whoami, no admin rights needed; asked once), or None."""
    if _SID:
        return _SID[0]
    import subprocess
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             errors="replace", timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for field in reversed(out.strip().replace('"', "").split(",")):
        if field.startswith("S-1-") and all(c.isdigit() or c == "-" for c in field[4:]):
            _SID.append(field)
            return field
    return None


def restrict_to_owner(path, folder: bool = False) -> str | None:
    """Give a file (or folder) the bridge just created an ACL of its own: this user and SYSTEM, full control,
    nothing inherited (icacls /inheritance:r /grant:r, argument list, no shell); a folder passes it on to what is
    created in it. POSIX: mode 0600 / 0700. None when done, else why not (the caller reports it; nothing raises).
    An administrator can still read any file: this keeps out other standard users, also where the token or audit
    folder was moved somewhere they can read (ProgramData, a share) with PPRO_BRIDGE_TOKEN_FILE / PPRO_AUDIT_DIR."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o700 if folder else 0o600)
            return None
        except OSError as e:
            return f"chmod {path}: {e}"
    import subprocess
    sid = _user_sid()
    if sid is None:
        return f"{path}: cannot tell this user's SID (whoami), the ACL stays inherited"
    inherit = "(OI)(CI)" if folder else ""
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:{inherit}F",
                            f"*S-1-5-18:{inherit}F"], capture_output=True, text=True, errors="replace", timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return f"{path}: icacls failed ({e})"
    return None if r.returncode == 0 else f"{path}: icacls exit {r.returncode}"


def ensure_token() -> tuple[str, str]:
    """The broker's token and where it came from ("env", "file" or "created"). Raises TokenError. A token file (and
    its folder) created here gets restrict_to_owner's ACL before the token is written; ACL_ERRORS says when not."""
    if os.environ.get(TOKEN_ENV):
        token = _valid(os.environ[TOKEN_ENV])
        if token is None:
            raise TokenError(f"{TOKEN_ENV} must be at least {MIN_LENGTH} characters.")
        return token, "env"
    path = token_path()
    try:
        new_folder = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if new_folder:
            ACL_ERRORS.append(restrict_to_owner(path.parent, folder=True))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            token = _valid(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            raise TokenError(f"Cannot read the token file {path} ({e}).") from e
        if token is None:
            raise TokenError(f"The token file {path} holds no valid token (at least {MIN_LENGTH} characters); "
                             "delete it and restart the broker to make a new one.")
        return token, "file"
    except OSError as e:
        raise TokenError(f"Cannot create the token file {path} ({e}).") from e
    import secrets  # here, not at the top: a CLI call only reads the token (secrets / hmac load OpenSSL: ms)
    try:
        ACL_ERRORS.append(restrict_to_owner(path))  # still empty: nobody can have read a token from it yet
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(token + "\n")
    except OSError as e:
        raise TokenError(f"Cannot write the token file {path} ({e}).") from e
    finally:
        ACL_ERRORS[:] = [e for e in ACL_ERRORS if e]
    return token, "created"


def _compare(a: bytes, b: bytes) -> bool:
    try:
        from _operator import _compare_digest  # what hmac.compare_digest falls back to; no OpenSSL to load
    except ImportError:  # pragma: no cover
        from hmac import compare_digest as _compare_digest
    return _compare_digest(a, b)


def matches(given, expected: str) -> bool:
    """Constant-time comparison; anything that is not a string never matches."""
    if not isinstance(given, str) or not given:
        return False
    return _compare(given.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------- pairing (challenge-response)

def _sha256():
    try:
        from _sha2 import sha256  # Python 3.12+: the builtin one (hashlib would load OpenSSL, several ms)
    except ImportError:  # pragma: no cover
        try:
            from _sha256 import sha256  # Python 3.10 / 3.11
        except ImportError:
            from hashlib import sha256
    return sha256


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    """HMAC-SHA256 (RFC 2104), the same bytes as hmac.new(key, message, "sha256").digest()."""
    sha256 = _sha256()
    if len(key) > 64:
        key = sha256(key).digest()
    key = key.ljust(64, b"\0")
    inner = sha256(bytes(b ^ 0x36 for b in key) + message).digest()
    return sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def new_nonce() -> str:
    """A fresh nonce for one handshake: 32 bytes from the OS's CSPRNG, as 64 lowercase hex digits."""
    return os.urandom(NONCE_HEX // 2).hex()


def valid_nonce(value) -> bool:
    """Exactly 64 lowercase hex digits (what new_nonce and the panel make); anything else is a malformed hello."""
    return (isinstance(value, str) and len(value) == NONCE_HEX
            and all(c in "0123456789abcdef" for c in value))


def proof_text(side: str, role: str, client_nonce: str, broker_nonce: str) -> str:
    """What a proof signs: "ppro-agent-bridge/1 <side> <role> <client nonce> <broker nonce>"."""
    if side not in SIDES or role not in ROLES:
        raise ValueError(f"proof for side {side!r}, role {role!r}")
    return f"{PROOF_LABEL} {side} {role} {client_nonce} {broker_nonce}"


def proof(token: str, side: str, role: str, client_nonce: str, broker_nonce: str) -> str:
    """HMAC-SHA256(token, proof_text(...)) as 64 lowercase hex digits."""
    return hmac_sha256(token.encode("utf-8"),
                       proof_text(side, role, client_nonce, broker_nonce).encode("utf-8")).hex()


def proof_matches(given, token: str | None, side: str, role: str, client_nonce, broker_nonce) -> bool:
    """`given` is the proof of `side` for this handshake (constant-time); False for anything malformed."""
    if not token or not isinstance(given, str) or not valid_nonce(client_nonce) or not valid_nonce(broker_nonce):
        return False
    want = proof(token, side, role, client_nonce, broker_nonce)
    return _compare(given.encode("utf-8", "replace"), want.encode("ascii"))
