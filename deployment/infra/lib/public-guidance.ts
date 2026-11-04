import { CfnOutput, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import { Construct } from "constructs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as vectors from "aws-cdk-lib/aws-s3vectors";
import * as iam from "aws-cdk-lib/aws-iam";
import * as bedrock from "aws-cdk-lib/aws-bedrock";
import { nag, s3AccessLogReason } from "./nag";
import { denyObjectAccessOutsideInstallation, operatorRoles } from "./bucket-access";

/** Public describes the reviewed content, never the network/storage permissions. */
export class PublicGuidance extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    // On unless a caller turns it off (D53), as for the other two CDK stacks.
    super(scope, id, { ...props, terminationProtection: props.terminationProtection ?? true });
    const documents = new s3.Bucket(this, "Documents", {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      // ACLs disabled (the S3 default for new buckets), stated so the template shows it.
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      enforceSSL: true, versioned: true, removalPolicy: RemovalPolicy.RETAIN,
    });
    const bucket = new vectors.CfnVectorBucket(this, "Vectors", {
      encryptionConfiguration: { sseType: "AES256" },
    });
    bucket.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const index = new vectors.CfnIndex(this, "GuidanceIndex", {
      vectorBucketArn: bucket.attrVectorBucketArn,
      dataType: "float32", dimension: 1024, distanceMetric: "cosine",
      metadataConfiguration: { nonFilterableMetadataKeys: ["AMAZON_BEDROCK_TEXT"] },
    });
    index.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const role = new iam.Role(this, "KnowledgeBaseRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": this.formatArn({ service: "bedrock", resource: "knowledge-base", resourceName: "*" }) },
        },
      }),
    });
    const embeddingArn = this.formatArn({
      service: "bedrock", account: "", resource: "foundation-model",
      resourceName: "amazon.titan-embed-text-v2:0",
    });
    const policy = new iam.Policy(this, "KnowledgeBaseAccess", { statements: [
      new iam.PolicyStatement({ actions: ["bedrock:InvokeModel"], resources: [embeddingArn] }),
      new iam.PolicyStatement({ actions: ["s3:ListBucket"], resources: [documents.bucketArn],
        conditions: { StringLike: { "s3:prefix": ["documents/", "documents/*"] } } }),
      new iam.PolicyStatement({ actions: ["s3:GetObject"], resources: [documents.arnForObjects("documents/*")] }),
      new iam.PolicyStatement({
        actions: ["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors",
          "s3vectors:QueryVectors", "s3vectors:GetIndex"],
        resources: [index.attrIndexArn],
      }),
    ] });
    policy.attachToRole(role);
    // The knowledge base's ingestion reads the documents; the deployer uploads them (D52).
    denyObjectAccessOutsideInstallation(documents, [role.roleArn], operatorRoles(this));
    nag(documents, "AwsSolutions-S1", s3AccessLogReason);
    nag(policy, "AwsSolutions-IAM5", "The knowledge base reads only the reviewed documents under documents/, whose " +
      "names the review step chooses, so the prefix wildcard is the narrowest resource.",
    [{ regex: "/^Resource::<Documents[0-9A-F]+\\.Arn>\\/documents\\/\\*$/g" }]);
    const kb = new bedrock.CfnKnowledgeBase(this, "KnowledgeBase", {
      name: `${this.stackName}-kb`, roleArn: role.roleArn,
      description: "Reviewed public NWCG and FEMA guidance. Planning reference, not incident orders.",
      knowledgeBaseConfiguration: {
        type: "VECTOR", vectorKnowledgeBaseConfiguration: {
          embeddingModelArn: embeddingArn,
          embeddingModelConfiguration: { bedrockEmbeddingModelConfiguration: {
            dimensions: 1024, embeddingDataType: "FLOAT32",
          } },
        },
      },
      storageConfiguration: { type: "S3_VECTORS", s3VectorsConfiguration: { indexArn: index.attrIndexArn } },
    });
    kb.addResourceDependency(policy.node.defaultChild as iam.CfnPolicy);
    kb.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const source = new bedrock.CfnDataSource(this, "Source", {
      knowledgeBaseId: kb.attrKnowledgeBaseId, name: "reviewed-public-guidance",
      dataDeletionPolicy: "RETAIN",
      dataSourceConfiguration: { type: "S3", s3Configuration: {
        bucketArn: documents.bucketArn, inclusionPrefixes: ["documents/"],
      } },
      // Documents are reviewed, complete text sections. Do not split safety checklists.
      vectorIngestionConfiguration: { chunkingConfiguration: { chunkingStrategy: "NONE" } },
    });
    source.applyRemovalPolicy(RemovalPolicy.RETAIN);
    new CfnOutput(this, "DocumentsBucket", { value: documents.bucketName });
    new CfnOutput(this, "PublicKnowledgeBaseId", { value: kb.attrKnowledgeBaseId });
    new CfnOutput(this, "PublicDataSourceId", { value: source.attrDataSourceId });
    new CfnOutput(this, "PublicVectorIndexArn", { value: index.attrIndexArn });
  }
}
