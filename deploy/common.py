"""Shared helpers for the Biddesk deploy scripts.

Credentials are read from ``biddesk/.env`` by python-dotenv and handed to boto3
through the environment. Nothing here prints a key or an account id: the account
id is fetched once for the ARNs the API needs and is redacted everywhere it is
written to disk.
"""
from __future__ import annotations

import re
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

REGION = "us-east-1"
PREFIX = "biddesk-"
RUNTIME_NAME = "biddesk_runtime"          # the API rejects '-' in a runtime name
ROLE_NAME = "biddesk-agentcore-runtime"
LAMBDA_ROLE_NAME = "biddesk-lambda-proxy"
LAMBDA_NAME = "biddesk-proxy"
BUCKET_CODE = "biddesk-agentcore-code"     # + '-' + account id
BUCKET_SITE = "biddesk-site"               # + '-' + account id

LONG = Config(read_timeout=900, connect_timeout=15, retries={"max_attempts": 0})

_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")


def redact(text: str) -> str:
    return _ACCOUNT.sub("<account>", str(text))


def account_id() -> str:
    return boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]


def client(service: str, **kw):
    return boto3.client(service, region_name=REGION, **kw)


def say(*parts) -> None:
    print(redact(" ".join(str(p) for p in parts)), flush=True)
