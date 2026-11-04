"""Two native Runtime endpoint slots; execution version changes never rename Memory conversations."""
import re

SLOTS = ("RELEASE_A", "RELEASE_B")


def runtime_id(arn):
    """The owned stack supplies the ARN; reject incomplete or differently shaped resource names."""
    if not isinstance(arn, str) or ":runtime/" not in arn:
        raise ValueError("Expected an AgentCore Runtime ARN")
    return arn.split(":runtime/", 1)[1]


def runtime_version(value):
    """Require the numeric AgentCore version, never an image/UI tag or DEFAULT."""
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", value):
        raise ValueError("Expected a numeric AgentCore Runtime version")
    return value


def verify_endpoint(control, arn, endpoint, version):
    """Read the endpoint's actual live/target version before treating it as a usable binding."""
    if endpoint not in SLOTS:
        raise ValueError("Expected a managed Runtime endpoint slot")
    runtime_version(version)
    actual = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id(arn), endpointName=endpoint)
    if (actual.get("status") != "READY" or actual.get("liveVersion") != version
            or actual.get("targetVersion", version) != version or actual.get("agentRuntimeArn") != arn):
        raise RuntimeError(f"{endpoint} is not READY at the expected Runtime version")
    return {"endpoint": endpoint, "version": version}


def initial_parameters(control, arn, previous):
    """Preserve the accepted binding of a bound stack; a new installation starts with both slots at version 1.

    deploy_workspace.preflight refuses a bound stack without both endpoint slots, so DEFAULT is never read.
    """
    if not arn:
        return {"SimulationRuntimeEndpoint": SLOTS[0],
                "RuntimeEndpointAVersion": "1", "RuntimeEndpointBVersion": "1"}
    values = {key: previous.get(key, "") for key in (
        "SimulationRuntimeEndpoint", "RuntimeEndpointAVersion", "RuntimeEndpointBVersion")}
    endpoint = values["SimulationRuntimeEndpoint"]
    if endpoint not in SLOTS:
        raise ValueError("Unknown installed Runtime endpoint")
    version = values[f"RuntimeEndpoint{endpoint[-1]}Version"]
    for slot in SLOTS:
        runtime_version(values[f"RuntimeEndpoint{slot[-1]}Version"])
    verify_endpoint(control, arn, endpoint, version)
    return values


def api_bindings(ecs, lab):
    """Read every live API task, including overrides; an incomplete rollout cannot release an endpoint slot."""
    services = ecs.describe_services(cluster=lab["ClusterName"], services=[lab["ApiServiceName"]])
    if services.get("failures") or len(services.get("services", [])) != 1:
        raise RuntimeError("API service binding is unavailable")
    service = services["services"][0]
    if (len(service.get("deployments", [])) != 1 or service.get("pendingCount") != 0
            or service.get("runningCount", 0) != service.get("desiredCount") or not service.get("runningCount")):
        raise RuntimeError("Wait for the API rollout to finish before changing Runtime endpoint slots")
    tasks = []
    paginator = ecs.get_paginator("list_tasks")
    for page in paginator.paginate(cluster=lab["ClusterName"], serviceName=lab["ApiServiceName"],
                                   desiredStatus="RUNNING"):
        tasks.extend(page.get("taskArns", []))
    if len(tasks) != service["runningCount"]:
        raise RuntimeError("API task inventory changed during binding verification")
    definitions, bindings = {}, []
    for offset in range(0, len(tasks), 100):
        response = ecs.describe_tasks(cluster=lab["ClusterName"], tasks=tasks[offset:offset+100])
        if response.get("failures") or len(response.get("tasks", [])) != len(tasks[offset:offset+100]):
            raise RuntimeError("API task binding is unavailable")
        for task in response["tasks"]:
            if task.get("lastStatus") != "RUNNING" or task["taskDefinitionArn"] != service["taskDefinition"]:
                raise RuntimeError("An old API task still holds a Runtime endpoint binding")
            ref = task["taskDefinitionArn"]
            if ref not in definitions:
                definitions[ref] = ecs.describe_task_definition(taskDefinition=ref)["taskDefinition"]
            containers = definitions[ref]["containerDefinitions"]
            container = next(c for c in containers if c["name"] == "Api")
            env = {v["name"]: v["value"] for v in container.get("environment", [])}
            for override in task.get("overrides", {}).get("containerOverrides", []):
                if override.get("name") == "Api":
                    env.update({v["name"]: v["value"] for v in override.get("environment", [])})
            bindings.append({"endpoint": env.get("SIMULATION_RUNTIME_ENDPOINT"),
                             "version": env.get("SIMULATION_RUNTIME_VERSION")})
    return bindings


def require_api_binding(ecs, lab, endpoint, version):
    """All running API processes must have the same verified endpoint/version before acceptance."""
    expected = {"endpoint": endpoint, "version": runtime_version(version)}
    bindings = api_bindings(ecs, lab)
    if not bindings or any(binding != expected for binding in bindings):
        raise RuntimeError("API tasks do not all use the expected Runtime binding")
    return expected
