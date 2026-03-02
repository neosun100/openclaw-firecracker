import os
import time
import boto3

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
tenants_table = ddb.Table(os.environ["TENANTS_TABLE"])
hosts_table = ddb.Table(os.environ["HOSTS_TABLE"])

MAX_FAILURES = 3


def lambda_handler(event, context):
    tenants = tenants_table.scan(
        FilterExpression="#s IN (:r, :c)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": "running", ":c": "creating"},
    ).get("Items", [])

    for tenant in tenants:
        check_tenant(tenant)


def check_tenant(tenant):
    tid = tenant["id"]
    host_id = tenant["host_id"]
    guest_ip = tenant["guest_ip"]
    now = _now()

    # Layer 1: microVM health (ping)
    vm_alive = _ssm_run(host_id, f"ping -c 1 -W 2 {guest_ip} > /dev/null 2>&1")

    if not vm_alive:
        failures = int(tenant.get("health_failures", 0)) + 1
        update_expr = "SET health_failures = :f, last_health_check = :t, vm_health = :vh, app_health = :ah"
        update_vals = {":f": failures, ":t": now, ":vh": "down", ":ah": "unknown"}

        if failures >= MAX_FAILURES:
            # Auto-restart VM
            vm_num = int(tenant.get("vm_num", 1))
            _ssm_run(host_id,
                f"export HOME=/home/ubuntu && cd /home/ubuntu && ~/stop-vm.sh {tid} {vm_num} && sleep 2 && ~/launch-vm.sh {tid} {vm_num} {tenant['vcpu']} {tenant['mem_mb']}",
                timeout=60,
            )
            update_vals[":f"] = 0
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=update_vals,
        )
        return

    # VM alive — Layer 2: OpenClaw gateway (informational, no restart on failure)
    app_alive = _ssm_run(host_id,
        f"curl -sf -o /dev/null --connect-timeout 3 http://{guest_ip}:18789/"
    )

    update_expr = "SET health_failures = :z, last_health_check = :t, vm_health = :vh, app_health = :ah"
    update_vals = {":z": 0, ":t": now, ":vh": "up", ":ah": "up" if app_alive else "down"}

    # Promote creating → running on first successful VM check
    if tenant.get("status") == "creating":
        update_expr += ", #s = :s, updated_at = :t"
        update_vals[":s"] = "running"
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=update_expr,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=update_vals,
        )
    else:
        tenants_table.update_item(
            Key={"id": tid},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=update_vals,
        )


def _ssm_run(instance_id, command, timeout=30):
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(3)
        for _ in range(timeout):
            try:
                result = ssm.get_command_invocation(
                    CommandId=cmd_id, InstanceId=instance_id,
                )
                if result["Status"] == "Success":
                    return True
                if result["Status"] in ("Failed", "TimedOut", "Cancelled"):
                    return False
            except Exception as e:
                if "InvocationDoesNotExist" in str(e):
                    time.sleep(1)
                    continue
                return False
            time.sleep(1)
        return False
    except Exception as ex:
        print(f"_ssm_run outer error: {ex}")
        return False


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
