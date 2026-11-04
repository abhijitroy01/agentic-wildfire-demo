"""Deployment identity and preflight shared by the CLI and its offline tests."""
import hashlib
import re
from dataclasses import dataclass

from botocore.exceptions import ClientError

MANAGED_BY = "wildfire-secure-deployer"
READY = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}


@dataclass(frozen=True)
class Target:
    account: str
    region: str
    name: str

    def __post_init__(self):
        if not re.fullmatch(r"\d{12}", self.account):
            raise ValueError("Account must contain twelve digits")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", self.region):
            raise ValueError("Invalid AWS region")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,18}[a-z0-9]", self.name):
            raise ValueError("Deployment name must be 3–20 lowercase letters/digits/hyphens, starting with a letter")

    @property
    def identity(self):
        return hashlib.sha256(f"{self.account}/{self.region}/{self.name}".encode()).hexdigest()[:10]

    @property
    def prefix(self):
        return f"{self.name}-{self.identity}"

    @property
    def project(self):
        return self.prefix.replace("-", "_")

    @property
    def tags(self):
        return {"ManagedBy": MANAGED_BY, "DeploymentId": self.identity, "DeploymentName": self.name}

    def stack(self, component):
        """Name the CloudFormation stack of one installation component."""
        if component not in {"foundation", "application", "backend", "public-guidance"}:
            raise ValueError("Unknown deployment component")
        return f"{self.prefix}-{component}"


def inspect_stack(client, name, target, *, allow_unmanaged=False):
    """Describe a stack, require a settled status and this installation's ownership tags."""
    try:
        stack = client.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as exc:
        error = exc.response["Error"]
        if error["Code"] == "ValidationError" and "does not exist" in error["Message"]:
            return None
        raise
    if stack["StackStatus"] not in READY:
        raise ValueError(f"{name} is {stack['StackStatus']}; finish recovery before deployment. Nothing will be deleted.")
    tags = {item["Key"]: item["Value"] for item in stack.get("Tags", [])}
    if not allow_unmanaged and any(tags.get(k) != v for k, v in target.tags.items()):
        raise ValueError(f"{name} is not owned by this deployment. Choose a different [aws] name in the installation "
                         "file; automatic adoption is disabled.")
    return stack


def validate_arn(arn, target, service):
    """Require an ARN of the given service in the selected AWS account and Region."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[:5] != ["arn", "aws", service, target.region, target.account] or not parts[5]:
        raise ValueError(f"{service} ARN must belong to the selected account and region")
    return arn


def safe_outputs(stack):
    """CloudFormation can contain secret-valued outputs; never persist those."""
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])
            if not any(word in o["OutputKey"].lower() for word in ("password", "tokenvalue", "clientsecret", "apikeyvalue"))}


def parameter_values(stack):
    """Return a described stack's parameters as {key: value}; a missing stack (None) has none."""
    return {p["ParameterKey"]: p.get("ParameterValue", "") for p in (stack or {}).get("Parameters", [])}


def stack_resources(cfn, stack):
    """Yield (logical path, resource type, physical ID) for every existing resource of a stack and the stacks
    nested in it: entries without a physical ID or with a *_FAILED status are skipped.

    Such an entry names no resource the installation runs on (a failed create may have none; a failed delete is
    leaving), and a nested stack in that state is not walked. change_sets.stack_inventory walks the same way but
    yields no physical IDs, and change_sets.py stays unchanged (owner brief Part 2).
    """
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for resource in page.get("StackResourceSummaries", []):
            physical = resource.get("PhysicalResourceId")
            if not physical or resource.get("ResourceStatus", "").endswith("_FAILED"):
                continue
            logical, kind = resource["LogicalResourceId"], resource["ResourceType"]
            yield logical, kind, physical
            if kind == "AWS::CloudFormation::Stack":
                yield from ((f"{logical}/{path}", nested, name) for path, nested, name in stack_resources(cfn, physical))
