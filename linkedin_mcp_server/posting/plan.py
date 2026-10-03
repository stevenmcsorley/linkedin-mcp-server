"""Validation, scheduling arithmetic and the local ledger for approval-gated posts.

Nothing here touches LinkedIn. A post is planned from the caller's exact text
and an optional local schedule time; the plan is what the preview shows and
what the composer types, so the approved text and the posted text are the same
string. The ledger remembers every post this server submitted, so a retried
call cannot publish the same text twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import hashlib
import json
import re

from linkedin_mcp_server.config.loaders import _env

MAX_POST_CHARS = 3000
MIN_LEAD = timedelta(minutes=20)
MAX_LEAD = timedelta(days=90)
SCHEDULE_STEP_MINUTES = 15
POSTS_DIR_ENV = "LINKEDIN_POSTS_DIR"
DEFAULT_POSTS_DIR = "~/.linkedin-mcp/posts"
_URL = re.compile(r"https?://\S+")


class PostingErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    WRITES_DISABLED = "WRITES_DISABLED"
    DUPLICATE_POST = "DUPLICATE_POST"
    COMPOSER_CHANGED = "COMPOSER_CHANGED"
    TEXT_MISMATCH = "TEXT_MISMATCH"
    SCHEDULE_MISMATCH = "SCHEDULE_MISMATCH"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"


class PostingError(Exception):
    def __init__(self, code: PostingErrorCode, message: str, *, submitted: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.submitted = submitted

    def to_result(self) -> dict[str, Any]:
        # Every refusal here happens before the Post/Schedule button is pressed,
        # so calling again after fixing the cause cannot publish twice.
        return {
            "status": "error",
            "code": self.code.value,
            "message": self.message,
            "posted": False,
            "retry_safe": not self.submitted,
        }


@dataclass(frozen=True)
class PostPlan:
    text: str
    schedule: datetime | None

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256("\n".join(content_lines(self.text)).encode()).hexdigest()

    @property
    def posting_at(self) -> str | None:
        return posting_at_label(self.schedule) if self.schedule else None

    def preview(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "characters": len(self.text),
            "links": _URL.findall(self.text),
            "when": f"Scheduled: Posting at {self.posting_at}" if self.schedule else "Immediately",
        }


def content_lines(text: str) -> list[str]:
    """The post as LinkedIn keeps it: its non-blank lines, whitespace collapsed.

    Blank lines are paragraph spacing, which the composer represents as empty
    paragraphs; comparing only the content lines makes a read-back exact
    without depending on that representation.
    """
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def parse_schedule(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        raise PostingError(
            PostingErrorCode.VALIDATION_ERROR,
            "schedule_at must be a local date and time such as 2026-10-06T08:30.",
        ) from None
    if when.tzinfo is not None:
        raise PostingError(
            PostingErrorCode.VALIDATION_ERROR,
            "schedule_at is wall-clock time in the browser's time zone; leave out the UTC offset.",
        )
    return when


def plan_post(text: str, schedule_at: str | None, *, now: datetime) -> PostPlan:
    text = text.strip()
    if not text:
        raise PostingError(PostingErrorCode.VALIDATION_ERROR, "The post text is empty.")
    if len(text) > MAX_POST_CHARS:
        raise PostingError(
            PostingErrorCode.VALIDATION_ERROR,
            f"The post is {len(text)} characters; LinkedIn allows {MAX_POST_CHARS}. Nothing is truncated.",
        )
    if not schedule_at:
        return PostPlan(text, None)
    when = parse_schedule(schedule_at)
    if when.second or when.microsecond or when.minute % SCHEDULE_STEP_MINUTES:
        raise PostingError(
            PostingErrorCode.VALIDATION_ERROR,
            "LinkedIn schedules on the quarter hour; use :00, :15, :30 or :45.",
        )
    if when < now + MIN_LEAD:
        raise PostingError(
            PostingErrorCode.VALIDATION_ERROR,
            f"schedule_at must be at least {int(MIN_LEAD.total_seconds() // 60)} minutes from now.",
        )
    if when > now + MAX_LEAD:
        raise PostingError(
            PostingErrorCode.VALIDATION_ERROR,
            "LinkedIn schedules at most three months ahead.",
        )
    return PostPlan(text, when)


def _hour(when: datetime) -> str:
    return f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def posting_at_label(when: datetime) -> str:
    """LinkedIn's own wording for a schedule, e.g. ``Tue, Oct 6, 8:30 AM``."""
    return f"{when:%a, %b} {when.day}, {_hour(when)}"


def date_field(when: datetime) -> str:
    return f"{when.month}/{when.day}/{when.year}"


def time_field(when: datetime) -> str:
    return _hour(when)


class PostLedger:
    """Append-only record of submitted posts, keyed by the text's fingerprint."""

    def __init__(self, root: Path | None = None):
        self._root = root or Path(_env(POSTS_DIR_ENV) or DEFAULT_POSTS_DIR).expanduser()
        self._path = self._root / "ledger.jsonl"

    def entries(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def find(self, fingerprint: str) -> dict[str, Any] | None:
        hits = [e for e in self.entries() if e.get("fingerprint") == fingerprint]
        return hits[-1] if hits else None

    def record(self, plan: PostPlan, status: str, **extra: Any) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        entry = {
            "fingerprint": plan.fingerprint,
            "status": status,
            "scheduledFor": plan.schedule.isoformat(timespec="minutes") if plan.schedule else None,
            "firstLine": content_lines(plan.text)[0][:120],
            "at": datetime.now().isoformat(timespec="seconds"),
            **extra,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
