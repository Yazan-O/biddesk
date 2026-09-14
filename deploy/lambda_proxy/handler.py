"""Public Function URL in front of the Biddesk AgentCore Runtime.

The phone posts JSON here; this signs the call to the Runtime with the function's
own role (SigV4, via boto3) and passes the JSON straight back. No key reaches the
browser. POST only, CORS open, and a 5 second ping path so the page can show
"waking up" without waiting on a desk run.

Environment: RUNTIME_ARN (set by deploy_lambda.py). Python 3.13 runtime; boto3 is
provided by Lambda.
"""
from __future__ import annotations

import json
import os
import re
import uuid

import boto3
from botocore.config import Config

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
QUALIFIER = os.environ.get("RUNTIME_QUALIFIER", "DEFAULT")

# a desk run is minutes long; the default 60 s read timeout would look like a hang
_CFG = Config(read_timeout=880, connect_timeout=10, retries={"max_attempts": 0})
_AGENTCORE = boto3.client("bedrock-agentcore", config=_CFG)

_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")

# CORS headers come from the Function URL's own CORS config (deploy_lambda.py). Adding them
# here too made the browser see "Access-Control-Allow-Origin: *, <origin>" and drop the answer
# (found 2026-09-14 from the live page), so the handler sends only the content type.
CORS = {
    "Content-Type": "application/json",
}

MAX_BODY = 64 * 1024


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": CORS,
            "body": _ACCOUNT.sub("<account>", json.dumps(body))}


def handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "").upper()
    path = event.get("rawPath", "/")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}
    if path.rstrip("/").endswith("/ping"):
        return _reply(200, {"ok": True, "service": "biddesk-proxy", "status": "Healthy"})
    if method != "POST":
        return _reply(405, {"ok": False, "error": "ValueError: POST only"})

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw) > MAX_BODY:
        return _reply(413, {"ok": False, "error": "ValueError: payload too large"})

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except Exception as exc:  # noqa: BLE001
        return _reply(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    session_id = payload.pop("session_id", None)
    if not isinstance(session_id, str) or len(session_id) < 33:
        session_id = f"biddesk-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"

    try:
        resp = _AGENTCORE.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=session_id,
            qualifier=QUALIFIER,
            payload=json.dumps(payload).encode("utf-8"),
        )
        body = resp["response"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return _reply(502, {"ok": False, "action": payload.get("action"),
                            "error": f"{type(exc).__name__}: {exc}"})

    return {"statusCode": 200, "headers": CORS, "body": _ACCOUNT.sub("<account>", body)}
