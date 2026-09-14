"""Single list call per service; prints OK or the exact error class and message."""
from common import client, say

CHECKS = {
    "lambda": lambda: client("lambda").list_functions(MaxItems=1),
    "s3": lambda: client("s3").list_buckets(),
    "cloudfront": lambda: client("cloudfront").list_distributions(MaxItems="1"),
    "iam": lambda: client("iam").list_roles(MaxItems=1),
    "bedrock-agentcore-control": lambda: client("bedrock-agentcore-control").list_agent_runtimes(maxResults=1),
    "codebuild": lambda: client("codebuild").list_projects(),
}

if __name__ == "__main__":
    for name, fn in CHECKS.items():
        try:
            fn()
            say(f"{name}: OK")
        except Exception as exc:  # noqa: BLE001
            say(f"{name}: {type(exc).__name__}: {str(exc)[:240]}")
