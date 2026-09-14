"""Guard hooks, enforced in code and not in prompts (Quiet Core section 4).

Four pieces:

* :class:`NoSubmitGuard` - cancels any tool call that would submit, sign, send, upload
  or move money in the owner's name, and writes the denial to the ledger. The same rule
  is also available as :class:`NoSubmitIntervention`, an ``InterventionHandler`` with
  ``on_error="deny"`` so a crash inside our own matcher fails closed.
* :class:`ProvenanceStamp` - stamps every fetch/read/extract result with the notice id,
  source file and page it came from, so a finding cannot travel without its origin.
* :class:`TierCounter` - counts model calls and tool calls per invocation (the tier
  counter the demo puts on screen).
* :class:`PiiRedactor` / :func:`redact` - masks emails, phone numbers and SSN-shaped
  numbers in tool output. **Off by default**: the point-of-contact email and phone in a
  SAM.gov notice are published public data, and the owner needs them to ask a question,
  so redacting them would break the product. Turn it on with ``redact_outputs=True``
  only for traces that leave the machine.

Every import below was verified against the installed ``strands-agents`` 1.55.1 on this
machine; see the module docstring of the test file for the exact probe lines.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional
from urllib.parse import unquote

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.interventions import Deny, InterventionHandler, Proceed

from .ledger import Ledger

# --------------------------------------------------------------------------- rules

DEFAULT_DENY_TOOLS: tuple[str, ...] = (
    "submit_bid",
    "submit_offer",
    "submit_proposal",
    "submit_quote",
    "sign",
    "sign_document",
    "esign",
    "send_email",
    "upload_to_sam",
    "make_payment",
    "transfer_funds",
    "wire_transfer",
)

DENIAL_MESSAGE = (
    "Blocked by Biddesk's no-submit guard: Biddesk drafts, the owner submits. "
    "This tool ({tool}) would act in the owner's name ({reason}). "
    "Produce the draft and the Decision Card instead; the owner files it."
)

_SAM_SUBMIT_URL = re.compile(
    r"https?://[^\s\"']*\bsam\.gov\b[^\s\"']*", re.IGNORECASE
)
_SECRET_KEY = re.compile(r"(^|_)(api_?key|key|token|secret|password|passwd|credential|authorization|auth)(_|$)", re.I)
# Query-string scoped so it cannot swallow 32-hex notice ids or resource-link GUIDs.
_SECRET_IN_QS = re.compile(r"([?&](?:api_?key|key|token|access_token|auth|signature|sig|secret|password)=)[^&\s\"']+", re.I)

PROVENANCE_TOOLS: tuple[str, ...] = (
    "sam_fetch_attachment",
    "read_attachment",
    "extract_requirements",
    "sam_search",
)


def _urls(value: Any) -> Iterable[str]:
    """Every sam.gov URL that is itself a tool-input value (a URL field), nested however deep.

    Free text (a SOW body that quotes a sam.gov link) is not a URL the tool will act on, so
    only values that are a bare URL count. Otherwise extract_requirements(text=...) on a real
    solicitation gets cancelled for quoting the portal.
    """
    if isinstance(value, str):
        v = value.strip()
        if v.lower().startswith(("http://", "https://")) and not any(ch.isspace() for ch in v):
            yield from _SAM_SUBMIT_URL.findall(v)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _urls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _urls(item)


def _url_path(url: str) -> str:
    body = url.split("://", 1)[-1]
    if "/" in body:
        return body.split("/", 1)[1].lower()
    return body.split("?", 1)[1].lower() if "?" in body else ""


def deny_reason(
    tool_name: str, tool_input: Any, deny_tools: Iterable[str] = DEFAULT_DENY_TOOLS
) -> Optional[str]:
    """Why this call must be blocked, or ``None`` when it may proceed."""
    raw = (tool_name or "").strip()
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).replace("-", "_").lower()  # submitBid, submit-bid
    flat = re.sub(r"[^a-z0-9]+", "", name)
    stems = {t.lower() for t in deny_tools}
    if (name in stems
            or any(re.search(rf"(^|_){re.escape(s)}(_|\d|$)", name) for s in stems)
            or any(s.replace("_", "") in flat for s in stems if "_" in s)):
        return f"tool '{tool_name}' is on the no-submit deny list"
    for url in _urls(tool_input):
        path = _url_path(unquote(url))
        if re.search(r"(^|[/?&=_-])(submit|sign|e-sign|esign)([/?&=_.-]|$)", path):
            return f"tool input points at a sam.gov submit/sign endpoint: {url}"
    return None


def scrub(value: Any) -> Any:
    """Copy of a tool input with anything that looks like a key replaced.

    The match is on the field NAME only. A value-shape rule ("long opaque string")
    would swallow SAM notice ids (32 hex chars) and resource-link GUIDs, which are the
    identifiers the ledger row exists to preserve.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _SECRET_IN_QS.sub(r"\1[redacted]", value)
    return value


def event_model_id(event: Any) -> str:
    """Which model asked for the tool, read off the hook event's agent.

    ``BeforeToolCallEvent`` inherits ``agent`` from ``HookEvent`` (strands 1.55.1,
    ``hooks/registry.py``), and ``BedrockModel.config`` is a plain dict carrying
    ``model_id``. Anything else records "unknown" rather than guessing: the row exists to
    answer "which model was asked", so a wrong answer is worse than no answer.
    """
    model = getattr(getattr(event, "agent", None), "model", None)
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        value = config.get("model_id")
        if value:
            return str(value)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter("model_id")
        except Exception:                               # noqa: BLE001 - a hook never fails a run
            value = None
        if value:
            return str(value)
    return "unknown"


def _evidence(tool_input: Any, model_id: Optional[str] = None) -> str:
    try:
        payload = scrub(tool_input)
    except (TypeError, ValueError):
        payload = None
    if payload is None:
        return json.dumps({"input": str(tool_input), "model_id": model_id or "unknown"},
                          ensure_ascii=False)
    try:
        if model_id is not None and isinstance(payload, dict):
            payload = {**payload, "model_id": model_id}
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"input": str(tool_input), "model_id": model_id or "unknown"},
                          ensure_ascii=False)


def _notice_id(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("notice_id", "noticeId", "noticeid"):
            if tool_input.get(key):
                return str(tool_input[key])
    return "-"


# ------------------------------------------------------------------- no-submit


class NoSubmitGuard(HookProvider):
    """Cancel any tool call that would submit, sign, send or pay in the owner's name."""

    def __init__(
        self,
        firm_slug: str = "unknown",
        ledger: Optional[Ledger] = None,
        deny_tools: Iterable[str] = DEFAULT_DENY_TOOLS,
    ) -> None:
        self.firm_slug = firm_slug
        self.ledger = ledger if ledger is not None else Ledger(firm_slug)
        self.deny_tools = tuple(deny_tools)
        self.denials: list[dict] = []

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_use = getattr(event, "tool_use", None) or {}
        name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})
        reason = deny_reason(name, tool_input, self.deny_tools)
        if reason is None:
            return
        event.cancel_tool = DENIAL_MESSAGE.format(tool=name, reason=reason)
        model_id = event_model_id(event)
        self.denials.append({"tool": name, "reason": reason})
        self.ledger.write(
            notice_id=_notice_id(tool_input),
            action="denied_tool",
            why=f"{name}: {reason}",
            evidence=_evidence(tool_input, model_id=model_id),
            tier=0,
            undo=None,
        )


class NoSubmitIntervention(InterventionHandler):
    """The same rule as an intervention, so a crash in the matcher fails closed.

    ``strands.interventions`` exists in the installed SDK (1.55.1), so this class is
    live: ``Agent(interventions=[NoSubmitIntervention(firm_slug="acme")])``.
    ``Deny`` short-circuits the remaining handlers, and ``on_error='deny'`` blocks the
    call if this handler itself raises.
    """

    name = "biddesk-no-submit"

    def __init__(
        self,
        firm_slug: str = "unknown",
        ledger: Optional[Ledger] = None,
        deny_tools: Iterable[str] = DEFAULT_DENY_TOOLS,
    ) -> None:
        self.firm_slug = firm_slug
        self.ledger = ledger if ledger is not None else Ledger(firm_slug)
        self.deny_tools = tuple(deny_tools)

    @property
    def on_error(self) -> str:
        return "deny"

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any):
        tool_use = getattr(event, "tool_use", None) or {}
        name = tool_use.get("name", "")
        tool_input = tool_use.get("input", {})
        reason = deny_reason(name, tool_input, self.deny_tools)
        if reason is None:
            return Proceed()
        self.ledger.write(
            notice_id=_notice_id(tool_input),
            action="denied_tool",
            why=f"{name}: {reason}",
            evidence=_evidence(tool_input),
            tier=0,
        )
        return Deny(reason=DENIAL_MESSAGE.format(tool=name, reason=reason))


# ------------------------------------------------------------------ provenance


class ProvenanceStamp(HookProvider):
    """Append the origin of every fetched or extracted finding to the tool result."""

    def __init__(
        self,
        firm_slug: str = "unknown",
        ledger: Optional[Ledger] = None,
        tools: Iterable[str] = PROVENANCE_TOOLS,
    ) -> None:
        self.firm_slug = firm_slug
        self.ledger = ledger if ledger is not None else Ledger(firm_slug)
        self.tools = tuple(tools)

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_use = getattr(event, "tool_use", None) or {}
        name = tool_use.get("name", "")
        if name not in self.tools:
            return
        if getattr(event, "exception", None) is not None:
            return
        result = getattr(event, "result", None)
        if isinstance(result, dict) and result.get("status", "success") != "success":
            return  # a failed read is not a read; the ledger must not claim one
        tool_input = tool_use.get("input", {}) or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        stamp = {
            "notice_id": _notice_id(tool_input),
            "source_file": tool_input.get("source_file") or tool_input.get("file"),
            "page": tool_input.get("page"),
        }
        if isinstance(result, dict):
            content = result.setdefault("content", [])
            if isinstance(content, list):
                content.append(
                    {"text": json.dumps({"biddesk_provenance": stamp}, ensure_ascii=False)}
                )
        self.ledger.write(
            notice_id=stamp["notice_id"],
            action="read",
            why=f"{name} returned content stamped with its origin",
            evidence=json.dumps(stamp, ensure_ascii=False),
            tier=2,
        )


# ---------------------------------------------------------------- tier counter


class TierCounter(HookProvider):
    """Count model calls and tool calls. Resets per invocation unless ``persist``."""

    def __init__(self, persist: bool = False) -> None:
        self.persist = persist
        self.model_calls = 0
        self.tool_calls = 0
        self.per_tool: dict[str, int] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        if not self.persist:
            self.reset()

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        self.model_calls += 1

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        name = (getattr(event, "tool_use", None) or {}).get("name", "unknown")
        self.tool_calls += 1
        self.per_tool[name] = self.per_tool.get(name, 0) + 1

    def reset(self) -> None:
        self.model_calls = 0
        self.tool_calls = 0
        self.per_tool = {}

    def snapshot(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "per_tool": dict(self.per_tool),
            "persist": self.persist,
        }


# ------------------------------------------------------------------ redaction

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(
    r"(?<!\d)(?:\+1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
)


def redact(text: str) -> str:
    """Mask emails, phone numbers and SSN-shaped numbers. SSN first, then phone."""
    if not isinstance(text, str):
        return text
    out = _SSN.sub("[SSN REDACTED]", text)
    out = _EMAIL.sub("[EMAIL REDACTED]", out)
    out = _PHONE.sub("[PHONE REDACTED]", out)
    return out


class PiiRedactor(HookProvider):
    """Apply :func:`redact` to tool output text when ``redact_outputs=True``.

    Default off. SAM.gov publishes the contracting officer's name, email and phone as
    the point of contact; the owner needs them to ask a question before the deadline,
    so masking them by default would remove real product value. Enable it for traces or
    transcripts that leave this machine.
    """

    def __init__(self, redact_outputs: bool = False) -> None:
        self.redact_outputs = redact_outputs

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        if not self.redact_outputs:
            return
        result = getattr(event, "result", None)
        if not isinstance(result, dict):
            return
        content = result.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block["text"] = redact(block["text"])
