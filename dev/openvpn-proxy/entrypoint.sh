#!/bin/sh
set -eu

config_file="${OPENVPN_CONFIG_FILE:-/run/secrets/openvpn-config}"
auth_file="${OPENVPN_AUTH_FILE:-/run/secrets/openvpn-auth}"
sockd_pid=""

test -s "$config_file"
test -s "$auth_file"

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

sockd -f /etc/sockd.conf &
sockd_pid=$!

while kill -0 "$openvpn_pid" 2>/dev/null && kill -0 "$sockd_pid" 2>/dev/null; do
    sleep 2
done

exit 1
