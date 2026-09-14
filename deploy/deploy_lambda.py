"""Create the biddesk-proxy Lambda and its public Function URL (auth NONE, CORS open).

The function holds the only AWS permission in the chain: bedrock-agentcore:InvokeAgentRuntime
on the Biddesk runtime. The browser never sees a credential.
"""
from __future__ import annotations

import io
import json
import time
import zipfile

from common import (LAMBDA_NAME, LAMBDA_ROLE_NAME, REGION, ROOT, account_id, client,
                    redact, say)
from invoke_runtime import runtime_arn

HANDLER_SRC = ROOT / "deploy" / "lambda_proxy" / "handler.py"
OUT = ROOT / "deploy" / "lambda.json"

TRUST = {"Version": "2012-10-17",
         "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                        "Action": "sts:AssumeRole"}]}


def role(arn_runtime: str) -> str:
    iam = client("iam")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "InvokeBiddeskRuntime", "Effect": "Allow",
         "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
         "Resource": [arn_runtime, arn_runtime + "/*"]},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": "arn:aws:logs:*:*:*"}]}
    try:
        iam.create_role(RoleName=LAMBDA_ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST),
                        Description="Biddesk public proxy Lambda")
        say("created role", LAMBDA_ROLE_NAME)
        time.sleep(10)  # let the new role propagate before Lambda assumes it
    except iam.exceptions.EntityAlreadyExistsException:
        say("role exists:", LAMBDA_ROLE_NAME)
    iam.put_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyName="biddesk-proxy-permissions",
                        PolicyDocument=json.dumps(policy))
    return iam.get_role(RoleName=LAMBDA_ROLE_NAME)["Role"]["Arn"]


def code_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo("handler.py")
        info.date_time = (2026, 9, 13, 0, 0, 0)
        info.external_attr = 0o644 << 16
        z.writestr(info, HANDLER_SRC.read_text(encoding="utf-8"))
    return buf.getvalue()


def main() -> dict:
    arn_runtime = runtime_arn()
    role_arn = role(arn_runtime)
    lam = client("lambda")
    zip_bytes = code_zip()
    env = {"Variables": {"RUNTIME_ARN": arn_runtime, "RUNTIME_QUALIFIER": "DEFAULT"}}

    try:
        lam.create_function(
            FunctionName=LAMBDA_NAME, Runtime="python3.13", Role=role_arn,
            Handler="handler.handler", Code={"ZipFile": zip_bytes},
            Timeout=900, MemorySize=512, Architectures=["arm64"], Environment=env,
            Description="Biddesk public proxy to the AgentCore Runtime")
        say("created function", LAMBDA_NAME)
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_bytes)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=LAMBDA_NAME)
        lam.update_function_configuration(FunctionName=LAMBDA_NAME, Role=role_arn,
                                          Timeout=900, MemorySize=512, Environment=env)
        say("updated function", LAMBDA_NAME)
    lam.get_waiter("function_active_v2").wait(FunctionName=LAMBDA_NAME)

    # AllowMethods members are capped at 6 characters, so "OPTIONS" is spelled "*"
    cors = {"AllowOrigins": ["*"], "AllowMethods": ["*"],
            "AllowHeaders": ["content-type"], "MaxAge": 86400}
    try:
        url = lam.create_function_url_config(FunctionName=LAMBDA_NAME, AuthType="NONE",
                                             Cors=cors)["FunctionUrl"]
    except lam.exceptions.ResourceConflictException:
        url = lam.update_function_url_config(FunctionName=LAMBDA_NAME, AuthType="NONE",
                                             Cors=cors)["FunctionUrl"]
    # Since October 2025 a public function URL needs BOTH actions in the resource
    # policy; with only InvokeFunctionUrl every anonymous request gets 403 Forbidden.
    # FunctionUrlAuthType is only accepted on the InvokeFunctionUrl statement.
    for sid, act, extra in (("public-function-url", "lambda:InvokeFunctionUrl",
                             {"FunctionUrlAuthType": "NONE"}),
                            ("public-function-invoke", "lambda:InvokeFunction", {})):
        try:
            lam.add_permission(FunctionName=LAMBDA_NAME, StatementId=sid, Action=act,
                               Principal="*", **extra)
            say("granted", act)
        except lam.exceptions.ResourceConflictException:
            say("already granted:", act)

    out = {"function_name": LAMBDA_NAME, "function_url": url, "region": REGION}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    say("function url:", url)
    return out


if __name__ == "__main__":
    main()
