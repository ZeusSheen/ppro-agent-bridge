"""The pairing token: one secret per Windows user that the broker requires from every controller.

The broker reads it from the token file at start, or creates the file (exclusive create, so two brokers starting
at once agree on one token). The CLI, scripts using ppro_bridge.transport and the future MCP server read the same
file, so pairing needs no setup. Browsers are already refused by the broker's Origin check; the token keeps out
other local programs and other users that cannot read this user's profile.

  PPRO_BRIDGE_TOKEN       the token itself (wins over the file; at least 32 characters)
  PPRO_BRIDGE_TOKEN_FILE  where the file is (default ~/.ppro-agent-bridge/token,
                          %USERPROFILE%\\.ppro-agent-bridge\\token on Windows)

Stdlib only: the broker imports this.
"""

import hmac
import os
import secrets
from pathlib import Path

TOKEN_ENV = "PPRO_BRIDGE_TOKEN"
TOKEN_FILE_ENV = "PPRO_BRIDGE_TOKEN_FILE"
MIN_LENGTH = 32


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


def ensure_token() -> tuple[str, str]:
    """The broker's token and where it came from ("env", "file" or "created"). Raises TokenError."""
    if os.environ.get(TOKEN_ENV):
        token = _valid(os.environ[TOKEN_ENV])
        if token is None:
            raise TokenError(f"{TOKEN_ENV} must be at least {MIN_LENGTH} characters.")
        return token, "env"
    path = token_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
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
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(token + "\n")
    return token, "created"


def matches(given, expected: str) -> bool:
    """Constant-time comparison; anything that is not a string never matches."""
    if not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
