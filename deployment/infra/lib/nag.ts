import { NagSuppressions } from "cdk-nag";
import { IConstruct } from "constructs";

/**
 * Suppress one cdk-nag finding on a construct and its children (D18), with its reason beside the resource (D13).
 * IAM5 suppressions name each wildcard they accept; a regex covers an ARN whose Region and account an
 * environment-specific synth writes literally.
 */
export const nag = (construct: IConstruct, id: string, reason: string, appliesTo?: (string | { regex: string })[]) =>
  NagSuppressions.addResourceSuppressions(construct, [{ id, reason, appliesTo }], true);

/** CDK's bucket grant action groups (grantRead, plus grantReadWrite's delete and abort). */
export const s3GrantActions = ["Action::s3:GetObject*", "Action::s3:GetBucket*", "Action::s3:List*"];
export const s3WriteActions = ["Action::s3:DeleteObject*", "Action::s3:Abort*"];
export const s3GrantReason = "CDK's bucket grant action groups, on this role's own key prefixes only; the " +
  "bucket-level List and GetBucket actions let a missing key answer 404, which the readers expect. Object keys " +
  "are per run, report, snapshot, cache entry or source digest, so a prefix wildcard is the narrowest resource.";

/** Regex for an ARN's Region and account, as tokens (agnostic synth) or literal values (a deploy's synth). */
export const regionAndAccount = "(<AWS::Region>|[a-z0-9-]+):(<AWS::AccountId>|[0-9]{12})";

/** The reason for an action that IAM can grant only on Resource "*". */
export const noResourceLevel = (action: string, scope: string) =>
  `${action} has no resource-level permissions, so its resource is "*"; ${scope}`;

/** G44 (D38): S3 server access logging is an accepted risk. */
export const s3AccessLogReason = "Accepted (D38): server access logs would need a new log bucket in each stack. " +
  "This bucket blocks all public access, accepts only TLS, has ACLs disabled and is read only by named roles, and " +
  "CloudTrail records every management call.";
