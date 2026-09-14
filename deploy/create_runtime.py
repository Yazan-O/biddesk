"""Upload the code zip and create (or update) the Biddesk AgentCore Runtime.

Direct code deployment: no Docker, no CodeBuild, no CloudFormation. The zip built by
``build_zip.py`` goes to a private S3 bucket in this account; ``create_agent_runtime``
points at it with ``agentRuntimeArtifact.codeConfiguration``.
"""
from __future__ import annotations

import json
import time

from common import (BUCKET_CODE, LONG, REGION, ROLE_NAME, RUNTIME_NAME, ROOT,
                    account_id, client, redact, say)

ZIP_PATH = ROOT / "_runs" / "2026-09-13_phase6_deploy" / "biddesk_runtime.zip"
KEY = f"{RUNTIME_NAME}/biddesk_runtime.zip"
OUT = ROOT / "deploy" / "runtime.json"


def ensure_bucket(acct: str) -> str:
    bucket = f"{BUCKET_CODE}-{acct}"
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=bucket)
        say("bucket exists:", redact(bucket))
    except Exception:
        s3.create_bucket(Bucket=bucket)  # us-east-1 takes no LocationConstraint
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={"BlockPublicAcls": True, "IgnorePublicAcls": True,
                                            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        say("created private bucket:", redact(bucket))
    return bucket


def upload(bucket: str, acct: str) -> None:
    s3 = client("s3", config=LONG)
    s3.upload_file(str(ZIP_PATH), bucket, KEY, ExtraArgs={"ExpectedBucketOwner": acct})
    size = s3.head_object(Bucket=bucket, Key=KEY)["ContentLength"]
    say(f"uploaded {KEY} ({size / 1e6:.1f} MB)")


def artifact(bucket: str) -> dict:
    return {"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": KEY}},
                                  "runtime": "PYTHON_3_13",
                                  "entryPoint": ["main.py"]}}


def wait_ready(ctl, runtime_id: str, timeout: int = 900) -> str:
    deadline = time.time() + timeout
    status = "CREATING"
    while time.time() < deadline:
        got = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = got["status"]
        if status not in ("CREATING", "UPDATING"):
            break
        time.sleep(10)
    say("status:", status)
    if status != "READY":
        say("failure reason:", redact(json.dumps(got.get("statusReason", got), default=str))[:500])
    return status


def main() -> dict:
    acct = account_id()
    bucket = ensure_bucket(acct)
    upload(bucket, acct)

    role_arn = client("iam").get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    ctl = client("bedrock-agentcore-control", config=LONG)

    existing = None
    for item in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", []):
        if item.get("agentRuntimeName") == RUNTIME_NAME:
            existing = item["agentRuntimeId"]
            break

    kwargs = dict(
        agentRuntimeArtifact=artifact(bucket),
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=role_arn,
    )
    if existing:
        resp = ctl.update_agent_runtime(agentRuntimeId=existing, **kwargs)
        say("updating runtime", existing)
    else:
        resp = ctl.create_agent_runtime(
            agentRuntimeName=RUNTIME_NAME,
            description="Biddesk: one bid/no-bid card per real SAM.gov candidate",
            lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 3600},
            **kwargs)
        say("creating runtime", RUNTIME_NAME)

    arn = resp["agentRuntimeArn"]
    runtime_id = arn.split("/")[-1]
    status = wait_ready(ctl, runtime_id)
    # every file on disk carries the redacted ARN; scripts resolve the real one by name
    out = {"runtime_arn": redact(arn), "runtime_id": runtime_id, "status": status,
           "region": REGION, "code": redact(f"s3://{bucket}/{KEY}")}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    say("runtime arn:", arn)
    return out


if __name__ == "__main__":
    main()
