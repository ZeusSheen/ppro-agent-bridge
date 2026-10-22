"""Errors the bridge reports as one JSON object: {"ok": false, "error": ..., "code": ..., "outcome": ...}."""

import json

OUTCOMES = ("not_committed", "committed", "partial", "unknown")


class BridgeError(Exception):
    """A panel, broker or CLI failure.

    `code` is machine-readable (AMBIGUOUS, NOT_FOUND, SEQUENCE_MISMATCH, PANEL_OUTDATED, ...; the full list is in
    actions.json `errors`). `outcome` says whether Premiere may have changed: "not_committed" (safe to retry),
    "committed", "partial" (some steps landed: see data.completed) or "unknown" (check the timeline first).
    `data` carries details (the panel's error data, a dry-run style report, ...); `sent` is True once the request
    left this process; `retryable` comes from the broker (BRIDGE_BUSY)."""

    def __init__(self, message: str, code: str | None = None, outcome: str | None = None, *, data=None,
                 sent: bool | None = None, retryable: bool | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.outcome = outcome
        self.data = data
        self.sent = sent
        self.retryable = retryable
        self.hint = hint

    def to_reply(self) -> dict:
        """The CLI's failure object. A message that is itself a JSON object (the 0.3.3 way of reporting a
        partial result) becomes {"error": "see data", "data": {...}}."""
        message, data = str(self), self.data
        if data is None and message.startswith("{"):
            try:
                parsed = json.loads(message)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                message, data = "see data", parsed
        reply = {"ok": False, "error": message}
        for key, value in (("code", self.code), ("outcome", self.outcome), ("sent", self.sent),
                           ("retryable", self.retryable), ("hint", self.hint), ("data", data)):
            if value is not None:
                reply[key] = value
        return reply
