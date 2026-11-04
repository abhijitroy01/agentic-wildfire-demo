import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { App, Aspects } from "aws-cdk-lib";
import { runInNewContext } from "node:vm";
import { Annotations, Template, Match } from "aws-cdk-lib/assertions";
import { AwsSolutionsChecks } from "cdk-nag";
import {
  Foundation, Application, imageBuildCommands, workStartingPaths, CAMERA_DEVICE_ID, CAMERA_ORIGIN, IMAGES_KEPT,
} from "../lib/stacks";
import { PublicGuidance } from "../lib/public-guidance";
import { OBJECT_ACTIONS, OPERATOR_ROLE_ARN } from "../lib/bucket-access";

test("private-origin public app, durable jobs, bounded workers, and encrypted storage", () => {
  const app = new App();
  const env = { account: "111111111111", region: "us-west-2" };
  const foundation = new Foundation(app, "Foundation", { env });
  const stack = new Application(app, "Application", { env, foundation, imageTag: "test-immutable" });
  const template = Template.fromStack(stack);
  template.resourceCountIs("AWS::ECS::Service", 5);
  const services = template.findResources("AWS::ECS::Service");
  const workerId = Object.keys(services).find(id => id.startsWith("WorkerService"))!;
  const apiId = Object.keys(services).find(id => id.startsWith("ApiService"))!;
  assert.deepEqual(services[workerId].Properties.DesiredCount, {Ref: "WorkerTasks"});
  assert.ok(services[apiId].DependsOn.includes(workerId),
    "Worker rollout must finish before the API publishes newly supported job types.");
  template.hasResourceProperties("AWS::ElasticLoadBalancingV2::LoadBalancer", { Scheme: "internal" });
  template.hasResourceProperties("AWS::Cognito::UserPoolClient", {
    GenerateSecret: false, AllowedOAuthFlows: ["code"],
  });
  template.hasResourceProperties("AWS::Cognito::UserPool", {
    AdminCreateUserConfig: Match.objectLike({ AllowAdminCreateUserOnly: true }),
  });
  template.hasResourceProperties("AWS::SQS::Queue", {
    VisibilityTimeout: 180, SqsManagedSseEnabled: true,
    RedrivePolicy: Match.objectLike({ maxReceiveCount: 5 }),
  });
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    BillingMode: "PAY_PER_REQUEST", TimeToLiveSpecification: { AttributeName: "expires", Enabled: true },
  });
  template.hasResourceProperties("AWS::ECS::TaskDefinition", {
    Cpu: { "Fn::FindInMap": ["FargateUnits", { Ref: "WorkerVcpu" }, "Units"] },
    Memory: { "Fn::FindInMap": ["FargateUnits", { Ref: "WorkerMemoryGib" }, "Units"] },
    ContainerDefinitions: Match.arrayWith([Match.objectLike({ ReadonlyRootFilesystem: true, StopTimeout: 120 })]),
  });
  template.hasResourceProperties("AWS::CloudFront::Distribution", {
    DistributionConfig: Match.objectLike({
      DefaultCacheBehavior: Match.objectLike({ ViewerProtocolPolicy: "redirect-to-https" }),
    }),
  });
  template.resourceCountIs("AWS::CloudFront::Distribution", 1);
  template.resourceCountIs("AWS::CloudFront::VpcOrigin", 1);
  template.hasOutput("SimulationApiVersion", { Value: "1" });
  template.hasOutput("SimulationCapabilityVersion", { Value: "1.0.0" });
  // The legacy Frontend distribution and its two outputs are gone (W2.11, DEPL-6).
  const outputs = template.toJSON().Outputs;
  assert.equal(outputs.ApplicationUrl, undefined);
  assert.equal(outputs.SimulationApiBaseUrl, undefined);
  Template.fromStack(foundation).hasResourceProperties("AWS::ECR::Repository", {
    ImageTagMutability: "IMMUTABLE", ImageScanningConfiguration: { ScanOnPush: true },
  });
});

test("simulation integration has a transactional outbox, private adapter and bounded broker permissions", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  template.hasResourceProperties("AWS::DynamoDB::Table", { StreamSpecification: { StreamViewType: "NEW_IMAGE" } });
  template.hasResourceProperties("AWS::DynamoDB::Table", {
    GlobalSecondaryIndexes: Match.arrayWith([Match.objectLike({
      IndexName: "pending-outbox", Projection: { ProjectionType: "KEYS_ONLY" },
      KeySchema: [{ AttributeName: "pending_outbox", KeyType: "HASH" },
        { AttributeName: "created", KeyType: "RANGE" }],
    })]),
  });
  template.hasResourceProperties("AWS::Events::Rule", { ScheduleExpression: "rate(1 minute)" });
  template.hasResourceProperties("AWS::Lambda::EventSourceMapping", {
    FunctionResponseTypes: ["ReportBatchItemFailures"], StartingPosition: "TRIM_HORIZON",
  });
  template.hasResourceProperties("AWS::CloudFront::OriginAccessControl", {
    OriginAccessControlConfig: Match.objectLike({ OriginAccessControlOriginType: "s3", SigningBehavior: "always" }),
  });
  const distributions = JSON.stringify(template.findResources("AWS::CloudFront::Distribution"));
  assert.ok(!distributions.includes("amplifyapp.com"), "Do not chain CloudFront in front of Amplify hosting.");
  for (const output of ["SimulationContextSecretArn", "SimulationInternalUrl",
    "SimulationAdapterSecurityGroupId", "SimulationAdapterSubnetIds", "IntegratedWorkspaceUrl"]) {
    template.hasOutput(output, { Value: Match.anyValue() });
  }
  // The API invokes only the configured Runtime and its two release endpoints, never DEFAULT (W2.11, DEPL-4).
  const invocation = Object.entries(template.findResources("AWS::IAM::Policy"))
    .find(([id]) => id.startsWith("SimulationRuntimeInvocation"))![1].Properties.PolicyDocument.Statement[0];
  assert.equal(invocation.Action, "bedrock-agentcore:InvokeAgentRuntime");
  assert.deepEqual(invocation.Resource, [{ Ref: "SimulationRuntimeArn" },
    { "Fn::GetAtt": ["RuntimeEndpointA", "AgentRuntimeEndpointArn"] },
    { "Fn::GetAtt": ["RuntimeEndpointB", "AgentRuntimeEndpointArn"] }]);
  assert.equal(template.toJSON().Parameters.AllowLegacyRuntimeDefault, undefined);
  assert.ok(!JSON.stringify(template.toJSON()).includes("runtime-endpoint/DEFAULT"));
  template.resourceCountIs("AWS::BedrockAgentCore::RuntimeEndpoint", 2);
  for (const slot of ["A", "B"]) {
    template.hasResourceProperties("AWS::BedrockAgentCore::RuntimeEndpoint", {
      Name: `RELEASE_${slot}`, AgentRuntimeVersion: { Ref: `RuntimeEndpoint${slot}Version` },
      AgentRuntimeId: { "Fn::Select": [1, { "Fn::Split": ["/", { Ref: "SimulationRuntimeArn" }] }] },
    });
  }
  const tasks = Object.entries(template.findResources("AWS::ECS::TaskDefinition"));
  const apiTask = tasks.find(([id]) => id.startsWith("ApiTask"))![1];
  const apiEnvironment = apiTask.Properties.ContainerDefinitions[0].Environment;
  assert.ok(apiEnvironment.some((e: {Name: string; Value: unknown}) =>
    e.Name === "SIMULATION_RUNTIME_ENDPOINT" && JSON.stringify(e.Value).includes("SimulationRuntimeEndpoint")));
  const version = apiEnvironment.find((e: {Name: string}) => e.Name === "SIMULATION_RUNTIME_VERSION").Value;
  assert.deepEqual(version, { "Fn::If": ["HasSimulationRuntime", { "Fn::If": ["RuntimeEndpointIsA",
    { "Fn::GetAtt": ["RuntimeEndpointA", "LiveVersion"] },
    { "Fn::GetAtt": ["RuntimeEndpointB", "LiveVersion"] }] }, ""] });
  const workerTask = tasks.find(([id]) => id.startsWith("WorkerTask"))![1];
  assert.ok(!workerTask.Properties.ContainerDefinitions[0].Environment.some(
    (e: {Name: string}) => ["SIMULATION_RUNTIME_ENDPOINT", "SIMULATION_RUNTIME_VERSION"].includes(e.Name)),
  "Runtime promotion must not restart SUMO workers.");
  const buckets = template.findResources("AWS::S3::Bucket");
  assert.ok(!JSON.stringify(buckets).includes('"ExpirationInDays":7'),
    "Shared input bundles must not expire underneath retained child runs.");
});

test("release builds use versioned source inputs and frozen verification gates", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {
    env: { account: "111111111111", region: "us-west-2" },
  });
  const template = Template.fromStack(foundation);
  template.hasResourceProperties("AWS::S3::Bucket", { VersioningConfiguration: { Status: "Enabled" } });
  const projects = template.findResources("AWS::CodeBuild::Project");
  assert.equal(Object.keys(projects).length, 1);
  const project = Object.values(projects)[0] as {
    Properties: { Source: { BuildSpec: string }; ConcurrentBuildLimit: number;
      Cache: { Type: string; Modes: string[] }; Environment: { EnvironmentVariables: any[] } }
  };
  const build = JSON.parse(project.Properties.Source.BuildSpec);
  assert.deepEqual(build.phases.build.commands, imageBuildCommands);
  // The captured runner enforces test/build/push ordering; its behavioral tests live in deployment/tests.
  assert.deepEqual(imageBuildCommands, ["python3 deployment/container_build.py"]);
  assert.equal(project.Properties.ConcurrentBuildLimit, 2);
  assert.deepEqual(project.Properties.Cache, { Type: "LOCAL", Modes: ["LOCAL_DOCKER_LAYER_CACHE"] });
  assert.ok(project.Properties.Environment.EnvironmentVariables.some(v => v.Name === "NATIVE_REPOSITORY_URI"));
  assert.equal(build.phases.post_build.commands[0], 'test "$CODEBUILD_BUILD_SUCCEEDING" = "1"');
  assert.ok(!JSON.stringify(build).includes("verified-locks"), "The build writes no lock copies back to S3.");
  assert.ok(!JSON.stringify(build).includes("fabric/"), "The source bundle is the repository root.");
  assert.ok(!JSON.stringify(build).includes("RESOLVE_WEB_LOCK"));
  assert.ok(!JSON.stringify(build).includes("--package-lock-only"));
  const policies = JSON.stringify(template.findResources("AWS::IAM::Policy"));
  assert.ok(policies.includes("sources/*"), "The builder must be able to read the pinned source prefix.");
  assert.ok(!policies.includes("verified-locks") && !policies.includes("s3:PutObject"),
    "The builder has no write access to the source bucket.");
});

test("native build artifacts have separate immutable storage and disposable retention", () => {
  const app = new App();
  const template = Template.fromStack(new Foundation(app, "Foundation", {}));
  const repositories = template.findResources("AWS::ECR::Repository");
  const native = Object.entries(repositories).find(([id]) => id.startsWith("NativeBuildArtifacts"))![1] as any;
  assert.equal(native.Properties.ImageTagMutability, "IMMUTABLE");
  assert.equal(native.DeletionPolicy, "Retain");
  const rules = JSON.parse(native.Properties.LifecyclePolicy.LifecyclePolicyText).rules;
  assert.equal(rules[0].selection.countType, "sinceImagePushed");
  assert.equal(rules[0].selection.countNumber, 30);
  template.hasOutput("NativeBuildRepositoryUri", { Value: Match.anyValue() });
});

test("public entry cannot forward a forged workspace header or internal route", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const functions = template.findResources("AWS::CloudFront::Function") as
    Record<string, { Properties: { FunctionCode: string; FunctionConfig: { Runtime: string } } }>;
  const named = (prefix: string) => Object.entries(functions).find(([id]) => id.startsWith(prefix))![1];
  const integrated = named("WorkspaceApiPath").Properties.FunctionCode;
  assert.equal(named("WorkspaceApiPath").Properties.FunctionConfig.Runtime, "cloudfront-js-2.0");
  const event = (uri: string) => ({ request: { uri, headers: { "x-lab-workspace": { value: "forged" } } },
    viewer: { ip: "203.0.113.1" } });
  const invoke = (source: string, input: object) => runInNewContext(source+"; handler(input)", { input });
  const forwarded = invoke(integrated, event("/simulation-api/v1/runs"));
  assert.equal(forwarded.uri, "/api/v1/runs");
  assert.equal(forwarded.headers["x-lab-workspace"].value, "public-demo");
  assert.equal(forwarded.headers["x-lab-viewer-ip"].value, "203.0.113.1");
  // Only /simulation-api/v1/* is forwarded, so encoded and case-varied internal paths never reach the origin.
  for (const path of ["/simulation-api/internal/v1/simulations/submit_simulation", "/simulation-api/jobs",
    "/simulation-api/%69nternal/v1/simulations/submit_simulation", "/simulation-api/INTERNAL/v1/simulations/status"]) {
    assert.equal(invoke(integrated, event(path)).statusCode, 404, path);
  }
  template.hasResourceProperties("AWS::WAFv2::WebACL", {
    Rules: Match.arrayWith([Match.objectLike({ Statement: { ManagedRuleGroupStatement: {
      VendorName: "AWS", Name: "AWSManagedRulesKnownBadInputsRuleSet",
    } } })]),
  });
  const services = JSON.stringify(template.findResources("AWS::ECS::Service"));
  assert.ok(!services.includes('"AssignPublicIp":"ENABLED"'));
});

test("the integrated workspace is the only public entry and needs no condition", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const json = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" })).toJSON();
  const ofType = (type: string) => Object.entries(json.Resources as Record<string, any>)
    .filter(([, resource]) => resource.Type === type);
  // W2.11, DEPL-6: the legacy Frontend distribution, its ViewerIp function and its VPC origin are gone.
  const distributions = ofType("AWS::CloudFront::Distribution");
  assert.deepEqual(distributions.map(([id]) => id.replace(/[0-9A-F]{8}$/, "")), ["IntegratedWorkspace"]);
  assert.equal(ofType("AWS::CloudFront::Function").length, 1);
  assert.equal(ofType("AWS::CloudFront::VpcOrigin").length, 1);
  // Nothing switches the workspace, its origin access bucket policy or its outputs off.
  const [[distributionId, distribution]] = distributions;
  assert.equal(json.Parameters.EnableIntegratedWorkspace, undefined);
  assert.ok(!JSON.stringify(json).includes("HasFabricOrigin"));
  assert.equal(distribution.Condition, undefined);
  const policies = ofType("AWS::S3::BucketPolicy").filter(([id]) => id.startsWith("WorkspaceAssetsPolicy"));
  assert.deepEqual(policies.map(([, policy]) => policy.Condition), [undefined]);
  for (const output of ["IntegratedWorkspaceUrl", "IntegratedDistributionId"]) {
    assert.equal(json.Outputs[output].Condition, undefined, output);
  }
  // The legacy client lists only the workspace domain, and both services publish it as their public URL.
  const domain = { "Fn::GetAtt": [distributionId, "DomainName"] };
  const [[clientId, client]] = ofType("AWS::Cognito::UserPoolClient");
  assert.ok(clientId.startsWith("ResearchersWebClient"));
  for (const property of ["CallbackURLs", "LogoutURLs"]) {
    assert.deepEqual(client.Properties[property], [{ "Fn::Join": ["", ["https://", domain, "/"]] }], property);
  }
  const containers = ofType("AWS::ECS::TaskDefinition").flatMap(([, task]) => task.Properties.ContainerDefinitions);
  for (const name of ["Api", "Worker"]) {
    const environment = containers.find((c: any) => c.Name === name).Environment as any[];
    assert.deepEqual(environment.find(v => v.Name === "FABRIC_PUBLIC_URL").Value,
      { "Fn::Join": ["", ["https://", domain]] }, name);
  }
});

test("internal services stay private: only CloudFront reaches the load balancer, and nothing has a public endpoint", () => {
  const app = new App();
  const env = { account: "111111111111", region: "us-west-2" };
  const foundation = new Foundation(app, "Foundation", { env });
  const application = new Application(app, "Application", { env, foundation, imageTag: "checked" });
  const guidance = new PublicGuidance(app, "Guidance", { env });
  const resources = Template.fromStack(application).toJSON().Resources as Record<string, any>;
  const ofType = (type: string) => Object.entries(resources).filter(([, resource]) => resource.Type === type);
  const privateSubnets = ofType("AWS::EC2::Subnet").filter(([id]) => id.startsWith("Networkapp")).map(([id]) => ({ Ref: id }));
  assert.equal(privateSubnets.length, 2);
  // An internal load balancer in the private subnets has no public address, so no shared origin secret is needed:
  // CloudFront reaches it only through its one VPC origin, and no distribution has an internet (custom) origin.
  const [[albId, alb]] = ofType("AWS::ElasticLoadBalancingV2::LoadBalancer");
  assert.equal(alb.Properties.Scheme, "internal");
  assert.deepEqual(alb.Properties.Subnets, privateSubnets);
  assert.deepEqual(ofType("AWS::CloudFront::VpcOrigin").map(([, origin]) => origin.Properties.VpcOriginEndpointConfig.Arn),
    [{ Ref: albId }]);
  for (const [id, distribution] of ofType("AWS::CloudFront::Distribution")) {
    for (const origin of distribution.Properties.DistributionConfig.Origins) {
      assert.ok(origin.VpcOriginConfig || origin.S3OriginConfig, `${id} ${origin.Id}`);
      assert.equal(origin.CustomOriginConfig, undefined, `${id} ${origin.Id}`);
    }
  }
  // The load balancer admits only CloudFront's origin-facing prefix list and the Gateway adapter, on port 80; only it
  // reaches the API, only the API reaches the three private services, and nothing reaches the worker.
  const [[prefixListId, prefixList]] = ofType("Custom::AWS");
  assert.ok(JSON.stringify(prefixList.Properties).includes("com.amazonaws.global.cloudfront.origin-facing"));
  const group = (prefix: string) => ofType("AWS::EC2::SecurityGroup").find(([id]) => id.startsWith(prefix))![0];
  const sources = (target: string) => ofType("AWS::EC2::SecurityGroupIngress")
    .filter(([, rule]) => rule.Properties.GroupId["Fn::GetAtt"][0] === target)
    .map(([, { Properties: rule }]) => JSON.stringify([rule.SourcePrefixListId ?? rule.SourceSecurityGroupId ??
      rule.CidrIp ?? rule.CidrIpv6, rule.IpProtocol, rule.FromPort, rule.ToPort])).sort();
  const from = (source: object, port: number) => JSON.stringify([source, "tcp", port, port]);
  const groupId = (prefix: string) => ({ "Fn::GetAtt": [group(prefix), "GroupId"] });
  assert.deepEqual(sources(group("ApiLoadBalancerSecurityGroup")), [
    from({ "Fn::GetAtt": [prefixListId, "PrefixLists.0.PrefixListId"] }, 80),
    from(groupId("SimulationGatewayAccess"), 80)].sort());
  assert.deepEqual(sources(group("ApiServiceSecurityGroup")), [from(groupId("ApiLoadBalancerSecurityGroup"), 8000)]);
  assert.deepEqual(sources(group("FiretruckPrivateServiceSecurityGroup")), [from(groupId("ApiServiceSecurityGroup"), 8080)]);
  assert.deepEqual(sources(group("RadioPrivateServiceSecurityGroup")), [from(groupId("ApiServiceSecurityGroup"), 8000)]);
  assert.deepEqual(sources(group("FireRiskPrivateServiceSecurityGroup")), [from(groupId("ApiServiceSecurityGroup"), 8085)]);
  assert.deepEqual(sources(group("WorkerServiceSecurityGroup")), []);
  assert.deepEqual(sources(group("SimulationGatewayAccess")), []);
  for (const [id, securityGroup] of ofType("AWS::EC2::SecurityGroup")) {
    assert.equal(securityGroup.Properties.SecurityGroupIngress, undefined, `${id} has no inline ingress`);
  }
  // Tasks run in the private subnets with no public IP; only the API is a load balancer target, and the worker and
  // the private services are found only through the private Cloud Map namespace.
  for (const [id, service] of ofType("AWS::ECS::Service")) {
    const network = service.Properties.NetworkConfiguration.AwsvpcConfiguration;
    assert.equal(network.AssignPublicIp, "DISABLED", id);
    assert.deepEqual(network.Subnets, privateSubnets, id);
    assert.equal(Boolean(service.Properties.LoadBalancers), id.startsWith("ApiService"), id);
  }
  const containers = ofType("AWS::ECS::TaskDefinition").flatMap(([, task]) => task.Properties.ContainerDefinitions);
  assert.equal(containers.find((c: any) => c.Name === "Worker").PortMappings, undefined, "The worker opens no port.");
  assert.equal(ofType("AWS::ServiceDiscovery::PrivateDnsNamespace").length, 1);
  // No function URL, API, or public resource policy on any function, bucket, queue, table or secret.
  for (const stack of [foundation, application, guidance]) {
    for (const [id, resource] of Object.entries(Template.fromStack(stack).toJSON().Resources as Record<string, any>)) {
      assert.ok(!["AWS::Lambda::Url", "AWS::SecretsManager::ResourcePolicy", "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api", "AWS::ServiceDiscovery::PublicDnsNamespace"].includes(resource.Type), id);
      if (resource.Type === "AWS::DynamoDB::Table") assert.equal(resource.Properties.ResourcePolicy, undefined, id);
      if (resource.Type === "AWS::Lambda::Permission") {
        assert.equal(resource.Properties.Principal, "events.amazonaws.com", id);
        assert.ok(resource.Properties.SourceArn, `${id} is limited to its schedule rule`);
      }
      if (["AWS::S3::BucketPolicy", "AWS::SQS::QueuePolicy"].includes(resource.Type)) {
        for (const statement of resource.Properties.PolicyDocument.Statement) {
          if (statement.Effect === "Allow") {
            assert.deepEqual(Object.keys(statement.Principal), ["Service"], `${id}: only AWS services are allowed`);
          }
        }
      }
    }
  }
});

test("cdk-nag AwsSolutions checks pass on every stack, each accepted finding suppressed beside its resource", () => {
  // Offline synth leaves the Region and account as tokens; a deploy's synth writes them literally.
  for (const env of [undefined, { account: "111111111111", region: "us-west-2" }]) {
    const app = new App();
    const foundation = new Foundation(app, "Foundation", { env });
    const stacks = [foundation, new Application(app, "Application", { env, foundation, imageTag: "checked" }),
      new PublicGuidance(app, "Guidance", { env })];
    Aspects.of(app).add(new AwsSolutionsChecks());
    for (const stack of stacks) {
      const annotations = Annotations.fromStack(stack);
      const findings = [...annotations.findError("*", Match.stringLikeRegexp("AwsSolutions-")),
        ...annotations.findWarning("*", Match.stringLikeRegexp("AwsSolutions-"))];
      assert.deepEqual(findings.map(finding =>
        `${finding.id} ${String(finding.entry.data).match(/AwsSolutions-[A-Z0-9]+(\[[^\]]*\])?/)?.[0]}`), [],
      `${stack.node.id}${env ? " with an environment" : ""}`);
    }
  }
});

test("independent installations use private services and explicit repository bindings", () => {
  for (const [name, region] of [["training-alpha", "us-west-2"], ["training-beta", "us-east-1"]]) {
    const app = new App();
    const env = {account: "111111111111", region};
    const foundation = new Foundation(app, "Foundation", {env});
    const template = Template.fromStack(new Application(app, "Application", {
      env, foundation, prefix: name, imageTag: "immutable-release",
    }));
    template.resourceCountIs("AWS::ECS::Service", 5);
    template.hasResourceProperties("AWS::ServiceDiscovery::PrivateDnsNamespace", {Name: `${name}.internal`});
    template.hasResourceProperties("AWS::Cognito::UserPoolDomain", {Domain: `${name}-lab`});
    const resources = template.toJSON();
    assert.ok(!JSON.stringify(resources).includes("Fn::ImportValue"));
    for (const resource of Object.values(template.findResources("AWS::ECS::Service")) as any[]) {
      assert.equal(resource.Properties.NetworkConfiguration.AwsvpcConfiguration.AssignPublicIp, "DISABLED");
    }
    for (const task of Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[]) {
      for (const container of task.Properties.ContainerDefinitions) {
        assert.deepEqual(container.LinuxParameters.Capabilities.Drop, ["ALL"]);
        assert.equal(container.LinuxParameters.InitProcessEnabled, true);
      }
    }
    const tasks = JSON.stringify(template.findResources("AWS::ECS::TaskDefinition"));
    assert.ok(tasks.includes("FIRETRUCK_SERVICE_KEY"));
    assert.ok(tasks.includes("BROADCASTIFY_PRIVATE_KEY"));
    assert.ok(tasks.includes("FIRE_RISK_SERVICE_KEY"));
    assert.ok(tasks.includes(`http://radio.${name}.internal:8000`));
    assert.ok(tasks.includes(`http://firerisk.${name}.internal:8085`));
    assert.ok(!tasks.includes("cloudfront.net/ws"));
    assert.ok(tasks.includes("GEOCODER_FUNCTION_ARN"));
    assert.ok(!tasks.includes("ARCGIS_GEOCODE_API_KEY"));
    const geocoderPolicies = Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((p: any) => JSON.stringify(p.Properties.PolicyDocument).includes('"Ref":"GeocoderFunctionArn"')) as any[];
    assert.equal(geocoderPolicies.length, 1, "Only the API may invoke the shared geocoder.");
    for (const policy of geocoderPolicies) {
      assert.equal(policy.Condition, "HasGeocoder");
      assert.deepEqual(policy.Properties.PolicyDocument.Statement, [{
        Action: "lambda:InvokeFunction", Effect: "Allow", Resource: {Ref: "GeocoderFunctionArn"},
      }]);
    }
    template.hasResource("AWS::DynamoDB::Table", {
      DeletionPolicy: "Retain", UpdateReplacePolicy: "Retain",
      Properties: Match.objectLike({PointInTimeRecoverySpecification: {PointInTimeRecoveryEnabled: true}}),
    });
    const build = JSON.stringify(Template.fromStack(foundation).findResources("AWS::CodeBuild::Project"));
    assert.ok(build.includes("python3 deployment/container_build.py"));
    assert.ok(!build.includes("|| true"));
  }
});

test("API and SUMO worker run separate images with their own entry points", () => {
  const app = new App();
  const env = {account: "111111111111", region: "us-west-2"};
  const foundation = new Foundation(app, "Foundation", {env});
  const template = Template.fromStack(new Application(app, "Application", {env, foundation, imageTag: "release"}));
  template.hasParameter("ImageRepositoryArn", {Type: "String"});
  template.hasParameter("TrafficSimulatorRepositoryArn", {Type: "String"});
  const containers = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((task: any) => task.Properties.ContainerDefinitions) as any[];
  const api = containers.find(c => c.Name === "Api")!;
  const worker = containers.find(c => c.Name === "Worker")!;
  assert.deepEqual(worker.Command, ["python", "-m", "traffic_simulator.worker"]);
  assert.equal(api.Command.slice(0, 2).join(" "), "uvicorn backend.app:app");
  assert.ok(JSON.stringify(worker.Image).includes("TrafficSimulatorRepositoryArn"));
  assert.ok(!JSON.stringify(api.Image).includes("TrafficSimulatorRepositoryArn"));
  Template.fromStack(foundation).resourceCountIs("AWS::ECR::Repository", 6);
  Template.fromStack(foundation).hasOutput("TrafficSimulatorRepositoryArn", {Value: Match.anyValue()});
});

test("each task receives only the secrets its own code reads", () => {
  const app = new App();
  const env = {account: "111111111111", region: "us-west-2"};
  const foundation = new Foundation(app, "Foundation", {env});
  const template = Template.fromStack(new Application(app, "Application", {env, foundation, imageTag: "release"}));
  const contextSecret = Object.keys(template.findResources("AWS::SecretsManager::Secret"))
    .find(id => id.startsWith("SimulationContextSecret"))!;
  const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
  const taskFor = (name: string) => tasks.find(t => t.Properties.ContainerDefinitions.some((c: any) => c.Name === name));
  const container = (name: string) => taskFor(name).Properties.ContainerDefinitions.find((c: any) => c.Name === name);
  const secretNames = (name: string) => (container(name).Secrets as any[]).map(s => s.Name).sort();
  // A task's IAM roles, both by default: the execution role resolves container secrets, the task role runs the code.
  const rolePolicies = (name: string, properties = ["ExecutionRoleArn", "TaskRoleArn"]) => {
    const roles = properties.map(property => taskFor(name).Properties[property]["Fn::GetAtt"][0]);
    return JSON.stringify(Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((p: any) => p.Properties.Roles.some((r: any) => roles.includes(r.Ref))));
  };
  assert.deepEqual(secretNames("Worker"), ["FIRMS_MAP_KEY", "TOMTOM_API_KEY"]);
  assert.ok(!JSON.stringify(container("Worker")).includes(contextSecret));
  assert.ok(!rolePolicies("Worker").includes(contextSecret), "The SUMO worker must not read the context-signing key.");
  // The API reads the context-signing key by ARN with its task role, so a rotated key applies without a restart
  // (D36); an ECS secret would be resolved once, when the task starts.
  assert.ok(!secretNames("Api").includes("SIMULATION_CONTEXT_SECRET"));
  assert.deepEqual(container("Api").Environment.find((v: any) => v.Name === "SIMULATION_CONTEXT_SECRET_ARN")?.Value,
    { Ref: contextSecret });
  assert.ok(rolePolicies("Api", ["TaskRoleArn"]).includes(contextSecret), "The API task role reads the key.");
  assert.ok(!rolePolicies("Api", ["ExecutionRoleArn"]).includes(contextSecret), "No container secret injects the key.");
  // The API receives the whole provider secret, whose DELPHIRE_API_KEY field exists only once an installation
  // configures the camera: a JSON-key reference to a missing field would stop the task from starting (D43). The
  // worker never gets it.
  const providerSecret = Object.keys(template.findResources("AWS::SecretsManager::Secret"))
    .find(id => id.startsWith("ProviderCredentials"))!;
  const valueFrom = (name: string) => Object.fromEntries(
    (container(name).Secrets as any[]).map(s => [s.Name, s.ValueFrom]));
  assert.deepEqual(valueFrom("Api").PROVIDER_CREDENTIALS, { Ref: providerSecret });
  assert.equal(valueFrom("Worker").PROVIDER_CREDENTIALS, undefined);
  // A new field of this secret would reach the API with it, so the secret's generated template is pinned.
  const generated = template.findResources("AWS::SecretsManager::Secret")[providerSecret].Properties
    .GenerateSecretString;
  assert.equal(generated.SecretStringTemplate, JSON.stringify({ TOMTOM_API_KEY: "", FIRMS_MAP_KEY: "" }));
  assert.equal(generated.GenerateStringKey, "nonce");
  for (const name of ["Api", "Worker"]) {
    const variables = (container(name).Environment as any[]).map(v => v.Name);
    assert.ok(container(name).Environment.some((v: any) => v.Name === "STORAGE_MODE" && v.Value === "aws"), name);
    // FABRIC_PUBLIC_URL is the single public-URL setting; the legacy PUBLIC_URL has no reader.
    assert.ok(variables.includes("FABRIC_PUBLIC_URL") && !variables.includes("PUBLIC_URL"), name);
  }
});

test("outbox dispatcher reads and clears outboxes and queries only the pending index", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const jobs = Object.keys(template.findResources("AWS::DynamoDB::Table")).find(id => id.startsWith("Jobs"))!;
  const policy = Object.entries(template.findResources("AWS::IAM::Policy"))
    .find(([id]) => id.startsWith("RunOutboxDispatcherServiceRoleDefaultPolicy"))![1];
  const statements = policy.Properties.PolicyDocument.Statement as any[];
  const actions = statements.flatMap(s => [s.Action].flat());
  for (const action of ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DescribeStream",
    "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams", "sqs:SendMessage"]) {
    assert.ok(actions.includes(action), action);
  }
  // The one-time pending-index backfill ran in October 2026 and is gone, and with it the only Scan (W2.11, DEPL-4).
  for (const action of ["dynamodb:Scan", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem",
    "dynamodb:*"]) {
    assert.ok(!actions.includes(action), action);
  }
  const tableStatement = statements.find(s => [s.Action].flat().includes("dynamodb:UpdateItem"));
  assert.deepEqual(tableStatement.Resource, {"Fn::GetAtt": [jobs, "Arn"]}, "Item access is limited to the base table.");
  assert.deepEqual(statements.find(s => [s.Action].flat().includes("dynamodb:Query")).Resource,
    { "Fn::Join": ["", [{ "Fn::GetAtt": [jobs, "Arn"] }, "/index/pending-outbox"]] });
  assert.ok(!JSON.stringify(policy).includes("kms:"), "The AWS managed table key needs no KMS grant.");
  // A failed scheduled (asynchronous) invocation lands in an encrypted, TLS-only dead-letter queue (D9).
  const deadLetters = Object.keys(template.findResources("AWS::SQS::Queue"))
    .find(id => id.startsWith("OutboxDispatcherDeadLetters"))!;
  template.hasResourceProperties("AWS::Lambda::Function", { ReservedConcurrentExecutions: 2,
    DeadLetterConfig: { TargetArn: { "Fn::GetAtt": [deadLetters, "Arn"] } } });
  template.hasResourceProperties("AWS::SQS::Queue", { SqsManagedSseEnabled: true, MessageRetentionPeriod: 1209600 });
  const sends = statements.find(s => [s.Action].flat().includes("sqs:SendMessage") &&
    JSON.stringify(s.Resource).includes(deadLetters));
  assert.ok(sends, "The dispatcher may send to its dead-letter queue.");
  const queuePolicies = JSON.stringify(template.findResources("AWS::SQS::QueuePolicy"));
  assert.ok(queuePolicies.includes(deadLetters) && queuePolicies.includes("aws:SecureTransport"));
});

test("artifact collection is scheduled, bounded, and may delete only unreferenced evidence objects", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  template.hasResourceProperties("AWS::Events::Rule", { ScheduleExpression: "rate(1 hour)" });
  const collector = Object.entries(template.findResources("AWS::Lambda::Function"))
    .find(([id]) => id.startsWith("ArtifactCollector"))![1];
  assert.equal(collector.Properties.ReservedConcurrentExecutions, 1);
  assert.equal(collector.Properties.Timeout, 120);
  const policy = Object.entries(template.findResources("AWS::IAM::Policy"))
    .find(([id]) => id.startsWith("ArtifactCollectorServiceRoleDefaultPolicy"))![1];
  const statements = policy.Properties.PolicyDocument.Statement as any[];
  assert.deepEqual(statements.find(s => s.Action === "s3:ListBucket").Condition,
    { StringLike: { "s3:prefix": ["runs/", "runs/*", "reports/", "reports/*",
      "fire-risk/inputs/", "fire-risk/inputs/*"] } });
  const deletes = statements.find(s => s.Action === "s3:DeleteObject");
  assert.equal(deletes.Resource.length, 3);
  assert.ok(JSON.stringify(deletes.Resource).includes("/runs/*"));
  assert.ok(JSON.stringify(deletes.Resource).includes("/reports/*"));
  assert.ok(JSON.stringify(deletes.Resource).includes("/fire-risk/inputs/*"));
  assert.ok(!JSON.stringify(policy).includes("s3:GetObject"));
  assert.ok(!JSON.stringify(policy).includes("dynamodb:DeleteItem"));
});

test("security groups send only HTTPS to the internet, plus the private service ports the API needs", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const groups = template.findResources("AWS::EC2::SecurityGroup") as Record<string, any>;
  const id = (prefix: string) => Object.keys(groups).find(key => key.startsWith(prefix))!;
  const egress = (prefix: string) => groups[id(prefix)].Properties.SecurityGroupEgress as any[];
  const https = { CidrIp: "0.0.0.0/0", IpProtocol: "tcp", FromPort: 443, ToPort: 443,
    Description: "HTTPS to AWS APIs and data providers" };
  for (const service of ["WorkerService", "FiretruckPrivateService", "RadioPrivateService", "FireRiskPrivateService"]) {
    assert.deepEqual(egress(`${service}SecurityGroup`), [https], service);
  }
  const toService = (service: string, port: number) => ({
    DestinationSecurityGroupId: { "Fn::GetAtt": [id(`${service}SecurityGroup`), "GroupId"] },
    IpProtocol: "tcp", FromPort: port, ToPort: port, Description: `Private service on port ${port}` });
  assert.deepEqual(egress("ApiServiceSecurityGroup"),
    [https, toService("FireRiskPrivateService", 8085), toService("FiretruckPrivateService", 8080),
      toService("RadioPrivateService", 8000)]);
  // The Gateway adapter holds no key and calls only the load balancer, so it has no internet egress at all.
  assert.deepEqual(egress("SimulationGatewayAccess"), [{ CidrIp: "255.255.255.255/32",
    Description: "Disallow all traffic", IpProtocol: "icmp", FromPort: 252, ToPort: 86 }]);
  const adapterRules = Object.values(template.findResources("AWS::EC2::SecurityGroupEgress") as Record<string, any>)
    .filter(rule => rule.Properties.GroupId["Fn::GetAtt"][0] === id("SimulationGatewayAccess"));
  assert.deepEqual(adapterRules.map(rule => [rule.Properties.DestinationSecurityGroupId, rule.Properties.FromPort]),
    [[{ "Fn::GetAtt": [id("ApiLoadBalancerSecurityGroup"), "GroupId"] }, 80]]);
  // No group allows every protocol, and none sends anything but HTTPS to an address range.
  for (const [name, group] of Object.entries(groups)) {
    for (const rule of group.Properties.SecurityGroupEgress ?? []) {
      assert.notEqual(rule.IpProtocol, "-1", name);
      if (rule.CidrIp && rule.CidrIp !== "255.255.255.255/32") assert.deepEqual([rule.IpProtocol, rule.FromPort,
        rule.ToPort], ["tcp", 443, 443], name);
    }
  }
});

test("the WAF per-IP rate comes from the deployment settings file, with no default in code", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const parameter = template.toJSON().Parameters.RequestsPerIpPer5Minutes;
  assert.equal(parameter.Type, "Number");
  assert.equal(parameter.Default, undefined, "deployment/settings.toml holds the default (D23).");
  const rules = Object.values(template.findResources("AWS::WAFv2::WebACL"))[0].Properties.Rules as any[];
  const rate = rules.find(r => r.Name === "BoundRequestRate").Statement.RateBasedStatement;
  assert.deepEqual(rate.Limit, { Ref: "RequestsPerIpPer5Minutes" });
});

test("container limits come from the settings file and reach only the tasks whose code reads them", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const parameters = template.toJSON().Parameters;
  // Both images read these limits. Road preparation must receive the same maximum as API admission.
  const simulator = {TOMTOM_CALLS_PER_DAY: "TomtomCallsPerDay", TOMTOM_REQUESTS_PER_RUN: "TomtomRequestsPerRun",
    TOMTOM_RESPONSE_MAX_AGE_SECONDS: "TomtomResponseMaxAgeSeconds", TOMTOM_CACHE_MINUTES: "TomtomCacheMinutes",
    DEMO_SESSION_DAYS: "DemoSessionDays", CALTRANS_RESPONSE_MAX_MB: "CaltransResponseMaxMb",
    MAX_STUDY_RADIUS_KM: "MaxStudyRadiusKm"};
  // Only backend.api_settings (the API image) reads these.
  const api = {AGENT_REQUESTS_PER_DAY: "AgentRequestsPerDay", GEOCODING_REQUESTS_PER_DAY: "GeocodingRequestsPerDay",
    ACTIVE_JOBS_ALL_VISITORS: "ActiveJobsAllVisitors", ACTIVE_JOBS_PER_SESSION: "ActiveJobsPerSession",
    DISCOVERIES_PER_API_PROCESS: "DiscoveriesPerApiProcess", REQUEST_BODY_MAX_KIB: "RequestBodyMaxKib",
    REQUEST_BODY_SECONDS: "RequestBodySeconds", DEMO_SESSIONS_PER_IP_PER_DAY: "DemoSessionsPerIpPerDay",
    CAMERA_CALLS_PER_DAY: "CameraCallsPerDay",
    CAMERA_CACHE_SECONDS: "CameraCacheSeconds", CAMERA_IMAGE_MAX_KIB: "CameraImageMaxKib",
    CAMERA_REQUEST_TIMEOUT_SECONDS: "CameraRequestTimeoutSeconds"};
  for (const parameter of [...Object.values(simulator), ...Object.values(api)]) {
    assert.equal(parameters[parameter].Type, "Number", parameter);
    assert.equal(parameters[parameter].Default, undefined, `${parameter}: deployment/settings.toml holds the default (D23).`);
  }
  const containers = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((task: any) => task.Properties.ContainerDefinitions) as any[];
  const environment = (name: string) => Object.fromEntries(
    (containers.find(c => c.Name === name).Environment as any[]).map(v => [v.Name, v.Value]));
  for (const [variable, parameter] of Object.entries(simulator)) {
    for (const name of ["Api", "Worker"]) assert.deepEqual(environment(name)[variable], { Ref: parameter }, name);
  }
  for (const [variable, parameter] of Object.entries(api)) {
    assert.deepEqual(environment("Api")[variable], { Ref: parameter }, variable);
    assert.equal(environment("Worker")[variable], undefined, `The worker does not read ${variable}.`);
  }
  // Only the SUMO worker simulates policies.
  assert.equal(parameters.PolicyParallelism.Type, "Number");
  assert.equal(parameters.PolicyParallelism.Default, undefined, "deployment/settings.toml holds the default (D23).");
  assert.deepEqual(environment("Worker").POLICY_PARALLELISM, { Ref: "PolicyParallelism" });
  assert.equal(environment("Api").POLICY_PARALLELISM, undefined, "The API does not simulate policies.");
  // Only the worker builds, caches and pre-builds road networks and captures TomTom traffic for runs.
  assert.deepEqual(environment("Api").OVERPASS_RESPONSE_MAX_MB, { Ref: "OverpassResponseMaxMb" });
  assert.deepEqual(environment("Worker").OVERPASS_RESPONSE_MAX_MB, { Ref: "OverpassResponseMaxMb" });
  const worker = {MAX_NETWORK_EDGES: "MaxNetworkEdges",
    CACHED_NETWORKS_PER_WORKER: "CachedNetworksPerWorker", CACHED_NETWORK_MIB_PER_WORKER: "CachedNetworkMibPerWorker",
    DEMO_AREAS_TO_PREBUILD: "DemoAreasToPrebuild", TOMTOM_REQUEST_CONCURRENCY: "TomtomRequestConcurrency"};
  for (const [variable, parameter] of Object.entries(worker)) {
    assert.equal(parameters[parameter].Type, variable === "DEMO_AREAS_TO_PREBUILD" ? "String" : "Number", parameter);
    assert.equal(parameters[parameter].Default, undefined, `${parameter}: deployment/settings.toml holds the default (D23).`);
    assert.deepEqual(environment("Worker")[variable], { Ref: parameter }, variable);
    assert.equal(environment("Api")[variable], undefined, `The API does not read ${variable}.`);
  }
});

test("the optional [camera] parameters admit an installation without a camera and reach only the API", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const parameters = template.toJSON().Parameters;
  const patterns: Record<string, string> = {
    CameraApiBaseUrl: `(${CAMERA_ORIGIN})?`, CameraDeviceId: `(${CAMERA_DEVICE_ID})?`,
    CameraAlias: "(camera_a|camera_b|infrared_camera_a|infrared_camera_b|test_camera_a)?",
  };
  for (const [name, pattern] of Object.entries(patterns)) {
    assert.deepEqual([parameters[name].Type, parameters[name].Default, parameters[name].AllowedPattern,
      parameters[name].AllowedValues], ["String", "", pattern, undefined], name);
  }
  assert.equal(parameters.CameraApiBaseUrl.MaxLength, 261);
  // CloudFormation matches an allowed pattern against the whole value.
  const allows = (name: string, value: string) => new RegExp(`^(?:${parameters[name].AllowedPattern})$`).test(value);
  for (const name of Object.keys(patterns)) assert.ok(allows(name, ""), `${name} admits an installation without a camera`);
  assert.ok(allows("CameraApiBaseUrl", "https://wildfireai.com") && allows("CameraAlias", "infrared_camera_b"));
  for (const refused of ["http://203.0.113.10:2361", "https://203.0.113.10", "https://wildfireai.com/x"]) {
    assert.ok(!allows("CameraApiBaseUrl", refused), refused);
  }
  assert.ok(allows("CameraDeviceId", "a1_B-2") && !allows("CameraDeviceId", "../a"));
  const containers = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((task: any) => task.Properties.ContainerDefinitions) as any[];
  const environment = (name: string) => Object.fromEntries(
    (containers.find(c => c.Name === name).Environment as any[]).map(v => [v.Name, v.Value]));
  for (const [variable, parameter] of [["CAMERA_API_BASE_URL", "CameraApiBaseUrl"],
    ["CAMERA_DEVICE_ID", "CameraDeviceId"], ["CAMERA_ALIAS", "CameraAlias"]]) {
    assert.deepEqual(environment("Api")[variable], { Ref: parameter }, variable);
    assert.equal(environment("Worker")[variable], undefined, `The worker does not read ${variable}.`);
  }
});

test("firetruck limits come from the settings file and reach only the firetruck service and its broker", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const parameters = template.toJSON().Parameters;
  const containers = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((task: any) => task.Properties.ContainerDefinitions) as any[];
  const environment = (name: string) => Object.fromEntries(
    (containers.find(c => c.Name === name).Environment as any[]).map(v => [v.Name, v.Value]));
  // The service stops a run at its lifetime and the broker stops counting it as active then, so both read it.
  const readers: Record<string, [string, string[]]> = {
    FIRETRUCK_RUN_LIFETIME_MINUTES: ["FiretruckRunLifetimeMinutes", ["Firetruck", "Api"]],
    FIRETRUCK_RUNS_PER_TASK: ["FiretruckRunsPerTask", ["Firetruck"]],
    FIRETRUCK_EVIDENCE_EVENTS_MAX: ["FiretruckEvidenceEventsMax", ["Firetruck"]],
    FIRETRUCK_ACTIVE_RUNS_PER_SESSION: ["FiretruckActiveRunsPerSession", ["Api"]],
    FIRETRUCK_RUNS_PER_SESSION: ["FiretruckRunsPerSession", ["Api"]],
    FIRETRUCK_STREAMS_PER_SESSION: ["FiretruckStreamsPerSession", ["Api"]],
    FIRETRUCK_STREAM_LIFETIME_MINUTES: ["FiretruckStreamLifetimeMinutes", ["Api"]],
  };
  for (const [variable, [parameter, tasks]] of Object.entries(readers)) {
    assert.equal(parameters[parameter].Type, "Number", parameter);
    assert.equal(parameters[parameter].Default, undefined, `${parameter}: deployment/settings.toml holds the default (D23).`);
    for (const name of ["Api", "Worker", "Firetruck", "Radio", "FireRisk"]) {
      assert.deepEqual(environment(name)[variable], tasks.includes(name) ? { Ref: parameter } : undefined,
        `${name} ${tasks.includes(name) ? "reads" : "does not read"} ${variable}`);
    }
  }
});

test("the radio task gets only the settings and grants its deployed code uses", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
  const task = tasks.find(t => t.Properties.ContainerDefinitions.some((c: any) => c.Name === "Radio"));
  const radio = task.Properties.ContainerDefinitions.find((c: any) => c.Name === "Radio");
  // The recorded investigation (broadcastify/monitor/private.py) reads its recordings, keywords and model. Only the
  // API calls it, so no CORS_ORIGINS; it has one mode (W2.5, BCFY-2).
  assert.deepEqual(radio.Environment.map((v: any) => v.Name).sort(), ["AWS_REGION", "FIRE_KEYWORDS",
    "MONITOR_BEDROCK_MODEL_ID", "MONITOR_TEST_FILES", "PYTHONDONTWRITEBYTECODE", "S3_BUCKET"]);
  assert.deepEqual(radio.Secrets.map((s: any) => s.Name), ["API_KEY"]);
  // The role reads only input/ and invokes no Lambda: the API resolves the address the investigation reports.
  const role = task.Properties.TaskRoleArn["Fn::GetAtt"][0];
  const statements = Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((p: any) => p.Properties.Roles.some((r: any) => r.Ref === role))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement as any[]);
  assert.ok(!statements.some(s => [s.Action].flat().includes("lambda:InvokeFunction")));
  assert.ok(!JSON.stringify(statements).includes("/output/*"));
  const bucketId = Object.keys(template.findResources("AWS::S3::Bucket")).find(id => id.startsWith("RadioAudio"));
  const bucket = { "Fn::GetAtt": [bucketId, "Arn"] };
  const s3 = statements.filter(s => [s.Action].flat().some((action: string) => action.startsWith("s3:")));
  assert.equal(s3.length, 1);
  assert.deepEqual(s3[0].Resource, [bucket, { "Fn::Join": ["", [bucket, "/input/*"]] }]);
  // Read-only: no put, delete or abort on the recordings the investigation reads (C27, BCFY-5).
  assert.deepEqual([s3[0].Action].flat(), ["s3:GetObject*", "s3:GetBucket*", "s3:List*"]);
  // Its accepted cdk-nag wildcards are the read grant's only, so a later write grant would be reported, not hidden.
  const accepted = Object.entries(template.toJSON().Resources as Record<string, any>)
    .filter(([id, resource]) => id === role || (resource.Type === "AWS::IAM::Policy"
      && resource.Properties.Roles.some((r: any) => r.Ref === role)))
    .flatMap(([, resource]) => resource.Metadata?.cdk_nag?.rules_to_suppress ?? [])
    .flatMap((rule: any) => rule.applies_to ?? []);
  assert.ok(accepted.includes("Action::s3:GetObject*"), "the read grant's wildcards stay accepted");
  assert.deepEqual(accepted.filter((entry: any) => ["Action::s3:DeleteObject*", "Action::s3:Abort*"]
    .includes(entry)), []);
});

test("the fire risk modeler is stateless, bounded and reachable only through its API broker", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
  const task = tasks.find(t => t.Properties.ContainerDefinitions.some((c: any) => c.Name === "FireRisk"));
  const modeler = task.Properties.ContainerDefinitions.find((c: any) => c.Name === "FireRisk");
  const api = tasks.find(t => t.Properties.ContainerDefinitions.some((c: any) => c.Name === "Api"))
    .Properties.ContainerDefinitions.find((c: any) => c.Name === "Api");
  assert.ok(JSON.stringify(modeler.Image).includes("FireRiskRepositoryArn"));
  assert.deepEqual(modeler.PortMappings, [{ ContainerPort: 8085, Protocol: "tcp" }]);
  assert.equal(modeler.ReadonlyRootFilesystem, true);
  assert.deepEqual(modeler.LinuxParameters, { Capabilities: { Drop: ["ALL"] }, InitProcessEnabled: true });
  assert.deepEqual(task.Properties.Volumes, [{ Name: "scratch" }]);
  assert.deepEqual(modeler.MountPoints, [{ ContainerPath: "/tmp", ReadOnly: false, SourceVolume: "scratch" }]);
  assert.equal(modeler.StopTimeout, 120);
  assert.deepEqual(modeler.HealthCheck, {
    Command: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=3)\""],
    Interval: 30, Timeout: 5, Retries: 3, StartPeriod: 60,
  });
  const secret = Object.keys(template.findResources("AWS::SecretsManager::Secret"))
    .find(id => id.startsWith("FireRiskServiceKey"))!;
  assert.deepEqual(modeler.Secrets, [{ Name: "FIRE_RISK_SERVICE_KEY", ValueFrom: { Ref: secret } }]);
  assert.deepEqual(api.Secrets.find((s: any) => s.Name === "FIRE_RISK_SERVICE_KEY")?.ValueFrom, { Ref: secret });
  // ECS resolves the bearer with the execution role; calculator code needs no AWS storage or provider permissions.
  const role = task.Properties.TaskRoleArn["Fn::GetAtt"][0];
  const policies = Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((p: any) => p.Properties.Roles.some((r: any) => r.Ref === role));
  assert.deepEqual(policies, []);
  const modelerEnvironment = Object.fromEntries(modeler.Environment.map((v: any) => [v.Name, v.Value]));
  for (const variable of ["JOBS_TABLE", "ARTIFACT_BUCKET", "QUEUE_URL", "WEATHER_FUNCTION_ARN",
    "PROVIDER_CREDENTIALS", "SIMULATION_RUNTIME_ARN"]) assert.equal(modelerEnvironment[variable], undefined, variable);
  assert.equal(modelerEnvironment.FIRE_RISK_ENVIRONMENT, "ecs");
  const services = template.findResources("AWS::ECS::Service") as Record<string, any>;
  const service = Object.entries(services).find(([id]) => id.startsWith("FireRiskPrivateService"))![1];
  assert.deepEqual(service.Properties.DeploymentConfiguration.DeploymentCircuitBreaker,
    { Enable: true, Rollback: true });
  template.hasOutput("FireRiskServiceName", { Value: Match.anyValue() });
  for (const output of ["FireRiskRepositoryUri", "FireRiskRepositoryArn"]) {
    Template.fromStack(foundation).hasOutput(output, { Value: Match.anyValue() });
  }
});

test("risk model and broker limits reach their readers and DTN invocation stays scoped to the configured Lambda", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const json = template.toJSON();
  const containers = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((task: any) => task.Properties.ContainerDefinitions) as any[];
  const environment = (name: string) => Object.fromEntries(
    (containers.find(c => c.Name === name).Environment as any[]).map(v => [v.Name, v.Value]));
  const model = ["InnerKm", "MiddleKm", "OuterKm", "RingPoints", "WindShapeScale", "MaxWindStretch",
    "RequestBodyMaxKib", "ResponseMaxKib", "PerimeterMaxVertices", "CalculationsPerProcess", "CalculationTimeoutSeconds",
    "LandscapeRequestMaxKib", "LandscapeCalculationTimeoutSeconds", "LandscapeMaxCells", "LandscapeDirectionalRadius"];
  const broker = ["AssessmentsPerDay", "WeatherCallsPerDay", "MaxFiresPerTurn", "PostprocessingSeconds",
    "WeatherTimeoutSeconds", "WeatherMaxAgeSeconds", "WeatherMaxDistanceKm", "SurfaceFuelModel",
    "SurfaceFirstMinutes", "SurfaceSecondMinutes", "SurfaceThirdMinutes", "SurfaceMoistureProfile",
    "LandscapeAcquisitionTimeoutSeconds", "LandscapeSourceMaxMib", "LandscapeInitialRadiusM"];
  const variableFor = (suffix: string) => "FIRE_RISK_" +
    suffix.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toUpperCase();
  for (const suffix of [...model, ...broker]) {
    const parameter = `FireRisk${suffix}`;
    const variable = variableFor(suffix);
    assert.equal(json.Parameters[parameter].Type, suffix === "SurfaceMoistureProfile" ? "String" : "Number", parameter);
    assert.equal(json.Parameters[parameter].Default, undefined, `${parameter} comes from deployment/settings.toml`);
    assert.deepEqual(environment("Api")[variable], { Ref: parameter }, `API ${variable}`);
    assert.deepEqual(environment("FireRisk")[variable], model.includes(suffix) ? { Ref: parameter } : undefined,
      `modeler ${variable}`);
    for (const name of ["Worker", "Firetruck", "Radio"]) assert.equal(environment(name)[variable], undefined, name);
  }
  assert.deepEqual(environment("Api").WEATHER_FUNCTION_ARN, { Ref: "WeatherFunctionArn" });
  for (const name of ["Worker", "Firetruck", "Radio", "FireRisk"]) {
    assert.equal(environment(name).WEATHER_FUNCTION_ARN, undefined, name);
  }
  const policies = template.findResources("AWS::IAM::Policy") as Record<string, any>;
  const invocation = Object.entries(policies).find(([id]) => id.startsWith("ApiWeatherInvocation"))![1];
  const apiTask = Object.entries(template.findResources("AWS::ECS::TaskDefinition"))
    .find(([id]) => id.startsWith("ApiTask"))![1] as any;
  assert.deepEqual(invocation.Properties.Roles, [{ Ref: apiTask.Properties.TaskRoleArn["Fn::GetAtt"][0] }]);
  assert.equal(invocation.Condition, "HasWeather");
  assert.deepEqual(invocation.Properties.PolicyDocument.Statement, [{
    Action: "lambda:InvokeFunction", Effect: "Allow", Resource: { Ref: "WeatherFunctionArn" },
  }]);
  assert.equal(conditionValue(json, "HasWeather", { WeatherFunctionArn: "" }), false);
  assert.equal(conditionValue(json, "HasWeather", {
    WeatherFunctionArn: "arn:aws:lambda:us-west-2:111111111111:function:weather-dtn",
  }), true);
  assert.equal(Object.values(policies).filter(policy =>
    JSON.stringify(policy.Properties.PolicyDocument).includes('"Ref":"WeatherFunctionArn"')).length, 1);
});

test("risk preparation has a dedicated encrypted queue and only the API consumes it", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const [queueId, queue] = Object.entries(template.findResources("AWS::SQS::Queue"))
    .find(([id]) => id.startsWith("FireRiskPreparation"))! as [string, any];
  assert.equal(queue.Properties.SqsManagedSseEnabled, true);
  assert.equal(queue.Properties.VisibilityTimeout, 180);
  assert.equal(queue.Properties.ReceiveMessageWaitTimeSeconds, 20);
  assert.equal(queue.Properties.RedrivePolicy.maxReceiveCount, 5);
  const tasks = Object.entries(template.findResources("AWS::ECS::TaskDefinition")) as [string, any][];
  const api = tasks.find(([id]) => id.startsWith("ApiTask"))![1];
  const worker = tasks.find(([id]) => id.startsWith("WorkerTask"))![1];
  const env = api.Properties.ContainerDefinitions.find((c: any) => c.Name === "Api").Environment;
  assert.deepEqual(env.find((v: any) => v.Name === "FIRE_RISK_QUEUE_URL").Value, { Ref: queueId });
  assert.ok(!worker.Properties.ContainerDefinitions[0].Environment.some(
    (v: any) => v.Name === "FIRE_RISK_QUEUE_URL"));
  const policies = Object.values(template.findResources("AWS::IAM::Policy")) as any[];
  const consumers = policies.filter(p => p.Properties.PolicyDocument.Statement.some(
    (s: any) => [s.Action].flat().includes("sqs:ReceiveMessage") && JSON.stringify(s.Resource).includes(queueId)));
  assert.equal(consumers.length, 1);
  assert.deepEqual(consumers[0].Properties.Roles, [{ Ref: api.Properties.TaskRoleArn["Fn::GetAtt"][0] }]);
});

// Resolve the intrinsic functions CDK writes into a policy (Ref, Fn::Join, Fn::Split, Fn::Select), as CloudFormation does.
// A resource attribute (Fn::GetAtt) resolves to the resource's logical ID without CDK's 8-hex suffix.
const resolveIntrinsics = (value: any, refs: Record<string, string | string[]>): any => {
  if (Array.isArray(value)) return value.map(v => resolveIntrinsics(v, refs));
  if (value === null || typeof value !== "object") return value;
  if ("Ref" in value) return refs[value.Ref];
  if ("Fn::GetAtt" in value) return value["Fn::GetAtt"][0].replace(/[0-9A-F]{8}$/, "");
  if ("Fn::Join" in value) return resolveIntrinsics(value["Fn::Join"][1], refs).join(value["Fn::Join"][0]);
  if ("Fn::Split" in value) return resolveIntrinsics(value["Fn::Split"][1], refs).split(value["Fn::Split"][0]);
  if ("Fn::Select" in value) return resolveIntrinsics(value["Fn::Select"][1], refs)[Number(value["Fn::Select"][0])];
  return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveIntrinsics(v, refs)]));
};

test("the radio task may invoke only its configured Bedrock model and stream Transcribe in its own Region", () => {
  const app = new App();
  const env = { account: "111111111111", region: "us-west-2" };
  const foundation = new Foundation(app, "Foundation", { env });
  const template = Template.fromStack(new Application(app, "Application", { env, foundation, imageTag: "checked" }));
  const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
  const role = tasks.find(t => t.Properties.ContainerDefinitions.some((c: any) => c.Name === "Radio"))
    .Properties.TaskRoleArn["Fn::GetAtt"][0];
  const statements = Object.values(template.findResources("AWS::IAM::Policy"))
    .filter((p: any) => p.Properties.Roles.some((r: any) => r.Ref === role))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement as any[]);
  const transcribe = statements.filter(s => JSON.stringify(s.Action).includes("transcribe:"));
  assert.deepEqual(transcribe.map(s => [s.Action, s.Resource, s.Condition]), [["transcribe:StartStreamTranscription",
    "*", { StringEquals: { "aws:RequestedRegion": "us-west-2" } }]], "Transcribe streaming has no resource ARNs.");
  const bedrock = statements.filter(s => JSON.stringify(s.Action).includes("bedrock:"));
  assert.equal(bedrock.length, 2);
  for (const statement of bedrock) {
    assert.equal(statement.Action, "bedrock:InvokeModel");
  }
  // The installation names its agent model by inference profile or foundation model ID, or by either ARN.
  const name = "anthropic.claude-sonnet-4-5-20250929-v1:0";
  for (const [modelId, model] of [[`us.${name}`, `us.${name}`], [`global.${name}`, `global.${name}`], [name, name],
    [`arn:aws:bedrock:us-west-2:111111111111:inference-profile/us.${name}`, `us.${name}`],
    [`arn:aws:bedrock:us-west-2::foundation-model/${name}`, name]]) {
    const [direct, routed] = [bedrock.find(s => !s.Condition), bedrock.find(s => s.Condition)]
      .map(s => resolveIntrinsics(s, { RadioModelId: modelId, "AWS::Partition": "aws" }));
    const profile = `arn:aws:bedrock:us-west-2:111111111111:inference-profile/${model}`;
    assert.deepEqual(direct.Resource, [profile, `arn:aws:bedrock:us-west-2::foundation-model/${model}`], modelId);
    // A cross-Region profile runs the model in other Regions (a global one in none), only through that profile.
    assert.deepEqual(routed.Resource, ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:::foundation-model/*"]);
    assert.deepEqual(routed.Condition, { StringEquals: { "bedrock:InferenceProfileArn": profile } }, modelId);
  }
});

test("the shared road-network cache expires by a lifecycle rule and only the worker reads and writes it", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const parameter = template.toJSON().Parameters.NetworkCacheDays;
  assert.equal(parameter.Type, "Number");
  assert.equal(parameter.Default, undefined, "deployment/settings.toml holds the default (D23).");
  const [artifactsId, artifacts] = Object.entries(template.findResources("AWS::S3::Bucket"))
    .find(([id]) => id.startsWith("Artifacts")) as [string, any];
  assert.equal(artifacts.DeletionPolicy, "Retain");
  const rules = artifacts.Properties.LifecycleConfiguration.Rules as any[];
  // The bucket is versioned, so an expired network leaves a noncurrent copy, which goes a day later.
  assert.deepEqual(rules.find(rule => rule.Id === "ExpireSharedRoadNetworks"), {
    Id: "ExpireSharedRoadNetworks", Prefix: "network-cache/", ExpirationInDays: { Ref: "NetworkCacheDays" },
    NoncurrentVersionExpiration: { NoncurrentDays: 1 }, Status: "Enabled",
  });
  assert.deepEqual(rules.filter(rule => rule.ExpirationInDays).map(rule => rule.Prefix),
    ["osm-extracts/", "network-cache/", "firetruck/"],
    "Disposable road caches expire; landscape inputs follow their job and assessment references.");
  const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
  const statements = (name: string) => {
    const task = tasks.find(t => t.Properties.ContainerDefinitions.some((c: any) => c.Name === name));
    const role = task.Properties.TaskRoleArn["Fn::GetAtt"][0];
    return Object.values(template.findResources("AWS::IAM::Policy"))
      .filter((p: any) => p.Properties.Roles.some((r: any) => r.Ref === role))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement as any[]);
  };
  const cacheActions = (name: string) => statements(name)
    .filter(s => JSON.stringify(s.Resource).includes("/network-cache/*"))
    .flatMap(s => [s.Action].flat() as string[]);
  const worker = cacheActions("Worker");
  assert.ok(worker.includes("s3:GetObject*") && worker.includes("s3:PutObject"), worker.join());
  assert.ok(!worker.some(action => action.startsWith("s3:DeleteObject")), "The lifecycle rule deletes, not the worker.");
  assert.ok(statements("Worker").filter(s => JSON.stringify(s.Resource).includes("/network-cache/*"))
    .every(s => JSON.stringify(s.Resource).includes(artifactsId)), "Only the artifacts bucket's prefix.");
  assert.deepEqual(cacheActions("Api"), [], "The API never reads or writes the network cache.");
});

test("every bucket is retained, private, TLS-only, ACL-free and versioned, and replaced copies expire", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const application = new Application(app, "Application", { foundation, imageTag: "checked" });
  for (const stack of [foundation, application]) {
    const template = Template.fromStack(stack);
    const policies = Object.values(template.findResources("AWS::S3::BucketPolicy")) as any[];
    const buckets = Object.entries(template.findResources("AWS::S3::Bucket")) as [string, any][];
    assert.equal(buckets.length, stack === foundation ? 2 : 3);
    for (const [id, bucket] of buckets) {
      assert.equal(bucket.DeletionPolicy, "Retain", id);
      assert.deepEqual(bucket.Properties.PublicAccessBlockConfiguration, { BlockPublicAcls: true,
        BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true }, id);
      assert.deepEqual(bucket.Properties.OwnershipControls, { Rules: [{ ObjectOwnership: "BucketOwnerEnforced" }] }, id);
      assert.deepEqual(bucket.Properties.VersioningConfiguration, { Status: "Enabled" }, id);
      const statements = policies.filter(p => p.Properties.Bucket.Ref === id)
        .flatMap(p => p.Properties.PolicyDocument.Statement as any[]);
      assert.ok(statements.some(s => s.Effect === "Deny" && s.Action === "s3:*" && s.Principal.AWS === "*" &&
        s.Condition.Bool["aws:SecureTransport"] === "false"), `${id} refuses requests without TLS`);
    }
  }
  // Firetruck evidence (G26): a run's final snapshot goes with the demo session lifetime; replaced copies a day later.
  const artifacts = Object.entries(Template.fromStack(application).findResources("AWS::S3::Bucket"))
    .find(([id]) => id.startsWith("Artifacts"))![1] as any;
  const rules = artifacts.Properties.LifecycleConfiguration.Rules as any[];
  assert.deepEqual(rules.find(rule => rule.Id === "ExpireFiretruckEvidence"), {
    Id: "ExpireFiretruckEvidence", Prefix: "firetruck/", ExpirationInDays: { Ref: "DemoSessionDays" },
    NoncurrentVersionExpiration: { NoncurrentDays: 1 }, Status: "Enabled",
  });
  // Everywhere else an overwritten or deleted artifact stays recoverable for 30 days (D37).
  assert.deepEqual(rules.find(rule => rule.Id === "ExpireReplacedArtifacts"), {
    Id: "ExpireReplacedArtifacts", NoncurrentVersionExpiration: { NoncurrentDays: 30 },
    AbortIncompleteMultipartUpload: { DaysAfterInitiation: 1 }, ExpiredObjectDeleteMarker: true, Status: "Enabled",
  });
});

test("every ECS task size comes from the settings file through the Fargate units table", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const json = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" })).toJSON();
  // CloudFormation cannot multiply: whole vCPUs and GiB map to CPU units and MiB (n -> n * 1024).
  const units = json.Mappings.FargateUnits;
  assert.equal(Object.keys(units).length, 120);
  for (const [key, value] of Object.entries(units) as [string, any][]) {
    assert.deepEqual(value, { Units: String(Number(key) * 1024) }, key);
  }
  const tasks = Object.entries(json.Resources).filter(([, r]: [string, any]) => r.Type === "AWS::ECS::TaskDefinition");
  assert.equal(tasks.length, 5);
  for (const [prefix, task] of [["ApiTask", "Api"], ["WorkerTask", "Worker"], ["FiretruckPrivateTask", "Firetruck"],
    ["RadioPrivateTask", "Radio"], ["FireRiskPrivateTask", "FireRisk"]]) {
    const [, resource] = tasks.find(([id]) => id.startsWith(prefix)) as [string, any];
    assert.deepEqual(resource.Properties.Cpu, { "Fn::FindInMap": ["FargateUnits", { Ref: `${task}Vcpu` }, "Units"] });
    assert.deepEqual(resource.Properties.Memory,
      { "Fn::FindInMap": ["FargateUnits", { Ref: `${task}MemoryGib` }, "Units"] });
    for (const parameter of [`${task}Vcpu`, `${task}MemoryGib`]) {
      assert.equal(json.Parameters[parameter].Type, "Number", parameter);
      assert.equal(json.Parameters[parameter].Default, undefined, `${parameter}: deployment/settings.toml holds it.`);
    }
  }
});

// Run a CloudFront Function's code on one viewer request, as the edge does.
const runViewerFunction = (template: Template, prefix: string, uri: string, headers: object = {}) => {
  const functions = template.findResources("AWS::CloudFront::Function") as Record<string, any>;
  const code = Object.entries(functions).find(([id]) => id.startsWith(prefix))![1].Properties.FunctionCode;
  return runInNewContext(code + "; handler(input)", {
    input: { request: { uri, headers: { ...headers } }, viewer: { ip: "203.0.113.1" } },
  });
};

test("WAF limits requests that start work per viewer IP, from the settings file, with a readable 429", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const parameter = template.toJSON().Parameters.WorkRequestsPerIpPer5Minutes;
  assert.equal(parameter.Type, "Number");
  assert.equal(parameter.Default, undefined, "deployment/settings.toml holds the default (D23).");
  const acl = Object.values(template.findResources("AWS::WAFv2::WebACL"))[0].Properties;
  const rules = acl.Rules as any[];
  // The managed groups and the per-IP rule for all requests stay, at their priorities.
  assert.deepEqual(rules.filter(r => r.Name).map(r => [r.Name, r.Priority]), [
    ["AWSManagedRulesAmazonIpReputationList", 0], ["AWSManagedRulesKnownBadInputsRuleSet", 1],
    ["BoundRequestRate", 10], ["BoundWorkRate", 11]]);
  const rule = rules.find(r => r.Name === "BoundWorkRate");
  const rate = rule.Statement.RateBasedStatement;
  assert.deepEqual(rate.Limit, { Ref: "WorkRequestsPerIpPer5Minutes" });
  assert.equal(rate.EvaluationWindowSec, 300);
  assert.equal(rate.AggregateKeyType, "FORWARDED_IP");
  assert.deepEqual(rate.ForwardedIPConfig, { HeaderName: "X-Lab-Viewer-IP", FallbackBehavior: "MATCH" });
  const [method, path] = rate.ScopeDownStatement.AndStatement.Statements;
  assert.deepEqual(method.ByteMatchStatement, { FieldToMatch: { Method: {} }, PositionalConstraint: "EXACTLY",
    SearchString: "post", TextTransformations: [{ Priority: 0, Type: "LOWERCASE" }] });
  assert.deepEqual(path.RegexMatchStatement.FieldToMatch, { UriPath: {} });
  assert.equal(path.RegexMatchStatement.RegexString, workStartingPaths);
  assert.deepEqual(path.RegexMatchStatement.TextTransformations.map((t: any) => t.Type), ["URL_DECODE", "LOWERCASE"]);
  // WAF decodes and lower-cases the path the load balancer receives, so encoded or case-varied requests still count.
  const counted = (uri: string) => new RegExp(workStartingPaths).test(decodeURIComponent(uri).toLowerCase());
  for (const uri of ["/api/v1/runs", "/api/v1/run%73", "/API/V1/Variants", "/api/v1/scenario-drafts/",
    "/api/v1/agent/turns", "/api/v1/investigation/turns", "/api/v1/radio/stream", "/api/v1/research/jobs",
    "/api/v1/research/discover", "/api/v1/research/sources", "/api/v1/firetruck/runs",
    "/api/v1/firetruck/runs/0c6f2a/reports"]) {
    assert.ok(counted(uri), uri);
  }
  for (const uri of ["/api/v1/runs/0c6f2a", "/api/v1/runs/0c6f2a/cancel", "/api/v1/sessions", "/api/v1/incidents",
    "/api/v1/demo/session", "/api/v1/evidence", "/api/v1/research/jobs/0c6f2a/cancel",
    "/api/internal/v1/simulations/submit_simulation", "/api/v1/runsx"]) {
    assert.ok(!counted(uri), uri);
  }
  // The integrated workspace's /simulation-api/* function rewrites to the /api path that WAF matches.
  assert.ok(counted(runViewerFunction(template, "WorkspaceApiPath", "/simulation-api/v1/runs").uri));
  // A blocked caller gets the v1 error envelope that the UI shows, and when to retry.
  const response = rule.Action.Block.CustomResponse;
  assert.equal(response.ResponseCode, 429);
  assert.deepEqual(response.ResponseHeaders, [{ Name: "Retry-After", Value: "300" },
    { Name: "Cache-Control", Value: "no-store" }]);
  const body = acl.CustomResponseBodies[response.CustomResponseBodyKey];
  assert.equal(body.ContentType, "APPLICATION_JSON");
  const { error } = JSON.parse(body.Content);
  assert.deepEqual([error.code, error.retryable], ["RATE_LIMITED", true]);
  assert.match(error.message, /Wait a few minutes/);
});

// Evaluate a template condition for the given parameter values, as CloudFormation does.
const conditionValue = (template: any, name: string, parameters: Record<string, string>): boolean => {
  const evaluate = (expression: any): boolean => {
    if ("Condition" in expression) return evaluate(template.Conditions[expression.Condition]);
    if ("Fn::Not" in expression) return !evaluate(expression["Fn::Not"][0]);
    if ("Fn::Or" in expression) return expression["Fn::Or"].some(evaluate);
    if ("Fn::And" in expression) return expression["Fn::And"].every(evaluate);
    const [left, right] = expression["Fn::Equals"].map((v: any) => v.Ref === undefined ? v : parameters[v.Ref]);
    return left === right;
  };
  return evaluate(template.Conditions[name]);
};
// Resolve Fn::If with the given conditions and drop AWS::NoValue list items, as CloudFormation does.
const resolveIf = (value: any, conditions: (name: string) => boolean): any => {
  if (Array.isArray(value)) return value.map(v => resolveIf(v, conditions)).filter(v => v !== undefined);
  if (value === null || typeof value !== "object") return value;
  if ("Fn::If" in value) {
    const [name, whenTrue, whenFalse] = value["Fn::If"];
    return resolveIf(conditions(name) ? whenTrue : whenFalse, conditions);
  }
  if (value.Ref === "AWS::NoValue") return undefined;
  return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveIf(v, conditions)]));
};

test("a staging installation admits only its listed networks, and a public one has no allowlist", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const json = template.toJSON();
  const sets = template.findResources("AWS::WAFv2::IPSet");
  assert.deepEqual(Object.keys(sets).sort(), ["StagingAllowlistIpv4", "StagingAllowlistIpv6"]);
  for (const version of ["Ipv4", "Ipv6"]) {
    const parameter = json.Parameters[`StagingAllowed${version}Cidrs`];
    assert.equal(parameter.Type, "String");
    assert.equal(parameter.Default, undefined, "Every deploy passes the installation's ranges.");
    const set = sets[`StagingAllowlist${version}`];
    assert.equal(set.Condition, `HasStaging${version}`);
    assert.equal(set.Properties.IPAddressVersion, version.toUpperCase());
    assert.deepEqual(set.Properties.Addresses, { "Fn::Split": [",", { Ref: `StagingAllowed${version}Cidrs` }] });
  }
  const acl = Object.values(template.findResources("AWS::WAFv2::WebACL"))[0].Properties;
  const reference = (version: string) => ({ IPSetReferenceStatement: {
    Arn: { "Fn::GetAtt": [`StagingAllowlist${version}`, "Arn"] },
    IPSetForwardedIPConfig: { HeaderName: "X-Lab-Viewer-IP", FallbackBehavior: "NO_MATCH", Position: "FIRST" } } });
  for (const [ipv4, ipv6, allowed] of [
    ["", "", null],
    ["198.51.100.0/24", "", reference("Ipv4")],
    ["", "2001:db8::/32", reference("Ipv6")],
    ["198.51.100.0/24,203.0.113.7/32", "2001:db8::/32",
      { OrStatement: { Statements: [reference("Ipv4"), reference("Ipv6")] } }],
  ] as const) {
    const parameters = { StagingAllowedIpv4Cidrs: ipv4, StagingAllowedIpv6Cidrs: ipv6 };
    const holds = (name: string) => conditionValue(json, name, parameters);
    assert.equal(holds("HasStagingIpv4"), ipv4 !== "");
    assert.equal(holds("HasStagingIpv6"), ipv6 !== "");
    const rules = resolveIf(acl.Rules, holds) as any[];
    const staging = rules.find(r => r.Name === "StagingAllowlist");
    if (allowed === null) {
      assert.equal(staging, undefined, "A public installation has no allowlist rule.");
      assert.equal(rules.length, 4);
      continue;
    }
    // Evaluated before the rate rules: everything outside the listed networks is blocked with a clear reason.
    assert.equal(staging.Priority, 2);
    assert.deepEqual(staging.Statement, { NotStatement: { Statement: allowed } });
    const response = staging.Action.Block.CustomResponse;
    assert.equal(response.ResponseCode, 403);
    assert.equal(JSON.parse(acl.CustomResponseBodies[response.CustomResponseBodyKey].Content).error.code,
      "NETWORK_NOT_ALLOWED");
  }
  // The allowlist trusts X-Lab-Viewer-IP only because the CloudFront function overwrites a forged one.
  const forged = { "x-lab-viewer-ip": { value: "198.51.100.7" } };
  assert.equal(runViewerFunction(template, "WorkspaceApiPath", "/simulation-api/v1/runs", forged)
    .headers["x-lab-viewer-ip"].value, "203.0.113.1");
});

test("the edge keeps sampled requests for every WAF rule, and CloudTrail records every Region and global service", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const acl = Object.values(template.findResources("AWS::WAFv2::WebACL"))[0].Properties as any;
  assert.equal(acl.VisibilityConfig.SampledRequestsEnabled, true, "requests the default action allows");
  // With every condition true, the staging allowlist (raw CloudFormation inside Fn::If) is present too.
  const rules = resolveIf(acl.Rules, () => true) as any[];
  assert.deepEqual(rules.map(rule => [rule.Name, rule.VisibilityConfig.SampledRequestsEnabled]), [
    ["AWSManagedRulesAmazonIpReputationList", true], ["AWSManagedRulesKnownBadInputsRuleSet", true],
    ["BoundRequestRate", true], ["BoundWorkRate", true], ["StagingAllowlist", true]]);
  Template.fromStack(foundation).hasResourceProperties("AWS::CloudTrail::Trail", {
    IsMultiRegionTrail: true, IncludeGlobalServiceEvents: true, EnableLogFileValidation: true, IsLogging: true,
  });
});

test("termination protection is on for every stack and lands in the cloud assembly", () => {
  // The CDK CLI sets each stack's protection to its artifact's value on every deploy, so code is the only lasting
  // place (L4, D53); a caller may still turn it off.
  for (const override of [undefined, false]) {
    const app = new App();
    const props = override === undefined ? {} : { terminationProtection: override };
    const foundation = new Foundation(app, "Foundation", props);
    const stacks = [foundation, new Application(app, "Application", { ...props, foundation, imageTag: "checked" }),
      new PublicGuidance(app, "Guidance", props)];
    const expected = override ?? true;
    for (const stack of stacks) assert.equal(stack.terminationProtection, expected, stack.node.id);
    const assembly = app.synth();
    for (const stack of stacks) {
      assert.equal(assembly.getStackArtifact(stack.artifactId).terminationProtection, expected, stack.node.id);
    }
  }
});

// The three CDK stacks of one installation, as the deployer synthesizes them.
const installationStacks = (prefix?: string) => {
  const app = new App();
  const env = { account: "111111111111", region: "us-west-2" };
  const foundation = new Foundation(app, "Foundation", { env, prefix });
  return [foundation, new Application(app, "Application", { env, foundation, prefix, imageTag: "checked" }),
    new PublicGuidance(app, "Guidance", { env })];
};

test("each bucket policy denies object access to every principal outside the installation", () => {
  // Each bucket's exception list (L1, D52): its own roles by logical ID, and the [aws] operator_role_arns list.
  for (const prefix of [undefined, "training-alpha-0123456789"]) {
    const expected: Record<string, string[]> = {
      Audit: ["operators"], WorkspaceAssets: ["operators"],
      BuildSource: ["ImageBuilderRole",
        ...(prefix ? [`arn:aws:iam::111111111111:role/${prefix}-backend-runtime-build`] : []), "operators"],
      Artifacts: ["ApiTaskTaskRole", "ArtifactCollectorServiceRole", "FiretruckPrivateTaskTaskRole",
        "WorkerTaskTaskRole", "operators"],
      RadioAudio: ["RadioPrivateTaskTaskRole", "operators"], Documents: ["KnowledgeBaseRole", "operators"],
    };
    const checked = [];
    for (const stack of installationStacks(prefix)) {
      const template = Template.fromStack(stack);
      const policies = Object.values(template.findResources("AWS::S3::BucketPolicy")) as any[];
      for (const id of Object.keys(template.findResources("AWS::S3::Bucket"))) {
        const denies = policies.filter(p => p.Properties.Bucket.Ref === id)
          .flatMap(p => p.Properties.PolicyDocument.Statement as any[])
          .filter(s => s.Sid === "DenyObjectAccessOutsideInstallation");
        assert.equal(denies.length, 1, id);
        const [deny] = denies;
        assert.equal(deny.Effect, "Deny", id);
        assert.equal(deny.Principal, "*", id);
        assert.deepEqual(deny.Action, OBJECT_ACTIONS, id);
        assert.deepEqual(deny.Resource, { "Fn::Join": ["", [{ "Fn::GetAtt": [id, "Arn"] }, "/*"]] }, id);
        // BoolIfExists, not Bool: an anonymous request has no aws:PrincipalIsAWSService, and the Deny applies to it.
        assert.deepEqual(Object.keys(deny.Condition).sort(), ["ArnNotLike", "BoolIfExists"], id);
        assert.deepEqual(deny.Condition.BoolIfExists, { "aws:PrincipalIsAWSService": "false" }, id);
        const exceptions = resolveIntrinsics(deny.Condition.ArnNotLike["aws:PrincipalArn"],
          { OperatorRoleArns: ["operators"], "AWS::Partition": "aws" }) as string[];
        const name = id.replace(/[0-9A-F]{8}$/, "");
        assert.deepEqual([...exceptions].sort(), expected[name], `${id}${prefix ? ` for ${prefix}` : ""}`);
        checked.push(name);
      }
    }
    assert.deepEqual(checked.sort(), Object.keys(expected).sort(), "every installation bucket is checked");
  }
});

test("no CDK construct writes these buckets with a role outside their lists", () => {
  // A BucketDeployment, auto-delete provider or S3-code Lambda acts with a role the bucket policies deny (D52), so
  // whoever adds one adds its role to the bucket's exception list first.
  for (const stack of installationStacks("training-alpha-0123456789")) {
    const template = Template.fromStack(stack);
    const resources = Object.values(template.toJSON().Resources) as any[];
    assert.deepEqual(resources.filter(r => ["Custom::CDKBucketDeployment", "Custom::S3AutoDeleteObjects"]
      .includes(r.Type)).map(r => r.Type), [], stack.node.id);
    const buckets = Object.keys(template.findResources("AWS::S3::Bucket"));
    const code = resources.filter(r => r.Type === "AWS::Lambda::Function")
      .map(r => JSON.stringify(r.Properties.Code.S3Bucket ?? null));
    assert.deepEqual(code.filter(bucket => buckets.some(id => bucket.includes(id))), [], stack.node.id);
  }
});

test("every function logs to a stack-owned 30-day group, except the provider the construct cannot configure", () => {
  // cdk.json's context, as a deploy synthesizes: restrictDefaultSecurityGroup adds the VPC's provider function, whose
  // construct takes no log group; the deployer gives its default group the same 30 days (L3, D53).
  const context = JSON.parse(readFileSync(join(__dirname, "../cdk.json"), "utf8")).context;
  const app = new App({ context });
  const env = { account: "111111111111", region: "us-west-2" };
  const foundation = new Foundation(app, "Foundation", { env, prefix: "training-alpha-0123456789" });
  const unconfigurable = ["CustomVpcRestrictDefaultSGCustomResourceProviderHandler"];
  const owned = [];
  for (const stack of [foundation, new Application(app, "Application", { env, foundation, imageTag: "checked" }),
    new PublicGuidance(app, "Guidance", { env })]) {
    const template = Template.fromStack(stack);
    const groups = template.findResources("AWS::Logs::LogGroup");
    for (const [id, group] of Object.entries(groups)) assert.equal(group.Properties.RetentionInDays, 30, id);
    for (const [id, resource] of Object.entries(template.findResources("AWS::Lambda::Function"))) {
      if (unconfigurable.some(prefix => id.startsWith(prefix))) continue;
      const group = resource.Properties.LoggingConfig?.LogGroup?.Ref;
      assert.ok(group && groups[group], `${id} logs to a group of its own stack`);
      assert.equal(groups[group].DeletionPolicy, "Retain", id);
      owned.push(id);
    }
  }
  // The dispatcher, the collector and the AwsCustomResource provider behind CloudFrontPrefixList.
  const functions = ["AWS679f53fac002430cb0da5b7982bd2287", "ArtifactCollector", "RunOutboxDispatcher"];
  assert.deepEqual(owned.map(id => functions.find(prefix => id.startsWith(prefix))).sort(), functions);
});

test("the trail records object writes to the site and build-source buckets only", () => {
  // L6, D53: who changed a page or a source bundle; write-only, so reads add no events and no cost.
  const template = Template.fromStack(new Foundation(new App(), "Foundation", {}));
  const json = template.toJSON();
  const [trail] = Object.values(template.findResources("AWS::CloudTrail::Trail")) as any[];
  const source = Object.keys(template.findResources("AWS::S3::Bucket")).find(id => id.startsWith("BuildSource"));
  assert.deepEqual(trail.Properties.EventSelectors, [
    { IncludeManagementEvents: true, ReadWriteType: "All" },
    { DataResources: [{ Type: "AWS::S3::Object", Values: [
      { "Fn::Join": ["", [{ "Fn::GetAtt": [source, "Arn"] }, "/"]] },
      { "Fn::If": ["HasWorkspaceAssetsBucket", { "Fn::Join": ["", ["arn:", { Ref: "AWS::Partition" }, ":s3:::",
        { Ref: "WorkspaceAssetsBucketName" }, "/"]] }, { Ref: "AWS::NoValue" }] }] }],
    IncludeManagementEvents: false, ReadWriteType: "WriteOnly" }]);
  // The deployer passes the application stack's WorkspaceAssetsBucket output on every foundation deploy, "" before
  // that stack exists.
  const parameter = json.Parameters.WorkspaceAssetsBucketName;
  assert.equal(parameter.Type, "String");
  assert.equal(parameter.Default, undefined);
  const name = new RegExp(`^(?:${parameter.AllowedPattern})$`);
  for (const value of ["", "fireguard-demo-64168c-workspaceassetsd44cb750-0123abcd"]) assert.ok(name.test(value));
  for (const value of ["Upper-Case", "ab", "bucket/key", "-site"]) assert.ok(!name.test(value), value);
  assert.equal(conditionValue(json, "HasWorkspaceAssetsBucket", { WorkspaceAssetsBucketName: "" }), false);
  assert.equal(conditionValue(json, "HasWorkspaceAssetsBucket", { WorkspaceAssetsBucketName: "site-bucket" }), true);
  template.hasResourceProperties("AWS::CloudTrail::Trail", {
    IsMultiRegionTrail: true, IncludeGlobalServiceEvents: true, EnableLogFileValidation: true, IsLogging: true,
  });
});

test("every deployable repository keeps its newest 10 images", () => {
  // L5, D53: ECR expires older images; the deployer refuses a release whose push would expire one in use.
  const app = new App();
  const repositories = Object.entries(Template.fromStack(new Foundation(app, "Foundation", {}))
    .findResources("AWS::ECR::Repository")).filter(([id]) =>
    !id.startsWith("NativeBuildArtifacts")) as [string, any][];
  assert.equal(repositories.length, 5);
  assert.equal(IMAGES_KEPT, 10);
  for (const [id, repository] of repositories) {
    const { rules } = JSON.parse(repository.Properties.LifecyclePolicy.LifecyclePolicyText);
    assert.equal(rules.length, 1, id);
    assert.deepEqual(rules[0].selection, { tagStatus: "any", countType: "imageCountMoreThan", countNumber: 10 }, id);
    assert.deepEqual(rules[0].action, { type: "expire" }, id);
  }
});

test("no CDK Lambda environment resolves a secret", () => {
  // D53: no function environment holds a credential; a reader takes a secret's ARN and reads it at run time.
  for (const stack of installationStacks()) {
    const functions = Object.entries(Template.fromStack(stack).findResources("AWS::Lambda::Function"));
    for (const [id, resource] of functions as [string, any][]) {
      assert.ok(!JSON.stringify(resource.Properties.Environment ?? {}).includes("{{resolve:secretsmanager"), id);
    }
  }
});

test("every stack takes the operator roles as a list parameter", () => {
  for (const stack of installationStacks()) {
    const parameter = Template.fromStack(stack).toJSON().Parameters.OperatorRoleArns;
    assert.ok(parameter, stack.node.id);
    assert.equal(parameter.Type, "CommaDelimitedList", stack.node.id);
    assert.equal(parameter.Default, undefined, "Every deploy passes [aws] operator_role_arns.");
    assert.equal(parameter.AllowedPattern, OPERATOR_ROLE_ARN, stack.node.id);
  }
});

test("there is no user sign-in, and the legacy researcher pool and client stay retained and unwired", () => {
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const json = template.toJSON();
  const tasks = Object.values(template.findResources("AWS::ECS::TaskDefinition")) as any[];
  const containers = tasks.flatMap(t => t.Properties.ContainerDefinitions as any[]);
  for (const container of containers) {
    const variables = (container.Environment as any[] || []).map(v => v.Name);
    assert.ok(!variables.some(name => name.startsWith("COGNITO_")), `${container.Name} must not receive sign-in settings`);
  }
  for (const name of ["Api", "Worker"]) {
    const environment = containers.find(c => c.Name === name).Environment as any[];
    assert.deepEqual(environment.find(v => v.Name === "PUBLIC_DEMO_MODE").Value, { Ref: "PublicDemoMode" }, name);
  }
  assert.deepEqual(json.Parameters.PublicDemoMode.AllowedValues, ["true", "false"]);
  assert.equal(json.Parameters.PublicDemoMode.Default, "false", "A new install stays closed until the bind pass.");
  // Unused but kept, with unchanged logical IDs, so an in-place update never deletes them.
  const pool = Object.entries(template.findResources("AWS::Cognito::UserPool"));
  assert.equal(pool.length, 1);
  assert.ok(pool[0][0].startsWith("Researchers"));
  assert.equal(pool[0][1].DeletionPolicy, "Retain");
  template.resourceCountIs("AWS::Cognito::UserPoolClient", 1);
  template.resourceCountIs("AWS::Cognito::UserPoolDomain", 1);
});
