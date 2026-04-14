"""Tests for deploy/lambda/api/handler.py — focus on overcommit scheduling logic."""

import json
from unittest.mock import patch, MagicMock
from conftest import make_ddb_table


# Patch boto3 before importing handler
_mock_ddb = MagicMock()
_mock_ssm = MagicMock()
_mock_s3 = MagicMock()
_mock_asg = MagicMock()
_mock_elbv2 = MagicMock()

with patch.dict("os.environ", {
    "CPU_OVERCOMMIT_RATIO": "2.0",
    "MEM_OVERCOMMIT_RATIO": "1.5",
}):
    with patch("boto3.resource", return_value=_mock_ddb), \
         patch("boto3.client") as mock_client:
        def _client_factory(service, **kw):
            return {"ssm": _mock_ssm, "s3": _mock_s3, "autoscaling": _mock_asg,
                    "elbv2": _mock_elbv2, "ec2": MagicMock(), "lambda": MagicMock()}.get(service, MagicMock())
        mock_client.side_effect = _client_factory
        _mock_ddb.Table.side_effect = lambda name: make_ddb_table()
        import importlib, sys
        spec = importlib.util.spec_from_file_location("api_handler", "deploy/lambda/api/handler.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules["api_handler"] = api
        spec.loader.exec_module(api)


class TestFindHost:
    """Test _find_host with CPU and memory overcommit."""

    def _make_host(self, total_vcpu=8, total_mem_mb=16384, used_vcpu=0, used_mem_mb=0, status="active"):
        return {
            "instance_id": "i-test",
            "total_vcpu": total_vcpu,
            "total_mem_mb": total_mem_mb,
            "used_vcpu": used_vcpu,
            "used_mem_mb": used_mem_mb,
            "status": status,
            "next_vm_num": 1,
        }

    def test_find_host_empty_host_fits(self):
        """Empty host should fit a standard VM."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [self._make_host()]}
        result = api._find_host(2, 4096)
        assert result is not None
        assert result["instance_id"] == "i-test"

    def test_find_host_cpu_overcommit_allows_more(self):
        """With CPU ratio 2.0, 8 vCPU host can allocate 16 vCPU total."""
        host = self._make_host(total_vcpu=8, used_vcpu=10)  # 10 used, physical=8, allocatable=16
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [host]}
        # Need 4 vCPU: 16 - 10 = 6 free → fits
        result = api._find_host(4, 0)
        assert result is not None

    def test_find_host_cpu_overcommit_rejects_when_full(self):
        """With CPU ratio 2.0, 8 vCPU host with 16 used → no room."""
        host = self._make_host(total_vcpu=8, used_vcpu=16)
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [host]}
        result = api._find_host(2, 0)
        assert result is None

    def test_find_host_mem_overcommit_allows_more(self):
        """With MEM ratio 1.5, 16GB host can allocate 24GB total."""
        host = self._make_host(total_mem_mb=16384, used_mem_mb=18000)  # 18GB used, allocatable=24GB
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [host]}
        # Need 4096MB: 24576 - 18000 = 6576 free → fits
        result = api._find_host(0, 4096)
        assert result is not None

    def test_find_host_mem_overcommit_rejects_when_full(self):
        """With MEM ratio 1.5, 16GB host with 24GB used → no room."""
        host = self._make_host(total_mem_mb=16384, used_mem_mb=24576)
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [host]}
        result = api._find_host(0, 4096)
        assert result is None

    def test_find_host_no_overcommit(self):
        """With ratio 1.0, no overcommit — strict physical limits."""
        original_cpu = api.CPU_OVERCOMMIT_RATIO
        original_mem = api.MEM_OVERCOMMIT_RATIO
        try:
            api.CPU_OVERCOMMIT_RATIO = 1.0
            api.MEM_OVERCOMMIT_RATIO = 1.0
            host = self._make_host(total_vcpu=8, total_mem_mb=16384, used_vcpu=7, used_mem_mb=13000)
            api.hosts_table = make_ddb_table()
            api.hosts_table.scan.return_value = {"Items": [host]}
            # Need 2 vCPU: 8 - 7 = 1 free → doesn't fit
            result = api._find_host(2, 4096)
            assert result is None
        finally:
            api.CPU_OVERCOMMIT_RATIO = original_cpu
            api.MEM_OVERCOMMIT_RATIO = original_mem

    def test_find_host_both_cpu_and_mem_must_fit(self):
        """Both CPU and memory must have room — not just one."""
        # CPU has room (overcommit), but memory is full
        host = self._make_host(total_vcpu=8, total_mem_mb=16384, used_vcpu=4, used_mem_mb=24576)
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [host]}
        result = api._find_host(2, 4096)
        assert result is None

    def test_find_host_skips_deleted(self):
        """Deleted hosts should not be returned by scan filter."""
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}  # scan filters out deleted
        result = api._find_host(2, 4096)
        assert result is None

    def test_find_host_picks_first_fit(self):
        """Should return the first host that fits."""
        h1 = self._make_host(total_vcpu=4, total_mem_mb=8192, used_vcpu=4, used_mem_mb=8192)
        h1["instance_id"] = "i-full"
        h2 = self._make_host(total_vcpu=8, total_mem_mb=16384)
        h2["instance_id"] = "i-empty"
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [h1, h2]}
        # h1: allocatable_vcpu=8, free=4; allocatable_mem=12288, free=4096 → fits 2/4096
        result = api._find_host(2, 4096)
        assert result["instance_id"] == "i-full"


class TestListHosts:
    """Test list_hosts returns overcommit ratios."""

    def test_list_hosts_includes_both_ratios(self):
        host = {"instance_id": "i-test", "status": "active", "vm_count": 0}
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": [host]}
        resp = api.list_hosts()
        body = json.loads(resp["body"])
        assert body[0]["cpu_overcommit_ratio"] == 2.0
        assert body[0]["mem_overcommit_ratio"] == 1.5


class TestListTenants:
    """Test list_tenants filters deleted."""

    def test_list_tenants_returns_200(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": [{"id": "t1", "status": "running"}]}
        resp = api.list_tenants()
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body) == 1


class TestCreateTenant:
    """Test create_tenant scheduling with overcommit."""

    def test_create_tenant_pending_when_no_host(self):
        """No host available → tenant goes pending + scale out."""
        api.tenants_table = make_ddb_table()
        api.hosts_table = make_ddb_table()
        api.hosts_table.scan.return_value = {"Items": []}
        _mock_asg.describe_auto_scaling_groups.return_value = {
            "AutoScalingGroups": [{"DesiredCapacity": 1, "MaxSize": 5}]
        }
        resp = api.create_tenant(json.dumps({"name": "test-vm"}))
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["status"] == "pending"


class TestRouting:
    """Test lambda_handler routing."""

    def test_unknown_route_returns_404(self):
        event = {"httpMethod": "GET", "resource": "/nonexistent", "pathParameters": {}}
        resp = api.lambda_handler(event, None)
        assert resp["statusCode"] == 404

    def test_cors_headers_present(self):
        api.tenants_table = make_ddb_table()
        api.tenants_table.scan.return_value = {"Items": []}
        event = {"httpMethod": "GET", "resource": "/tenants", "pathParameters": {}}
        resp = api.lambda_handler(event, None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
