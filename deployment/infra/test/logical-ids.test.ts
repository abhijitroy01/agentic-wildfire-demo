import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { Foundation, Application } from "../lib/stacks";

// The deploy guard's stateful types (deployment/change_sets.py STATEFUL_TYPES), read from its source so the two lists
// cannot drift apart.
const guard = readFileSync(join(__dirname, "../../change_sets.py"), "utf8");
const statefulTypes = new Set(guard.match(/STATEFUL_TYPES = frozenset\(\{([^}]*)\}\)/)![1].match(/AWS::[A-Za-z0-9:]+/g));

// A changed construct ID or scope renames the logical ID, and CloudFormation then replaces the resource: a new empty
// table, bucket, repository, secret, user pool or log group. The deploy guard stops that at deploy time; this test
// stops it locally (W3.3, DEPL-8).
const expected = {
  Foundation: {
    Audit3D8ADE0A: "AWS::S3::Bucket",
    BuildSourceA07CCA8C: "AWS::S3::Bucket",
    Images2D38C313: "AWS::ECR::Repository",
    TrafficSimulatorImagesA40E3300: "AWS::ECR::Repository",
    FiretruckImages1414CEA2: "AWS::ECR::Repository",
    RadioImagesA24E13E4: "AWS::ECR::Repository",
    FireRiskImages221229D4: "AWS::ECR::Repository",
    NativeBuildArtifactsB3B258CC: "AWS::ECR::Repository",
    BuildLogs3759266E: "AWS::Logs::LogGroup",
  },
  Application: {
    Artifacts82DD59A1: "AWS::S3::Bucket",
    JobsDF1CC2D4: "AWS::DynamoDB::Table",
    SimulationContextSecretE04D16D1: "AWS::SecretsManager::Secret",
    RadioBrokerCredentialE9104CA7: "AWS::SecretsManager::Secret",
    ProviderCredentials9BA01DC4: "AWS::SecretsManager::Secret",
    WorkspaceAssetsD44CB750: "AWS::S3::Bucket",
    Researchers0D543E1B: "AWS::Cognito::UserPool",
    ResearchersWebClient6BA30D86: "AWS::Cognito::UserPoolClient",
    ApiLogs3D05D88B: "AWS::Logs::LogGroup",
    FiretruckServiceKeyF0307920: "AWS::SecretsManager::Secret",
    RadioServiceKey6386CEF8: "AWS::SecretsManager::Secret",
    FireRiskServiceKeyFA65B43F: "AWS::SecretsManager::Secret",
    FiretruckEvidenceIndex8E40ADC9: "AWS::DynamoDB::Table",
    RadioAudio48468512: "AWS::S3::Bucket",
    FiretruckPrivateLogs12301451: "AWS::Logs::LogGroup",
    RadioPrivateLogs6FB351E9: "AWS::Logs::LogGroup",
    FireRiskPrivateLogs372CC44E: "AWS::Logs::LogGroup",
    WorkerLogsC1193B08: "AWS::Logs::LogGroup",
    // The functions' own 30-day groups (L3, D53).
    RunOutboxDispatcherLogsA022257F: "AWS::Logs::LogGroup",
    ArtifactCollectorLogs25D14A63: "AWS::Logs::LogGroup",
    CloudFrontPrefixListLogs57BD7D52: "AWS::Logs::LogGroup",
  },
};

test("every stateful resource keeps its logical ID and type", () => {
  assert.ok(statefulTypes.has("AWS::DynamoDB::Table") && statefulTypes.has("AWS::Logs::LogGroup"),
    "STATEFUL_TYPES must be readable from deployment/change_sets.py");
  // Offline synth and a deploy's synth for a named installation must produce the same stateful logical IDs.
  for (const [env, prefix] of [[undefined, undefined],
    [{ account: "111111111111", region: "us-west-2" }, "training-alpha-0123456789"]] as const) {
    const app = new App();
    const foundation = new Foundation(app, "WildfireLabFoundation", { env });
    const application = new Application(app, "WildfireLabApplication", {
      env, foundation, prefix, imageTag: "checked",
    });
    for (const [name, stack] of [["Foundation", foundation], ["Application", application]] as const) {
      const resources = Template.fromStack(stack).toJSON().Resources as Record<string, { Type: string }>;
      const stateful = Object.fromEntries(Object.entries(resources)
        .filter(([, resource]) => statefulTypes.has(resource.Type)).map(([id, resource]) => [id, resource.Type]));
      assert.deepEqual(stateful, expected[name], `${name}${prefix ? ` for ${prefix}` : ""}`);
    }
  }
});
