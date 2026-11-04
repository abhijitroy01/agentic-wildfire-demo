"""Runtime checks: the Gateway client secret reaches the Runtime only through Secrets Manager."""
import base64
import importlib.util
import io
import json
import logging
import os
import types
import unittest
import urllib.parse
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from checks_support import access_denied, load

# The pre-signed URL form CloudFormation sends a custom resource; the signature part must never be logged.
RESPONSE_URL = ("https://cloudformation-custom-resource-response-uswest2.s3-us-west-2.amazonaws.com"
                "/stack?signed-canary")
# Stand-in secret ARN and the canary value the tests watch for in replies and logs. They are joined at run
# time so no source line assigns a secret-shaped literal for credential scanners to flag; neither is real.
SECRET_ARN = "-".join(("secret", "arn"))  # noqa: FLY002 -- assembled fixture avoids secret-shaped scanner literals
SECRET_CANARY = "-".join(("SECRET", "CANARY"))  # noqa: FLY002 -- assembled fixture avoids secret-shaped scanner literals


class GatewayClientSecretChecks(unittest.TestCase):
    """The gateway client secret reaches the Runtime only through its Secrets Manager secret (G34)."""

    def copy(self, request="Create", url=RESPONSE_URL, describe=None):
        """Run the copy custom resource with stand-in Cognito, Secrets Manager and HTTP clients."""
        helper = load("checked_cognito_secret",
                      "backend/deployment/lambda/custom-resources/get_cognito_client_secret.py")
        event = {"RequestType": request, "ResponseURL": url, "StackId": "stack", "RequestId": "request",
                 "LogicalResourceId": "GatewayClientSecretCopy",
                 "ResourceProperties": {"UserPoolId": "pool", "ClientId": "client", "SecretArn": SECRET_ARN}}
        cognito, secrets_manager, http = MagicMock(), MagicMock(), MagicMock()
        cognito.describe_user_pool_client.side_effect = describe or [
            {"UserPoolClient": {"ClientSecret": SECRET_CANARY}}]
        http.request.return_value.status = 200
        output = io.StringIO()
        clients = {"cognito-idp": cognito, "secretsmanager": secrets_manager}
        with patch.object(helper.boto3, "client", side_effect=clients.__getitem__), \
             patch.object(helper.urllib3, "PoolManager", return_value=http), \
             patch.object(helper.time, "sleep") as sleep, redirect_stdout(output):
            helper.handler(event, types.SimpleNamespace(log_stream_name="logs"))
        body = http.request.call_args.kwargs["body"] if http.request.called else ""
        self.assertNotIn(SECRET_CANARY, body + output.getvalue())
        self.assertNotIn("signed-canary", output.getvalue())
        return types.SimpleNamespace(cognito=cognito, secrets=secrets_manager, http=http, sleep=sleep,
                                     response=json.loads(body) if body else None)

    def test_the_secret_is_stored_in_secrets_manager_and_never_returned_to_the_stack(self):
        """Check the custom resource puts the Cognito secret in Secrets Manager and returns no data."""
        run = self.copy()
        run.cognito.describe_user_pool_client.assert_called_once_with(UserPoolId="pool", ClientId="client")
        run.secrets.put_secret_value.assert_called_once_with(SecretId=SECRET_ARN, SecretString=SECRET_CANARY)
        self.assertEqual((run.response["Status"], run.response["Data"]), ("SUCCESS", {}))
        self.assertEqual(run.http.request.call_args.args[:2], ("PUT", RESPONSE_URL))
        deleted = self.copy("Delete")
        deleted.cognito.describe_user_pool_client.assert_not_called()
        deleted.secrets.put_secret_value.assert_not_called()
        self.assertEqual(deleted.response["Status"], "SUCCESS")

    def test_access_errors_are_retried_while_a_new_grant_propagates_and_other_errors_fail(self):
        """Check AccessDenied is retried while IAM propagates and other errors fail the resource."""
        run = self.copy(describe=[access_denied("DescribeUserPoolClient"),
                                  {"UserPoolClient": {"ClientSecret": SECRET_CANARY}}])
        self.assertEqual(run.sleep.call_count, 1)
        run.secrets.put_secret_value.assert_called_once()
        self.assertEqual(run.response["Status"], "SUCCESS")
        for describe in ([RuntimeError(SECRET_CANARY)], [{"UserPoolClient": {}}],
                         [access_denied("DescribeUserPoolClient")] * 6):
            with self.subTest(describe=describe):
                run = self.copy(describe=describe)
                run.secrets.put_secret_value.assert_not_called()
                self.assertEqual(run.response["Status"], "FAILED")

    def test_only_a_cloudformation_response_url_receives_a_response(self):
        """Check custom-resource replies go only to an https CloudFormation S3 callback URL."""
        bucket = "cloudformation-custom-resource-response-uswest2"
        host = f"{bucket}.s3.amazonaws.com"
        for url in ("https://callback.invalid/signed-canary", f"http://{host}/x", f"https://{host}.evil.invalid/x",
                    f"https://user@{host}/x", f"https://{host}:8443/x", "https://s3.amazonaws.com/other-bucket/x",
                    f"https://s3.amazonaws.com.evil.invalid/{bucket}/x", None):
            with self.subTest(url=url):
                run = self.copy(url=url)
                run.http.request.assert_not_called()
                run.cognito.describe_user_pool_client.assert_not_called()
        regional = f"{bucket}.s3.us-west-2.amazonaws.com"
        china = "cloudformation-custom-resource-response-cnnorth1.s3.cn-north-1.amazonaws.com.cn"
        for url in (f"https://{host}/x", f"https://{regional}/x", f"https://{china}/x",
                    f"https://s3.us-west-2.amazonaws.com/{bucket}/x"):
            with self.subTest(url=url):
                self.assertEqual(self.copy(url=url).response["Status"], "SUCCESS")

    def mcp_client(self):
        """Load the Runtime's Gateway MCP client module with its real Region and log-safety helpers."""
        # Import the SDKs outside patch.dict, which would drop them again; a re-import breaks pydantic generics.
        for sdk in ("mcp.client.streamable_http", "strands.tools.mcp.mcp_client"):
            importlib.import_module(sdk)
        return load("checked_mcp_client", "backend/runtime/utils/mcp_client.py")

    def test_a_refused_gateway_token_request_reports_only_its_status(self):
        """Check the token request's grant and auth, and that a refusal names only its HTTP status."""
        module = self.mcp_client()
        manager = module.AgentCoreTokenManager(module.GatewayConfiguration(
            gateway_url="https://gateway.invalid/mcp", region="us-west-2", client_id="client",
            client_secret=SECRET_CANARY, token_endpoint="https://auth.invalid/oauth2/token"))
        replies = [httpx.Response(400, text=f"invalid_client {SECRET_CANARY}"),
                   httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 3600})]
        sent = []

        def reply(request):
            sent.append(request)
            return replies.pop(0)
        client = httpx.Client
        with patch.object(module.httpx, "Client",
                          lambda **options: client(transport=httpx.MockTransport(reply), **options)):
            with self.assertLogs(module.logger, logging.DEBUG) as logs, \
                    self.assertRaisesRegex(RuntimeError, "HTTP 400") as raised:
                manager.get_valid_token()
            self.assertEqual(manager.get_valid_token(), "fixture-token")
        self.assertNotIn(SECRET_CANARY, str(raised.exception) + "\n".join(logs.output))
        # RTGW-13: the client-credentials grant goes over httpx with HTTP Basic auth, to the configured endpoint.
        self.assertEqual(str(sent[-1].url), "https://auth.invalid/oauth2/token")
        self.assertEqual(sent[-1].headers["Authorization"],
                         "Basic " + base64.b64encode(f"client:{SECRET_CANARY}".encode()).decode())
        self.assertEqual(urllib.parse.parse_qs(sent[-1].content.decode()), {"grant_type": ["client_credentials"]})

    def test_runtime_reads_the_client_secret_by_arn_and_ignores_a_plaintext_variable(self):
        """Check the MCP client reads the Cognito secret from Secrets Manager, never plaintext env."""
        module = self.mcp_client()
        env = {"DEPLOYED_DeploymentRegion": "us-west-2", "DEPLOYED_GatewayUrl": "https://gateway.invalid/mcp",
               "DEPLOYED_CognitoClientId": "client", "DEPLOYED_CognitoTokenEndpoint": "https://auth.invalid/token",
               "DEPLOYED_CognitoClientSecretArn": SECRET_ARN, "DEPLOYED_CognitoClientSecret": "PLAINTEXT-CANARY"}
        secrets_manager = MagicMock()
        secrets_manager.get_secret_value.return_value = {"SecretString": SECRET_CANARY}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(module.boto3, "client", return_value=secrets_manager) as client, \
             self.assertLogs(module.logger, logging.DEBUG) as logs:
            gateway = module.MCPGatewayClient()
        self.assertEqual(gateway.config.client_secret, SECRET_CANARY)
        client.assert_called_once_with("secretsmanager", region_name="us-west-2", config=unittest.mock.ANY)
        timeout = client.call_args.kwargs["config"]
        self.assertEqual((timeout.connect_timeout, timeout.read_timeout), (3, 5))
        secrets_manager.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)
        self.assertNotIn(SECRET_CANARY, "\n".join(logs.output))
        without = {key: value for key, value in env.items() if key != "DEPLOYED_CognitoClientSecretArn"}
        with patch.dict(os.environ, without, clear=True), \
             self.assertRaisesRegex(ValueError, "DEPLOYED_CognitoClientSecretArn"):
            module.MCPGatewayClient()
        secrets_manager.get_secret_value.side_effect = access_denied("GetSecretValue")
        with patch.dict(os.environ, env, clear=True), \
             patch.object(module.boto3, "client", return_value=secrets_manager), \
             self.assertRaisesRegex(RuntimeError, "DEPLOYED_CognitoClientSecretArn"):
            module.MCPGatewayClient()
