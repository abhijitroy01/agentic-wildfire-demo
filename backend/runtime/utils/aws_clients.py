"""
AWS Client Factory for Backend Runtime.

Provides singleton boto3 clients to avoid creating multiple instances, and deployment_region(), the one
source of the Runtime's AWS Region.

Usage:
    from utils.aws_clients import AWSClients

    s3 = AWSClients.s3()
    s3.put_object(Bucket='my-bucket', Key='key', Body=data)
"""

import os

import boto3
from botocore.client import BaseClient
from botocore.config import Config


def deployment_region() -> str:
    """Return the Runtime's AWS Region from DEPLOYED_DeploymentRegion; fail with its name when it is missing.

    The AgentCore Runtime template sets it from AWS::Region. There is no default Region, so a missing value
    never sends a request to another Region.
    """
    region = os.getenv("DEPLOYED_DeploymentRegion", "").strip()
    if not region:
        raise RuntimeError("DEPLOYED_DeploymentRegion is not set. The AgentCore Runtime template sets it from "
                           "AWS::Region; for a local run, export the Region of your installation.")
    return region


# Force SigV4 for all S3 operations including presigned URLs.
# Without this, generate_presigned_url defaults to SigV2 which AWS rejects
# with InvalidToken when used with STS temporary credentials.
_S3_CONFIG = Config(signature_version='s3v4', connect_timeout=5, read_timeout=30,
                    retries={"mode": "standard", "total_max_attempts": 2},
                    s3={"addressing_style": "virtual"})
# Shared timeouts and bounded retries for every other Runtime client.
_CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=30,
                        retries={"mode": "standard", "total_max_attempts": 2})


class AWSClients:
    """
    Singleton factory for AWS boto3 clients.

    All AWS service operations in the backend runtime should use this
    factory to ensure consistent configuration (region, retries, etc.)
    and avoid the ~100-300 ms cost of constructing fresh clients on every
    invocation. AgentCore Runtime microVMs are reused across invocations
    within a session, so the singletons live for the lifetime of the
    container.
    """

    _s3: BaseClient | None = None
    _polly: BaseClient | None = None
    _translate: BaseClient | None = None
    _bedrock_agent_runtime: BaseClient | None = None

    @classmethod
    def s3(cls) -> BaseClient:
        """Get S3 client (singleton). Uses SigV4 for presigned URL compatibility."""
        if cls._s3 is None:
            region = deployment_region()
            cls._s3 = boto3.client(
                's3', region_name=region, config=_S3_CONFIG,
                endpoint_url=f"https://s3.{region}.amazonaws.com")
        return cls._s3

    @classmethod
    def polly(cls) -> BaseClient:
        """Get Polly client (singleton)."""
        if cls._polly is None:
            cls._polly = boto3.client('polly', region_name=deployment_region(), config=_CLIENT_CONFIG)
        return cls._polly

    @classmethod
    def translate(cls) -> BaseClient:
        """Get Translate client (singleton)."""
        if cls._translate is None:
            cls._translate = boto3.client(
                'translate', region_name=deployment_region(), config=_CLIENT_CONFIG)
        return cls._translate

    @classmethod
    def bedrock_agent_runtime(cls) -> BaseClient:
        """Get Bedrock Agent Runtime client (used for KB Retrieve API)."""
        if cls._bedrock_agent_runtime is None:
            cls._bedrock_agent_runtime = boto3.client(
                'bedrock-agent-runtime', region_name=deployment_region(), config=_CLIENT_CONFIG)
        return cls._bedrock_agent_runtime

    @classmethod
    def reset(cls) -> None:
        """Reset all client instances (for testing)."""
        cls._s3 = None
        cls._polly = None
        cls._translate = None
        cls._bedrock_agent_runtime = None
