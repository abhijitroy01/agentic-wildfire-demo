"""Read-only check that a release left the Runtime's Gateway client secret as it was (GatewayClientSecret review).

Capture a baseline before bash deployment/deploy-all.sh --apply, then verify it at least 15 minutes after the deploy:

    .venv/bin/python -m deployment.acceptance.check_gateway_secret capture --output BASELINE.json
    .venv/bin/python -m deployment.acceptance.check_gateway_secret verify --baseline BASELINE.json --output REPORT.json

It reads metadata only, never the value:
- the secret's KMS key, last-changed time, tags, resource policy, rotation and replication;
- every version and its stages, deprecated versions included;
- the KMS key's state and key policy;
- the CloudFormation records of GatewayClientSecret and GatewayClientSecretCopy.

verify compares these with the baseline. It then lists every Secrets Manager write to the secret since the
baseline, found in CloudTrail by ARN, by name and by event name, with its caller: CloudFormation, the copy Lambda's
role, or someone else (for example a rotation by hand). A changed GatewayClientSecret record is expected only in
the release that adds its Retain policies, so it is a note; anything else that differs is a finding. CloudTrail
can take about 15 minutes to show a call. Exit status 1 means a finding.
"""
import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

from deployment import ROOT
from deployment.acceptance.visitor import write_report
from deployment.deployment_target import safe_outputs
from deployment.installation import load_installation

# Every Secrets Manager call that writes a secret, its versions, tags, policy, rotation or replication.
WRITES = ("CreateSecret", "UpdateSecret", "PutSecretValue", "TagResource", "UntagResource", "PutResourcePolicy",
          "DeleteResourcePolicy", "RotateSecret", "CancelRotateSecret", "UpdateSecretVersionStage",
          "ReplicateSecretToRegions", "RemoveRegionsFromReplication", "StopReplicationToReplica", "DeleteSecret",
          "RestoreSecret")
# CloudTrail usually shows a call within about 5 minutes; allow 15.
DELIVERY = timedelta(minutes=15)
COMPARED = ("kms_key", "last_changed", "deleted", "tags", "policy_sha256", "rotation", "replication", "versions",
            "key_state", "key_policy_sha256", "key_matches_stack_output", "copy_record")
CLOUDFORMATION = "cloudformation.amazonaws.com"


def arguments(argv=None):
    """Parse the installation file and the capture or verify mode."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--installation", help="installation file (default: deployment/installation.toml)")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("capture", help="record the baseline before a deploy").add_argument(
        "--output", required=True, type=Path)
    verify = modes.add_parser("verify", help="compare with the baseline and list CloudTrail writes since it")
    verify.add_argument("--baseline", required=True, type=Path)
    verify.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def digest(text):
    """Return the SHA-256 of a policy document, or None when there is none."""
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def snapshot(session, target):
    """Read the metadata a release must leave unchanged, as JSON-ready values; never the secret value."""
    cfn, secrets, kms = (session.client(name) for name in ("cloudformation", "secretsmanager", "kms"))

    def detail(stack, logical):
        return cfn.describe_stack_resource(StackName=stack, LogicalResourceId=logical)["StackResourceDetail"]

    root = target.stack("backend")
    cognito = detail(root, "CognitoAuthStack")["PhysicalResourceId"]
    outputs = safe_outputs(cfn.describe_stacks(
        StackName=detail(root, "InfrastructureStack")["PhysicalResourceId"])["Stacks"][0])
    records = {name: detail(cognito, name) for name in ("GatewayClientSecret", "GatewayClientSecretCopy")}
    arn = records["GatewayClientSecret"]["PhysicalResourceId"]
    secret = secrets.describe_secret(SecretId=arn)
    versions, token = [], None
    while True:  # ListSecretVersionIds has no boto3 paginator
        page = secrets.list_secret_version_ids(SecretId=arn, IncludeDeprecated=True,
                                               **({"NextToken": token} if token else {}))
        versions += [{"id": v["VersionId"], "stages": sorted(v.get("VersionStages", [])),
                      "kms_keys": sorted(v.get("KmsKeyIds", []))} for v in page.get("Versions", [])]
        token = page.get("NextToken")
        if not token:
            break
    key = kms.describe_key(KeyId=secret.get("KmsKeyId") or "alias/aws/secretsmanager")["KeyMetadata"]
    found = {
        "captured_at": datetime.now(UTC).isoformat(), "secret_arn": arn, "secret_name": secret["Name"],
        "copy_role_arn": outputs.get("CustomResourceLambdaRoleArn"), "kms_key": secret.get("KmsKeyId"),
        "last_changed": secret.get("LastChangedDate"), "deleted": secret.get("DeletedDate"),
        "tags": {tag["Key"]: tag["Value"] for tag in secret.get("Tags", [])},
        "policy_sha256": digest(secrets.get_resource_policy(SecretId=arn).get("ResourcePolicy")),
        "rotation": bool(secret.get("RotationEnabled")),
        "replication": sorted([r.get("Region"), r.get("Status")] for r in secret.get("ReplicationStatus", [])),
        "versions": sorted(versions, key=lambda version: version["id"]),
        "key_state": {"state": key.get("KeyState"), "enabled": key.get("Enabled"), "manager": key.get("KeyManager")},
        "key_policy_sha256": digest(kms.get_key_policy(KeyId=key["KeyId"], PolicyName="default").get("Policy")),
        "key_matches_stack_output": key.get("Arn") == outputs.get("StackEncryptionKeyArn"),
        **{f"{label}_record": {"status": records[name].get("ResourceStatus"),
                               "updated": records[name].get("LastUpdatedTimestamp")}
           for label, name in (("secret", "GatewayClientSecret"), ("copy", "GatewayClientSecretCopy"))},
    }
    return json.loads(json.dumps(found, default=str))


def concerns(event, arn, name):
    """Whether a CloudTrail event concerns the secret, by its resources or by the request's secret ID."""
    if any(resource.get("ResourceName") in (arn, name) for resource in event.get("Resources", [])):
        return True
    request = json.loads(event.get("CloudTrailEvent") or "{}").get("requestParameters") or {}
    return request.get("secretId") in (arn, name) or request.get("name") == name


def described(event, baseline):
    """Name a write's time, event, principal and caller; never its request or response."""
    identity = json.loads(event.get("CloudTrailEvent") or "{}").get("userIdentity", {})
    issuer = identity.get("sessionContext", {}).get("sessionIssuer", {}).get("arn")
    if identity.get("invokedBy") == CLOUDFORMATION:
        caller = "CloudFormation"
    elif issuer and issuer == baseline.get("copy_role_arn"):
        caller = "the copy Lambda (GatewayClientSecretCopy)"
    else:
        caller = "outside CloudFormation (for example a rotation by hand)"
    return {"time": str(event.get("EventTime")), "event": event.get("EventName"),
            "principal": issuer or identity.get("arn"), "caller": caller}


def writes(cloudtrail, baseline, start, end):
    """Return every Secrets Manager write to the secret between start and end, oldest first."""
    arn, name = baseline["secret_arn"], baseline["secret_name"]
    found = {}
    for key, value in [("ResourceName", arn), ("ResourceName", name), *(("EventName", event) for event in WRITES)]:
        for page in cloudtrail.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": key, "AttributeValue": value}], StartTime=start, EndTime=end):
            for event in page.get("Events", []):
                if event.get("EventName") in WRITES and concerns(event, arn, name):
                    found[event["EventId"]] = described(event, baseline)
    return sorted(found.values(), key=lambda write: write["time"])


def verify(session, target, baseline):
    """Compare the secret with the baseline and list its CloudTrail writes since then; return the report."""
    current, now = snapshot(session, target), datetime.now(UTC)
    findings = [f"{field} changed from {baseline.get(field)!r} to {current.get(field)!r}"
                for field in COMPARED if baseline.get(field) != current.get(field)]
    notes = []
    if baseline.get("secret_record") != current.get("secret_record"):
        notes.append(f"the GatewayClientSecret record changed from {baseline.get('secret_record')!r} to "
                     f"{current.get('secret_record')!r}; expected only in the release that adds its Retain policies")
    events = writes(session.client("cloudtrail"), baseline, datetime.fromisoformat(baseline["captured_at"]), now)
    findings += [f"{write['event']} at {write['time']} by {write['principal']}: {write['caller']}" for write in events]
    return {"status": "failed" if findings else "passed", "findings": findings, "notes": notes, "writes": events,
            "cloudtrail_complete_until": (now - DELIVERY).isoformat(), "baseline": baseline, "current": current}


def main(argv=None):
    """Capture a baseline or verify against it; return 1 when verify finds a change or a write."""
    args = arguments(argv)
    installation = load_installation(args.installation, root=ROOT)
    target = installation.target
    session = boto3.Session(profile_name=installation.profile, region_name=target.region)
    if session.client("sts").get_caller_identity()["Account"] != target.account:
        raise SystemExit("The installation's profile belongs to another account")
    if args.mode == "capture":
        write_report(args.output, snapshot(session, target))
        print("Baseline captured:", args.output)
        return 0
    report = verify(session, target, json.loads(args.baseline.read_text()))
    if args.output:
        write_report(args.output, report)
    for note in report["notes"]:
        print("Note:", note)
    for finding in report["findings"]:
        print("Finding:", finding)
    print(f"CloudTrail is complete up to about {report['cloudtrail_complete_until']}; rerun later to cover later calls.")
    print("The Gateway client secret is unchanged." if report["status"] == "passed"
          else "The Gateway client secret changed; review the findings.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
