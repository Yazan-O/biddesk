# Biddesk deployment (us-east-1)

Live as of 2026-09-13 22:00 America/Chicago.

| Piece | Value |
| --- | --- |
| Public page | https://dpnzgd4gtjs45.cloudfront.net/web/ |
| Public API (Lambda Function URL) | https://5mzjweelnir5lszqlfosvoyt4m0rzdbr.lambda-url.us-east-1.on.aws/ |
| AgentCore Runtime | `arn:aws:bedrock-agentcore:us-east-1:<account-id>:runtime/biddesk_runtime-sBS5N4G0Fo` |
| Runtime status | READY (direct code deployment, PYTHON_3_13, ARM64) |
| Code package | `s3://biddesk-agentcore-code-<account-id>/biddesk_runtime/biddesk_runtime.zip` (32.5 MB, 4489 files) |
| Site bucket | `biddesk-site-<account-id>` (private, CloudFront OAC only) |
| CloudFront distribution | `EJS7ZC1JONY7Z` |
| Execution role | `biddesk-agentcore-runtime` |
| Proxy role | `biddesk-lambda-proxy` |

The account id is redacted everywhere on disk. Every script resolves the real ARNs
by resource name, so nothing here needs it. Credentials are read from `biddesk/.env`
by python-dotenv (`<from .env>`); no key is printed, logged, or packaged.

## Architecture

```
phone ──HTTPS──> CloudFront (OAC) ──> private S3 bucket        web/ + gallery/ + data/bench/
  │
  └──POST JSON──> Lambda Function URL (auth NONE, CORS *)
                      └── SigV4 (function role) ──> AgentCore Runtime ──> Bedrock
```

The browser holds no credential. The only AWS permission in the public path is
`bedrock-agentcore:InvokeAgentRuntime` on this one runtime, held by the function's role.

## Commands actually run

```bash
# 0. permissions preflight (one list call per service)
python deploy/preflight.py
#   lambda: OK   s3: OK   cloudfront: OK   iam: OK
#   bedrock-agentcore-control: OK   codebuild: OK

# 1. build the code zip (linux/aarch64 wheels + src/ + data/ + gallery/)
python deploy/build_zip.py
#   source files: 22 | snapshot 3 | bench 3 | geo 2 | ledger 3 | extracted.json 112 | gallery 9
#   secret gate: 0 staged files contain a credential value
#   zipped files: 4489 | zip size: 32.5 MB

# 2. execution role (trust: bedrock-agentcore.amazonaws.com, scoped to this account)
python deploy/create_role.py
#   created role biddesk-agentcore-runtime

# 3. upload + create_agent_runtime with agentRuntimeArtifact.codeConfiguration
python deploy/create_runtime.py
#   created private bucket biddesk-agentcore-code-<account-id>
#   uploaded biddesk_runtime/biddesk_runtime.zip (32.5 MB)
#   status: READY

# 4. invoke through boto3 (runtimeSessionId is 33+ chars)
python deploy/invoke_runtime.py '{"action":"health"}'
python deploy/invoke_runtime.py '{"action":"triage","firm":"red-cedar"}'
python deploy/invoke_runtime.py '{"action":"desk","firm":"red-cedar","notice_id":"4f8dba02","model":"haiku"}'

# 5. public proxy: Lambda (python3.13, arm64, 900 s timeout) + Function URL auth NONE
python deploy/deploy_lambda.py
#   function url: https://5mzjweelnir5lszqlfosvoyt4m0rzdbr.lambda-url.us-east-1.on.aws/

# 6. static site: private bucket + CloudFront OAC + index rewrite function
python deploy/deploy_site.py
#   site url: https://dpnzgd4gtjs45.cloudfront.net/web/
```

Rerun `deploy/deploy_site.py` after any change under `web/` or `gallery/`; it
re-uploads and invalidates `/*`. Rerun `build_zip.py` then `create_runtime.py`
after any change under `src/` (the second call updates the existing runtime).

## Verification (public, no credentials)

```bash
curl -s https://5mzjweelnir5lszqlfosvoyt4m0rzdbr.lambda-url.us-east-1.on.aws/ping
# {"ok": true, "service": "biddesk-proxy", "status": "Healthy"}

curl -s -X POST https://5mzjweelnir5lszqlfosvoyt4m0rzdbr.lambda-url.us-east-1.on.aws \
  -H 'content-type: application/json' -d '{"action":"triage","firm":"sooner-systems"}'
# {"ok": true, ..., "counts": {"total": 115, "tier0_filed": 69, "tier1_filed": 41, "tier2_sent": 5},
#  "cached": true, "reason": "SAM.gov API quota", "as_of": "2026-09-13T20:14:45-05:00"}

curl -s -o /dev/null -w '%{http_code}\n' https://dpnzgd4gtjs45.cloudfront.net/web/      # 200
curl -s -o /dev/null -w '%{http_code}\n' https://dpnzgd4gtjs45.cloudfront.net/gallery/index.json  # 200
```

Full transcripts: `_runs/2026-09-13_phase6_deploy/curl_function_url.log`, `curl_site.log`,
`curl_local.log`, `invoke_runtime.log`.

## Files here

| File | What it does |
| --- | --- |
| `common.py` | dotenv load, region/name constants, account-id redaction, long-timeout botocore config |
| `preflight.py` | one list call per service; prints OK or the exact error class and message |
| `build_zip.py` | stages the package, vendors aarch64 wheels with `uv`, runs the secret gate, writes the zip |
| `create_role.py` | execution role: trust policy + Bedrock/Logs/S3/ECR/X-Ray permissions |
| `create_runtime.py` | uploads the zip, creates or updates the runtime, waits for READY |
| `invoke_runtime.py` | boto3 `invoke_agent_runtime`; also used as a library by `deploy_lambda.py` |
| `lambda_proxy/handler.py` | the public proxy: POST only, CORS, 5 s `/ping`, SigV4 to the runtime |
| `deploy_lambda.py` | function, role, Function URL (auth NONE), public invoke permissions |
| `deploy_site.py` | S3 upload, OAC, distribution, index-rewrite function, invalidation |
| `urls.json` | the three public values, account id redacted |
| `runtime.json`, `lambda.json`, `site.json` | what each step produced |

## Three traps that cost a cycle

1. **`pip --platform` resolves markers for the build machine.** Installing
   `strands-agents` for `manylinux2014_aarch64` from Windows failed with
   `ResolutionImpossible` because `mcp` requires `pywin32` on win32. `uv pip install
   --python-platform aarch64-manylinux2014` resolves markers for the *target* and
   succeeds. All 9 native `.so` files in the zip are ELF `e_machine` 183 (AArch64).
2. **A public Function URL needs two actions since October 2025.** With only
   `lambda:InvokeFunctionUrl` in the resource policy every anonymous request returned
   `403 Forbidden` / `x-amzn-ErrorType: AccessDeniedException`, despite `AuthType: NONE`.
   Adding a second statement for `lambda:InvokeFunction` (without `FunctionUrlAuthType`,
   which that action rejects) fixed it. Also `Cors.AllowMethods` members are capped at
   6 characters, so `OPTIONS` must be spelled `*`.
3. **An S3 origin has no directory index.** `/web/` returned 403 until a CloudFront
   viewer-request function (`biddesk-index-rewrite`) appended `index.html` to paths
   ending in `/`.

## Cleanup

```bash
python - <<'PY'
from deploy.common import client
client("cloudfront")      # disable + delete distribution EJS7ZC1JONY7Z first
client("lambda").delete_function(FunctionName="biddesk-proxy")
client("bedrock-agentcore-control").delete_agent_runtime(agentRuntimeId="biddesk_runtime-sBS5N4G0Fo")
PY
```
Then empty and delete the two buckets and the two roles.
