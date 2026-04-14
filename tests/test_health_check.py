"""Tests for deploy/lambda/health_check/handler.py — stale detection + host-agent recovery."""

from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta
from conftest import make_ddb_table

_mock_ddb = MagicMock()
_mock_ssm = MagicMock()

with patch("boto3.resource", return_value=_mock_ddb), \
     patch("boto3.client", return_value=_mock_ssm):
    _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
    import importlib, sys
    spec = importlib.util.spec_from_file_location("hc_handler", "deploy/lambda/health_check/handler.py")
    hc = importlib.util.module_from_spec(spec)
    sys.modules["hc_handler"] = hc
    spec.loader.exec_module(hc)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class TestStaleDetection:
    """Test that stale tenants are correctly identified."""

    def test_fresh_tenant_not_marked_stale(self):
        """Tenant with recent health check should not be marked stale."""
        hc.tenants_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(30)},
        ]}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_not_called()

    def test_stale_tenant_marked(self):
        """Tenant with old health check should be marked stale."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_called()
        args = hc.tenants_table.update_item.call_args
        assert args[1]["ExpressionAttributeValues"][":vh"] == "stale"

    def test_missing_health_check_treated_as_stale(self):
        """Tenant with no last_health_check field should be marked stale."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1"},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_called()

    def test_no_running_tenants(self):
        """No running tenants → no action."""
        hc.tenants_table = make_ddb_table()
        hc.tenants_table.scan.return_value = {"Items": []}
        hc.lambda_handler({}, None)
        hc.tenants_table.update_item.assert_not_called()


class TestHostAgentRecovery:
    """Test host-agent restart logic."""

    def test_all_stale_triggers_restart(self):
        """If ALL tenants on a host are stale → restart host-agent."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
            {"id": "t2", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_called_once()
        cmd = hc.ssm.send_command.call_args[1]
        assert cmd["InstanceIds"] == ["i-1"]
        assert "systemctl restart host-agent" in cmd["Parameters"]["commands"][0]

    def test_partial_stale_no_restart(self):
        """If only SOME tenants on a host are stale → don't restart (agent is alive)."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
            {"id": "t2", "status": "running", "host_id": "i-1", "last_health_check": _ago(30)},  # fresh
        ]}
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_not_called()

    def test_cooldown_prevents_repeated_restart(self):
        """Don't restart if last restart was within cooldown period."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        # Last restart was 2 minutes ago (within 10 min cooldown)
        hc.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-1", "status": "active", "agent_restart_at": _ago(120)}
        }
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_not_called()

    def test_cooldown_expired_allows_restart(self):
        """Restart allowed if cooldown has expired."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        # Last restart was 15 minutes ago (cooldown expired)
        hc.hosts_table.get_item.return_value = {
            "Item": {"instance_id": "i-1", "status": "active", "agent_restart_at": _ago(900)}
        }
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_called_once()

    def test_deleted_host_not_restarted(self):
        """Deleted host should not be restarted."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "deleted"}}
        hc.lambda_handler({}, None)
        hc.ssm.send_command.assert_not_called()

    def test_multi_host_independent_recovery(self):
        """Each host evaluated independently — one stale, one fresh."""
        hc.tenants_table = make_ddb_table()
        hc.hosts_table = make_ddb_table()
        hc.ssm = MagicMock()
        hc.tenants_table.scan.return_value = {"Items": [
            {"id": "t1", "status": "running", "host_id": "i-1", "last_health_check": _ago(300)},
            {"id": "t2", "status": "running", "host_id": "i-2", "last_health_check": _ago(30)},
        ]}
        hc.hosts_table.get_item.return_value = {"Item": {"instance_id": "i-1", "status": "active"}}
        hc.lambda_handler({}, None)
        # Only i-1 should be restarted
        assert hc.ssm.send_command.call_count == 1
        assert hc.ssm.send_command.call_args[1]["InstanceIds"] == ["i-1"]
