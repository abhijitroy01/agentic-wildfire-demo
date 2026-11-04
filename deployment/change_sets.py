"""Execute a CloudFormation change set only when no stateful resource would be replaced or deleted (D24).

Hard stops (D24; nothing overrides them): a stateful resource that would be replaced, possibly replaced or
deleted, including the stateful resources of a nested stack the change set would remove or replace.

Review stops (from the 2026-10-14 review of the GatewayClientSecret entry): an in-place change to a sensitive
property of a secret or KMS key, and any operation of a custom resource that writes a stored value. A placeholder
that change_proof proves resolves to today's value passes. Anything else waits for the operator, who reruns with
--acknowledge naming the stack, the resource, its type and the expected scope. Receipts record each one.

After execution the guard reads each stack's new events and stops the release if CloudFormation changed a resource
it proved unchanged, ran a value writer nobody acknowledged, or changed a stateful resource the change set did not
list. Receipts keep both DescribeChangeSet views, without before or after values.
"""
import json
import re
import time
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

from deployment.change_proof import Templates, Unproven, prove

# Resource types whose replacement or deletion loses data or identity: tables, buckets, image repositories,
# secrets, keys, user pools and clients, log groups, knowledge bases and their vector stores.
STATEFUL_TYPES = frozenset({
    "AWS::DynamoDB::Table", "AWS::DynamoDB::GlobalTable", "AWS::S3::Bucket", "AWS::ECR::Repository",
    "AWS::SecretsManager::Secret", "AWS::KMS::Key", "AWS::Cognito::UserPool", "AWS::Cognito::UserPoolClient",
    "AWS::Logs::LogGroup", "AWS::Bedrock::KnowledgeBase", "AWS::Bedrock::DataSource",
    "AWS::S3Vectors::VectorBucket", "AWS::S3Vectors::Index",
})
NESTED_STACK = "AWS::CloudFormation::Stack"
# In-place changes to these properties change a stored value or who can read it.
SENSITIVE_PROPERTIES = {
    "AWS::SecretsManager::Secret": frozenset({"KmsKeyId", "Name", "SecretString", "GenerateSecretString",
                                              "ReplicaRegions"}),
    "AWS::KMS::Key": frozenset({"KeyPolicy", "Enabled"}),
}
# Custom resources whose handler writes a stored value. GatewayClientSecretCopy (3-cognito-auth.yaml) copies the
# Cognito app client secret into GatewayClientSecret on Create and Update; mid-rotation that could restore the old
# secret, so it never runs unreviewed.
VALUE_WRITERS = frozenset({"Custom::GatewayClientSecretCopy"})
# CloudFormation's after value for a nested-stack parameter fed by a sibling output (undocumented; seen in every
# backend prepare change set). Any other after value is a concrete change.
UNRESOLVED = "{{changeSet:KNOWN_AFTER_APPLY}}"
NO_CHANGES = ("didn't contain changes", "No updates are to be performed")
# Stack creation and update waits: 15 s polls for up to an hour (ECS rollouts dominate).
WAIT = {"Delay": 15, "MaxAttempts": 240}
# --acknowledge STACK/RESOURCE=TYPE:SCOPE[,SCOPE...]; RESOURCE is the logical ID path under the root stack.
ACKNOWLEDGMENT = re.compile(r"(?P<stack>[A-Za-z][A-Za-z0-9-]*)/(?P<path>[A-Za-z0-9]+(?:/[A-Za-z0-9]+)*)"
                            r"=(?P<type>[A-Za-z0-9]+(?:::[A-Za-z0-9_@-]+)+):(?P<scope>[A-Za-z0-9.]+(?:,[A-Za-z0-9.]+)*)")
OPERATIONS = {"Add": "adds {path}, whose Create writes a stored value",
              "Remove": "removes {path}; CloudFormation would send Delete to the handler it has then",
              "Replace": "replaces {path}, whose Create writes a stored value"}


@dataclass(frozen=True)
class Acknowledgment:
    """The operator's approval of one reviewed change: stack, resource path, resource type and expected scope."""
    stack: str
    path: str
    type: str
    scope: frozenset

    def __str__(self):
        return f"{self.stack}/{self.path}={self.type}:{','.join(sorted(self.scope))}"


def parse_acknowledgment(text):
    """Read one --acknowledge value as a review stop prints it: STACK/RESOURCE=TYPE:SCOPE[,SCOPE...]."""
    match = ACKNOWLEDGMENT.fullmatch(text)
    if not match:
        raise ValueError(f"--acknowledge {text!r} must read STACK/RESOURCE=TYPE:SCOPE[,SCOPE...]; "
                         "copy it from the guard's stop message")
    return Acknowledgment(match["stack"], match["path"], match["type"], frozenset(match["scope"].split(",")))


@dataclass
class ChangeSetNode:
    """One change set of a nested hierarchy: its stack, its logical ID path under the root and its changes."""
    arn: str
    stack_id: str | None
    path: tuple = ()
    parent: ChangeSetNode | None = None
    changes: dict = field(default_factory=dict)
    children: dict = field(default_factory=dict)

    def walk(self):
        """Yield this change set and every change set nested under it."""
        yield self
        for child in self.children.values():
            yield from child.walk()

    def flat(self):
        """Yield every resource change; a nested stack's changes follow its own entry."""
        for logical, change in self.changes.items():
            yield change
            if logical in self.children:
                yield from self.children[logical].flat()


def describe(cfn, change_set, stack, **extra):
    """Describe one change set by ARN, or by name within stack."""
    where = {} if change_set.startswith("arn:") else {"StackName": stack}
    return cfn.describe_change_set(ChangeSetName=change_set, **where, **extra)


def pages(cfn, change_set, stack=None, **extra):
    """Yield every page of one DescribeChangeSet view, following NextToken."""
    token = None
    while True:
        page = describe(cfn, change_set, stack, **extra, **({"NextToken": token} if token else {}))
        yield page
        token = page.get("NextToken")
        if not token:
            return


def detail_record(detail):
    """Return a change detail without its values; AfterUnresolved says whether CloudFormation knows the new value.

    AfterUnresolved is absent when the view carries no values (DescribeChangeSet without IncludePropertyValues).
    """
    target = detail.get("Target", {})
    record = {key: target.get(key) for key in ("Attribute", "Name", "Path", "RequiresRecreation",
                                                  "AttributeChangeType")}
    record.update({key: detail.get(key) for key in ("Evaluation", "ChangeSource", "CausingEntity")})
    if "AfterValue" in target:
        record["AfterUnresolved"] = UNRESOLVED in str(target["AfterValue"])
    return record


def detail_key(detail):
    """Name what a detail changes: the property for Properties details, otherwise the attribute (Tags, ...)."""
    return detail.get("Name") if detail.get("Attribute") == "Properties" else detail.get("Attribute")


def change_set_tree(cfn, change_set, stack=None, *, parent=None, path=()):
    """Read a change set and its nested change sets, in both DescribeChangeSet views, into ChangeSetNodes."""
    plain = list(pages(cfn, change_set, stack))
    node = ChangeSetNode(plain[0].get("ChangeSetId", change_set), plain[0].get("StackId"), path, parent)
    # The IncludePropertyValues view adds before and after values but drops a nested stack's causing entities.
    values = {}
    for page in pages(cfn, change_set, stack, IncludePropertyValues=True):
        for item in page.get("Changes", []):
            change = item.get("ResourceChange", {})
            values[change.get("LogicalResourceId")] = [detail_record(d) for d in change.get("Details", [])]
    for page in plain:
        for item in page.get("Changes", []):
            change = {**item.get("ResourceChange", {})}
            logical = change.get("LogicalResourceId")
            change.update(Path="/".join((*path, logical)), PropertyDetails=values.get(logical))
            node.changes[logical] = change
            if change.get("ResourceType") == NESTED_STACK and change.get("ChangeSetId"):
                node.children[logical] = change_set_tree(cfn, change["ChangeSetId"], parent=node,
                                                         path=(*path, logical))
    return node


def resource_changes(cfn, change_set, stack=None):
    """List every ResourceChange of a change set, following NextToken pages and nested-stack change sets."""
    return list(change_set_tree(cfn, change_set, stack).flat())


def stateful_risks(changes):
    """Name each change that would replace or delete a stateful resource; Conditional and Dynamic count too."""
    risks = []
    for change in changes:
        if change.get("ResourceType") not in STATEFUL_TYPES:
            continue
        action, replacement = change.get("Action"), change.get("Replacement")
        if action == "Remove":
            risks.append(f"delete {change.get('LogicalResourceId')} ({change['ResourceType']})")
        elif replacement == "True":
            risks.append(f"replace {change.get('LogicalResourceId')} ({change['ResourceType']})")
        elif action == "Dynamic" or replacement == "Conditional":
            risks.append(f"possibly replace {change.get('LogicalResourceId')} ({change['ResourceType']})")
    return risks


def stack_inventory(cfn, stack_id, prefix):
    """Yield (path, type) for every resource of a stack and of the stacks nested in it."""
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_id):
        for resource in page.get("StackResourceSummaries", []):
            path = f"{prefix}/{resource['LogicalResourceId']}"
            yield path, resource["ResourceType"]
            if resource["ResourceType"] == NESTED_STACK and resource.get("PhysicalResourceId"):
                yield from stack_inventory(cfn, resource["PhysicalResourceId"], path)


def nested_stack_risks(cfn, changes):
    """Name the stateful resources that removing or replacing a nested stack would delete (D24).

    A change set lists a removed nested stack as one entry with no nested change set, so the guard lists the
    stack's resources itself. A stack whose resources cannot be listed counts as a risk.
    """
    risks = []
    for change in changes:
        if change.get("ResourceType") != NESTED_STACK:
            continue
        action, replacement = change.get("Action"), change.get("Replacement")
        verb = ("delete" if action == "Remove" else "replace" if replacement == "True"
                else "possibly replace" if action == "Dynamic" or replacement == "Conditional" else None)
        if verb is None:
            continue
        name = change.get("Path") or change.get("LogicalResourceId")
        try:
            stateful = sorted(f"{path} ({kind})" for path, kind in
                              stack_inventory(cfn, change["PhysicalResourceId"], name) if kind in STATEFUL_TYPES)
        except (KeyError, ClientError):
            risks.append(f"{verb} nested stack {name}, whose resources could not be listed")
            continue
        if stateful:
            risks.append(f"{verb} nested stack {name} and its stateful resources {', '.join(stateful)}")
    return risks


def review_changes(cfn, tree, *, created):
    """Return (review items, proofs, paths proven unchanged) for the changes the guard reviews.

    A review item names a resource path, its type, the scope to acknowledge (property names, or Add, Remove or
    Replace) and the reasons. created says the root stack is new, so a value writer's first Create is expected.
    A path counts as proven unchanged only when its Scope is Properties alone and every listed property is proven;
    a Scope that also names DeletionPolicy, UpdateReplacePolicy or Tags may update the resource even when the
    change lists no detail for that part.
    """
    templates, items, proofs, unchanged = Templates(cfn), {}, [], []

    def flag(change, scope, reason):
        item = items.setdefault(change["Path"], {"path": change["Path"], "type": change.get("ResourceType"),
                                                   "scope": set(), "reasons": []})
        item["scope"].add(scope)
        item["reasons"].append(reason)

    for node in tree.walk():
        for logical, change in node.changes.items():
            kind, action, path = change.get("ResourceType"), change.get("Action"), change["Path"]
            writer = kind in VALUE_WRITERS
            if writer and (action != "Modify" or change.get("Replacement") == "True"):
                operation = "Replace" if change.get("Replacement") == "True" else action
                if not (operation == "Add" and created):
                    flag(change, operation, OPERATIONS.get(operation, "{path} has action " + str(operation)
                                                           + "; its handler writes a stored value").format(path=path))
                continue
            if action != "Modify" or not (writer or kind in SENSITIVE_PROPERTIES):
                continue
            details = change.get("PropertyDetails")
            if details is None:  # no IncludePropertyValues entry, so the new values are unknown
                details = [detail_record(d) for d in change.get("Details", [])]
            groups = {}
            for detail in details:
                key = detail_key(detail)
                if writer or (detail.get("Attribute") == "Properties" and key in SENSITIVE_PROPERTIES[kind]):
                    groups.setdefault(key or "Unknown", []).append(detail)
            proven = set()
            for key, group in groups.items():
                states = {detail.get("AfterUnresolved") for detail in group}
                if False in states:
                    flag(change, key, f"changes {key} of {path} in place")
                elif None in states:
                    flag(change, key, f"changes {key} of {path} to a value the change set does not show")
                else:
                    cause = next((d["CausingEntity"] for d in group
                                  if d.get("ChangeSource") == "ParameterReference" and d.get("CausingEntity")), None)
                    try:
                        if cause is None or group[0].get("Attribute") != "Properties":
                            raise Unproven("no nested-stack parameter causes the placeholder")
                        proofs.append({"path": path, "property": key,
                                       "proof": prove(templates, node, logical, key, cause)})
                        proven.add(key)
                    except Unproven as why:
                        flag(change, key, f"cannot prove the new {key} of {path} equals the current one: {why}")
            properties_only = set(change.get("Scope") or ["Properties"]) == {"Properties"}
            if properties_only and details and all(detail_key(detail) in proven for detail in details):
                unchanged.append(path)
    return list(items.values()), proofs, unchanged


def acknowledged_items(stack, items, acknowledgments):
    """Split review items into (covered, open): an acknowledgment covers an item with the same stack, resource
    path and type whose scope is within the acknowledged scope."""
    covered, open_items = [], []
    for item in items:
        match = next((ack for ack in acknowledgments if (ack.stack, ack.path, ack.type) == (stack, item["path"],
                      item["type"]) and item["scope"] <= ack.scope), None)
        record = {**item, "scope": sorted(item["scope"]), "acknowledged_by": str(match) if match else None}
        (covered if match else open_items).append(record)
    return covered, open_items


def suggestion(stack, item):
    """Return the --acknowledge value that would cover a review item."""
    return str(Acknowledgment(stack, item["path"], str(item["type"]), frozenset(item["scope"])))


def change_record(change):
    """Return a receipt entry: the change's identity and both detail views, never a before or after value."""
    record = {key: change.get(key) for key in ("LogicalResourceId", "ResourceType", "Action", "Replacement")}
    record.update({key: change[key] for key in ("Path", "Scope", "PolicyAction", "ChangeSetId")
                   if change.get(key) is not None})
    record["Details"] = [detail_record(d) for d in change.get("Details", [])]
    record["PropertyDetails"] = change.get("PropertyDetails")
    return record


def stack_events(cfn, stack_id):
    """Yield a stack's events, newest first."""
    for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack_id):
        yield from page.get("StackEvents", [])


def event_marks(cfn, tree):
    """Return each stack's newest event ID before execution (None for a stack without events)."""
    marks = {}
    for node in tree.walk():
        if node.stack_id:
            newest = next(iter(stack_events(cfn, node.stack_id)), None)
            marks[node.stack_id] = newest.get("EventId") if newest else None
    return marks


def executed_changes(cfn, tree, marks):
    """Return {resource path: {"type", "statuses"}} for the resource events each stack recorded after its mark."""
    executed = {}
    for node in tree.walk():
        if not node.stack_id:
            continue
        for event in stack_events(cfn, node.stack_id):
            if marks.get(node.stack_id) and event.get("EventId") == marks[node.stack_id]:
                break
            if event.get("ResourceType") == NESTED_STACK:
                continue  # a stack's own rows; the resources inside it have their own events
            entry = executed.setdefault("/".join((*node.path, event.get("LogicalResourceId", ""))),
                                        {"type": event.get("ResourceType"), "statuses": []})
            if event.get("ResourceStatus") not in entry["statuses"]:
                entry["statuses"].append(event.get("ResourceStatus"))
    return executed


def unexpected_changes(tree, executed, unchanged, writers):
    """Name every executed change that contradicts the review: a resource proven unchanged that changed, a value
    writer that ran without acknowledgment (writers holds the allowed paths), or an unlisted stateful resource."""
    listed = {change["Path"] for node in tree.walk() for change in node.changes.values()}
    problems = []
    for path, entry in executed.items():
        statuses = ", ".join(str(status) for status in entry["statuses"])
        if path in unchanged:
            problems.append(f"changed {path} ({statuses}) although the review proved its listed change unchanged")
        elif entry["type"] in VALUE_WRITERS and path not in writers:
            problems.append(f"ran the value writer {path} ({statuses}) without an acknowledgment")
        elif entry["type"] in STATEFUL_TYPES and path not in listed:
            problems.append(f"changed {path} ({entry['type']}: {statuses}), which the change set did not list")
    return problems


def ready(cfn, change_set, stack, timeout=600):
    """Wait until a change set is prepared; return its description, or None when it was removed as empty."""
    deadline = time.monotonic()+timeout
    while True:
        try:
            description = describe(cfn, change_set, stack)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ChangeSetNotFound":
                return None  # CDK deletes a prepared change set that has no changes.
            raise
        if description["Status"] in ("CREATE_COMPLETE", "FAILED"):
            return description
        if time.monotonic() >= deadline:
            raise TimeoutError(f"CloudFormation change set {change_set} preparation timed out")
        time.sleep(3)


def preserve(receipt):
    """Keep an earlier run's receipt (a resumed release reuses the name) under the next free numbered name."""
    if receipt.exists():
        number = 1
        while receipt.with_name(f"{receipt.stem}.{number}{receipt.suffix}").exists():
            number += 1
        receipt.rename(receipt.with_name(f"{receipt.stem}.{number}{receipt.suffix}"))


def execute_guarded(cfn, stack, change_set, *, created, receipt, acknowledged=()):
    """Execute a prepared change set unless the guard stops it (see the module docstring); write and return its
    summary. acknowledged holds the operator's Acknowledgments for review stops; none overrides a hard stop."""
    description = ready(cfn, change_set, stack)
    preserve(receipt)
    summary = {"stack": stack, "change_set": change_set, "changes": [], "stateful_risks": [], "review": [],
               "proofs": [], "executed": False}
    reason = (description or {}).get("StatusReason", "")
    if description is None or (description["Status"] == "FAILED" and any(text in reason for text in NO_CHANGES)):
        summary["result"] = "no changes"
        receipt.write_text(json.dumps(summary, indent=2))
        print(f"{stack}: no changes", flush=True)
        return summary
    if description["Status"] == "FAILED":
        raise RuntimeError(f"CloudFormation change set {change_set} for {stack} failed: {reason}")
    tree = change_set_tree(cfn, description["ChangeSetId"])
    changes = list(tree.flat())
    items, proofs, unchanged = review_changes(cfn, tree, created=created)
    covered, open_items = acknowledged_items(stack, items, acknowledged)
    summary.update(change_set=description["ChangeSetId"], review=covered + open_items, proofs=proofs,
                   stateful_risks=stateful_risks(changes) + nested_stack_risks(cfn, changes),
                   changes=[change_record(change) for change in changes])
    receipt.write_text(json.dumps(summary, indent=2))
    if summary["stateful_risks"]:
        summary["result"] = "stopped"
        receipt.write_text(json.dumps(summary, indent=2))
        raise ValueError(f"Stopped before changing {stack}: its change set would " + "; ".join(summary["stateful_risks"])
                         + f". Nothing was executed. Review the change set in the CloudFormation console and {receipt}.")
    if open_items:
        summary["result"] = "stopped for review"
        receipt.write_text(json.dumps(summary, indent=2))
        raise ValueError(f"Stopped {stack} for review: its change set "
                         + "; ".join(reason for item in open_items for reason in item["reasons"])
                         + f". Nothing was executed. Review the change set in the CloudFormation console and {receipt}"
                         + ". If each change is intended, rerun with "
                         + " ".join(f"--acknowledge '{suggestion(stack, item)}'" for item in open_items) + ".")
    marks = event_marks(cfn, tree)
    cfn.execute_change_set(ChangeSetName=description["ChangeSetId"])
    cfn.get_waiter("stack_create_complete" if created else "stack_update_complete").wait(StackName=stack,
                                                                                         WaiterConfig=WAIT)
    executed = executed_changes(cfn, tree, marks)
    writers = {item["path"] for item in covered if item["type"] in VALUE_WRITERS}
    if created:
        writers |= {change["Path"] for change in changes if change.get("ResourceType") in VALUE_WRITERS}
    problems = unexpected_changes(tree, executed, set(unchanged), writers)
    summary.update(executed=True, result="executed with unexpected changes" if problems else "executed",
                   executed_changes=executed, unexpected=problems)
    receipt.write_text(json.dumps(summary, indent=2))
    if problems:
        raise RuntimeError(f"{stack}: CloudFormation " + "; ".join(problems) + f". The change set ran; check {receipt} "
                           "and those resources before rerunning. The release stopped here.")
    print(f"{stack}: {len(changes)} CloudFormation changes executed; receipt: {receipt}", flush=True)
    return summary


def deploy_guarded(cfn, stack, change_set, *, run, cdk, construct, created, receipt, parameters=None, acknowledged=()):
    """Prepare a CDK stack's change set, then run it through execute_guarded and return the summary.

    cdk is the CDK command for one app or cloud assembly, construct the stack's construct ID, and run executes one
    argument list. acknowledged is passed on to execute_guarded.
    """
    args = [*cdk, "deploy", construct, "--exclusively", "--method", "prepare-change-set",
            "--change-set-name", change_set, "--require-approval", "never"]
    for key, value in (parameters or {}).items():
        args.extend(["--parameters", f"{key}={value}"])
    # Prepare only: execute_guarded runs the change set unless it replaces or deletes a stateful resource.
    run(args)
    return execute_guarded(cfn, stack, change_set, created=created, receipt=receipt, acknowledged=acknowledged)
