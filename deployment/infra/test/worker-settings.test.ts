import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { Application, Foundation } from "../lib/stacks";

test("every worker operational setting is validated at startup and supplied by ECS", () => {
  // Both bootstrap and the verification image provide Python; AST inspection imports no worker configuration.
  const inventory = JSON.parse(execFileSync("python3",
    [join(__dirname, "worker_settings.py")], { encoding: "utf8" })) as {
      required: string[]; startup: string[];
    };
  assert.ok(inventory.required.length > 0, "The worker settings inventory must not be empty.");
  assert.deepEqual(inventory.required.filter(name => !inventory.startup.includes(name)), [],
    "Every worker operational setting must fail early when missing from AWS configuration.");
  const app = new App();
  const foundation = new Foundation(app, "Foundation", {});
  const template = Template.fromStack(new Application(app, "Application", { foundation, imageTag: "checked" }));
  const containers = Object.values(template.findResources("AWS::ECS::TaskDefinition"))
    .flatMap((task: any) => task.Properties.ContainerDefinitions) as any[];
  const environment = Object.fromEntries(containers.find(container => container.Name === "Worker").Environment
    .map((entry: { Name: string; Value: unknown }) => [entry.Name, entry.Value])) as Record<string, { Ref?: string }>;
  assert.deepEqual(inventory.required.filter(name => !(name in environment)), [],
    "The synthesized Worker task must receive every operational setting its Python code consumes.");
  for (const name of inventory.required) {
    const parameter = environment[name].Ref;
    assert.ok(parameter, `${name} must come from a deployment parameter, without a code default.`);
    assert.ok(template.toJSON().Parameters[parameter], `${name} references a declared deployment parameter.`);
    assert.equal(template.toJSON().Parameters[parameter].Default, undefined,
      `${name}: deployment/settings.toml owns the default.`);
  }
});
