"""Invoke the deployed Biddesk Runtime with boto3 (SigV4 under the hood).

    python deploy/invoke_runtime.py '{"action":"health"}'

``runtimeSessionId`` must be 33 characters or more. The real ARN is resolved by
runtime name so no account id has to live in a file.
"""
from __future__ import annotations

import json
import sys
import time
import uuid

from common import LONG, RUNTIME_NAME, client, redact, say


def runtime_arn() -> str:
    ctl = client("bedrock-agentcore-control")
    for item in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", []):
        if item.get("agentRuntimeName") == RUNTIME_NAME:
            return item["agentRuntimeArn"]
    raise SystemExit(f"runtime {RUNTIME_NAME} not found")


def invoke(payload: dict, session_id: str | None = None) -> dict:
    data = client("bedrock-agentcore", config=LONG)
    started = time.time()
    resp = data.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn(),
        runtimeSessionId=session_id or f"biddesk-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        qualifier="DEFAULT",
        payload=json.dumps(payload).encode("utf-8"),
    )
    body = resp["response"].read()
    out = json.loads(body)
    say(f"# {json.dumps(payload)} -> {time.time() - started:.1f}s, HTTP "
        f"{resp['ResponseMetadata']['HTTPStatusCode']}")
    return out


if __name__ == "__main__":
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"action": "health"}
    print(redact(json.dumps(invoke(payload), indent=1)))
