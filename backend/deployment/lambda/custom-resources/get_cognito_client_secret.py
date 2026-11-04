"""CloudFormation custom resource: copy the gateway Cognito app client secret into its Secrets Manager secret.

3-cognito-auth.yaml GatewayClientSecretCopy calls this on Create and Update with UserPoolId, ClientId and
SecretArn. The value goes only to Secrets Manager: the CloudFormation response carries no secret, nothing is
logged, and a response goes only to a CloudFormation pre-signed S3 URL. GatewayClientSecretReadPolicy limits
the function to describing clients of this installation's gateway user pool and writing that one secret.
Delete changes nothing; CloudFormation deletes the secret resource itself.
"""
import json
import re
import time
import urllib.parse

import boto3
import urllib3
from botocore.exceptions import ClientError

# CloudFormation's per-Region custom-resource response bucket on any S3 endpoint, in virtual-hosted
# (bucket.s3...amazonaws.com) or path-style (s3...amazonaws.com/bucket/...) form.
S3_ENDPOINT = r"s3[a-z0-9.-]*\.amazonaws\.com(\.cn)?"
RESPONSE_BUCKET = r"cloudformation-custom-resource-response-[a-z0-9]+"
VIRTUAL_HOSTED = re.compile(rf"{RESPONSE_BUCKET}\.{S3_ENDPOINT}")
PATH_STYLE_HOST = re.compile(S3_ENDPOINT)
PATH_STYLE_PATH = re.compile(rf"/{RESPONSE_BUCKET}/")
# A grant added earlier in the same deployment can take seconds to apply, so access errors are retried:
# up to 6 attempts, 10 seconds apart, well inside the resource's 180-second ServiceTimeout.
ACCESS_ATTEMPTS = 6
ACCESS_RETRY_SECONDS = 10
ACCESS_ERRORS = {"AccessDenied", "AccessDeniedException"}


def is_cloudformation_response_url(url):
    """Return whether url is an https pre-signed URL of CloudFormation's custom-resource response bucket."""
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return False
    return bool(VIRTUAL_HOSTED.fullmatch(parts.netloc)
                or (PATH_STYLE_HOST.fullmatch(parts.netloc) and PATH_STYLE_PATH.match(parts.path)))


def send_response(event, context, status, physical_resource_id):
    """PUT the custom-resource result to CloudFormation's pre-signed URL; the body never carries the secret."""
    body = json.dumps({
        "Status": status,
        "Reason": f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_resource_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": {},
    })
    # Never log the signed callback URL.
    print(f"CloudFormation response status: {status}")
    try:
        # Like AWS's cfn-response module: only content-length, no content-type, as the signed URL expects.
        response = urllib3.PoolManager().request(
            "PUT", event["ResponseURL"], headers={"content-length": str(len(body))}, body=body)
        print(f"Status code: {response.status}")
    except Exception as exc:  # noqa: BLE001 - CloudFormation times the request out; the Lambda must not fail
        print(f"Failed to send response: {type(exc).__name__}")


def call_with_access_retries(call, **arguments):
    """Call an AWS API, retrying an access error while an IAM grant from the same deployment propagates."""
    for attempt in range(1, ACCESS_ATTEMPTS + 1):
        try:
            return call(**arguments)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ACCESS_ERRORS or attempt == ACCESS_ATTEMPTS:
                raise
            time.sleep(ACCESS_RETRY_SECONDS)


def copy_client_secret(properties):
    """Read the app client's secret from Cognito and store it as the current value of the named secret."""
    described = call_with_access_retries(boto3.client("cognito-idp").describe_user_pool_client,
                                         UserPoolId=properties["UserPoolId"], ClientId=properties["ClientId"])
    value = described["UserPoolClient"].get("ClientSecret")
    if not value:
        raise ValueError("The app client has no client secret")
    call_with_access_retries(boto3.client("secretsmanager").put_secret_value,
                             SecretId=properties["SecretArn"], SecretString=value)


def handler(event, context):
    """Copy the gateway client secret on Create and Update; Delete and other requests change nothing."""
    request = event.get("RequestType", "Unknown")
    print(f"Request type: {request}")
    if not is_cloudformation_response_url(event.get("ResponseURL")):
        print("Rejected: the response URL is not a CloudFormation custom-resource response URL")
        return
    physical = event.get("PhysicalResourceId") or context.log_stream_name
    try:
        if request in ("Create", "Update"):
            copy_client_secret(event["ResourceProperties"])
            physical = event["ResourceProperties"]["ClientId"]
        send_response(event, context, "SUCCESS", physical)
    except Exception as exc:  # noqa: BLE001 - every failure must reach CloudFormation as FAILED, not a timeout
        print(f"Error type: {type(exc).__name__}")
        send_response(event, context, "FAILED", physical)
