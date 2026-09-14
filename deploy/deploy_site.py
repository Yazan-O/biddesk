"""Publish web/ and gallery/ behind CloudFront with an Origin Access Control.

The bucket stays private (all four public-access blocks on); only the distribution
can read it, through OAC plus a bucket policy scoped to that distribution ARN.
Re-running uploads changed files and invalidates the cache.
"""
from __future__ import annotations

import json
import mimetypes
import time

from common import BUCKET_SITE, REGION, ROOT, account_id, client, redact, say

OUT = ROOT / "deploy" / "site.json"
OAC_NAME = "biddesk-site-oac"
COMMENT = "biddesk-site"

CACHE_MANAGED_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"  # CachingOptimized


def ensure_bucket(acct: str) -> str:
    bucket = f"{BUCKET_SITE}-{acct}"
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=bucket)
        say("bucket exists:", redact(bucket))
    except Exception:
        s3.create_bucket(Bucket=bucket)
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={"BlockPublicAcls": True, "IgnorePublicAcls": True,
                                            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        say("created private bucket:", redact(bucket))
    return bucket


def upload(bucket: str, api_url: str | None = None) -> int:
    """Mirror the repo layout the page expects: /web, /gallery, /data/bench.

    ``web/app.js`` resolves every fetch against ``BASE_PATH = "../"``, so the page
    must live one level down from the JSON it reads. The root object is a redirect
    to /web/, and the live endpoint is injected as ``web/config.js`` at upload time
    (the repo copy of index.html stays free of the deployed URL).
    """
    s3 = client("s3")
    n = 0
    trees = ((ROOT / "web", "web/"), (ROOT / "gallery", "gallery/"),
             (ROOT / "data" / "bench", "data/bench/"))
    for base, prefix in trees:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            key = prefix + str(path.relative_to(base)).replace("\\", "/")
            ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
            cache = "public, max-age=60" if key.endswith((".html", ".json", ".js")) else "public, max-age=3600"
            body = path.read_bytes()
            if key == "web/index.html" and b'<script src="app.js">' in body:
                body = body.replace(
                    b'<script src="app.js">',
                    b'<script src="config.js"></script>\n<script src="app.js">')
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype, CacheControl=cache)
            n += 1

    if api_url:
        s3.put_object(Bucket=bucket, Key="web/config.js",
                      Body=('window.BIDDESK_API = "%s";\n' % api_url).encode("utf-8"),
                      ContentType="application/javascript", CacheControl="public, max-age=60")
        n += 1
        say("live endpoint injected as web/config.js")

    s3.put_object(Bucket=bucket, Key="index.html", ContentType="text/html",
                  CacheControl="public, max-age=60",
                  Body=b'<!doctype html><meta charset="utf-8">'
                       b'<meta http-equiv="refresh" content="0; url=/web/">'
                       b'<title>Biddesk</title><a href="/web/">Biddesk</a>')
    n += 1
    say("uploaded objects:", n)
    return n


def ensure_oac() -> str:
    cf = client("cloudfront")
    for item in cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []):
        if item["Name"] == OAC_NAME:
            return item["Id"]
    return cf.create_origin_access_control(OriginAccessControlConfig={
        "Name": OAC_NAME, "Description": "Biddesk static site",
        "SigningProtocol": "sigv4", "SigningBehavior": "always",
        "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]["Id"]


INDEX_FN_NAME = "biddesk-index-rewrite"
INDEX_FN_CODE = """function handler(event) {
  var req = event.request;
  if (req.uri.endsWith('/')) { req.uri += 'index.html'; }
  return req;
}
"""


def ensure_index_function() -> str:
    """S3 origins have no directory index, so /web/ would 403. Rewrite it at the edge."""
    cf = client("cloudfront")
    try:
        got = cf.describe_function(Name=INDEX_FN_NAME, Stage="LIVE")
        say("cloudfront function exists:", INDEX_FN_NAME)
        return got["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]
    except cf.exceptions.NoSuchFunctionExists:
        pass
    created = cf.create_function(
        Name=INDEX_FN_NAME,
        FunctionConfig={"Comment": "append index.html to directory paths",
                        "Runtime": "cloudfront-js-2.0"},
        FunctionCode=INDEX_FN_CODE.encode("utf-8"))
    etag = created["ETag"]
    cf.publish_function(Name=INDEX_FN_NAME, IfMatch=etag)
    say("published cloudfront function:", INDEX_FN_NAME)
    return created["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]


def attach_index_function(dist_id: str, fn_arn: str) -> None:
    cf = client("cloudfront")
    got = cf.get_distribution_config(Id=dist_id)
    cfg, etag = got["DistributionConfig"], got["ETag"]
    assoc = {"Quantity": 1, "Items": [{"FunctionARN": fn_arn, "EventType": "viewer-request"}]}
    if cfg["DefaultCacheBehavior"].get("FunctionAssociations") == assoc:
        say("index rewrite already attached")
        return
    cfg["DefaultCacheBehavior"]["FunctionAssociations"] = assoc
    cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=cfg)
    say("attached index rewrite to", dist_id)


def find_distribution() -> dict | None:
    cf = client("cloudfront")
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for item in page.get("DistributionList", {}).get("Items", []):
            if item.get("Comment") == COMMENT:
                return item
    return None


def distribution_config(bucket: str, oac_id: str) -> dict:
    origin_id = "biddesk-s3"
    return {
        "CallerReference": f"biddesk-site-{int(time.time())}",
        "Comment": COMMENT,
        "Enabled": True,
        "DefaultRootObject": "index.html",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": origin_id,
            "DomainName": f"{bucket}.s3.{REGION}.amazonaws.com",
            "OriginAccessControlId": oac_id,
            "S3OriginConfig": {"OriginAccessIdentity": ""},
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "Compress": True,
            "CachePolicyId": CACHE_MANAGED_OPTIMIZED,
        },
        "PriceClass": "PriceClass_100",
        "HttpVersion": "http2and3",
    }


def bucket_policy(bucket: str, acct: str, dist_arn: str) -> None:
    client("s3").put_bucket_policy(Bucket=bucket, Policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowCloudFrontServicePrincipalReadOnly",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/*",
            "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}},
        }]}))
    say("bucket policy: CloudFront read only, no public bucket")


def main() -> dict:
    acct = account_id()
    bucket = ensure_bucket(acct)
    api_url = None
    lam = ROOT / "deploy" / "lambda.json"
    if lam.exists():
        api_url = json.loads(lam.read_text(encoding="utf-8")).get("function_url")
    upload(bucket, api_url)
    cf = client("cloudfront")

    dist = find_distribution()
    if dist:
        dist_id, domain = dist["Id"], dist["DomainName"]
        say("distribution exists:", dist_id)
    else:
        oac_id = ensure_oac()
        created = cf.create_distribution(
            DistributionConfig=distribution_config(bucket, oac_id))["Distribution"]
        dist_id, domain = created["Id"], created["DomainName"]
        say("created distribution:", dist_id)

    bucket_policy(bucket, acct, f"arn:aws:cloudfront::{acct}:distribution/{dist_id}")
    attach_index_function(dist_id, ensure_index_function())
    cf.create_invalidation(DistributionId=dist_id,
                           InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                                              "CallerReference": f"deploy-{int(time.time())}"})
    say("invalidated /*")

    out = {"distribution_id": dist_id, "site_url": f"https://{domain}/web/",
           "gallery_url": f"https://{domain}/gallery/index.json",
           "function_url": api_url, "region": REGION}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    say("site url:", out["site_url"])
    return out


if __name__ == "__main__":
    main()
