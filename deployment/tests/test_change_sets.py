import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from deployment import change_sets
from deployment.change_sets import (
    STATEFUL_TYPES,
    UNRESOLVED,
    deploy_guarded,
    execute_guarded,
    parse_acknowledgment,
    stateful_risks,
)
from deployment.validate_infra_baseline import load_template

ROOT_SET = "arn:aws:cloudformation:us-west-2:123456789012:changeSet/release/root"
NESTED_SET = "arn:aws:cloudformation:us-west-2:123456789012:changeSet/release/nested"
TEMPLATES = Path(__file__).resolve().parents[2] / "backend/deployment/templates"
BACKEND = "training-backend"
STACKS = {name: f"arn:aws:cloudformation:us-west-2:123456789012:stack/{BACKEND}-{name}/fixture"
          for name in ("root", "cognito", "lambda", "infra", "agentcore")}
SETS = {name: f"arn:aws:cloudformation:us-west-2:123456789012:changeSet/release-{name}/fixture" for name in STACKS}
NESTED = {"cognito": "CognitoAuthStack", "lambda": "LambdaFunctionsStack", "infra": "InfrastructureStack",
          "agentcore": "AgentCoreStack"}


def change(logical, kind, action="Modify", replacement="False", **extra):
    return {"ResourceChange": {"LogicalResourceId": logical, "ResourceType": kind, "Action": action,
                               "Replacement": replacement, **extra}}


def cloudformation(*pages, nested=(), status="CREATE_COMPLETE", reason=""):
    """Stub CloudFormation: the root change set has the given pages; one nested stack holds the nested changes."""
    client = MagicMock()
    root_pages = list(pages)

    def describe(ChangeSetName, StackName=None, NextToken=None, IncludePropertyValues=False):
        if ChangeSetName == NESTED_SET:
            return {"Changes": list(nested)}
        if ChangeSetName != ROOT_SET:
            assert StackName == "training-application"
            return {"Status": status, "StatusReason": reason, "ChangeSetId": ROOT_SET}
        index = int(NextToken or 0)
        page = {"Changes": root_pages[index]}
        if index + 1 < len(root_pages):
            page["NextToken"] = str(index + 1)
        return page
    client.describe_change_set.side_effect = describe
    return client


def run(client, tmp_path, created=False):
    return execute_guarded(client, "training-application", "release-1-application-bind", created=created,
                           receipt=tmp_path / "changes.json")


def test_safe_change_set_executes_and_records_every_page(tmp_path):
    client = cloudformation([change("Api", "AWS::ECS::Service")],
                            [change("Function", "AWS::Lambda::Function", replacement="True")])
    summary = run(client, tmp_path)
    client.execute_change_set.assert_called_once_with(ChangeSetName=ROOT_SET)
    client.get_waiter.assert_called_once_with("stack_update_complete")
    assert summary["executed"] and summary["stateful_risks"] == []
    assert [c["LogicalResourceId"] for c in summary["changes"]] == ["Api", "Function"]
    assert json.loads((tmp_path / "changes.json").read_text())["result"] == "executed"


@pytest.mark.parametrize("risky,expected", [
    (change("Jobs", "AWS::DynamoDB::Table", replacement="True"), "replace Jobs (AWS::DynamoDB::Table)"),
    (change("Audio", "AWS::S3::Bucket", action="Remove", replacement=None), "delete Audio (AWS::S3::Bucket)"),
    (change("Logs", "AWS::Logs::LogGroup", replacement="Conditional"), "possibly replace Logs (AWS::Logs::LogGroup)"),
    (change("Pool", "AWS::Cognito::UserPool", action="Dynamic", replacement=None), "possibly replace Pool"),
])
def test_stateful_replacement_or_deletion_stops_before_execution(tmp_path, risky, expected):
    client = cloudformation([change("Api", "AWS::ECS::Service"), risky])
    with pytest.raises(ValueError, match="Stopped before changing training-application") as stopped:
        run(client, tmp_path)
    assert expected in str(stopped.value) and "Nothing was executed" in str(stopped.value)
    client.execute_change_set.assert_not_called()
    receipt = json.loads((tmp_path / "changes.json").read_text())
    assert receipt["result"] == "stopped" and receipt["stateful_risks"]


def test_nested_stack_changes_are_inspected(tmp_path):
    client = cloudformation([change("Infrastructure", "AWS::CloudFormation::Stack", ChangeSetId=NESTED_SET)],
                            nested=[change("KnowledgeBucket", "AWS::S3::Bucket", replacement="True")])
    with pytest.raises(ValueError, match=r"replace KnowledgeBucket \(AWS::S3::Bucket\)"):
        run(client, tmp_path)
    client.execute_change_set.assert_not_called()


def test_new_stack_waits_for_creation(tmp_path):
    client = cloudformation([change("Jobs", "AWS::DynamoDB::Table", action="Add", replacement=None)])
    assert run(client, tmp_path, created=True)["executed"]
    client.get_waiter.assert_called_once_with("stack_create_complete")


@pytest.mark.parametrize("status,reason", [("FAILED", "The submitted information didn't contain changes."),
                                           ("FAILED", "No updates are to be performed.")])
def test_empty_change_set_executes_nothing(tmp_path, status, reason):
    client = cloudformation([], status=status, reason=reason)
    assert run(client, tmp_path)["result"] == "no changes"
    client.execute_change_set.assert_not_called()


def test_change_set_removed_as_empty_executes_nothing(tmp_path):
    client = MagicMock()
    client.describe_change_set.side_effect = ClientError(
        {"Error": {"Code": "ChangeSetNotFound", "Message": "missing"}}, "DescribeChangeSet")
    assert run(client, tmp_path)["result"] == "no changes"
    client.execute_change_set.assert_not_called()


def test_failed_change_set_raises_its_reason(tmp_path):
    client = cloudformation([], status="FAILED", reason="Parameter RequestsPerIpPer5Minutes has no value")
    with pytest.raises(RuntimeError, match="has no value"):
        run(client, tmp_path)
    client.execute_change_set.assert_not_called()


def test_preparation_wait_is_bounded(tmp_path, monkeypatch):
    client = cloudformation([], status="CREATE_PENDING")
    monkeypatch.setattr(change_sets.time, "sleep", lambda _: None)
    clock = iter(range(0, 10_000, 400))
    monkeypatch.setattr(change_sets.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="preparation timed out"):
        run(client, tmp_path)


def test_stateful_types_cover_the_protected_resources():
    for kind in ("AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::ECR::Repository", "AWS::SecretsManager::Secret",
                 "AWS::KMS::Key", "AWS::Cognito::UserPool", "AWS::Cognito::UserPoolClient", "AWS::Logs::LogGroup",
                 "AWS::Bedrock::KnowledgeBase", "AWS::Bedrock::DataSource"):
        assert kind in STATEFUL_TYPES
    assert stateful_risks([change("Role", "AWS::IAM::Role", action="Remove")["ResourceChange"]]) == []


# A backend release as DescribeChangeSet shows it: the root, four nested change sets and the real templates.

def detail(name, evaluation, source, *, after, entity=None, recreation="Never", attribute="Properties"):
    """One change detail as DescribeChangeSet with IncludePropertyValues returns it."""
    target = {"Attribute": attribute, "RequiresRecreation": recreation, "AttributeChangeType": "Modify",
              "BeforeValue": "before-value", "AfterValue": after}
    if attribute == "Properties":
        target.update(Name=name, Path=f"/Properties/{name}")
    return {"Evaluation": evaluation, "ChangeSource": source, "CausingEntity": entity, "Target": target}


def placeholder(name, parameter, recreation="Never"):
    """The Static/Dynamic pair listed for a property whose parameter comes from a sibling stack's output."""
    return [detail(name, "Static", "ParameterReference", after=UNRESOLVED, entity=parameter, recreation=recreation),
            detail(name, "Dynamic", "DirectModification", after=UNRESOLVED, recreation=recreation)]


def concrete(name, after="new-value", attribute="Properties"):
    return detail(name, "Static", "DirectModification", after=after, attribute=attribute)


def entry(logical, kind, *details, action="Modify", replacement="False", **extra):
    scope = sorted({d["Target"]["Attribute"] for d in details}) or ["Properties"]
    return {"LogicalResourceId": logical, "ResourceType": kind, "Action": action, "Replacement": replacement,
            "Scope": scope, "Details": list(details), **extra}


def nested(name, **extra):
    return entry(NESTED[name], "AWS::CloudFormation::Stack", ChangeSetId=SETS[name],
                 PhysicalResourceId=STACKS[name], **extra)


def backend_release(*, retain=False, secret=None, copy_entry=None):
    """The changes of every backend prepare since 2026-10-04; retain adds the phase 4 Retain policies."""
    secret_details = secret or placeholder("KmsKeyId", "StackEncryptionKeyArn")
    if retain:
        secret_details = [*secret_details, concrete(None, "Retain", "DeletionPolicy"),
                          concrete(None, "Retain", "UpdateReplacePolicy")]
    return {
        "root": [nested("infra"), nested("lambda"), nested("cognito"), nested("agentcore")],
        "infra": [entry("GatewayExecutionRole", "AWS::IAM::Role", concrete("Policies")),
                  entry("RuntimeExecutionRole", "AWS::IAM::Role", concrete("Policies"))],
        "lambda": [entry("GetCognitoClientSecretFunction", "AWS::Lambda::Function", concrete("Code")),
                   entry("WeatherDTNServiceFunction", "AWS::Lambda::Function", concrete("Code"))],
        "cognito": [entry("GatewayClientSecret", "AWS::SecretsManager::Secret", *secret_details),
                    entry("GatewayClientSecretReadPolicy", "AWS::IAM::Policy",
                          *placeholder("PolicyDocument", "StackEncryptionKeyArn")),
                    copy_entry or entry("GatewayClientSecretCopy", "Custom::GatewayClientSecretCopy",
                                        *placeholder("ServiceToken", "GetCognitoClientSecretFunctionArn",
                                                     "Conditionally"), replacement="Conditional")],
        "agentcore": [entry("AgentCoreRuntime", "AWS::BedrockAgentCore::Runtime", concrete("EnvironmentVariables"))],
    }


def event(stack, logical, kind, status):
    return {"EventId": f"{stack}-{logical}-{status}", "LogicalResourceId": logical, "ResourceType": kind,
            "ResourceStatus": status}


def updated(stack, logical, kind):
    return [event(stack, logical, kind, "UPDATE_COMPLETE"), event(stack, logical, kind, "UPDATE_IN_PROGRESS")]


class Backend:
    """CloudFormation for one backend change set: both views, templates, resources and stack events."""

    def __init__(self, changes, *, templates=None, events=None, inventory=None, hide_values=()):
        self.changes, self.events, self.inventory, self.hide_values = changes, events or {}, inventory or {}, hide_values
        texts = {"root": (TEMPLATES / "wildfire-backend-stack.yaml").read_text(),
                 "infra": (TEMPLATES / "nested/1-infrastructure.yaml").read_text(),
                 "lambda": (TEMPLATES / "nested/2-lambda-functions.yaml").read_text(),
                 "cognito": (TEMPLATES / "nested/3-cognito-auth.yaml").read_text(),
                 "agentcore": (TEMPLATES / "nested/4-agentcore-resources.yaml").read_text()}
        self.templates = {**{("StackName", STACKS[n]): t for n, t in texts.items()},
                          **{("ChangeSetName", SETS[n]): t for n, t in texts.items()}, **(templates or {})}
        self.history = {arn: [event(name, "old", "AWS::CloudFormation::Stack", "UPDATE_COMPLETE")]
                        for name, arn in STACKS.items()}
        self.executed, self.reads = [], []

    def describe_change_set(self, ChangeSetName, StackName=None, NextToken=None, IncludePropertyValues=False):
        if not ChangeSetName.startswith("arn:"):
            assert StackName == BACKEND
            return {"Status": "CREATE_COMPLETE", "ChangeSetId": SETS["root"]}
        name = next(key for key, arn in SETS.items() if arn == ChangeSetName)
        changes = copy.deepcopy(self.changes.get(name, []))
        if IncludePropertyValues and name in self.hide_values:
            changes = []
        for change in changes if not IncludePropertyValues else ():
            for item in change["Details"]:
                item["Target"].pop("BeforeValue"), item["Target"].pop("AfterValue")
        return {"ChangeSetId": ChangeSetName, "StackId": STACKS[name], "Status": "CREATE_COMPLETE",
                "Changes": [{"Type": "Resource", "ResourceChange": change} for change in changes]}

    def get_template(self, TemplateStage, StackName=None, ChangeSetName=None):
        assert TemplateStage == "Original"
        key = ("StackName", StackName) if StackName else ("ChangeSetName", ChangeSetName)
        self.reads.append(key)
        return {"TemplateBody": self.templates[key]}

    def describe_stack_resource(self, StackName, LogicalResourceId):
        assert StackName == STACKS["root"]
        name = next(key for key, logical in NESTED.items() if logical == LogicalResourceId)
        return {"StackResourceDetail": {"PhysicalResourceId": STACKS[name]}}

    def get_paginator(self, operation):
        def paginate(StackName):
            if operation == "describe_stack_events":
                yield {"StackEvents": self.history[StackName]}
            else:
                assert operation == "list_stack_resources"
                if StackName not in self.inventory:
                    raise ClientError({"Error": {"Code": "ValidationError", "Message": "missing"}}, "ListStackResources")
                yield {"StackResourceSummaries": self.inventory[StackName]}
        return SimpleNamespace(paginate=paginate)

    def execute_change_set(self, ChangeSetName):
        self.executed.append(ChangeSetName)
        for name, events in self.events.items():
            self.history[STACKS[name]] = [*events, *self.history[STACKS[name]]]

    def get_waiter(self, name):
        assert name == "stack_update_complete"
        return SimpleNamespace(wait=lambda **_: None)


def guarded(cfn, tmp_path, acknowledged=(), created=False):
    return execute_guarded(cfn, BACKEND, "release-1-backend-prepare", created=created,
                           receipt=tmp_path / "backend-prepare-changes.json", acknowledged=acknowledged)


def parsed(name):
    """A repository template as the guard parses it, so a test can change one part of it."""
    return load_template((TEMPLATES / ("wildfire-backend-stack.yaml" if name == "root" else f"nested/{name}")).read_text())


ROUTINE_EVENTS = {
    "infra": updated("infra", "GatewayExecutionRole", "AWS::IAM::Role"),
    "lambda": updated("lambda", "GetCognitoClientSecretFunction", "AWS::Lambda::Function"),
    "agentcore": updated("agentcore", "AgentCoreRuntime", "AWS::BedrockAgentCore::Runtime"),
    "root": updated("root", "CognitoAuthStack", "AWS::CloudFormation::Stack"),
}


@pytest.mark.parametrize("infra_listed", [True, False])
def test_routine_backend_release_proves_both_placeholders_and_executes(tmp_path, infra_listed):
    # The resumed 10-05 run had no InfrastructureStack entry; the proof then reads its deployed template.
    changes = backend_release()
    if not infra_listed:
        changes["root"] = [c for c in changes["root"] if c["LogicalResourceId"] != "InfrastructureStack"]
    cfn = Backend(changes, events=ROUTINE_EVENTS)
    summary = guarded(cfn, tmp_path)
    assert cfn.executed == [SETS["root"]] and summary["result"] == "executed"
    assert summary["review"] == [] and summary["stateful_risks"] == [] and summary["unexpected"] == []
    proved = {(p["path"], p["property"]): p["proof"] for p in summary["proofs"]}
    assert set(proved) == {("CognitoAuthStack/GatewayClientSecret", "KmsKeyId"),
                           ("CognitoAuthStack/GatewayClientSecretCopy", "ServiceToken")}
    assert "InfrastructureStack.Outputs.StackEncryptionKeyArn = StackEncryptionKey.Arn" in proved[
        ("CognitoAuthStack/GatewayClientSecret", "KmsKeyId")]
    assert "GetCognitoClientSecretFunction (AWS::Lambda::Function) is updated in place" in proved[
        ("CognitoAuthStack/GatewayClientSecretCopy", "ServiceToken")]
    # Stacks outside the change set are not updated, so only the listed stacks' events are read.
    assert set(summary["executed_changes"]) == {"LambdaFunctionsStack/GetCognitoClientSecretFunction",
                                                "AgentCoreStack/AgentCoreRuntime"} | (
        {"InfrastructureStack/GatewayExecutionRole"} if infra_listed else set())


@pytest.mark.parametrize("policy_details", [True, False])
def test_phase_4_retain_release_passes_and_may_update_the_secret_record(tmp_path, policy_details):
    # Phase 4 adds DeletionPolicy and UpdateReplacePolicy Retain; CloudFormation lists and updates the secret once.
    # Its Scope names both policies; without policy details only the proven KmsKeyId placeholder is listed.
    cognito = parsed("3-cognito-auth.yaml")
    before, after = copy.deepcopy(cognito), copy.deepcopy(cognito)
    for policy in ("DeletionPolicy", "UpdateReplacePolicy"):
        before["Resources"]["GatewayClientSecret"].pop(policy, None)
        after["Resources"]["GatewayClientSecret"][policy] = "Retain"
    changes = backend_release(retain=True)
    if not policy_details:
        secret = next(c for c in changes["cognito"] if c["LogicalResourceId"] == "GatewayClientSecret")
        secret["Details"] = [d for d in secret["Details"] if d["Target"]["Attribute"] == "Properties"]
    events = {**ROUTINE_EVENTS, "cognito": updated("cognito", "GatewayClientSecret", "AWS::SecretsManager::Secret")}
    cfn = Backend(changes, events=events, templates={
        ("StackName", STACKS["cognito"]): before, ("ChangeSetName", SETS["cognito"]): after})
    summary = guarded(cfn, tmp_path)
    assert summary["result"] == "executed" and summary["review"] == [] and summary["unexpected"] == []
    assert summary["executed_changes"]["CognitoAuthStack/GatewayClientSecret"]["statuses"] == [
        "UPDATE_COMPLETE", "UPDATE_IN_PROGRESS"]
    secret = next(c for c in summary["changes"] if c["Path"] == "CognitoAuthStack/GatewayClientSecret")
    assert secret["Scope"] == ["DeletionPolicy", "Properties", "UpdateReplacePolicy"]


def test_a_secret_proven_unchanged_that_changes_stops_the_release_after_execution(tmp_path):
    events = {**ROUTINE_EVENTS, "cognito": updated("cognito", "GatewayClientSecret", "AWS::SecretsManager::Secret")}
    cfn = Backend(backend_release(), events=events)
    with pytest.raises(RuntimeError, match="changed CognitoAuthStack/GatewayClientSecret .*proved its listed change "
                                           "unchanged.*The release stopped here"):
        guarded(cfn, tmp_path)
    receipt = json.loads((tmp_path / "backend-prepare-changes.json").read_text())
    assert receipt["executed"] and receipt["result"] == "executed with unexpected changes"


@pytest.mark.parametrize("stack, change, reason", [
    # The assessment's case: the output points at another existing key; the change set looks exactly the same.
    ("infra", lambda t: t["Outputs"]["StackEncryptionKeyArn"].update(Value={"GetAtt": "OtherKey.Arn"}),
     "output StackEncryptionKeyArn of InfrastructureStack is not the same Ref or Arn"),
    ("root", lambda t: t["Resources"]["CognitoAuthStack"]["Properties"]["Parameters"].update(
        StackEncryptionKeyArn={"GetAtt": "InfrastructureStack.Outputs.OtherKeyArn"}),
     "the parent does not pass StackEncryptionKeyArn from the same sibling stack output"),
    ("cognito", lambda t: t["Resources"]["GatewayClientSecret"]["Properties"].update(
        KmsKeyId={"Fn::Sub": "${StackEncryptionKeyArn}"}), "KmsKeyId is not a plain Ref"),
])
def test_an_unproven_placeholder_stops_for_review_until_acknowledged(tmp_path, stack, change, reason):
    name = {"root": "root", "infra": "1-infrastructure.yaml", "cognito": "3-cognito-auth.yaml"}[stack]
    proposed = parsed(name)
    change(proposed)
    cfn = Backend(backend_release(), templates={("ChangeSetName", SETS[stack]): proposed})
    with pytest.raises(ValueError, match="Stopped training-backend for review") as stopped:
        guarded(cfn, tmp_path)
    assert reason in str(stopped.value) and cfn.executed == []
    flag = (f"{BACKEND}/CognitoAuthStack/GatewayClientSecret=AWS::SecretsManager::Secret:KmsKeyId")
    assert f"--acknowledge '{flag}'" in str(stopped.value)
    summary = guarded(cfn, tmp_path, acknowledged=[parse_acknowledgment(flag)])
    assert summary["executed"] and summary["review"][0]["acknowledged_by"] == flag
    assert (tmp_path / "backend-prepare-changes.1.json").exists(), "the stopped run's receipt is kept"


@pytest.mark.parametrize("secret, scope", [
    ([concrete("KmsKeyId", "arn:aws:kms:us-west-2:123456789012:key/other")], "KmsKeyId"),
    ([concrete("SecretString", "****")], "SecretString"),
])
def test_a_concrete_sensitive_change_needs_an_acknowledgment_with_its_type_and_scope(tmp_path, secret, scope):
    cfn = Backend(backend_release(secret=secret))
    path = "CognitoAuthStack/GatewayClientSecret"
    for wrong in (f"{BACKEND}/{path}=AWS::SecretsManager::Secret:Description",
                  f"{BACKEND}/{path}=AWS::KMS::Key:{scope}", f"other-backend/{path}=AWS::SecretsManager::Secret:{scope}"):
        with pytest.raises(ValueError, match=f"changes {scope} of {path} in place"):
            guarded(cfn, tmp_path, acknowledged=[parse_acknowledgment(wrong)])
    assert guarded(cfn, tmp_path, acknowledged=[
        parse_acknowledgment(f"{BACKEND}/{path}=AWS::SecretsManager::Secret:{scope},Tags")])["executed"]


def test_missing_property_values_stop_for_review(tmp_path):
    cfn = Backend(backend_release(), hide_values=("cognito",))
    with pytest.raises(ValueError, match="changes KmsKeyId of CognitoAuthStack/GatewayClientSecret to a value the "
                                         "change set does not show"):
        guarded(cfn, tmp_path)


COPY = "Custom::GatewayClientSecretCopy"


@pytest.mark.parametrize("copy_entry, scope, message", [
    (entry("GatewayClientSecretCopy", COPY, action="Add", replacement=None), "Add", "adds CognitoAuthStack/"),
    (entry("GatewayClientSecretCopy", COPY, action="Remove", replacement=None), "Remove", "removes CognitoAuthStack/"),
    (entry("GatewayClientSecretCopy", COPY, *placeholder("ServiceToken", "GetCognitoClientSecretFunctionArn"),
           replacement="True"), "Replace", "replaces CognitoAuthStack/"),
    (entry("GatewayClientSecretCopy", COPY, concrete("ServiceTimeout", "300"), replacement="False"),
     "ServiceTimeout", "changes ServiceTimeout of CognitoAuthStack/GatewayClientSecretCopy in place"),
])
def test_every_value_writer_operation_stops_for_review(tmp_path, copy_entry, scope, message):
    cfn = Backend(backend_release(copy_entry=copy_entry))
    with pytest.raises(ValueError, match="for review") as stopped:
        guarded(cfn, tmp_path)
    assert message in str(stopped.value)
    assert f"'{BACKEND}/CognitoAuthStack/GatewayClientSecretCopy={COPY}:{scope}'" in str(stopped.value)


def test_a_new_stack_creates_its_value_writer_without_review(tmp_path):
    # The first Create runs the copy; neither the review nor the check of the executed events stops it.
    events = {"cognito": [event("cognito", "GatewayClientSecretCopy", COPY, "CREATE_COMPLETE")]}
    cfn = Backend(backend_release(copy_entry=entry("GatewayClientSecretCopy", COPY, action="Add", replacement=None)),
                  events=events)
    cfn.get_waiter = lambda name: SimpleNamespace(wait=lambda **_: None)
    summary = guarded(cfn, tmp_path, created=True)
    assert summary["executed"] and summary["unexpected"] == []
    assert "CognitoAuthStack/GatewayClientSecretCopy" in summary["executed_changes"]


@pytest.mark.parametrize("listed, message", [
    (True, "changed CognitoAuthStack/GatewayClientSecretCopy (UPDATE_COMPLETE) although the review proved"),
    (False, "ran the value writer CognitoAuthStack/GatewayClientSecretCopy (UPDATE_COMPLETE) without an acknowledgment"),
])
def test_a_value_writer_that_runs_unreviewed_stops_the_release(tmp_path, listed, message):
    changes = backend_release()
    if not listed:
        changes["cognito"] = changes["cognito"][:2]
    events = {"cognito": [event("cognito", "GatewayClientSecretCopy", COPY, "UPDATE_COMPLETE")]}
    with pytest.raises(RuntimeError) as stopped:
        guarded(Backend(changes, events=events), tmp_path)
    assert message in str(stopped.value)


def test_an_unlisted_stateful_change_stops_the_release(tmp_path):
    events = {"infra": updated("infra", "StackEncryptionKey", "AWS::KMS::Key")}
    with pytest.raises(RuntimeError, match=r"changed InfrastructureStack/StackEncryptionKey \(AWS::KMS::Key: "
                                           r"UPDATE_COMPLETE, UPDATE_IN_PROGRESS\), which the change set did not list"):
        guarded(Backend(backend_release(), events=events), tmp_path)


STATEFUL_CHILD = [{"LogicalResourceId": "GatewayClientSecret", "ResourceType": "AWS::SecretsManager::Secret"}]


@pytest.mark.parametrize("inventory, expected", [
    ({STACKS["cognito"]: STATEFUL_CHILD}, ("delete nested stack CognitoAuthStack and its stateful resources "
                                           "CognitoAuthStack/GatewayClientSecret (AWS::SecretsManager::Secret)")),
    ({}, "delete nested stack CognitoAuthStack, whose resources could not be listed"),
])
def test_removing_a_nested_stack_with_stateful_resources_is_a_hard_stop(tmp_path, inventory, expected):
    changes = backend_release()
    changes["root"] = [nested("infra"), nested("lambda"), nested("agentcore"),
                       {**nested("cognito"), "Action": "Remove", "Replacement": None, "ChangeSetId": None}]
    cfn = Backend(changes, inventory=inventory)
    # No acknowledgment overrides D24: the stop names the stateful resources inside the removed stack.
    flag = parse_acknowledgment(f"{BACKEND}/CognitoAuthStack=AWS::CloudFormation::Stack:Remove")
    with pytest.raises(ValueError, match="Stopped before changing training-backend") as stopped:
        guarded(cfn, tmp_path, acknowledged=[flag])
    assert expected in str(stopped.value) and cfn.executed == []


def test_removing_a_nested_stack_without_stateful_resources_executes(tmp_path):
    changes = backend_release()
    changes["root"] = [*changes["root"], entry("ReportsStack", "AWS::CloudFormation::Stack", action="Remove",
                                               replacement=None, PhysicalResourceId="arn:reports")]
    cfn = Backend(changes, inventory={"arn:reports": [{"LogicalResourceId": "Renderer",
                                                       "ResourceType": "AWS::Lambda::Function"}]})
    assert guarded(cfn, tmp_path)["executed"]


def test_a_rerun_keeps_the_earlier_receipt(tmp_path):
    cfn = Backend(backend_release())
    guarded(cfn, tmp_path)
    guarded(cfn, tmp_path)
    assert json.loads((tmp_path / "backend-prepare-changes.1.json").read_text())["result"] == "executed"
    assert json.loads((tmp_path / "backend-prepare-changes.json").read_text())["result"] == "executed"


def test_receipts_keep_both_views_without_values(tmp_path):
    summary = guarded(Backend(backend_release(secret=[concrete("Description", "visible-description")])), tmp_path)
    text = (tmp_path / "backend-prepare-changes.json").read_text()
    assert UNRESOLVED not in text and "before-value" not in text and "visible-description" not in text
    writer = next(c for c in summary["changes"] if c["Path"] == "CognitoAuthStack/GatewayClientSecretCopy")
    assert writer["Details"][0]["CausingEntity"] == "GetCognitoClientSecretFunctionArn"
    assert "AfterUnresolved" not in writer["Details"][0]
    assert [d["AfterUnresolved"] for d in writer["PropertyDetails"]] == [True, True]


@pytest.mark.parametrize("text", [
    "training-backend/CognitoAuthStack/GatewayClientSecret", "training-backend/X=AWS::SecretsManager::Secret",
    "training-backend/X=Secret:KmsKeyId", "/X=AWS::S3::Bucket:Tags", "training-backend/X=AWS::S3::Bucket:Tags;rm",
])
def test_malformed_acknowledgments_are_refused(text):
    with pytest.raises(ValueError, match="must read STACK/RESOURCE=TYPE:SCOPE"):
        parse_acknowledgment(text)


def test_acknowledgments_round_trip_and_reach_the_guard(monkeypatch, tmp_path):
    text = "training-application/ResearchersWebClient=AWS::Cognito::UserPoolClient:CallbackURLs,LogoutURLs"
    assert str(parse_acknowledgment(text)) == text
    seen = []
    monkeypatch.setattr(change_sets, "execute_guarded", lambda *_, **kwargs: seen.append(kwargs["acknowledged"]))
    deploy_guarded(None, "training-application", "set", run=lambda _: None, cdk=["cdk"], construct="Stack",
                   created=False, receipt=tmp_path / "r.json", acknowledged=(parse_acknowledgment(text),))
    assert seen == [(parse_acknowledgment(text),)]


# The W2.11 release's predicted change sets, with the logical IDs a synthesized diff shows, need no acknowledgment.

def test_w2_11_application_change_set_passes_the_guard_without_an_acknowledgment(tmp_path):
    removed = [change(logical, kind, action="Remove", replacement=None) for logical, kind in (
        ("Frontend23D93C55", "AWS::CloudFront::Distribution"), ("ViewerIpDA713B85", "AWS::CloudFront::Function"),
        ("FrontendOrigin1VpcOrigin5EE6C5CB", "AWS::CloudFront::VpcOrigin"),
        ("RadioGeocoding64196514", "AWS::IAM::Policy"))]
    # The only stateful-type change: the legacy domain leaves the unused client's URLs, in place.
    web_client = change("ResearchersWebClient6BA30D86", "AWS::Cognito::UserPoolClient", Scope=["Properties"],
                        Details=[concrete("CallbackURLs"), concrete("LogoutURLs")])
    modified = [change(logical, kind) for logical, kind in (
        ("RunOutboxDispatcherCECAEC24", "AWS::Lambda::Function"),
        ("RunOutboxDispatcherServiceRoleDefaultPolicy3B40057D", "AWS::IAM::Policy"),
        ("SimulationRuntimeInvocation588BD84B", "AWS::IAM::Policy"),
        ("RadioPrivateTaskTaskRoleDefaultPolicyD7A841F9", "AWS::IAM::Policy"))]
    task = change("RadioPrivateTask6305BE33", "AWS::ECS::TaskDefinition", replacement="True")
    client = cloudformation([*removed, web_client, *modified, task])
    summary = run(client, tmp_path)
    assert summary["executed"] and summary["stateful_risks"] == [] and summary["review"] == []
    client.execute_change_set.assert_called_once_with(ChangeSetName=ROOT_SET)


def test_w2_11_backend_change_set_passes_the_guard_without_an_acknowledgment(tmp_path):
    changes = backend_release()
    changes["lambda"].append(entry("WeatherServiceFunction", "AWS::Lambda::Function", action="Remove",
                                   replacement=None))
    changes["agentcore"].append(entry("WeatherServiceTarget", "AWS::BedrockAgentCore::GatewayTarget",
                                      action="Remove", replacement=None))
    changes["infra"].append(entry("ExistingProcedurePolicy", "AWS::IAM::Policy", action="Remove", replacement=None))
    deleted = {"lambda": ("WeatherServiceFunction", "AWS::Lambda::Function"),
               "agentcore": ("WeatherServiceTarget", "AWS::BedrockAgentCore::GatewayTarget"),
               "infra": ("ExistingProcedurePolicy", "AWS::IAM::Policy")}
    events = {**ROUTINE_EVENTS, **{stack: [event(stack, logical, kind, "DELETE_COMPLETE"), *ROUTINE_EVENTS[stack]]
                                   for stack, (logical, kind) in deleted.items()}}
    cfn = Backend(changes, events=events)
    summary = guarded(cfn, tmp_path)
    assert cfn.executed == [SETS["root"]] and summary["result"] == "executed"
    assert summary["review"] == [] and summary["stateful_risks"] == [] and summary["unexpected"] == []
    assert {(p["path"], p["property"]) for p in summary["proofs"]} == {
        ("CognitoAuthStack/GatewayClientSecret", "KmsKeyId"), ("CognitoAuthStack/GatewayClientSecretCopy", "ServiceToken")}
