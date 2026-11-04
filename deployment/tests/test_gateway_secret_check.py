"""The read-only Gateway client secret check: metadata comparison and CloudTrail writes, against fakes only."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deployment.acceptance import check_gateway_secret as check

ACCOUNT = "123456789012"
SECRET = f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:training-gateway-client-AbCdEf"
NAME = "training-gateway-client"
KEY = f"arn:aws:kms:us-west-2:{ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
OTHER_KEY = f"arn:aws:kms:us-west-2:{ACCOUNT}:key/66666666-7777-8888-9999-000000000000"
COPY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/training-CustomResourceLambdaRole"
ADMIN = f"arn:aws:iam::{ACCOUNT}:role/Admin"
TARGET = SimpleNamespace(stack=lambda component: f"training-{component}", region="us-west-2", account=ACCOUNT)
LATER = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)


class Account:
    """CloudFormation, Secrets Manager, KMS and CloudTrail of one installation; it cannot return a secret value."""

    def __init__(self):
        self.kms_key, self.changed = KEY, datetime(2026, 10, 4, 4, 22, 4, tzinfo=UTC)
        self.versions = [{"VersionId": "v1", "VersionStages": ["AWSCURRENT"], "KmsKeyIds": [KEY]},
                         {"VersionId": "v0", "VersionStages": [], "KmsKeyIds": [KEY]}]
        self.updated = {"GatewayClientSecret": datetime(2026, 10, 4, 4, 21, 42, tzinfo=UTC),
                        "GatewayClientSecretCopy": datetime(2026, 10, 4, 4, 22, 5, tzinfo=UTC)}
        self.trail = {}

    def client(self, name):
        return {"cloudformation": SimpleNamespace(describe_stack_resource=self.resource, describe_stacks=self.stacks),
                "secretsmanager": SimpleNamespace(describe_secret=self.secret, list_secret_version_ids=self.listed,
                                                  get_resource_policy=lambda SecretId: {"Name": NAME}),
                "kms": SimpleNamespace(describe_key=self.key, get_key_policy=lambda **_: {"Policy": '{"Statement": []}'}),
                "cloudtrail": SimpleNamespace(get_paginator=self.paginator),
                "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT})}[name]

    def resource(self, StackName, LogicalResourceId):
        physical = {"CognitoAuthStack": "arn:cognito", "InfrastructureStack": "arn:infra",
                    "GatewayClientSecret": SECRET, "GatewayClientSecretCopy": "client-id"}[LogicalResourceId]
        return {"StackResourceDetail": {"PhysicalResourceId": physical, "ResourceStatus": "CREATE_COMPLETE",
                                        "LastUpdatedTimestamp": self.updated.get(LogicalResourceId)}}

    def stacks(self, StackName):
        assert StackName == "arn:infra"
        return {"Stacks": [{"Outputs": [{"OutputKey": "StackEncryptionKeyArn", "OutputValue": KEY},
                                        {"OutputKey": "CustomResourceLambdaRoleArn", "OutputValue": COPY_ROLE}]}]}

    def secret(self, SecretId):
        assert SecretId == SECRET
        return {"ARN": SECRET, "Name": NAME, "KmsKeyId": self.kms_key, "LastChangedDate": self.changed,
                "Tags": [{"Key": "Project", "Value": "training"}]}

    def listed(self, SecretId, IncludeDeprecated, NextToken=None):
        assert IncludeDeprecated, "deprecated versions carry no stage, so only this listing shows them"
        index = int(NextToken or 0)
        page = {"Versions": self.versions[index:index + 1]}
        if index + 1 < len(self.versions):
            page["NextToken"] = str(index + 1)
        return page

    def key(self, KeyId):
        return {"KeyMetadata": {"KeyId": KeyId, "Arn": KeyId, "KeyState": "Enabled", "Enabled": True,
                                "KeyManager": "CUSTOMER"}}

    def paginator(self, operation):
        assert operation == "lookup_events"

        def paginate(LookupAttributes, StartTime, EndTime):
            (lookup,) = LookupAttributes
            events = self.trail.get((lookup["AttributeKey"], lookup["AttributeValue"]), [])
            yield {"Events": events[:1]}
            yield {"Events": events[1:]}
        return SimpleNamespace(paginate=paginate)


def written(name, issuer, *, secret_id=SECRET, listed=True, invoked_by=None, event_id=None):
    """A CloudTrail Secrets Manager event; listed says whether CloudTrail names the secret as a resource."""
    record = {"userIdentity": {"arn": f"{issuer}/session", "invokedBy": invoked_by,
                               "sessionContext": {"sessionIssuer": {"arn": issuer}}},
              "requestParameters": {"secretId": secret_id}}
    return {"EventId": event_id or f"{name}-{issuer}", "EventName": name, "EventTime": "2026-10-14 20:00:00+00:00",
            "Resources": [{"ResourceName": secret_id}] if listed else [], "CloudTrailEvent": json.dumps(record)}


def test_an_unchanged_secret_passes_and_every_version_page_is_read():
    account = Account()
    baseline = check.snapshot(account, TARGET)
    assert [version["id"] for version in baseline["versions"]] == ["v0", "v1"]
    assert baseline["key_matches_stack_output"] and baseline["copy_role_arn"] == COPY_ROLE
    report = check.verify(account, TARGET, baseline)
    assert (report["status"], report["findings"], report["notes"], report["writes"]) == ("passed", [], [], [])


def test_a_rotation_by_hand_is_a_finding_made_outside_cloudformation():
    account = Account()
    baseline = check.snapshot(account, TARGET)
    account.changed = LATER
    account.versions = [{"VersionId": "v2", "VersionStages": ["AWSCURRENT"], "KmsKeyIds": [KEY]},
                        {"VersionId": "v1", "VersionStages": ["AWSPREVIOUS"], "KmsKeyIds": [KEY]}]
    account.trail[("ResourceName", SECRET)] = [written("PutSecretValue", ADMIN)]
    report = check.verify(account, TARGET, baseline)
    assert report["status"] == "failed"
    assert [f.split(" ")[0] for f in report["findings"]] == ["last_changed", "versions", "PutSecretValue"]
    assert report["findings"][-1].endswith(f"by {ADMIN}: outside CloudFormation (for example a rotation by hand)")


def test_a_copy_run_found_only_by_event_name_is_attributed_to_the_copy_lambda():
    account = Account()
    baseline = check.snapshot(account, TARGET)
    account.updated["GatewayClientSecretCopy"] = LATER
    account.trail[("EventName", "PutSecretValue")] = [
        written("PutSecretValue", COPY_ROLE, secret_id=NAME, listed=False),
        written("PutSecretValue", ADMIN, secret_id="another-secret", listed=False)]
    report = check.verify(account, TARGET, baseline)
    assert report["writes"] == [{"time": "2026-10-14 20:00:00+00:00", "event": "PutSecretValue", "principal": COPY_ROLE,
                                 "caller": "the copy Lambda (GatewayClientSecretCopy)"}]
    assert report["findings"][0].startswith("copy_record changed")


@pytest.mark.parametrize("change, finding", [
    (lambda account: setattr(account, "kms_key", OTHER_KEY), "kms_key changed"),
    (lambda account: account.trail.update({("ResourceName", NAME): [written(
        "UpdateSecret", ADMIN, secret_id=NAME, invoked_by="cloudformation.amazonaws.com")]}), "UpdateSecret at"),
])
def test_a_key_change_or_a_cloudformation_write_is_a_finding(change, finding):
    account = Account()
    baseline = check.snapshot(account, TARGET)
    change(account)
    report = check.verify(account, TARGET, baseline)
    assert report["status"] == "failed" and any(f.startswith(finding) for f in report["findings"])


def test_the_retain_release_only_notes_the_secret_record_change():
    account = Account()
    baseline = check.snapshot(account, TARGET)
    account.updated["GatewayClientSecret"] = LATER
    report = check.verify(account, TARGET, baseline)
    assert report["status"] == "passed" and report["findings"] == []
    assert report["notes"] and "expected only in the release that adds its Retain policies" in report["notes"][0]


def test_main_captures_then_exits_one_on_a_finding(monkeypatch, tmp_path, capsys):
    account = Account()
    installation = SimpleNamespace(target=TARGET, profile="training")
    monkeypatch.setattr(check, "load_installation", lambda path, root: installation)
    monkeypatch.setattr(check.boto3, "Session", lambda **_: account)
    baseline, report = tmp_path / "baseline.json", tmp_path / "report.json"
    assert check.main(["capture", "--output", str(baseline)]) == 0
    assert check.main(["verify", "--baseline", str(baseline), "--output", str(report)]) == 0
    account.kms_key = OTHER_KEY
    assert check.main(["verify", "--baseline", str(baseline), "--output", str(report)]) == 1
    assert json.loads(report.read_text())["status"] == "failed"
    assert "Finding: kms_key changed" in capsys.readouterr().out
