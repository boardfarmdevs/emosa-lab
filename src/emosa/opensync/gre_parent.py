# SPDX-License-Identifier: Apache-2.0
"""A GRE parent's tunnels to its children, as an EMOSA scope (spec 8.6).

A child of a GRE parent joins the parent AP as a 3-address station, takes a lease from the
AP's DHCP (the pod's underlay) and its ``cm`` builds a gretap to the AP's address. The parent
holds the other end, as the OpenSync cloud writes it (opensync-lab local-noc ``gre_step``): per
child a ``gre`` Wifi_Inet_Config row ``pgd<b3>_<b4>`` (the lease's last two octets) on the AP,
from the AP's address to the lease, and its Interface and Port as a port of ``br-home``. A child
is a lease (DHCP_leased_IP) in the underlay whose MAC is associated to the AP (the AP's
Wifi_Associated_Clients): a lease alone may be left from an earlier association. A tunnel
whose child is gone is removed, its port with it (OVSDB collects the Port and its Interface).

The scope's intent is the whole set of tunnels on the AP; each change is one guarded
transaction. Every gre row on the parent AP is EMOSA's, as the AP is.
"""

import ipaddress
import json
from dataclasses import asdict, dataclass

from emosa.backends.base import Snapshot, SubmitResult
from emosa.clock import utc_now
from emosa.errors import EmosaError, Reason
from emosa.model import Observation
from emosa.opensync.mapping import check_results, guard, where_uuid
from emosa.opensync.uplink import start_instance
from emosa.opensync.wired import INTERFACE

MODE = "opensync-6.6-gre-parent"
PROVENANCE = "opensync-nm:Wifi_Inet_State"
TUNNEL_MTU = 1562
INET_GUARDS = ["if_name", "if_type", "gre_ifname", "gre_local_inet_addr", "gre_remote_inet_addr"]
# What the scope monitors beyond the AP scope's tables (emosa.opensync.schema.TABLES).
MONITOR = {
    "DHCP_leased_IP": ["hwaddr", "inet_addr"],
    "Wifi_Inet_Config": [*INET_GUARDS, "enabled", "network", "mtu", "ip_assign_scheme"],
    "Wifi_Inet_State": ["if_name", "inet_addr"],
    "Bridge": ["name", "ports"],
    "Port": ["name", "interfaces"],
    "Interface": ["name"],
}


def tunnel_name(remote):
    """pgd<b3>_<b4>: the child's address's last two octets, as the OpenSync cloud names it."""
    packed = ipaddress.IPv4Address(remote).packed
    return f"pgd{packed[2]}_{packed[3]}"


@dataclass(frozen=True)
class TunnelIntent:
    """The parent AP ``ap``'s tunnels, one to each child address in ``remotes``, in ``bridge``."""

    pod_id: str
    ap: str
    bridge: str
    remotes: tuple

    def record(self):
        return {**asdict(self), "remotes": list(self.remotes)}

    def validate(self):
        for name in (self.ap, self.bridge):
            if not isinstance(name, str) or not INTERFACE.match(name):
                raise EmosaError(Reason.INVALID_INPUT, "AP and bridge must be interface names")
        try:
            addresses = [ipaddress.IPv4Address(r) for r in self.remotes]
        except (TypeError, ValueError) as exc:
            raise EmosaError(Reason.INVALID_INPUT, "a child address is not IPv4") from exc
        if len({tunnel_name(a) for a in addresses}) != len(addresses):
            raise EmosaError(Reason.INVALID_INPUT, "two children share a tunnel name")

    def target(self, vault=None):
        self.validate()
        return {
            "ap": self.ap,
            "bridge": self.bridge,
            "tunnels": {tunnel_name(r): str(r) for r in sorted(self.remotes)},
        }


def _set(value):
    """An OVSDB set as decoded (a list, or one bare value), as a list."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]


class GreParentBackend:
    """The tunnels of one pod's parent AP, bound by serial."""

    mode = MODE

    def __init__(self, pod_id, session, *, serial, ap, bridge, underlay):
        self.pod_id, self.session = pod_id, session
        self.expected_serial, self.ap, self.bridge = serial, ap, bridge
        self.underlay = ipaddress.ip_network(underlay)
        self.instance = None
        self.children = ()  # the child addresses at the last snapshot
        self.last = None
        self.write_count = 0

    def _decode(self, raw):
        schema = raw["schema"]
        return {
            t: {u: schema.row(t, row) for u, row in rows.items()}
            for t, rows in raw["tables"].items()
        }

    def _binding(self, raw):
        decoded = self._decode(raw)
        nodes = decoded.get("AWLAN_Node", {})
        if len(nodes) != 1 or next(iter(nodes.values())).get("serial_number") != (
            self.expected_serial
        ):
            raise EmosaError(Reason.NOT_READY, "pod identity absent or not the bound serial")
        self.instance = start_instance(decoded)
        return decoded, next(iter(nodes))

    def _bridge(self, decoded):
        rows = [
            (u, r) for u, r in decoded.get("Bridge", {}).items() if r.get("name") == self.bridge
        ]
        return rows[0] if len(rows) == 1 else (None, None)

    def _ports(self, decoded):
        """The bridge's ports by name: name -> Port uuid."""
        _, bridge = self._bridge(decoded)
        ports = decoded.get("Port", {})
        return {ports[u]["name"]: u for u in _set((bridge or {}).get("ports")) if u in ports}

    def _gre_rows(self, decoded):
        """The gre Wifi_Inet_Config rows on the AP: if_name -> (uuid, row)."""
        return {
            r.get("if_name"): (u, r)
            for u, r in decoded.get("Wifi_Inet_Config", {}).items()
            if r.get("if_type") == "gre" and r.get("gre_ifname") == self.ap
        }

    def local_address(self, decoded):
        for row in decoded.get("Wifi_Inet_State", {}).values():
            if row.get("if_name") == self.ap and row.get("inet_addr") not in (None, "", "0.0.0.0"):
                return row["inet_addr"]
        return None

    def child_addresses(self, decoded):
        """The leases in the underlay whose MAC is associated to the AP, sorted."""
        clients = decoded.get("Wifi_Associated_Clients", {})
        associated = set()
        for vif in decoded.get("Wifi_VIF_State", {}).values():
            if vif.get("if_name") != self.ap or vif.get("mode") != "ap":
                continue
            for uuid in _set(vif.get("associated_clients")):
                client = clients.get(uuid)
                if client and client.get("state", "active") == "active" and client.get("mac"):
                    associated.add(client["mac"].lower())
        found = set()
        for lease in decoded.get("DHCP_leased_IP", {}).values():
            try:
                address = ipaddress.IPv4Address(lease.get("inet_addr"))
            except (TypeError, ValueError):
                continue
            if (
                address in self.underlay
                and address != self.underlay.network_address + 1
                and str(lease.get("hwaddr") or "").lower() in associated
            ):
                found.add(address)
        return tuple(str(a) for a in sorted(found))

    def _configured(self, decoded):
        """The tunnels whose gre row and port are both there."""
        ports = self._ports(decoded)
        tunnels = {
            name: row.get("gre_remote_inet_addr")
            for name, (_, row) in self._gre_rows(decoded).items()
            if name in ports
        }
        return {"ap": self.ap, "bridge": self.bridge, "tunnels": tunnels}

    async def snapshot(self):
        try:
            raw = await self.session.snapshot()
            decoded, _ = self._binding(raw)
            configured = self._configured(decoded)
            self.children = self.child_addresses(decoded)
            # nm's own table: each tunnel's interface is up once it has a State row
            up = {r.get("if_name") for r in decoded.get("Wifi_Inet_State", {}).values()}
            observed = {
                **configured,
                "tunnels": {n: r for n, r in configured["tunnels"].items() if n in up},
            }
            self.last = Snapshot(
                configured,
                Observation(
                    self.pod_id,
                    "gre-parent",
                    observed,
                    "ovsdb",
                    self.mode,
                    raw["generation"],
                    utc_now(),
                    bool(raw["ready"]),
                    PROVENANCE,
                    revision=raw["revision"],
                ),
                bool(raw["ready"]),
                raw["generation"],
                raw["schema"].fingerprint,
            )
        except (EmosaError, ConnectionError, TimeoutError):
            if self.last is None:
                raise
            self.last.ready = False
            self.last.observed.fresh = False
        return self.last

    def _check(self, intent):
        target = intent.target()
        if (intent.pod_id, intent.ap, intent.bridge) != (self.pod_id, self.ap, self.bridge):
            raise EmosaError(Reason.UNSUPPORTED_OPERATION, "request exceeds the bound parent AP")
        if any(ipaddress.IPv4Address(r) not in self.underlay for r in intent.remotes):
            raise EmosaError(Reason.INVALID_INPUT, "a child address outside the underlay")
        return target

    async def plan(self, intent):
        target = self._check(intent)
        raw = await self.session.snapshot()
        decoded, _ = self._binding(raw)
        if not raw["ready"]:
            raise EmosaError(Reason.NOT_READY, "the pod's database is not ready")
        if self._bridge(decoded)[0] is None:
            raise EmosaError(Reason.NOT_READY, f"no bridge {self.bridge}")
        if target["tunnels"] and self.local_address(decoded) is None:
            raise EmosaError(Reason.NOT_READY, "the parent AP has no address yet")
        return {
            "mapping": MODE,
            "action": "gre-parent-tunnels",
            "ap": intent.ap,
            "tunnels": sorted(target["tunnels"]),
            "instance": self.instance,
            "fields": ["Wifi_Inet_Config gre rows", "Interface", "Port", "Bridge.ports"],
            "guard": "pod serial, the bridge's ports and each changed gre row",
        }

    def _transaction(self, intent, decoded, node_uuid):
        target = intent.target()["tunnels"]
        local = self.local_address(decoded)
        bridge_uuid, bridge = self._bridge(decoded)
        ports, rows = self._ports(decoded), self._gre_rows(decoded)
        ops = [
            {
                "op": "wait",
                "table": "AWLAN_Node",
                "where": where_uuid(node_uuid),
                "columns": ["serial_number"],
                "until": "==",
                "rows": [{"serial_number": self.expected_serial}],
                "timeout": 0,
            },
            guard(
                "Bridge",
                bridge_uuid,
                {
                    "name": bridge["name"],
                    "ports": ["set", [["uuid", u] for u in _set(bridge.get("ports"))]],
                },
                ["name", "ports"],
            ),
        ]
        counts = [None, None]
        for name in sorted(set(rows) | set(ports)):
            if name in target or not name.startswith("pgd"):
                continue
            # a child gone: its gre row and its port
            if name in rows:
                uuid, row = rows[name]
                ops += [
                    guard("Wifi_Inet_Config", uuid, row, [c for c in INET_GUARDS if c in row]),
                    {"op": "delete", "table": "Wifi_Inet_Config", "where": where_uuid(uuid)},
                ]
                counts += [None, 1]
            if name in ports and name in rows:
                ops.append(
                    {
                        "op": "mutate",
                        "table": "Bridge",
                        "where": where_uuid(bridge_uuid),
                        "mutations": [["ports", "delete", ["set", [["uuid", ports[name]]]]]],
                    }
                )
                counts.append(1)
        for index, (name, remote) in enumerate(sorted(target.items())):
            want = {
                "if_name": name,
                "if_type": "gre",
                "enabled": True,
                "network": True,
                "mtu": TUNNEL_MTU,
                "ip_assign_scheme": "none",
                "gre_ifname": self.ap,
                "gre_local_inet_addr": local,
                "gre_remote_inet_addr": remote,
            }
            if name not in rows:
                ops += [
                    {
                        "op": "wait",
                        "table": "Wifi_Inet_Config",
                        "where": [["if_name", "==", name]],
                        "columns": ["if_name"],
                        "until": "==",
                        "rows": [],
                        "timeout": 0,
                    },
                    {"op": "insert", "table": "Wifi_Inet_Config", "row": want},
                ]
                counts += [None, None]
            else:
                uuid, row = rows[name]
                if any(row.get(k) != v for k, v in want.items()):
                    ops += [
                        guard("Wifi_Inet_Config", uuid, row, [c for c in INET_GUARDS if c in row]),
                        {
                            "op": "update",
                            "table": "Wifi_Inet_Config",
                            "where": where_uuid(uuid),
                            "row": {k: v for k, v in want.items() if k != "if_name"},
                        },
                    ]
                    counts += [None, 1]
            if name not in ports:
                interface, port = f"i{index}", f"p{index}"
                ops += [
                    {
                        "op": "wait",
                        "table": "Port",
                        "where": [["name", "==", name]],
                        "columns": ["name"],
                        "until": "==",
                        "rows": [],
                        "timeout": 0,
                    },
                    {
                        "op": "insert",
                        "table": "Interface",
                        "uuid-name": interface,
                        "row": {"name": name},
                    },
                    {
                        "op": "insert",
                        "table": "Port",
                        "uuid-name": port,
                        "row": {"name": name, "interfaces": ["named-uuid", interface]},
                    },
                    {
                        "op": "mutate",
                        "table": "Bridge",
                        "where": where_uuid(bridge_uuid),
                        "mutations": [["ports", "insert", ["set", [["named-uuid", port]]]]],
                    },
                ]
                counts += [None, None, None, 1]
        return ops, counts

    async def submit(self, intent, attempt):
        await self.plan(intent)
        raw = await self.session.snapshot()
        decoded, node_uuid = self._binding(raw)
        if raw["generation"] != attempt["session_generation"]:
            return SubmitResult("rejected", {}, Reason.NOT_READY)
        transaction, counts = self._transaction(intent, decoded, node_uuid)
        self.write_count += 1
        try:
            results = await self.session.transact(
                transaction, attempt["transaction_id"], attempt["session_generation"]
            )
            check_results(results, counts)
        except (ConnectionError, TimeoutError):
            return SubmitResult("unknown", {"attribution": "unknown"}, Reason.OUTCOME_UNKNOWN)
        except EmosaError as exc:
            if exc.code == Reason.PRECONDITION_FAILED:
                return SubmitResult("conflict", {}, exc.code)
            return SubmitResult(
                "unknown" if exc.code == Reason.OUTCOME_UNKNOWN else "rejected", {}, exc.code
            )
        return SubmitResult(
            "committed",
            {
                "attribution": "reply",
                "transaction_validated": True,
                "transaction_id": attempt["transaction_id"],
                "session_generation": raw["generation"],
                "action": "gre-parent-tunnels",
                "instance": self.instance,
                "results": json.loads(json.dumps(results, default=str)),
            },
            None,
        )

    async def close(self):
        return None
