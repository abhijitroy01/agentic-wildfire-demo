import { test } from "node:test";
import assert from "node:assert/strict";
import { App } from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { PublicGuidance } from "../lib/public-guidance";

test("public source collection has private retained storage and narrowly scoped KB access", () => {
  const app = new App();
  const template = Template.fromStack(new PublicGuidance(app, "Guidance", {
    env: { account: "111111111111", region: "us-west-2" },
  }));
  template.hasResourceProperties("AWS::S3::Bucket", {
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true,
      IgnorePublicAcls: true, RestrictPublicBuckets: true },
    VersioningConfiguration: { Status: "Enabled" },
    OwnershipControls: { Rules: [{ ObjectOwnership: "BucketOwnerEnforced" }] },
  });
  template.hasResourceProperties("AWS::S3Vectors::Index", {
    Dimension: 1024, MetadataConfiguration: { NonFilterableMetadataKeys: ["AMAZON_BEDROCK_TEXT"] },
  });
  template.hasResourceProperties("AWS::Bedrock::DataSource", {
    DataDeletionPolicy: "RETAIN",
    VectorIngestionConfiguration: { ChunkingConfiguration: { ChunkingStrategy: "NONE" } },
    DataSourceConfiguration: { Type: "S3", S3Configuration: Match.objectLike({
      InclusionPrefixes: ["documents/"],
    }) },
  });
  // The data source belongs to this stack's own knowledge base, never to an existing (private) one.
  template.resourceCountIs("AWS::Bedrock::KnowledgeBase", 1);
  const [knowledgeBase] = Object.keys(template.findResources("AWS::Bedrock::KnowledgeBase"));
  template.hasResourceProperties("AWS::Bedrock::DataSource", {
    KnowledgeBaseId: { "Fn::GetAtt": [knowledgeBase, "KnowledgeBaseId"] },
  });
  const data = JSON.stringify(template.toJSON());
  assert.ok(!JSON.stringify(template.findResources("AWS::IAM::Policy")).includes("s3:*"));
  assert.ok(!data.includes("bedrock:*"));
  for (const type of ["AWS::S3::Bucket", "AWS::S3Vectors::VectorBucket",
    "AWS::S3Vectors::Index", "AWS::Bedrock::KnowledgeBase", "AWS::Bedrock::DataSource"]) {
    for (const resource of Object.values(template.findResources(type))) {
      assert.equal((resource as { DeletionPolicy: string }).DeletionPolicy, "Retain");
    }
  }
});
