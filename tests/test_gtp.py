"""The GRE termination point: OpenSync's rules, dnsmasq, and one gretap per lease."""

import copy
import time

import pytest

from emosa.config import validate
from emosa.errors import EmosaError
from emosa.gtp import GTP, ConfigError, Links, check, render_dnsmasq, tunnel_name

pytestmark = pytest.mark.unit

CONFIG = {
    "underlay": {
        "interface": "podbh",
        "address": "169.254.2.1/25",
        "mtu": 1600,
        "dhcp_range": ["169.254.2.10", "169.254.2.126"],
        "lease_time": "1h",
    },
    "lan": {"bridge": "br-gtp", "ports": ["eth1"]},
    "tunnel_mtu": 1562,
    "state_dir": "/tmp/unused",
}


class FakeIp:
    """iproute2 over an in-memory set of links."""

    def __init__(self, *links):
        self.links = {name: {} for name in links}
        self.calls = []

    def __call__(self, *args, check=True):
        self.calls.append(args)
        if args[:3] == ("-br", "link", "show"):
            return f"{args[4]} UP\n" if args[4] in self.links else ""
        if args[:5] == ("-d", "-o", "link", "show", "type"):
            return "".join(
                f"7: {n}@podbh: <UP> mtu 1562 \\    gretap"
                f" remote {v['remote']} local {v['local']} dev podbh\n"
                for n, v in self.links.items()
                if v.get("type") == "gretap"
            )
        if args[:4] == ("-o", "link", "show", "master"):
            return "".join(
                f"{i}: {n}: <UP> mtu 1500 master {args[4]}\n"
                for i, (n, v) in enumerate(self.links.items())
                if v.get("master") == args[4]
            )
        if args[:2] == ("link", "add"):
            name = args[2]
            spec = dict(zip(args[3::2], args[4::2], strict=False))
            self.links[name] = spec
        elif args[:2] == ("link", "del"):
            del self.links[args[2]]
        elif args[:2] == ("link", "set") and "master" in args:
            self.links[args[2]]["master"] = args[args.index("master") + 1]
        return ""


def gtp(tmp_path, *leases, links=("podbh", "eth1")):
    config = copy.deepcopy(CONFIG)
    config["state_dir"] = str(tmp_path)
    expiry = int(time.time()) + 3600
    (tmp_path / "leases").write_text("".join(f"{expiry} {mac} {ip} pod *\n" for mac, ip in leases))
    fake = FakeIp(*links)
    return GTP(config, Links(fake)), fake


def test_the_configuration_follows_opensyncs_rules():
    validate("gtp-config", CONFIG)
    check(copy.deepcopy(CONFIG))
    for change, message in (
        ({"address": "10.0.0.1/24"}, None),  # schema: not link-local
        ({"address": "169.254.2.2/25"}, "first host"),
        ({"dhcp_range": ["169.254.2.1", "169.254.2.126"]}, "DHCP range"),
        ({"dhcp_range": ["169.254.2.10", "169.254.3.10"]}, "DHCP range"),
    ):
        config = copy.deepcopy(CONFIG)
        config["underlay"].update(change)
        if message is None:
            with pytest.raises(EmosaError):
                validate("gtp-config", config)
        else:
            with pytest.raises(ConfigError, match=message):
                check(config)
    config = copy.deepcopy(CONFIG)
    config["tunnel_mtu"] = 1580  # 1580 + 38 > 1600
    with pytest.raises(ConfigError, match="MTU"):
        check(config)


def test_dnsmasq_gives_the_mtu_and_no_router_or_dns(tmp_path):
    conf, hook = render_dnsmasq({**CONFIG, "state_dir": str(tmp_path)}, "/etc/emosa-gtp.json")
    lines = set(conf.splitlines())
    assert "interface=podbh" in lines and "port=0" in lines
    assert "dhcp-range=169.254.2.10,169.254.2.126,255.255.255.128,1h" in lines
    assert "dhcp-option=option:mtu,1600" in lines
    assert "dhcp-option=option:router" in lines and "dhcp-option=option:dns-server" in lines
    assert f"dhcp-script={tmp_path / 'hook'}" in lines and "script-on-renewal" in lines
    assert 'emosa.gtp lease "/etc/emosa-gtp.json" "$@"' in hook


def test_setup_addresses_the_underlay_and_bridges_the_lan_port(tmp_path):
    gateway, fake = gtp(tmp_path, links=("podbh", "eth1", "wlan0"))
    fake.links["wlan0"]["master"] = "podbh"  # the pod-backhaul AP, in the underlay bridge
    gateway.setup(tmp_path / "gtp.json")
    assert ("addr", "replace", "169.254.2.1/25", "dev", "podbh") in fake.calls
    assert ("link", "set", "wlan0", "mtu", "1600") in fake.calls  # underlay ports carry 1600
    assert fake.links["br-gtp"] == {"type": "bridge"}
    assert fake.links["eth1"]["master"] == "br-gtp"
    assert fake.links["podbh"].get("master") is None  # the underlay is never bridged
    assert (tmp_path / "dnsmasq.conf").exists() and (tmp_path / "hook").exists()


def test_one_gretap_per_lease_into_the_lan_bridge(tmp_path):
    gateway, fake = gtp(tmp_path)
    gateway.setup(tmp_path / "gtp.json")
    gateway.lease("add", "02:00:00:00:05:00", "169.254.2.57")
    tunnel = fake.links["gtp2_57"]
    assert tunnel["type"] == "gretap" and tunnel["remote"] == "169.254.2.57"
    assert tunnel["local"] == "169.254.2.1" and tunnel["master"] == "br-gtp"
    assert ("link", "set", "gtp2_57", "mtu", "1562") in fake.calls
    calls = len(fake.calls)
    gateway.lease("old", "02:00:00:00:05:00", "169.254.2.57")  # a renewal changes nothing
    assert not [c for c in fake.calls[calls:] if c[:2] in (("link", "add"), ("link", "del"))]
    gateway.lease("del", "02:00:00:00:05:00", "169.254.2.57")
    assert "gtp2_57" not in fake.links
    with pytest.raises(ConfigError):
        gateway.lease("add", "02:00:00:00:05:00", "169.254.1.57")  # not our underlay


def test_reconcile_makes_the_tunnels_exactly_the_leases(tmp_path):
    gateway, fake = gtp(tmp_path, ("02:00:00:00:05:00", "169.254.2.57"))
    fake.links["gtp2_99"] = {"type": "gretap", "local": "169.254.2.1", "remote": "169.254.2.99"}
    listed = gateway.reconcile()
    assert set(listed) == {"gtp2_57"} and listed["gtp2_57"]["mac"] == "02:00:00:00:05:00"
    assert tunnel_name("169.254.2.126") == "gtp2_126"
