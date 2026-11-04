import { CfnParameter, Fn, Stack, aws_iam as iam, aws_s3 as s3 } from "aws-cdk-lib";

/** Object actions the bucket policies limit to the installation (D52): reads, writes, deletes, restores, uploads. */
export const OBJECT_ACTIONS = ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*", "s3:RestoreObject",
  "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:Replicate*"];
/** [aws] operator_role_arns: deployment/installation.py and the backend templates use the same pattern text.
 * Whole path segments, then a 1-64 character role name, so no value that passes it can make iam:GetRole fail. */
export const OPERATOR_ROLE_ARN = String.raw`arn:aws:iam::[0-9]{12}:role/(?:[A-Za-z0-9+=.@_-]+/)*[A-Za-z0-9+=.@_-]{1,64}`;

/** The installation file's operator roles as a stack parameter; the deployer passes them on every deploy. */
export const operatorRoles = (stack: Stack): CfnParameter => new CfnParameter(stack, "OperatorRoleArns", {
  type: "CommaDelimitedList", allowedPattern: OPERATOR_ROLE_ARN,
  description: "[aws] operator_role_arns in the installation file: IAM roles (deployer, break-glass) that may read and write this installation's bucket objects.",
});

/** Deny object access to every principal except the bucket's own roles, the operators and AWS services (D52). */
export const denyObjectAccessOutsideInstallation = (bucket: s3.Bucket, roleArns: string[],
  operators: CfnParameter): void => {
  // One exception list: role ARNs and validated operator ARNs hold no comma, so joining and splitting is exact.
  const exceptions = roleArns.length
    ? Fn.split(",", Fn.join(",", [...roleArns, Fn.join(",", operators.valueAsList)]))
    : operators.valueAsList;
  bucket.addToResourcePolicy(new iam.PolicyStatement({
    // StarPrincipal renders "Principal": "*", the form of the backend templates' statements.
    sid: "DenyObjectAccessOutsideInstallation", effect: iam.Effect.DENY, principals: [new iam.StarPrincipal()],
    actions: OBJECT_ACTIONS, resources: [bucket.arnForObjects("*")],
    // BoolIfExists: an anonymous request carries no aws:PrincipalIsAWSService, and the Deny must apply to it too.
    conditions: { ArnNotLike: { "aws:PrincipalArn": exceptions },
      BoolIfExists: { "aws:PrincipalIsAWSService": "false" } },
  }));
};
