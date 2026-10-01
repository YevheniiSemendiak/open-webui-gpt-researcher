#!/bin/sh
set -eu

config_file="${OPENVPN_CONFIG_FILE:-/run/secrets/openvpn-config}"
auth_file="${OPENVPN_AUTH_FILE:-/run/secrets/openvpn-auth}"
sockd_pid=""
tinyproxy_pid=""

test -s "$config_file"
test -s "$auth_file"

default_route="$(ip -4 route show default | head -n 1)"
uplink_interface="$(printf '%s\n' "$default_route" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
uplink_gateway="$(printf '%s\n' "$default_route" | awk '{for (i = 1; i <= NF; i++) if ($i == "via") {print $(i + 1); exit}}')"

if [ -z "$uplink_interface" ]; then
    echo "Unable to discover the pre-VPN default-route interface" >&2
    exit 1
fi

uplink_cidr="$(ip -o -4 addr show dev "$uplink_interface" scope global | awk 'NR == 1 {print $4}')"
if [ -z "$uplink_cidr" ]; then
    echo "Unable to discover the IPv4 subnet on $uplink_interface" >&2
    exit 1
fi

echo "Preserving pre-VPN uplink: interface=$uplink_interface gateway=${uplink_gateway:-direct} subnet=$uplink_cidr"

config_dir="$(dirname "$config_file")"
config_name="$(basename "$config_file")"

openvpn --cd "$config_dir" --config "$config_name" --auth-user-pass "$auth_file" --auth-nocache &
openvpn_pid=$!

cleanup() {
    kill "$openvpn_pid" 2>/dev/null || true
    if [ -n "$sockd_pid" ]; then
        kill "$sockd_pid" 2>/dev/null || true
        wait "$sockd_pid" 2>/dev/null || true
    fi
    if [ -n "$tinyproxy_pid" ]; then
        kill "$tinyproxy_pid" 2>/dev/null || true
        wait "$tinyproxy_pid" 2>/dev/null || true
    fi
    wait "$openvpn_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

while ! ip link show tun0 >/dev/null 2>&1; do
    if ! kill -0 "$openvpn_pid" 2>/dev/null; then
        wait "$openvpn_pid"
        exit 1
    fi
    sleep 1
done

# OpenVPN providers commonly install two /1 routes through tun0. Those routes are
# more specific than the original default route and can capture replies to proxy
# clients in Kubernetes or other private networks. The directly connected uplink
# subnet remains more specific automatically; operators can declare additional
# client/service networks as a comma- or whitespace-separated IPv4 CIDR list.
bypass_cidrs="$(printf '%s' "${VPN_BYPASS_CIDRS:-}" | tr ',' ' ')"
for cidr in $bypass_cidrs; do
    if [ -n "$uplink_gateway" ]; then
        ip -4 route replace "$cidr" via "$uplink_gateway" dev "$uplink_interface"
    else
        ip -4 route replace "$cidr" dev "$uplink_interface"
    fi
    echo "Installed VPN bypass route: $cidr via ${uplink_gateway:-$uplink_interface}"
done

sockd -f /etc/sockd.conf &
sockd_pid=$!

tun_address="$(ip -o -4 addr show dev tun0 scope global | awk 'NR == 1 {split($4, address, "/"); print address[1]}')"
if [ -z "$tun_address" ]; then
    echo "Unable to discover the VPN tunnel IPv4 address" >&2
    exit 1
fi

# Bind Tinyproxy's outbound sockets to the tunnel address. This gives the HTTP
# CONNECT listener the same fail-closed property as Dante's `external: tun0`:
# it cannot silently fall back to the pod's ordinary interface.
tinyproxy_runtime_config=/tmp/tinyproxy.conf
cp /etc/tinyproxy.conf "$tinyproxy_runtime_config"
printf '\nBind %s\n' "$tun_address" >>"$tinyproxy_runtime_config"
tinyproxy -d -c "$tinyproxy_runtime_config" &
tinyproxy_pid=$!

while kill -0 "$openvpn_pid" 2>/dev/null \
    && kill -0 "$sockd_pid" 2>/dev/null \
    && kill -0 "$tinyproxy_pid" 2>/dev/null; do
    # Both proxies bind outbound traffic to tun0. Exit as soon as the tunnel
    # disappears so the supervisor tears them down and restarts fail-closed.
    if ! ip link show tun0 >/dev/null 2>&1; then
        exit 1
    fi
    sleep 1
done

exit 1
