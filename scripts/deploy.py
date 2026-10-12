#!/usr/bin/env python3
"""Deploy one isolated IAM-authenticated SDK test Runtime, or clean up its resources."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import zipfile

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(os.environ.get("TMPDIR", ROOT / ".tmp")) / "deployment"
DEFAULT_STATE = ROOT / "docs" / "deployment.local.json"
PROJECT = "agentcore-e2b-sdk"


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def wait_ready(control, runtime_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = result["status"]
        if status == "READY":
            return result
        if "FAILED" in status:
            raise RuntimeError(f"Runtime failed: {result.get('failureReason', status)}")
        print(f"Runtime status: {status}", flush=True)
        time.sleep(3)
    raise TimeoutError("Runtime did not become READY; resources remain in the state file")


def bundle():
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / "adapter.zip"
    entrypoint = (
        "from agentcore_sandbox.server import main\n"
        "if __name__ == '__main__':\n"
        "    main(['--host', '0.0.0.0'])\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo("main.py")
        entry.external_attr = 0o100644 << 16
        archive.writestr(entry, entrypoint)
        for source in sorted((ROOT / "src").rglob("*.py")):
            archive.write(source, source.relative_to(ROOT / "src"))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def clients(region):
    session = boto3.Session(region_name=region)
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 2})
    return {name: session.client(name, config=config) for name in
            ["sts", "s3", "iam", "logs", "bedrock-agentcore-control"]}


def policy(state):
    account, region, name = state["account"], state["region"], state["name"]
    log = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{name}-*"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"arn:aws:s3:::{state['bucket']}/code/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                                         "logs:PutLogEvents", "logs:DescribeLogStreams"],
         "Resource": [log, log + ":log-stream:*"]},
    ]}


def artifact_config(state, key):
    return dict(
        roleArn=state["role_arn"],
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": state["bucket"], "prefix": key}},
            "runtime": "PYTHON_3_12", "entryPoint": ["main.py"]}},
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
        environmentVariables={
            "AGENTCORE_SANDBOX_WORKSPACE": state["workspace"],
            "AGENTCORE_SANDBOX_BOOT_FILE": state["boot_file"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "/var/task",
        },
    )


def deploy(args):
    if args.state.exists():
        raise RuntimeError("State file already exists; use update/status or a different --state")
    cloud = clients(args.region)
    account = cloud["sts"].get_caller_identity()["Account"]
    suffix = uuid.uuid4().hex[:12]
    name = "e2bsdk_" + suffix
    state = dict(project=PROJECT, account=account, region=args.region, name=name,
                 bucket=f"e2bsdk-{account}-{suffix}", role_name=name,
                 workspace="/home/agentcore-runtime-user/agentcore-sandbox/workspace",
                 boot_file="/home/agentcore-runtime-user/agentcore-sandbox/run/boot.json",
                 module_path="/var/task", artifacts=[], created_at=time.time())
    save(args.state, state)
    s3, iam, control = cloud["s3"], cloud["iam"], cloud["bedrock-agentcore-control"]
    create = {"Bucket": state["bucket"]}
    if args.region != "us-east-1":
        create["CreateBucketConfiguration"] = {"LocationConstraint": args.region}
    s3.create_bucket(**create)
    state["bucket_created"] = True
    save(args.state, state)
    s3.put_public_access_block(Bucket=state["bucket"], PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=state["bucket"], ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_tagging(Bucket=state["bucket"], Tagging={"TagSet": [{"Key": "Project", "Value": PROJECT}]})
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole", "Condition": {
            "StringEquals": {"aws:SourceAccount": account},
            "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{args.region}:{account}:*"}}}]}
    response = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                               Description="Isolated AgentCore SDK contract test Runtime",
                               Tags=[{"Key": "Project", "Value": PROJECT}])
    state["role_arn"] = response["Role"]["Arn"]
    save(args.state, state)
    iam.put_role_policy(RoleName=name, PolicyName=PROJECT, PolicyDocument=json.dumps(policy(state)))
    path, digest = bundle()
    key = f"code/{digest}.zip"
    s3.upload_file(str(path), state["bucket"], key, ExtraArgs={"ExpectedBucketOwner": account})
    state["artifacts"].append(key)
    save(args.state, state)
    time.sleep(8)
    created = control.create_agent_runtime(agentRuntimeName=name,
        description="AgentCore E2B-style client SDK isolated acceptance test",
        tags={"Project": PROJECT}, **artifact_config(state, key))
    state.update(runtime_id=created["agentRuntimeId"], runtime_arn=created["agentRuntimeArn"],
                 qualifier="DEFAULT", source_sha256=digest)
    save(args.state, state)
    ready = wait_ready(control, state["runtime_id"])
    state.update(status=ready["status"], runtime_version=ready["agentRuntimeVersion"])
    save(args.state, state)
    print(json.dumps({k: state[k] for k in ["runtime_arn", "region", "qualifier", "workspace", "status"]}, indent=2))


def load_owned(args):
    state = json.loads(args.state.read_text())
    if state.get("project") != PROJECT or not state.get("name", "").startswith("e2bsdk_"):
        raise RuntimeError("State does not identify resources owned by this project")
    cloud = clients(state["region"])
    if cloud["sts"].get_caller_identity()["Account"] != state["account"]:
        raise RuntimeError("AWS account differs from the deployment state")
    return state, cloud


def update(args):
    state, cloud = load_owned(args)
    path, digest = bundle()
    key = f"code/{digest}.zip"
    cloud["s3"].upload_file(str(path), state["bucket"], key,
                             ExtraArgs={"ExpectedBucketOwner": state["account"]})
    if key not in state["artifacts"]:
        state["artifacts"].append(key)
    save(args.state, state)
    control = cloud["bedrock-agentcore-control"]
    result = control.update_agent_runtime(agentRuntimeId=state["runtime_id"], **artifact_config(state, key))
    version = result["agentRuntimeVersion"]
    wait_ready(control, state["runtime_id"])
    # Current Runtime service manages DEFAULT as part of UpdateAgentRuntime;
    # explicitly updating it returns ConflictException. Named endpoints still
    # require an explicit version update.
    qualifier = state.get("qualifier", "DEFAULT")
    if qualifier != "DEFAULT":
        control.update_agent_runtime_endpoint(agentRuntimeId=state["runtime_id"],
                                             endpointName=qualifier, agentRuntimeVersion=version)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=state["runtime_id"], endpointName=qualifier)
        if endpoint.get("status") == "READY" and endpoint.get("liveVersion") == version:
            break
        time.sleep(3)
    else:
        raise TimeoutError("Runtime Endpoint did not route to the new version")
    state.update(source_sha256=digest, runtime_version=version, status="READY")
    save(args.state, state)
    print("Updated Runtime and DEFAULT Endpoint to version", version)


def resume(args):
    """Resume resource creation after IAM propagation or Runtime validation failure."""
    state, cloud = load_owned(args)
    if state.get("runtime_id"):
        ready = wait_ready(cloud["bedrock-agentcore-control"], state["runtime_id"])
        state.update(status=ready["status"], runtime_version=ready["agentRuntimeVersion"])
        save(args.state, state)
        print("Runtime READY:", state["runtime_arn"])
        return
    account, region = state["account"], state["region"]
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole", "Condition": {
            "StringEquals": {"aws:SourceAccount": account},
            "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}}}]}
    cloud["iam"].update_assume_role_policy(RoleName=state["role_name"], PolicyDocument=json.dumps(trust))
    path, digest = bundle()
    key = f"code/{digest}.zip"
    cloud["s3"].upload_file(str(path), state["bucket"], key, ExtraArgs={"ExpectedBucketOwner": account})
    if key not in state["artifacts"]:
        state["artifacts"].append(key)
    token = state.setdefault("create_token", str(uuid.uuid4()))
    save(args.state, state)
    time.sleep(8)
    control = cloud["bedrock-agentcore-control"]
    created = control.create_agent_runtime(agentRuntimeName=state["name"], clientToken=token,
        description="AgentCore E2B-style client SDK isolated acceptance test",
        tags={"Project": PROJECT}, **artifact_config(state, key))
    state.update(runtime_id=created["agentRuntimeId"], runtime_arn=created["agentRuntimeArn"],
                 qualifier="DEFAULT", source_sha256=digest)
    save(args.state, state)
    ready = wait_ready(control, state["runtime_id"])
    state.update(status=ready["status"], runtime_version=ready["agentRuntimeVersion"])
    save(args.state, state)
    print("Runtime READY:", state["runtime_arn"])


def cleanup(args):
    state, cloud = load_owned(args)
    control = cloud["bedrock-agentcore-control"]
    if state.get("runtime_id"):
        try:
            control.delete_agent_runtime(agentRuntimeId=state["runtime_id"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                control.get_agent_runtime(agentRuntimeId=state["runtime_id"])
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                    break
                raise
            time.sleep(3)
        else:
            raise TimeoutError("Runtime still deleting; rerun cleanup later")
    if state.get("bucket_created"):
        for key in state["artifacts"]:
            cloud["s3"].delete_object(Bucket=state["bucket"], Key=key,
                                      ExpectedBucketOwner=state["account"])
        try:
            cloud["s3"].delete_bucket(Bucket=state["bucket"], ExpectedBucketOwner=state["account"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchBucket":
                raise
    if state.get("role_arn"):
        for operation in [lambda: cloud["iam"].delete_role_policy(RoleName=state["role_name"], PolicyName=PROJECT),
                          lambda: cloud["iam"].delete_role(RoleName=state["role_name"])]:
            try:
                operation()
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NoSuchEntity":
                    raise
    state.update(status="DELETED", cleaned_at=time.time())
    save(args.state, state)
    print("Deleted this test Runtime, recorded S3 artifacts/bucket and execution role.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "resume", "update", "status", "cleanup"])
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.action == "status":
        state, cloud = load_owned(args)
        current = cloud["bedrock-agentcore-control"].get_agent_runtime(agentRuntimeId=state["runtime_id"])
        print(json.dumps({"status": current["status"], "version": current["agentRuntimeVersion"]}))
    else:
        globals()[args.action](args)


if __name__ == "__main__":
    main()
