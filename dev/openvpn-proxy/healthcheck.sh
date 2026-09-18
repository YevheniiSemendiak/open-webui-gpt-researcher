#!/bin/sh
set -eu

ip link show tun0 >/dev/null 2>&1
pidof sockd >/dev/null 2>&1
pidof openvpn >/dev/null 2>&1

uplink_interface="$(ip -4 route show default | awk 'NR == 1 {for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
test -n "$uplink_interface"

bypass_cidrs="$(printf '%s' "${VPN_BYPASS_CIDRS:-}" | tr ',' ' ')"
for cidr in $bypass_cidrs; do
    destination="${cidr%/*}"
    ip -4 route get "$destination" | grep -q "dev $uplink_interface"
done
