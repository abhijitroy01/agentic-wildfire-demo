#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";
import { Foundation, Application } from "../lib/stacks";
import { PublicGuidance } from "../lib/public-guidance";
import { createHash } from "node:crypto";

const app = new cdk.App();
const offline = process.env.LAB_SYNTH_OFFLINE === "1";
if (!offline && !(process.env.CDK_DEFAULT_ACCOUNT && process.env.CDK_DEFAULT_REGION)) {
  // Never fall back to a default account or Region; the deployer sets both explicitly.
  throw new Error("Set CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION, or LAB_SYNTH_OFFLINE=1 for offline synth.");
}
const env = offline ? undefined : {
  account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION,
};
const name = app.node.tryGetContext("deploymentName");
if (name && !/^[a-z][a-z0-9-]{1,18}[a-z0-9]$/.test(name)) throw new Error("Invalid deploymentName");
const identity = name ? createHash("sha256").update(
  `${process.env.CDK_DEFAULT_ACCOUNT}/${process.env.CDK_DEFAULT_REGION}/${name}`,
).digest("hex").slice(0, 10) : undefined;
const prefix = name ? `${name}-${identity}` : undefined;
// Stack construct IDs keep their original "WildfireLab" names so installed stacks update in place.
const foundation = new Foundation(app, "WildfireLabFoundation", {
  env, stackName: prefix ? `${prefix}-foundation` : undefined, prefix,
});
new Application(app, "WildfireLabApplication", {
  env, foundation, stackName: prefix ? `${prefix}-application` : undefined, prefix,
  imageTag: app.node.tryGetContext("imageTag") || "development-not-deployed",
});
if (app.node.tryGetContext("includePublicGuidance") === "true") {
  if (!prefix) throw new Error("Public guidance requires an explicit deploymentName");
  new PublicGuidance(app, "WildfireLabPublicGuidance", {
    env, stackName: `${prefix}-public-guidance`,
  });
}
// cdk-nag (D18): every synth checks each stack against the AWS Solutions rules and writes its NagReport CSV into the
// cloud assembly. A finding fails the synth unless it is fixed or suppressed, with its reason, beside the resource.
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ reports: true }));
// Tag values are unchanged from existing installations to avoid a fleet-wide tag rewrite.
cdk.Tags.of(app).add("Project", "WildfireCorridorLab");
cdk.Tags.of(app).add("Purpose", "ResearchDemo");
if (name) {
  cdk.Tags.of(app).add("ManagedBy", "wildfire-secure-deployer");
  cdk.Tags.of(app).add("DeploymentId", identity!);
  cdk.Tags.of(app).add("DeploymentName", name);
}
