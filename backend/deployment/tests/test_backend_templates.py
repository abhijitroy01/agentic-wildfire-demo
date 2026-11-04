"""Offline checks of the backend CloudFormation templates: nested-stack wiring, IAM scope, S3 and guardrail settings."""
import ast
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from deployment.validate_infra_baseline import CloudFormationLoader

REPOSITORY = Path(__file__).resolve().parents[3]
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
NESTED = TEMPLATES / "nested"
# Actions that support no resource-level permission, so they are the only ones allowed on Resource "*".
RESOURCE_WILDCARD_ACTIONS = {
    "ecr:GetAuthorizationToken", "xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
    "xray:GetSamplingTargets", "polly:SynthesizeSpeech", "translate:TranslateText",
}


def load(path):
    """Parse a CloudFormation YAML template with the infra baseline validator's SafeLoader subclass (intrinsic tags kept)."""
    loader = CloudFormationLoader(path.read_text())
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def resources(name, kind=None):
    """Return {logical id: resource} of a nested template, optionally only one resource type."""
    found = load(NESTED / name)["Resources"]
    return {key: value for key, value in found.items() if kind is None or value["Type"] == kind}


def listed(value):
    """Return a policy field (Action or Resource) as a list."""
    return value if isinstance(value, list) else [value]


def statements(template):
    """Yield every IAM policy statement of the roles and policies in a template."""
    for resource in template["Resources"].values():
        properties = resource.get("Properties", {})
        if resource["Type"] == "AWS::IAM::Role":
            for policy in properties.get("Policies", []):
                yield from policy["PolicyDocument"]["Statement"]
        elif resource["Type"] == "AWS::IAM::Policy":
            yield from properties["PolicyDocument"]["Statement"]


def nested_stacks():
    """Map each nested stack logical id in the parent template to (its resource, its parsed template)."""
    parent = load(TEMPLATES / "wildfire-backend-stack.yaml")["Resources"]
    return {key: (value, load(NESTED / value["Properties"]["TemplateURL"]["Sub"].rsplit("/", 1)[1]))
            for key, value in parent.items() if value["Type"] == "AWS::CloudFormation::Stack"}


@pytest.mark.parametrize("stack", sorted(nested_stacks()))
def test_parent_passes_exactly_the_parameters_each_nested_stack_declares(stack):
    resource, template = nested_stacks()[stack]
    declared = template["Parameters"]
    passed = set(resource["Properties"]["Parameters"])
    assert passed <= set(declared)
    assert {name for name, spec in declared.items() if "Default" not in spec} <= passed


def test_parent_reads_only_outputs_the_nested_stacks_define():
    stacks = nested_stacks()
    text = (TEMPLATES / "wildfire-backend-stack.yaml").read_text()
    references = re.findall(r"!GetAtt (\w+)\.Outputs\.(\w+)", text)
    assert references
    for stack, output in references:
        assert output in stacks[stack][1]["Outputs"], f"{stack} has no output {output}"


def test_every_deploy_names_its_environment():
    templates = [TEMPLATES / "wildfire-backend-stack.yaml", *sorted(NESTED.glob("*.yaml"))]
    for path in templates:
        assert "Default" not in load(path)["Parameters"]["Environment"], path.name


def test_iam_resource_wildcards_are_limited_to_actions_without_resource_level_permissions():
    for path in sorted(NESTED.glob("*.yaml")):
        for statement in statements(load(path)):
            if "*" in listed(statement["Resource"]):
                assert set(listed(statement["Action"])) <= RESOURCE_WILDCARD_ACTIONS, path.name


def through_secrets_manager(actions, key=None):
    """The policy statement allowing KMS actions on the stack key only when Secrets Manager makes the call.

    key is how the template names the stack key: the StackEncryptionKeyArn parameter outside the infrastructure
    stack, which owns the key."""
    return {"Effect": "Allow", "Action": actions, "Resource": key or {"Ref": "StackEncryptionKeyArn"},
            "Condition": {"StringEquals": {"kms:ViaService": {"Sub": "secretsmanager.${AWS::Region}.amazonaws.com"}}}}


def test_no_lambda_environment_resolves_a_secret():
    """L2, D53: no function environment holds a credential; the DTN Lambda reads its secret by ARN at run time."""
    for path in [TEMPLATES / "wildfire-backend-stack.yaml", *sorted(NESTED.glob("*.yaml"))]:
        for name, resource in load(path)["Resources"].items():
            if resource["Type"] == "AWS::Lambda::Function":
                variables = resource["Properties"].get("Environment", {}).get("Variables", {})
                assert "{{resolve:secretsmanager" not in json.dumps(variables, default=str), f"{path.name}: {name}"
    dtn = resources("2-lambda-functions.yaml")["WeatherDTNServiceFunction"]["Properties"]["Environment"]
    assert dtn["Variables"] == {"ENVIRONMENT": {"Ref": "Environment"},
                                "DTN_CREDENTIALS_SECRET_ARN": {"Ref": "APIKeysSecretArn"},
                                "DTN_OPERATION_TIMEOUT_SECONDS": {"Ref": "DtnOperationTimeoutSeconds"},
                                "DTN_REQUEST_TIMEOUT_SECONDS": {"Ref": "DtnRequestTimeoutSeconds"},
                                "DTN_MAX_ATTEMPTS": {"Ref": "DtnMaxAttempts"},
                                "DTN_SERIALIZATION_RESERVE_SECONDS": {"Ref": "DtnSerializationReserveSeconds"},
                                "LOG_LEVEL": "INFO", "POWERTOOLS_SERVICE_NAME": "weather-dtn-service"}
    # Only the DTN function runs as LambdaExecutionRole; it may read that one secret, and decrypt it only through
    # Secrets Manager.
    role = resources("1-infrastructure.yaml")["LambdaExecutionRole"]["Properties"]
    assert role["Policies"] == [{"PolicyName": "ReadDtnCredentials", "PolicyDocument": {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": {"Ref": "APIKeysSecret"}},
            through_secrets_manager("kms:Decrypt", {"GetAtt": "StackEncryptionKey.Arn"})]}}]
    assert [name for name, function in resources("2-lambda-functions.yaml", "AWS::Lambda::Function").items()
            if function["Properties"]["Role"] == {"Ref": "LambdaExecutionRoleArn"}] == ["WeatherDTNServiceFunction"]


def test_custom_resource_role_has_no_inline_grant_and_copies_only_the_gateway_client_secret():
    role = resources("1-infrastructure.yaml")["CustomResourceLambdaRole"]["Properties"]
    assert "Policies" not in role
    cognito = resources("3-cognito-auth.yaml")
    grant = cognito["GatewayClientSecretReadPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert grant == [{"Effect": "Allow", "Action": "cognito-idp:DescribeUserPoolClient",
                      "Resource": {"GetAtt": "GatewayUserPool.Arn"}},
                     {"Effect": "Allow", "Action": "secretsmanager:PutSecretValue",
                      "Resource": {"Ref": "GatewayClientSecret"}},
                     through_secrets_manager(["kms:GenerateDataKey", "kms:Decrypt"])]
    copy = cognito["GatewayClientSecretCopy"]
    assert copy["DependsOn"] == "GatewayClientSecretReadPolicy"
    assert copy["Properties"]["SecretArn"] == {"Ref": "GatewayClientSecret"}
    assert "CognitoClientSecretRetriever" not in cognito


def test_gateway_client_secret_survives_its_removal_or_replacement():
    secret = resources("3-cognito-auth.yaml")["GatewayClientSecret"]
    assert (secret["DeletionPolicy"], secret["UpdateReplacePolicy"]) == ("Retain", "Retain")


def test_gateway_client_secret_reaches_the_runtime_only_as_a_secret_arn():
    cognito = load(NESTED / "3-cognito-auth.yaml")
    secret = cognito["Resources"]["GatewayClientSecret"]["Properties"]
    assert secret["KmsKeyId"] == {"Ref": "StackEncryptionKeyArn"}
    assert not {"SecretString", "GenerateSecretString"} & set(secret)
    assert cognito["Outputs"]["GatewayClientSecretArn"]["Value"] == {"Ref": "GatewayClientSecret"}
    for path in [TEMPLATES / "wildfire-backend-stack.yaml", *sorted(NESTED.glob("*.yaml"))]:
        template = load(path)
        values = [output["Value"] for output in template.get("Outputs", {}).values()]
        # No output reads a client secret attribute or passes one on from a nested stack.
        assert not [value for value in values
                    if isinstance(value, dict) and str(value.get("GetAtt", "")).endswith("ClientSecret")], path.name
        assert "CognitoClientSecret" not in template.get("Parameters", {}), path.name
    agentcore = load(NESTED / "4-agentcore-resources.yaml")["Resources"]
    runtime = agentcore["AgentCoreRuntime"]
    environment = runtime["Properties"]["EnvironmentVariables"]
    assert environment["DEPLOYED_CognitoClientSecretArn"] == {"Ref": "GatewayClientSecretArn"}
    assert "DEPLOYED_CognitoClientSecret" not in environment
    assert "RuntimeGatewayClientSecretAccess" in runtime["DependsOn"]
    read = agentcore["RuntimeGatewayClientSecretAccess"]["Properties"]
    assert read["Roles"] == [{"Select": [1, {"Split": ["/", {"Ref": "RuntimeExecutionRoleArn"}]}]}]
    assert read["PolicyDocument"]["Statement"] == [
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": {"Ref": "GatewayClientSecretArn"}},
        through_secrets_manager("kms:Decrypt")]


def test_only_the_runtime_can_read_the_simulation_context_signing_secret():
    """G17: the Gateway adapter forwards signed contexts unchanged, so it has neither the grant nor the variable."""
    readers = []
    for path in sorted(NESTED.glob("*.yaml")):
        for logical_id, resource in load(path)["Resources"].items():
            if "SimulationContextSecretArn" in str(resource):
                readers.append(logical_id)
    assert readers == ["SimulationRuntimeSecretPolicy", "AgentCoreRuntime"]
    grant = resources("4-agentcore-resources.yaml")["SimulationRuntimeSecretPolicy"]["Properties"]
    assert grant["Roles"] == [{"Select": [1, {"Split": ["/", {"Ref": "RuntimeExecutionRoleArn"}]}]}]
    assert grant["PolicyDocument"]["Statement"] == [
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": {"Ref": "SimulationContextSecretArn"}}]
    lambdas = resources("2-lambda-functions.yaml")
    assert "Policies" not in lambdas["SimulationAdapterRole"]["Properties"]
    assert set(lambdas["SimulationServiceFunction"]["Properties"]["Environment"]["Variables"]) == {
        "SIMULATION_INTERNAL_URL"}


def test_every_changeable_backend_function_logs_to_a_retained_30_day_group():
    """L3, D53: each function the phase may change logs to a stack-owned group that keeps 30 days; the frozen
    GetCognitoClientSecretFunction keeps its default group, whose retention the deployer sets."""
    from deployment.retention import LOG_RETENTION_DAYS
    found = resources("2-lambda-functions.yaml")
    functions = {name: value for name, value in found.items() if value["Type"] == "AWS::Lambda::Function"}
    assert "LoggingConfig" not in functions.pop("GetCognitoClientSecretFunction")["Properties"]
    for name, function in functions.items():
        logs = name.removesuffix("Function") + "Logs"
        assert function["Properties"]["LoggingConfig"] == {"LogGroup": {"Ref": logs}}, name
        group = found[logs]
        assert group["Type"] == "AWS::Logs::LogGroup", name
        # No LogGroupName (a generated name never collides with the function's old default group) and no key.
        assert group["Properties"] == {"RetentionInDays": LOG_RETENTION_DAYS}, name
        assert (group["DeletionPolicy"], group["UpdateReplacePolicy"]) == ("Retain", "Retain"), name
        assert group.get("Condition") == function.get("Condition"), name
        # The accepted AWS-managed encryption (G41) and the fixed retention (D53), for every scanner.
        skips = {skip["id"]: skip["comment"] for skip in group["Metadata"]["checkov"]["skip"]}
        assert set(skips) == {"CKV_AWS_158", "CKV_AWS_338"}, name
        assert skips["CKV_AWS_158"].startswith("G41: ") and skips["CKV_AWS_338"].startswith("D53: "), name
        nag = group["Metadata"]["cfn_nag"]["rules_to_suppress"]
        assert [rule["id"] for rule in nag] == ["W84"] and nag[0]["reason"].startswith("G41: "), name
        assert group["Metadata"]["guard"]["SuppressedRules"] == ["CLOUDWATCH_LOG_GROUP_ENCRYPTED"], name


def test_the_runtime_repository_keeps_the_same_newest_images():
    """L5, D53: the Runtime repository keeps the newest images, as many as the CDK repositories and the deployer's
    retention guard assume."""
    from deployment.retention import IMAGES_KEPT
    policy = resources("1-infrastructure.yaml")["RuntimeECRRepository"]["Properties"]["LifecyclePolicy"]
    rules = json.loads(policy["LifecyclePolicyText"])["rules"]
    assert len(rules) == 1
    assert rules[0]["selection"] == {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": IMAGES_KEPT}
    assert rules[0]["action"] == {"type": "expire"}
    stacks = (REPOSITORY / "deployment/infra/lib/stacks.ts").read_text()
    assert re.search(rf"^export const IMAGES_KEPT = {IMAGES_KEPT};", stacks, re.MULTILINE)


def test_accepted_scanner_risks_carry_their_decision_beside_the_resource():
    for name, function in resources("2-lambda-functions.yaml", "AWS::Lambda::Function").items():
        skips = {skip["id"]: skip["comment"] for skip in function["Metadata"]["checkov"]["skip"]}
        properties = function["Properties"]
        # D9: no dead-letter queue, and no VPC for functions that reach no VPC resource. D53: environment
        # variables hold no credential and keep Lambda's default encryption, so every function that has them
        # carries that decision.
        expected = {"CKV_AWS_116": "D9: "} | ({} if "VpcConfig" in properties else {"CKV_AWS_117": "D9: "}) | (
            {"CKV_AWS_173": "D53: "} if "Environment" in properties else {})
        assert set(skips) == set(expected), name
        assert all(skips[check].startswith(decision) for check, decision in expected.items()), name
    repository = resources("1-infrastructure.yaml")["RuntimeECRRepository"]
    skips = repository["Metadata"]["checkov"]["skip"]
    assert [skip["id"] for skip in skips] == ["CKV_AWS_136"] and skips[0]["comment"].startswith("D3: ")


def test_gateway_invokes_only_the_named_target_functions():
    functions = resources("2-lambda-functions.yaml", "AWS::Lambda::Function")
    removed = {"CreateApiKeyProviderFunction", "CreateOAuthProviderFunction"}
    assert not removed & set(functions)
    targets = {value["Properties"]["FunctionName"]["Sub"].removeprefix("${ParentStackName}-")
               for value in functions.values()
               if "FunctionName" in value["Properties"]
               and {"Key": "Purpose", "Value": "CustomResource"} not in value["Properties"].get("Tags", [])}
    role = resources("1-infrastructure.yaml")["GatewayExecutionRole"]["Properties"]
    invoke = [statement for policy in role["Policies"] for statement in policy["PolicyDocument"]["Statement"]
              if "lambda:InvokeFunction" in listed(statement["Action"])]
    names = {re.search(r":function:\$\{ParentStackName\}-([^:]+)", arn["Sub"])[1]
             for statement in invoke for arn in statement["Resource"]}
    assert targets == names == {"geocode-service", "wildfire-service", "weather-dtn-service"}


def test_every_gateway_tool_is_one_the_runtime_calls_and_has_a_ui_label():
    """RTGW-4: no Gateway target serves a tool that no agent calls, and each provider target has a UI label."""
    tools = set()
    for target in resources("4-agentcore-resources.yaml", "AWS::BedrockAgentCore::GatewayTarget").values():
        properties = target["Properties"]
        schema = properties["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        tools |= {(properties["Name"], tool["Name"]) for tool in schema}
    # The provider tool names the Runtime calls (gateway.py imports only json); the simulation agent calls the
    # simulation-service tools by their generated contract names.
    path = REPOSITORY / "backend/runtime/utils/gateway.py"
    spec = importlib.util.spec_from_file_location("runtime_gateway_names", path)
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    providers = {target for target, _ in tools if target != "simulation-service"}
    assert {f"{target}___{tool}" for target, tool in tools if target in providers} == {
        *gateway.DETECTION_TOOLS, gateway.WILDFIRES}
    labels = (REPOSITORY / "ui/src/shared/services/agentToolLabels.ts").read_text()
    assert sorted(re.findall(r'\{ prefix: "([^"]+)"', labels)) == sorted(providers)


def test_the_gateway_role_holds_no_agentcore_credential_access():
    """VERIFY 7: every target invokes its Lambda with the Gateway role itself, so the role needs no workload
    access token or other credential-provider grant; its one resource wildcard is the X-Ray statement."""
    for target in resources("4-agentcore-resources.yaml", "AWS::BedrockAgentCore::GatewayTarget").values():
        assert target["Properties"]["CredentialProviderConfigurations"] == [
            {"CredentialProviderType": "GATEWAY_IAM_ROLE"}], target["Properties"]["Name"]
    role = resources("1-infrastructure.yaml")["GatewayExecutionRole"]["Properties"]
    granted = [statement for policy in role["Policies"] for statement in policy["PolicyDocument"]["Statement"]]
    # Policies attached to the role by its ARN in the other nested stacks (SimulationGatewayInvokePolicy).
    attached = [policy["Properties"] for path in sorted(NESTED.glob("*.yaml"))
                for policy in load(path)["Resources"].values() if policy["Type"] == "AWS::IAM::Policy"]
    granted += [statement for policy in attached if "GatewayExecutionRole" in str(policy["Roles"])
                for statement in policy["PolicyDocument"]["Statement"]]
    assert [action for statement in granted for action in listed(statement["Action"])
            if action.startswith("bedrock-agentcore:")] == []
    xray = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    assert [statement for statement in granted if "*" in listed(statement["Resource"])] == [
        {"Effect": "Allow", "Resource": "*", "Action": xray}]


@pytest.mark.parametrize("role", ["RuntimeExecutionRole", "GatewayExecutionRole"])
def test_agentcore_roles_trust_only_agentcore_in_this_account(role):
    trust = resources("1-infrastructure.yaml")[role]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert [statement["Condition"] for statement in trust] == [{
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
        "ArnLike": {"aws:SourceArn": {"Sub": "arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:*"}},
    }]


def test_runtime_gateway_and_tool_functions_have_no_public_endpoint():
    """G00d: no function URL, resource-based invoke grant or API; the Runtime takes only IAM-signed calls, the Gateway
    only tokens of the stack's own machine client, and the simulation adapter runs inside the workspace VPC."""
    for path in [TEMPLATES / "wildfire-backend-stack.yaml", *sorted(NESTED.glob("*.yaml"))]:
        kinds = {resource["Type"] for resource in load(path)["Resources"].values()}
        assert not kinds & {"AWS::Lambda::Url", "AWS::Lambda::Permission", "AWS::ApiGateway::RestApi",
                            "AWS::ApiGatewayV2::Api"}, path.name
    agentcore = resources("4-agentcore-resources.yaml")
    # With no AuthorizerConfiguration, InvokeAgentRuntime needs SigV4 IAM authorization (the API task role's grant).
    assert "AuthorizerConfiguration" not in agentcore["AgentCoreRuntime"]["Properties"]
    gateway = agentcore["AgentCoreGateway"]["Properties"]
    assert gateway["AuthorizerType"] == "CUSTOM_JWT"
    assert gateway["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"] == [{"Ref": "CognitoClientId"}]
    adapter = resources("2-lambda-functions.yaml")["SimulationServiceFunction"]["Properties"]["VpcConfig"]
    assert adapter["SecurityGroupIds"] == [{"Ref": "SimulationAdapterSecurityGroupId"}]


def test_every_bucket_has_a_tls_only_policy_and_the_log_bucket_keeps_versions_briefly():
    found = resources("1-infrastructure.yaml")
    buckets = {key for key, value in found.items() if value["Type"] == "AWS::S3::Bucket"}
    covered = set()
    for value in found.values():
        if value["Type"] == "AWS::S3::BucketPolicy":
            deny = [s for s in value["Properties"]["PolicyDocument"]["Statement"] if s.get("Sid") == "EnforceTLS"]
            assert deny and deny[0]["Effect"] == "Deny" and deny[0]["Principal"] == "*"
            assert deny[0]["Condition"] == {"Bool": {"aws:SecureTransport": False}}
            covered.add(value["Properties"]["Bucket"]["Ref"])
    assert covered == buckets
    logs = found["AccessLogBucket"]["Properties"]
    assert logs["VersioningConfiguration"] == {"Status": "Enabled"}
    assert logs["LifecycleConfiguration"]["Rules"][0]["NoncurrentVersionExpiration"]["NoncurrentDays"] > 0


BUCKET_ACCESS = REPOSITORY / "deployment/infra/lib/bucket-access.ts"


def deny_outside_installation(bucket, exceptions):
    """The statement every installation bucket policy carries (L1, D52), with the CDK helper's object actions."""
    actions = re.findall(r'"(s3:[A-Za-z*]+)"', re.search(r"OBJECT_ACTIONS = \[([^\]]*)\]", BUCKET_ACCESS.read_text())[1])
    return {"Sid": "DenyObjectAccessOutsideInstallation", "Effect": "Deny", "Principal": "*", "Action": actions,
            "Resource": {"Sub": f"${{{bucket}.Arn}}/*"},
            "Condition": {"ArnNotLike": {"aws:PrincipalArn": exceptions},
                          "BoolIfExists": {"aws:PrincipalIsAWSService": "false"}}}


def test_every_backend_bucket_denies_object_access_outside_the_installation():
    found = resources("1-infrastructure.yaml")
    operators = {"Ref": "OperatorRoleArns"}
    runtime = {"Split": [",", {"Join": [",", [{"GetAtt": "RuntimeExecutionRole.Arn"}, {"Join": [",", operators]}]]}]}
    expected = {"AccessLogBucket": operators, "ReportImagesBucket": operators, "VoiceMessageBucket": runtime}
    assert {key for key, value in found.items() if value["Type"] == "AWS::S3::Bucket"} == set(expected)
    for bucket, exceptions in expected.items():
        document = found[f"{bucket}Policy"]["Properties"]["PolicyDocument"]
        # 2012-10-17, as CDK writes; without a Version S3 reads a policy in the 2008-10-17 language.
        assert document["Version"] == "2012-10-17", bucket
        denies = [s for s in document["Statement"] if s.get("Sid") == "DenyObjectAccessOutsideInstallation"]
        assert denies == [deny_outside_installation(bucket, exceptions)], bucket
    pattern = re.search(r"OPERATOR_ROLE_ARN = String\.raw`([^`]*)`", BUCKET_ACCESS.read_text())[1]
    for path in (TEMPLATES / "wildfire-backend-stack.yaml", NESTED / "1-infrastructure.yaml"):
        parameter = load(path)["Parameters"]["OperatorRoleArns"]
        assert parameter["Type"] == "CommaDelimitedList" and "Default" not in parameter, path.name
        assert parameter["AllowedPattern"] == pattern, path.name
    root = load(TEMPLATES / "wildfire-backend-stack.yaml")["Resources"]
    assert root["InfrastructureStack"]["Properties"]["Parameters"]["OperatorRoleArns"] == {"Join": [",", operators]}
    # BuildSource's policy (deployment/infra/lib/stacks.ts) names the Runtime build role by its fixed name.
    role = resources("4-agentcore-resources.yaml")["CodeBuildServiceRole"]["Properties"]
    assert role["RoleName"] == {"Sub": "${ParentStackName}-runtime-build"}
    assert root["AgentCoreStack"]["Properties"]["Parameters"]["ParentStackName"] == {"Ref": "AWS::StackName"}
    assert "-backend-runtime-build" in (REPOSITORY / "deployment/infra/lib/stacks.ts").read_text()


# SHA-256 of the frozen Gateway client secret definitions, recorded before they were last reviewed. A change
# re-copies the Gateway client secret or changes the deploy guard.
FROZEN = {
    "GetCognitoClientSecretFunction (2-lambda-functions.yaml)":
        "c65fa0b31bd327f272ad3b400405ff244025740b4a5f1e5c99eca5dbf1335961",
    "3-cognito-auth.yaml, which holds GatewayClientSecretCopy":
        "6e01c5b0b1cc4ca8a9c424787eea729f5d321e650b6d2bee3fd370075dfa049b",
    "StackEncryptionKey (1-infrastructure.yaml)": "0856e0481c3e8cafeae7bd3905216cffe551fcbb741465baf051c54e501b5837",
    "the StackEncryptionKeyArn output (1-infrastructure.yaml)":
        "309f545c2650dbb85c1927358e7c5f98cfb469427dab12342d9081a78cd39184",
    "the root template's CognitoAuthStack block": "bde6383ab214c041631746fc2a2427267bbfa0f45262f252e229b6874b85d1fe",
    "deployment/change_sets.py": "c770a71491755a6a59c336dad4ee396de90a846c72089a26906e283a5a0040bd",
    "deploy_backend.change_stack": "250b15c4b31339ea574f90a8ce6d8c4f4e799350c894a538ffaf26bd0d7e0a17",
}


def frozen_definitions():
    """Return each frozen definition's bytes: parsed resources as sorted JSON, files and change_stack's source."""
    def parsed(value):
        return json.dumps(value, sort_keys=True, default=str).encode()
    source = (REPOSITORY / "deployment/deploy_backend.py").read_text()
    change_stack = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.FunctionDef) and node.name == "change_stack")
    infrastructure = load(NESTED / "1-infrastructure.yaml")
    return dict(zip(FROZEN, (
        parsed(resources("2-lambda-functions.yaml")["GetCognitoClientSecretFunction"]),
        (NESTED / "3-cognito-auth.yaml").read_bytes(),
        parsed(infrastructure["Resources"]["StackEncryptionKey"]),
        parsed(infrastructure["Outputs"]["StackEncryptionKeyArn"]),
        parsed(load(TEMPLATES / "wildfire-backend-stack.yaml")["Resources"]["CognitoAuthStack"]),
        (REPOSITORY / "deployment/change_sets.py").read_bytes(),
        ast.get_source_segment(source, change_stack).encode()), strict=True))


def test_frozen_backend_resources_keep_their_reviewed_definitions():
    for name, data in frozen_definitions().items():
        assert hashlib.sha256(data).hexdigest() == FROZEN[name], (
            f"owner brief Part 2: {name} must not change in phase 7 (it re-copies the Gateway client secret or "
            "changes the deploy guard); stop and ask the owner")


def test_no_backend_bucket_allows_cross_origin_reads():
    """G11: the UI opens presigned report links in a new tab and never fetches them, so no bucket has a CORS rule."""
    for path in [TEMPLATES / "wildfire-backend-stack.yaml", *sorted(NESTED.glob("*.yaml"))]:
        for name, resource in load(path)["Resources"].items():
            if resource["Type"] == "AWS::S3::Bucket":
                assert "CorsConfiguration" not in resource.get("Properties", {}), f"{path.name}: {name}"


def test_runtime_uses_the_published_guardrail_version_never_draft():
    guardrails = load(NESTED / "5-bedrock-guardrails.yaml")
    version = guardrails["Resources"]["WildfireGuardrailVersion"]
    assert version["Type"] == "AWS::Bedrock::GuardrailVersion"
    assert version["Properties"]["GuardrailIdentifier"] == {"GetAtt": "WildfireGuardrail.GuardrailId"}
    assert guardrails["Outputs"]["GuardrailVersion"]["Value"] == {"GetAtt": "WildfireGuardrailVersion.Version"}
    agentcore = load(NESTED / "4-agentcore-resources.yaml")
    assert "Default" not in agentcore["Parameters"]["GuardrailVersion"]
    environment = agentcore["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    assert environment["DEPLOYED_GuardrailVersion"] == {"Ref": "GuardrailVersion"}
    assert agentcore["Parameters"]["GuardrailVersion"]["AllowedPattern"] == "^[1-9][0-9]{0,7}$"


# Runtime variables that no repository code reads, each with its reader; every other one must be read by
# backend/runtime code, so an inert variable fails test_every_runtime_variable_has_a_reader (RTGW-8).
RUNTIME_VARIABLES_READ_ELSEWHERE = {
    "AGENT_OBSERVABILITY_ENABLED": "aws-opentelemetry-distro switches its agent instrumentation on it",
    "ENVIRONMENT": "aws-opentelemetry-distro reports it as the deployment environment",
}


def test_every_runtime_variable_has_a_reader():
    variables = resources("4-agentcore-resources.yaml")["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    literals = set()
    for path in (TEMPLATES.parents[1] / "runtime").rglob("*.py"):
        # The release checks set fixture values; they read nothing.
        if path.name != "check_startup.py" and path.parent.name != "tests":
            literals |= {node.value for node in ast.walk(ast.parse(path.read_text()))
                         if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert set(RUNTIME_VARIABLES_READ_ELSEWHERE) <= set(variables)
    assert sorted(set(variables) - set(RUNTIME_VARIABLES_READ_ELSEWHERE) - literals) == []


def test_the_runtime_reads_only_the_public_knowledge_base():
    """RTGW-3: the Runtime has no private knowledge-base retrieval, so no role may read another KB and no template
    declares, passes or uses KnowledgeBaseId."""
    templates = [TEMPLATES / "wildfire-backend-stack.yaml", *sorted(NESTED.glob("*.yaml"))]
    retrieve = [statement for path in templates for statement in statements(load(path))
                if "bedrock:Retrieve" in listed(statement["Action"])]
    public = {"Sub": "arn:aws:bedrock:${AWS::Region}:${AWS::AccountId}:knowledge-base/${PublicDemoKnowledgeBaseId}"}
    assert [listed(statement["Resource"]) for statement in retrieve] == [[public]]
    for path in (TEMPLATES / "wildfire-backend-stack.yaml", NESTED / "4-agentcore-resources.yaml"):
        assert "KnowledgeBaseId" not in load(path)["Parameters"], path.name
    for path in templates:
        template = load(path)
        used = json.dumps([template.get(section, {}) for section in ("Conditions", "Resources", "Outputs")],
                          default=str)
        # Whole names only: PublicDemoKnowledgeBaseId ends with the same letters.
        assert not re.search(r'"Ref": "KnowledgeBaseId"|\$\{KnowledgeBaseId[}.]', used), path.name


def test_runtime_takes_its_region_from_the_stack():
    """RTGW-8: every Runtime AWS client reads DEPLOYED_DeploymentRegion, which has no default in code."""
    variables = resources("4-agentcore-resources.yaml")["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
    assert variables["DEPLOYED_DeploymentRegion"] == {"Ref": "AWS::Region"}


def test_runtime_release_checks_run_without_network():
    build = resources("4-agentcore-resources.yaml")["RuntimeContainerBuildProject"]["Properties"]["Source"]
    runs = [line for line in build["BuildSpec"].splitlines() if "docker run" in line]
    assert len(runs) == 2
    assert all("--network none" in line for line in runs)
    # DEPL-3: the release tests stay out of the image; CodeBuild mounts them read-only from the source bundle.
    mount = '--mount type=bind,source="$CODEBUILD_SRC_DIR/release-tests",target=/release-tests,readonly '
    # The image has no pytest, so the stdlib runs the mounted folder; -P keeps /app off sys.path, as a script
    # path did, and discover exits non-zero when it finds no tests.
    tests = "/release-tests/backend/runtime/tests"
    assert [line.split(":$IMAGE_TAG ", 1)[1] for line in runs] == [
        f"-P -m unittest discover -s {tests} -t {tests} -v", "/app/check_startup.py"]
    assert [mount in line for line in runs] == [True, False]


def test_runtime_build_project_encrypts_with_the_stack_key():
    found = resources("4-agentcore-resources.yaml")
    assert found["RuntimeContainerBuildProject"]["Properties"]["EncryptionKey"] == {"Ref": "StackEncryptionKeyArn"}
    grants = found["CodeBuildServiceRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert any(statement["Resource"] == {"Ref": "StackEncryptionKeyArn"} and "kms:Decrypt" in statement["Action"]
               for statement in grants)
