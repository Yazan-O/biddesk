"""Append-only JSONL ledger with undo and a veto window.

Quiet Core section 3: every action the agent takes is a row holding what, why (the
evidence), when, and the undo. Silent actions sit behind a veto window (default 10
minutes) before they are final; reads do not. Nothing is ever deleted or rewritten:
an undo appends an ``undone`` row that points at the original row's timestamp.

One file per firm: ``data/ledger/<firm_slug>.jsonl``. Timestamps are ISO-8601 with
microseconds in America/Chicago, which makes them both sortable and unique enough to
serve as the row key inside one firm's file.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

try:
    import msvcrt  # Windows: O_APPEND is seek-then-write, not atomic
except ImportError:  # POSIX: O_APPEND is atomic
    msvcrt = None

from pydantic import ValidationError

from .config import DATA
from .models import LedgerRow

TZ = ZoneInfo("America/Chicago")
LEDGER_DIR = DATA / "ledger"

#: Actions that are not silent agent actions and therefore never sit in a veto window.
#: reads are exempt by the spec; the rest are either records of a human decision or of
#: something that never happened.
NON_VETOABLE_ACTIONS = frozenset({"read", "undone", "denied_tool", "card_answered"})


def now_iso() -> str:
    """Current wall clock in America/Chicago, ISO-8601 with microseconds."""
    return datetime.now(TZ).isoformat()


def _parse_ts(ts: str | datetime) -> datetime:
    """Parse a ledger timestamp; naive input is read as America/Chicago."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


class Ledger:
    """Append-only JSONL ledger for one firm."""

    def __init__(self, firm_slug: str, path: Optional[Path | str] = None) -> None:
        self.firm_slug = firm_slug
        self.path = Path(path) if path is not None else LEDGER_DIR / f"{firm_slug}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.skipped = 0  # unreadable lines seen on the last read; surfaced on the page, never fatal

    # ---------------------------------------------------------------- writing

    def append(self, row: LedgerRow) -> LedgerRow:
        """Append one row. Fills ``ts`` and ``firm_slug`` when the caller left them blank."""
        if not row.ts:
            row = row.model_copy(update={"ts": now_iso()})
        if not row.firm_slug:
            row = row.model_copy(update={"firm_slug": self.firm_slug})
        line = (json.dumps(row.model_dump(), ensure_ascii=False) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o644)
        try:
            if msvcrt is not None:
                for _ in range(400):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.005)
                os.lseek(fd, 0, os.SEEK_END)
            os.write(fd, line)
            if msvcrt is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(fd)
        return row

    def write(
        self,
        notice_id: str,
        action: str,
        why: str,
        evidence: str,
        tier: Optional[int] = None,
        undo: Optional[str] = None,
    ) -> LedgerRow:
        """Build a row from its parts and append it."""
        return self.append(
            LedgerRow(
                ts=now_iso(),
                firm_slug=self.firm_slug,
                notice_id=notice_id,
                action=action,
                tier=tier,
                why=why,
                evidence=evidence,
                undo=undo,
            )
        )

    # ---------------------------------------------------------------- reading

    def _all_rows(self) -> list[LedgerRow]:
        if not self.path.exists():
            return []
        rows: list[LedgerRow] = []
        skipped = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise TypeError("ledger line is not a JSON object")
                    rows.append(LedgerRow(**obj))
                except (json.JSONDecodeError, ValidationError, TypeError):
                    skipped += 1
        self.skipped = skipped
        return rows

    def rows(
        self,
        limit: Optional[int] = None,
        notice_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> list[LedgerRow]:
        """Rows in write order, oldest first. ``limit`` keeps the most recent N."""
        out: Iterable[LedgerRow] = self._all_rows()
        if notice_id is not None:
            out = [r for r in out if r.notice_id == notice_id]
        if action is not None:
            out = [r for r in out if r.action == action]
        out = list(out)
        if limit is not None:
            out = out[-limit:]
        return out

    def undone_ts(self) -> set[str]:
        """Timestamps of rows that a later ``undone`` row cancelled."""
        cancelled: set[str] = set()
        for row in self._all_rows():
            if row.action != "undone":
                continue
            try:
                cancelled.add(json.loads(row.evidence)["undoes_ts"])
            except (ValueError, KeyError, TypeError):
                continue
        return cancelled

    # ------------------------------------------------------------------ undo

    def undo(self, row_ts: str, reason: str) -> LedgerRow:
        """Append an ``undone`` row referencing ``row_ts``. The original row stays."""
        target = _parse_ts(row_ts)
        matches = [r for r in self._all_rows() if _parse_ts(r.ts) == target]
        if not matches:
            raise KeyError(f"no ledger row at ts {row_ts!r} in {self.path}")
        if len(matches) > 1:
            # ts is the row key; two rows sharing one would make the undo ambiguous.
            raise KeyError(f"{len(matches)} ledger rows share ts {row_ts!r} in {self.path}")
        original = matches[0]
        if original.action == "undone":
            raise ValueError(f"ledger row at ts {row_ts!r} is itself an undo and cannot be undone")
        if original.ts in self.undone_ts():
            raise ValueError(f"ledger row at ts {row_ts!r} is already undone")
        evidence = json.dumps(
            {
                "undoes_ts": original.ts,
                "original_action": original.action,
                "original_why": original.why,
                "undo_recipe": original.undo,
            },
            ensure_ascii=False,
        )
        return self.append(
            LedgerRow(
                ts=now_iso(),
                firm_slug=original.firm_slug or self.firm_slug,
                notice_id=original.notice_id,
                action="undone",
                tier=original.tier,
                why=reason,
                evidence=evidence,
                undo=None,
            )
        )

    def pending_vetoes(
        self, now: str | datetime | None = None, window_minutes: int = 10
    ) -> list[LedgerRow]:
        """Silent actions still inside their veto window and not yet undone.

        Reads, denials, human answers and ``undone`` rows are never vetoable.
        """
        moment = _parse_ts(now) if now is not None else datetime.now(TZ)
        cutoff = moment - timedelta(minutes=window_minutes)
        cancelled = self.undone_ts()
        pending = []
        for row in self._all_rows():
            if row.action in NON_VETOABLE_ACTIONS or row.ts in cancelled:
                continue
            ts = _parse_ts(row.ts)
            if cutoff <= ts <= moment:
                pending.append(row)
        return pending

    # --------------------------------------------------------------- summary

    def summary(self) -> dict:
        """Counts by action and by tier, plus the total and the file path."""
        by_action: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        rows = self._all_rows()
        for row in rows:
            by_action[row.action] = by_action.get(row.action, 0) + 1
            key = "none" if row.tier is None else str(row.tier)
            by_tier[key] = by_tier.get(key, 0) + 1
        return {
            "firm_slug": self.firm_slug,
            "path": str(self.path),
            "total": len(rows),
            "by_action": by_action,
            "by_tier": by_tier,
        }


def record(
    firm_slug: str,
    notice_id: str,
    action: str,
    why: str,
    evidence: str,
    tier: Optional[int] = None,
    undo: Optional[str] = None,
    path: Optional[Path | str] = None,
) -> LedgerRow:
    """One-line append for callers that do not hold a :class:`Ledger`."""
    return Ledger(firm_slug, path=path).write(
        notice_id=notice_id, action=action, why=why, evidence=evidence, tier=tier, undo=undo
    )
