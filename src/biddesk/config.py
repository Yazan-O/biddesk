"""Paths, environment, model ids. Loads .env from the repo root; never prints a key."""
from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
RAW = DATA / "raw"                 # raw API responses (gitignored)
SNAPSHOT = DATA / "snapshot"       # committed, normalized notices per profile
ATTACH = DATA / "attachments"      # cached attachments + extracted text
GALLERY = ROOT / "gallery"
RUNS = ROOT / "_runs"
for _p in (RAW, SNAPSHOT, ATTACH, GALLERY, RUNS):
    try:
        _p.mkdir(parents=True, exist_ok=True)
    except OSError:  # read-only package filesystem on the Runtime; the zip ships the folders
        pass

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_PRIMARY = os.environ.get("MODEL_PRIMARY", "us.anthropic.claude-sonnet-4-6")
MODEL_FALLBACK = os.environ.get("MODEL_FALLBACK", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
SAM_API_KEY = os.environ.get("SAM_API_KEY", "")
SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
OFFLINE = os.environ.get("BIDDESK_OFFLINE", "0") == "1"   # force the cached snapshot
