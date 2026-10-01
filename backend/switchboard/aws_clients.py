"""Reuse AWS connections while keeping workspace state local to each request."""

from functools import lru_cache

import boto3
from botocore.config import Config


@lru_cache(maxsize=8)
def aws_client(service: str):
    return boto3.client(service, config=Config(tcp_keepalive=True))
