"""
AWS Client Factory for Broadcastify Package.

Provides singleton boto3 clients to avoid creating multiple instances.
Uses lazy initialization to defer client creation until first use.
"""


import boto3
from botocore.config import Config

from .config import AWS_REGION

# Every client sets its waits and makes two standard-mode attempts, as backend/runtime/utils/aws_clients.py
# does: botocore's defaults (60 s to connect, five legacy-mode attempts) could hold a radio session for minutes.
_RETRIES = {"mode": "standard", "total_max_attempts": 2}
# Force SigV4 for all S3 operations including presigned URLs.
# Without this, generate_presigned_url defaults to SigV2 which AWS rejects
# with InvalidToken when used with STS temporary credentials.
_S3_CONFIG = Config(signature_version='s3v4', connect_timeout=5, read_timeout=30, retries=_RETRIES)
# A Bedrock read waits for the whole model answer, so it keeps botocore's 60 s read timeout.
_BEDROCK_CONFIG = Config(connect_timeout=5, read_timeout=60, retries=_RETRIES)


class AWSClients:
    """
    Singleton factory for AWS boto3 clients.

    Clients are lazily initialized on first access and reused thereafter.
    This avoids the overhead of creating multiple client instances.

    Usage:
        from broadcastify.shared import AWSClients

        # Get clients (created on first call, reused thereafter)
        s3 = AWSClients.s3()
        bedrock = AWSClients.bedrock()

        # Use normally
        s3.list_objects_v2(Bucket='my-bucket')
    """

    _s3: boto3.client | None = None
    _bedrock: boto3.client | None = None

    @classmethod
    def s3(cls) -> boto3.client:
        """
        Get S3 client (singleton). Uses SigV4 for presigned URL compatibility.

        Returns:
            Configured boto3 S3 client
        """
        if cls._s3 is None:
            cls._s3 = boto3.client(
                's3', region_name=AWS_REGION, config=_S3_CONFIG)
        return cls._s3

    @classmethod
    def bedrock(cls) -> boto3.client:
        """
        Get Bedrock Runtime client (singleton).

        Returns:
            Configured boto3 Bedrock Runtime client
        """
        if cls._bedrock is None:
            cls._bedrock = boto3.client(
                'bedrock-runtime', region_name=AWS_REGION, config=_BEDROCK_CONFIG)
        return cls._bedrock
