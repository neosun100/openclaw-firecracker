#!/bin/bash
TENANT_ID="${1:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
VM_NUM="${2:?Usage: stop-vm.sh <tenant_id> <vm_num>}"
curl -s --unix-socket /tmp/fc-${TENANT_ID}.sock -X PUT http://localhost/actions \
  -H 'Content-Type: application/json' -d '{"action_type":"SendCtrlAltDel"}' 2>/dev/null || true
sleep 2
pkill -f "api-sock /tmp/fc-${TENANT_ID}.sock" 2>/dev/null || true
sudo ip link del tap-vm${VM_NUM} 2>/dev/null || true
rm -f /tmp/fc-${TENANT_ID}.sock /tmp/${TENANT_ID}-rootfs.ext4
echo "✓ ${TENANT_ID} stopped (data volume preserved)"
