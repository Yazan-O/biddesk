"""Create (or refresh) the AgentCore Runtime execution role for Biddesk.

Trust: bedrock-agentcore.amazonaws.com, scoped to this account and to runtimes in
this account/Region. Permissions: Bedrock model invocation (including the streaming
call the refusal scene makes), CloudWatch Logs, the code bucket, and ECR read for
the managed base image.
"""
from __future__ import annotations

import json

from common import BUCKET_CODE, REGION, ROLE_NAME, account_id, client, say


def trust_policy(acct: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": acct},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{acct}:*"},
            },
        }],
    }


def inline_policy(acct: str) -> dict:
    bucket = f"{BUCKET_CODE}-{acct}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "BedrockModelInvocation", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                          f"arn:aws:bedrock:*:{acct}:inference-profile/*"]},
            {"Sid": "CloudWatchLogsAccess", "Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                        "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
             "Resource": f"arn:aws:logs:{REGION}:{acct}:log-group:/aws/bedrock-agentcore/*"},
            {"Sid": "CodePackageRead", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]},
            {"Sid": "EcrRead", "Effect": "Allow",
             "Action": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
             "Resource": "*"},
            {"Sid": "Observability", "Effect": "Allow",
             "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                        "cloudwatch:PutMetricData"],
             "Resource": "*"},
        ],
    }


def main() -> str:
    acct = account_id()
    iam = client("iam")
    try:
        iam.create_role(RoleName=ROLE_NAME,
                        AssumeRolePolicyDocument=json.dumps(trust_policy(acct)),
                        Description="Biddesk AgentCore Runtime execution role")
        say("created role", ROLE_NAME)
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=ROLE_NAME,
                                      PolicyDocument=json.dumps(trust_policy(acct)))
        say("role exists, trust policy refreshed:", ROLE_NAME)
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="biddesk-runtime-permissions",
                        PolicyDocument=json.dumps(inline_policy(acct)))
    arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    say("role arn:", arn)
    return arn


if __name__ == "__main__":
    main()
