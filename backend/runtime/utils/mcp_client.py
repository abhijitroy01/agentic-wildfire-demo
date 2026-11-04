"""
Bedrock AgentCore Gateway MCP client.

Gets OAuth2 client-credentials tokens for the Gateway and opens its streamable HTTP MCP transport, both with
httpx. The Gateway client secret is read from Secrets Manager by ARN.
"""

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp.mcp_client import MCPClient

from utils.aws_clients import deployment_region
from utils.log_safety import error_summary

# Note: Environment variables are loaded by utils.env_loader (imported first in utils/__init__.py)

# Configure logging
logger = logging.getLogger(__name__)


def read_gateway_client_secret(region: str) -> str:
    """Read the Gateway Cognito client secret from Secrets Manager (ARN in DEPLOYED_CognitoClientSecretArn).

    The backend stack passes only the secret's ARN (3-cognito-auth.yaml GatewayClientSecret); the value
    is never an environment variable, a stack output or a log line.
    """
    secret_arn = os.getenv("DEPLOYED_CognitoClientSecretArn", "").strip()
    if not secret_arn:
        raise ValueError("DEPLOYED_CognitoClientSecretArn environment variable is required")
    try:
        response = boto3.client("secretsmanager", region_name=region,
                                  config=Config(connect_timeout=3, read_timeout=5,
                                                retries={"mode": "standard", "max_attempts": 2})).get_secret_value(SecretId=secret_arn)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            "Could not read the Gateway client secret named by DEPLOYED_CognitoClientSecretArn") from exc
    return response.get("SecretString", "")


@dataclass
class GatewayConfiguration:
    """Simplified gateway configuration"""
    gateway_url: str
    region: str
    client_id: str
    client_secret: str
    token_endpoint: str
    scope: str | None = None


class AgentCoreTokenManager:
    """Cache the Gateway access token and refresh it with the OAuth2 client-credentials grant."""

    def __init__(self, config: GatewayConfiguration):
        self.config = config
        self._cached_token: str | None = None
        self._token_expiry: datetime | None = None
        self._lock = threading.Lock()

        logger.info("Gateway token manager initialized for region: %s", config.region)

    def _is_token_expired(self) -> bool:
        """Check if current token is expired (with 5 minute buffer)"""
        if not self._token_expiry:
            return True
        return datetime.now(UTC) >= (self._token_expiry - timedelta(minutes=5))

    def _refresh_token(self) -> str:
        """Request a new Gateway access token: OAuth2 client credentials with HTTP Basic auth, over httpx."""
        logger.info("Requesting new access token (OAuth2 client credentials)")

        try:
            # A form-encoded client-credentials grant. Redirects are not followed, so the credentials are
            # sent to the configured token endpoint only. 5 s to connect, 30 s for the reply.
            with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as http:
                response = http.post(
                    self.config.token_endpoint,
                    data={'grant_type': 'client_credentials', 'scope': self.config.scope or ''},
                    auth=(self.config.client_id, self.config.client_secret))

            if response.status_code != 200:
                # Only the status: the token endpoint's reply body can echo the request or credentials.
                raise RuntimeError(f"Token request failed: HTTP {response.status_code}")
            token_info = response.json()
            new_token = token_info['access_token']
            expires_in = token_info.get(
                'expires_in', 3600)  # Default 1 hour

            # Token expiry
            self._token_expiry = datetime.now(
                UTC) + timedelta(seconds=expires_in)

            logger.info(
                f"✅ OAuth2 token obtained (expires in {expires_in}s)")
            return new_token

        except RuntimeError as e:
            logger.warning("Failed to refresh access token: %s", e)
            raise
        except Exception as e:
            logger.warning("Failed to refresh access token: %s", error_summary(e))
            raise RuntimeError(f"Token refresh failed: {error_summary(e)}") from e

    def get_valid_token(self) -> str:
        """Get a valid access token, refreshing if necessary"""
        with self._lock:
            # Check if we need to refresh the token
            if not self._cached_token or self._is_token_expired():
                logger.debug("Token expired or missing, refreshing...")
                self._cached_token = self._refresh_token()
            else:
                logger.debug(
                    f"Using cached token (expires in {(self._token_expiry - datetime.now(UTC)).total_seconds():.0f}s)")

            return self._cached_token


class MCPGatewayClient:
    """Modern MCP client with automatic token management"""

    def __init__(self, config: GatewayConfiguration | None = None):
        self.config = config or self._load_configuration()
        self._validate_configuration()

        # Initialize token manager
        self.token_manager = AgentCoreTokenManager(self.config)

        logger.info("MCP Gateway client initialized")
        logger.info(f"Gateway URL: {self.config.gateway_url}")
        logger.info(f"Region: {self.config.region}")

    def _load_configuration(self) -> GatewayConfiguration:
        """Load the Gateway client settings from DEPLOYED_* variables and its secret from Secrets Manager."""
        # Note: .env is loaded by utils/env_loader.py before this module is imported

        # Extract configuration from DEPLOYED_* variables (set by CloudFormation)
        gateway_url = os.getenv("DEPLOYED_GatewayUrl", "")
        region = deployment_region()
        client_id = os.getenv("DEPLOYED_CognitoClientId", "")

        # Require explicit token endpoint configuration (fail-fast)
        token_endpoint = os.getenv("DEPLOYED_CognitoTokenEndpoint", "")

        if not token_endpoint:
            raise ValueError(
                "DEPLOYED_CognitoTokenEndpoint environment variable is required")

        # Read once when the Gateway client starts; the global client keeps it for the process lifetime.
        client_secret = read_gateway_client_secret(region)

        # No scope: the Gateway machine client uses its default client scopes.
        return GatewayConfiguration(
            gateway_url=gateway_url,
            region=region,
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint=token_endpoint,
        )

    def _validate_configuration(self):
        """Validate required configuration"""
        required_fields = ["gateway_url", "region",
                           "client_id", "client_secret", "token_endpoint"]
        missing = [field for field in required_fields if not getattr(
            self.config, field)]

        if missing:
            raise ValueError(
                f"Missing required configuration: {', '.join(missing)}")

        if not self.config.gateway_url.startswith("https://"):
            raise ValueError("Gateway URL must use HTTPS")

        logger.info("Configuration validated successfully")
        if self.config.scope:
            logger.info(f"Using scope: {self.config.scope}")
        else:
            logger.info("No scope configured, using default client scopes")

    def create_mcp_client(self) -> MCPClient:
        """Create MCP client with automatic token refresh"""
        @asynccontextmanager
        async def create_transport():
            access_token = await asyncio.to_thread(self.token_manager.get_valid_token)
            headers = {"Authorization": f"Bearer {access_token}"}
            # The current MCP SDK accepts a configured HTTP client. Own its
            # lifetime here so reconnects and failures cannot leak connections.
            async with (
                httpx.AsyncClient(headers=headers, follow_redirects=False,
                                  timeout=httpx.Timeout(30, read=180)) as http,
                streamable_http_client(self.config.gateway_url, http_client=http) as transport,
            ):
                yield transport

        return MCPClient(create_transport)



# Global client instance for reuse
_global_gateway_client: MCPGatewayClient | None = None


def get_gateway_client() -> MCPGatewayClient:
    """Get the global gateway client instance"""
    global _global_gateway_client
    if _global_gateway_client is None:
        _global_gateway_client = MCPGatewayClient()
    return _global_gateway_client


def create_mcp_client() -> MCPClient:
    """Create an AgentCore Gateway MCP client that shares the process-wide Gateway token manager."""
    return get_gateway_client().create_mcp_client()
