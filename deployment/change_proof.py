"""Prove that a change-set placeholder resolves to the value a resource already has (GatewayClientSecret review).

A nested stack's parameter that comes from a sibling stack's output is unknown while CloudFormation prepares the
change set, so every resource reading it is listed as changed, with the placeholder change_sets.UNRESOLVED as its
after value, even when nothing will change. A proof accepts such an entry only when every link from the property
to the value's source is the same in the template the stack runs today and in the change set's template:

1. the property is a plain Ref of the nested stack's parameter;
2. the parent passes that parameter as Fn::GetAtt of a sibling stack's output;
3. the output is a Ref, or the Arn, of one resource of that sibling;
4. that resource keeps its type and is neither removed nor (possibly) replaced in the sibling's change set.

A change set lists resource changes only, never output changes, so links 2 and 3 come from the templates.
Anything else, including a template that cannot be read, is no proof: change_sets then stops for review.
"""
from botocore.exceptions import ClientError

from deployment.validate_infra_baseline import load_template

# Intrinsic functions the template loader keeps in YAML short form ("!Sub x" becomes {"Sub": x}).
SHORT_FORMS = frozenset({"And", "Base64", "Cidr", "Equals", "FindInMap", "GetAZs", "If", "ImportValue", "Join",
                         "Length", "Not", "Or", "Select", "Split", "Sub", "ToJsonString", "Transform"})
# Attributes that keep their value while the resource is not replaced.
STABLE_ATTRIBUTES = frozenset({"Arn"})


class Unproven(Exception):
    """A link from a placeholder to its source could not be shown to be unchanged."""


def normalize(value):
    """Rewrite YAML short-form intrinsics in the long JSON form, so the two forms compare equal."""
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        (key, inner), = value.items()
        name = key.removeprefix("Fn::")
        if name == "GetAtt":
            parts = inner.split(".", 1) if isinstance(inner, str) else inner
            return {"Fn::GetAtt": [normalize(part) for part in parts]}
        if name in SHORT_FORMS:
            return {"Fn::" + name: normalize(inner)}
    return {key: normalize(item) for key, item in value.items()}


def get_att(value):
    """Return (resource, attribute) when value is exactly one Fn::GetAtt of literal names, else None."""
    value = normalize(value)
    parts = value.get("Fn::GetAtt") if isinstance(value, dict) and len(value) == 1 else None
    if isinstance(parts, list) and len(parts) == 2 and all(isinstance(part, str) for part in parts):
        return parts[0], parts[1]
    return None


def ref(value):
    """Return the referenced name when value is exactly one Ref, else None."""
    value = normalize(value)
    if isinstance(value, dict) and len(value) == 1 and isinstance(value.get("Ref"), str):
        return value["Ref"]
    return None


class Templates:
    """Read and parse each stack's current template and each change set's template once."""

    def __init__(self, cfn):
        self.cfn, self.cache = cfn, {}

    def read(self, **where):
        """Return the parsed template of one stack (StackName) or change set (ChangeSetName)."""
        key = tuple(sorted(where.items()))
        if key not in self.cache:
            try:
                body = self.cfn.get_template(TemplateStage="Original", **where)["TemplateBody"]
            except ClientError as exc:
                raise Unproven(f"a template could not be read ({exc.response.get('Error', {}).get('Code')})") from None
            # boto3 returns a JSON template as a mapping and a YAML template as text.
            self.cache[key] = body if isinstance(body, dict) else load_template(body)
        return self.cache[key]

    def physical_id(self, stack_id, logical):
        """Return the physical ID of a resource of a stack."""
        try:
            return self.cfn.describe_stack_resource(StackName=stack_id, LogicalResourceId=logical)[
                "StackResourceDetail"]["PhysicalResourceId"]
        except ClientError as exc:
            raise Unproven(f"{logical} could not be described ({exc.response.get('Error', {}).get('Code')})") from None


def resource(template, logical):
    """Return a template's resource by logical ID, or an empty mapping."""
    return template.get("Resources", {}).get(logical, {})


def prove(templates, node, logical, prop, parameter):
    """Explain why prop of logical, in the stack of change-set node, keeps its value; raise Unproven otherwise.

    node is a change_sets.ChangeSetNode; parameter is the nested-stack parameter the change detail names as its
    causing entity.
    """
    if node.parent is None:
        raise Unproven("it is in the root stack, so no sibling stack output feeds it")
    for template in (templates.read(StackName=node.stack_id), templates.read(ChangeSetName=node.arn)):
        if ref(resource(template, logical).get("Properties", {}).get(prop)) != parameter:
            raise Unproven(f"{prop} is not a plain Ref of the parameter {parameter} in both templates")
    parent, nested = node.parent, node.path[-1]
    bindings = [normalize(resource(template, nested).get("Properties", {}).get("Parameters", {}).get(parameter))
                for template in (templates.read(StackName=parent.stack_id), templates.read(ChangeSetName=parent.arn))]
    link = get_att(bindings[1])
    if bindings[0] != bindings[1] or not link or not link[1].startswith("Outputs."):
        raise Unproven(f"the parent does not pass {parameter} from the same sibling stack output as before")
    sibling, output = link[0], link[1].removeprefix("Outputs.")
    sibling_change, sibling_node = parent.changes.get(sibling), parent.children.get(sibling)
    if sibling_change and (sibling_node is None or sibling_change.get("Action") != "Modify"
                           or sibling_change.get("Replacement") != "False"):
        raise Unproven(f"the change set does more to {sibling} than update it in place with a nested change set")
    sibling_id = sibling_node.stack_id if sibling_node else templates.physical_id(parent.stack_id, sibling)
    deployed = templates.read(StackName=sibling_id)
    proposed = templates.read(ChangeSetName=sibling_node.arn) if sibling_node else deployed
    values = [normalize(template.get("Outputs", {}).get(output, {}).get("Value")) for template in (deployed, proposed)]
    attribute = get_att(values[1])
    source = attribute[0] if attribute and attribute[1] in STABLE_ATTRIBUTES else ref(values[1])
    if values[0] != values[1] or not source:
        raise Unproven(f"output {output} of {sibling} is not the same Ref or Arn of one resource as before")
    kinds = [resource(template, source).get("Type") for template in (deployed, proposed)]
    if not kinds[0] or kinds[0] != kinds[1]:
        raise Unproven(f"{sibling} resource {source} is missing or changes type")
    change = sibling_node.changes.get(source) if sibling_node else None
    if change and (change.get("Action") != "Modify" or change.get("Replacement") in ("True", "Conditional")):
        raise Unproven(f"{sibling} resource {source} is removed or may be replaced")
    used = f"{source}.Arn" if attribute else source
    return (f"{prop} is Ref {parameter}, which {nested} receives from {sibling}.Outputs.{output} = {used} in both "
            f"templates; {source} ({kinds[1]}) is {'updated in place' if change else 'unchanged'}")
