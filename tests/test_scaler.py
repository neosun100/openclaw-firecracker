"""Tests for deploy/lambda/scaler/handler.py — two-round idle host reclamation."""

from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_asg = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_asg):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    import importlib, sys
    spec = importlib.util.spec_from_file_location("sc_handler", "deploy/lambda/scaler/handler.py")
    sc = importlib.util.module_from_spec(spec)
    sys.modules["sc_handler"] = sc
    spec.loader.exec_module(sc)


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class TestScaler:
    """Test two-round confirmation scale-in logic."""

    def test_active_host_with_vms_stays_active(self):
        """Host with VMs should remain active."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 2},
        ]}
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_not_called()

    def test_idle_host_with_vms_recovers_to_active(self):
        """Idle host that got a VM assigned should recover to active."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "idle", "vm_count": 1},
        ]}
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_called_once()
        args = sc.hosts_table.update_item.call_args[1]
        assert args["ExpressionAttributeValues"][":s"] == "active"

    def test_empty_active_host_records_idle_since(self):
        """Empty active host without idle_since → record timestamp."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0},
        ]}
        sc.lambda_handler({}, None)
        sc.hosts_table.update_item.assert_called_once()

    def test_empty_active_host_within_timeout_stays(self):
        """Empty active host within timeout → no status change."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0, "idle_since": _ago(60)},
        ]}
        sc.lambda_handler({}, None)
        # Should not change status (only 1 min idle, timeout is 10 min)
        sc.hosts_table.update_item.assert_not_called()

    def test_empty_active_host_past_timeout_marked_idle(self):
        """Empty active host past timeout → marked idle (round 1)."""
        sc.hosts_table = make_ddb_table()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "active", "vm_count": 0, "idle_since": _ago(700)},
        ]}
        sc.lambda_handler({}, None)
        args = sc.hosts_table.update_item.call_args[1]
        assert args["ExpressionAttributeValues"][":s"] == "idle"

    def test_idle_host_terminated_when_asg_allows(self):
        """Idle host (round 2) → terminate if ASG desired > min."""
        sc.hosts_table = make_ddb_table()
        sc.autoscaling = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "idle", "vm_count": 0},
        ]}
        sc.autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 2, "MinSize": 1}]
        }
        sc.lambda_handler({}, None)
        sc.autoscaling.terminate_instance_in_auto_scaling_group.assert_called_once()

    def test_idle_host_not_terminated_at_min(self):
        """Idle host at ASG min → don't terminate."""
        sc.hosts_table = make_ddb_table()
        sc.autoscaling = MagicMock()
        sc.hosts_table.scan.return_value = {"Items": [
            {"instance_id": "i-1", "status": "idle", "vm_count": 0},
        ]}
        sc.autoscaling.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MinSize": 1}]
        }
        sc.lambda_handler({}, None)
        sc.autoscaling.terminate_instance_in_auto_scaling_group.assert_not_called()
