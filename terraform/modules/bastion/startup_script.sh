#!/bin/bash
set -euo pipefail

apt-get update -qq
apt-get install -y -qq tinyproxy

cat > /etc/tinyproxy/tinyproxy.conf <<'EOF'
User tinyproxy
Group tinyproxy
Port 8888
Timeout 600
DefaultErrorFile "/usr/share/tinyproxy/default.html"
LogFile "/var/log/tinyproxy/tinyproxy.log"
LogLevel Info
MaxClients 100
Allow 127.0.0.1
Allow ::1
EOF

systemctl enable tinyproxy
systemctl restart tinyproxy
