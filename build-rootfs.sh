#!/bin/bash
# 构建 OpenClaw rootfs 镜像并上传到 S3
# 用法: ./build-rootfs.sh [version]
# 示例: ./build-rootfs.sh v1.1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.deploy"
if [ -f "$ENV_FILE" ]; then
  source "$ENV_FILE"
else
  echo "❌ 未找到 .env.deploy，请先运行 ./setup.sh"
  exit 1
fi

VERSION="${1:-v1.0}"
BUCKET="${ASSETS_BUCKET}"
ROOTFS_IMG="/tmp/openclaw-rootfs-${VERSION}.ext4"
ROOTFS_DIR="/tmp/openclaw-rootfs-build"

# 根据 region 选择镜像源
case ${REGION} in
  ap-northeast-1) MIRROR="http://ap-northeast-1.ec2.archive.ubuntu.com/ubuntu" ;;
  ap-southeast-1) MIRROR="http://ap-southeast-1.ec2.archive.ubuntu.com/ubuntu" ;;
  eu-west-1)      MIRROR="http://eu-west-1.ec2.archive.ubuntu.com/ubuntu" ;;
  eu-central-1)   MIRROR="http://eu-central-1.ec2.archive.ubuntu.com/ubuntu" ;;
  *)              MIRROR="http://archive.ubuntu.com/ubuntu" ;;
esac

echo "=== Building rootfs ${VERSION} ==="
echo "Mirror: ${MIRROR}"

# 清理
sudo umount -l ${ROOTFS_DIR}/proc ${ROOTFS_DIR}/sys ${ROOTFS_DIR}/dev 2>/dev/null || true
sudo umount -l ${ROOTFS_DIR} 2>/dev/null || true
rm -f ${ROOTFS_IMG}

dd if=/dev/zero of=${ROOTFS_IMG} bs=1M count=5120 status=none
mkfs.ext4 -q ${ROOTFS_IMG}
sudo mkdir -p ${ROOTFS_DIR}
sudo mount ${ROOTFS_IMG} ${ROOTFS_DIR}

sudo debootstrap --include=curl,ca-certificates,systemd,dbus,iproute2,iputils-ping,git \
  noble ${ROOTFS_DIR} ${MIRROR}

sudo mount --bind /proc ${ROOTFS_DIR}/proc
sudo mount --bind /sys ${ROOTFS_DIR}/sys
sudo mount --bind /dev ${ROOTFS_DIR}/dev

sudo chroot ${ROOTFS_DIR} /bin/bash << 'CHROOT'
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq openssh-server
ssh-keygen -A
echo "PermitRootLogin yes" >> /etc/ssh/sshd_config

curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y -qq nodejs

systemctl enable systemd-networkd systemd-resolved

mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/dns.conf << 'DNSCONF'
[Resolve]
DNS=8.8.8.8 8.8.4.4
FallbackDNS=1.1.1.1
DNSCONF
echo "openclaw-vm" > /etc/hostname
echo "127.0.0.1 localhost openclaw-vm" > /etc/hosts
echo "root:openclaw" | chpasswd

mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d
cat > /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf << 'GETTY'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
Type=idle
GETTY

# 数据盘挂载 + OpenClaw 数据 symlink
cat > /etc/fstab << 'FSTAB'
/dev/vdb /mnt/data ext4 defaults,nofail 0 2
FSTAB

cat > /etc/systemd/system/openclaw-data.service << 'OCSVC'
[Unit]
Description=Link OpenClaw data volume
After=local-fs.target
RequiresMountsFor=/mnt/data
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c "mkdir -p /mnt/data && mount /dev/vdb /mnt/data 2>/dev/null; mkdir -p /mnt/data/openclaw && ln -sfn /mnt/data/openclaw /root/.openclaw"
ExecStartPost=/bin/bash -c "test -L /root/.openclaw && echo 'openclaw data linked' || echo 'WARNING: symlink failed'"
[Install]
WantedBy=multi-user.target
OCSVC
systemctl enable openclaw-data.service

echo "node=$(node --version) npm=$(npm --version)"

# --- OpenClaw CLI ---
npm install -g openclaw

# --- OpenClaw Gateway (systemd service) ---
NODE_BIN=$(dirname $(which node))
cat > /etc/systemd/system/openclaw-gateway.service << GWSVC
[Unit]
Description=OpenClaw Gateway
After=network.target openclaw-data.service

[Service]
Type=simple
Environment=PATH=${NODE_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${NODE_BIN}/openclaw daemon start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
GWSVC
systemctl enable openclaw-gateway

# --- Mission Control Dashboard ---
git clone --depth 1 https://github.com/robsannaa/openclaw-mission-control.git /opt/openclaw-mission-control
cd /opt/openclaw-mission-control
npm install
npm run build

cat > /etc/systemd/system/openclaw-dashboard.service << DASHSVC
[Unit]
Description=OpenClaw Mission Control Dashboard
After=network.target openclaw-gateway.service

[Service]
Type=simple
WorkingDirectory=/opt/openclaw-mission-control
Environment=PORT=3333
Environment=HOST=0.0.0.0
Environment=PATH=${NODE_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${NODE_BIN}/npm run start -- -H 0.0.0.0 -p 3333
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
DASHSVC
systemctl enable openclaw-dashboard

# --- Cleanup ---
apt-get clean
rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/* /root/.npm /tmp/*
rm -rf /opt/openclaw-mission-control/.next/cache /opt/openclaw-mission-control/node_modules/.cache

echo "openclaw=$(openclaw --version 2>&1 || echo 'installed')"
CHROOT

sudo umount -l ${ROOTFS_DIR}/proc ${ROOTFS_DIR}/sys ${ROOTFS_DIR}/dev
sudo umount ${ROOTFS_DIR}

echo "=== Uploading to S3 ==="
aws s3 cp ${ROOTFS_IMG} s3://${BUCKET}/rootfs/openclaw-rootfs-${VERSION}.ext4 --profile ${PROFILE}
aws s3 cp ${ROOTFS_IMG} s3://${BUCKET}/rootfs/openclaw-rootfs-latest.ext4 --profile ${PROFILE}

SIZE=$(ls -lh ${ROOTFS_IMG} | awk '{print $5}')
rm -f ${ROOTFS_IMG}

echo ""
echo "✓ rootfs ${VERSION} uploaded (${SIZE})"
echo "  s3://${BUCKET}/rootfs/openclaw-rootfs-${VERSION}.ext4"
echo "  s3://${BUCKET}/rootfs/openclaw-rootfs-latest.ext4"
