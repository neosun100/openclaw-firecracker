"""Shared fixtures for all tests. Patches AWS SDK clients before handler import."""

import os
import pytest
from unittest.mock import MagicMock, patch

# Set env vars before any handler import
os.environ.setdefault("TENANTS_TABLE", "test-tenants")
os.environ.setdefault("HOSTS_TABLE", "test-hosts")
os.environ.setdefault("ASSETS_BUCKET", "test-bucket")
os.environ.setdefault("ROOTFS_PREFIX", "rootfs")
os.environ.setdefault("HOST_RESERVED_VCPU", "1")
os.environ.setdefault("HOST_RESERVED_MEM", "2048")
os.environ.setdefault("CPU_OVERCOMMIT_RATIO", "2.0")
os.environ.setdefault("MEM_OVERCOMMIT_RATIO", "1.5")
os.environ.setdefault("VM_DEFAULT_VCPU", "2")
os.environ.setdefault("VM_DEFAULT_MEM", "4096")
os.environ.setdefault("VM_DATA_DISK_MB", "8192")
os.environ.setdefault("VM_PORT_BASE", "18789")
os.environ.setdefault("VM_SUBNET_PREFIX", "172.16")
os.environ.setdefault("ASG_NAME", "test-asg")
os.environ.setdefault("ALB_LISTENER_ARN", "arn:aws:elasticloadbalancing:us-east-1:123:listener/app/test/123/456")
os.environ.setdefault("VPC_ID", "vpc-test")
os.environ.setdefault("IDLE_TIMEOUT_MINUTES", "10")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def make_ddb_table():
    """Create a mock DynamoDB Table with common methods."""
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    table.get_item.return_value = {}
    table.put_item.return_value = {}
    table.update_item.return_value = {}
    return table
