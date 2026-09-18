from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_openvpn_proxy_is_fail_closed() -> None:
    sockd_config = (ROOT / "dev/openvpn-proxy/sockd.conf").read_text()
    entrypoint = (ROOT / "dev/openvpn-proxy/entrypoint.sh").read_text()
    healthcheck = (ROOT / "dev/openvpn-proxy/healthcheck.sh").read_text()

    assert "external: tun0" in sockd_config
    assert 'ip -4 route show default' in entrypoint
    assert 'ip -o -4 addr show dev "$uplink_interface"' in entrypoint
    assert 'VPN_BYPASS_CIDRS' in entrypoint
    assert 'ip -4 route replace "$cidr"' in entrypoint
    assert "if ! ip link show tun0 >/dev/null 2>&1; then" in entrypoint
    assert 'kill -0 "$openvpn_pid"' in entrypoint
    assert "pidof openvpn" in healthcheck
    assert "pidof sockd" in healthcheck
    assert 'ip -4 route get "$destination"' in healthcheck
