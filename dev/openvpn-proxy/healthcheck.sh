#!/bin/sh
set -eu

ip link show tun0 >/dev/null 2>&1
pidof sockd >/dev/null 2>&1
pidof openvpn >/dev/null 2>&1
