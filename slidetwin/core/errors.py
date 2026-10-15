"""Error types. Every failure carries a stable code, a specific message and an optional hint."""

from __future__ import annotations

from typing import ClassVar


class SlideTwinError(Exception):
    """Base error. `message` says what went wrong; `hint` says how to fix it."""

    code: ClassVar[str] = "INTERNAL"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


class InvalidParameter(SlideTwinError):
    code = "INVALID_PARAMETER"


class MagnificationNotPreset(SlideTwinError):
    code = "MAG_NOT_PRESET"


class MagnificationUnavailable(SlideTwinError):
    code = "MAG_UNAVAILABLE"


class OutsideView(SlideTwinError):
    code = "OUTSIDE_VIEW"


class NoCurrentView(SlideTwinError):
    code = "NO_CURRENT_VIEW"


class ActionNotAllowed(SlideTwinError):
    code = "ACTION_NOT_ALLOWED"


class BudgetExhausted(SlideTwinError):
    code = "BUDGET_EXHAUSTED"


class SessionClosed(SlideTwinError):
    code = "SESSION_CLOSED"


class SessionNotFound(SlideTwinError):
    code = "SESSION_NOT_FOUND"


class TooManySessions(SlideTwinError):
    code = "TOO_MANY_SESSIONS"


class ImageNotFound(SlideTwinError):
    code = "IMAGE_NOT_FOUND"


class SlideNotFound(SlideTwinError):
    code = "SLIDE_NOT_FOUND"


class SlideMetadataError(SlideTwinError):
    code = "SLIDE_METADATA"


class SlideIntegrityError(SlideTwinError):
    code = "SLIDE_HASH_MISMATCH"


class ConfigError(SlideTwinError):
    code = "CONFIG_ERROR"


class ReportInvalid(SlideTwinError):
    code = "REPORT_INVALID"


class ReplayError(SlideTwinError):
    code = "REPLAY_ERROR"
