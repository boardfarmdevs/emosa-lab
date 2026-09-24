# OpenSync pods as EasyMesh agents: the translation and the fleet

[Architecture index](README.md)

EMOSA makes each OpenSync pod an EasyMesh agent that an existing EasyMesh
controller onboards and manages. The pod is not changed: it keeps its own
OpenSync managers, and EMOSA takes the place of its cloud manager. There are
three parts:

1. The **fleet** gives every pod that appears its own virtual agent.
2. The **translation** turns the pod's OVSDB model into the EasyMesh/1905.1
   model, and the controller's configuration back into OVSDB rows.
3. The **virtual agent** speaks 1905.1 for the pod on the controller's LAN.

```
 OpenSync pod                  EMOSA                                  EasyMesh
 (unchanged)                                                          controller
 cm ──redirect──▶ fleet front port (6650)                            (prplMesh,
      identify serial, allocate agent, manager_addr ─▶ agent port     RDK, ...)
 ovsdb-server ◀── OVSDB (monitor, guarded transact) ── virtual agent ◀─ 1905.1 ─▶
 owm/osw apply                          │  device_view (OVSDB → view)
 Wifi_*_Config, publish Wifi_*_State    │  topology/capabilities (view → TLVs)
                                        │  M2 set → VIF rows (pod_profile)
```

## 1. The fleet (`emosa.agent.fleet`)

The fleet works like the OpenSync redirector. The operator's cloud (in the lab,
local-noc) hands a pod to the fleet's front port, so the pod's ovsdb-server
connects there, with EMOSA as the OVSDB client. For each connection the fleet:

- reads `AWLAN_Node` (serial, node ID, model, firmware) and checks the serial;
- finds or allocates the pod's agent in a persistent registry:
  - an AL MAC derived from the serial (locally administered);
  - a port from a range;
  - a 1905 interface (`emN`, a macvlan whose MAC is the AL);
- writes the agent's configuration and starts `emosa-agent@<serial>`;
- writes `AWLAN_Node.manager_addr` to that port, and closes the session.
  `cm` moves to a new manager address only once disconnected.

The same pod always gets the same agent back, even after a reboot, a fleet
restart or the loss of the registry, because its AL comes from its serial.
Pods are isolated: one process and one state directory per pod, so a fault in
one pod or its agent never reaches another. Admission is either every pod
handed over (`"admit": "*"`) or a list of serials. The controller is set per
fleet, not per pod.

## 2. The translation (`emosa.opensync.easymesh_view`)

This module is pure, with no I/O and no clock. It has two steps.

**OVSDB → view.** `device_view(rows)` reads only the pod's State tables, so
what the controller hears is what the pod's managers actually applied:

| OpenSync | EasyMesh / 1905.1 |
| --- | --- |
| `AWLAN_Node.serial_number`, `model`, `firmware_version` | Device Inventory. The serial also derives the agent's AL MAC (fleet) |
| `Wifi_Radio_State.mac` | Radio unique identifier (RUID) |
| `Wifi_Radio_State.freq_band`, `channel`, `tx_power` | Operating class (2.4G → 81), channel, maximum EIRP |
| `Wifi_VIF_State` with `mode=ap`, `enabled`, a `mac`, listed in its radio's `vif_states` | A BSS: BSSID = VIF MAC, SSID |
| `Wifi_VIF_State.multi_ap` | BSS Configuration Report flags: `backhaul_bss` → 0x80 (backhaul), otherwise 0x40 (fronthaul) |
| `Wifi_Associated_Clients` with `state=active`, listed in the VIF's `associated_clients` | Associated Clients of that BSS |
| `Wifi_VIF_State` with `mode=sta` | The pod's Wi-Fi uplink. Kept in the view but not reported yet: the agent is declared Ethernet-attached to EMOSA |

**View → payloads.** Three functions turn the view into what the agent sends:
- `radio_capabilities`: the AP Radio Basic Capabilities. Only the current
  channel is operable, because EMOSA does not move the pod's radio.
- `inventory`: the Device Inventory.
- `topology`: the Topology Response contents, which are the device, its
  interfaces, the BSS Configuration Report, the operational BSSes and the
  associated clients.

**EasyMesh → OVSDB** (`emosa.opensync.pod_profile`):
- A controller's authenticated M2 set becomes one intent and one guarded
  OVSDB transaction.
- The first fronthaul BSS updates or creates the bound VIF (`home-ap-24`).
- With multi-BSS on, further BSSes take the platform's own VIF slots:
  `svc-d-ap`, `svc-e-ap`, `fh`, and `b-ap` with `multi_ap=backhaul_bss`.
  Slot VIFs no longer in the set are removed.
- The operation counts as applied only once the view shows it in State.

The agent (`PodReportSource`) only decides what it represents: the bound
fronthaul, plus the managed slot BSSes when multi-BSS is on. It does no
translation of its own. The translation is tested on a real OpenSync 6.6.1.0
pod's rows (`tests/fixtures/opensync/pod-6.6.1-hwsim-tables.json`,
`tests/test_easymesh_view.py`).

## 3. Plugging into an existing EasyMesh network

EMOSA needs two things from the EasyMesh side:
- an L2 segment where the controller hears 1905.1 multicast, reached through
  one trunk interface (`emlan`, with one macvlan per agent);
- the controller's AL MAC.

It needs nothing from the controller's software. On the prplMesh and RDK
unified-wifi-mesh controllers, the differences are handled by configuration:

| Setting | prplMesh 6.0 | RDK unified-wifi-mesh |
| --- | --- | --- |
| `message_set` | `easymesh-6.1` (default) | `r1` |
| `multi_bss` | optional | needed: RDK configures fronthaul, backhaul, IoT and hotspot BSSes in one M2 set |
| `m2_session` | `distinct` | `shared` (one registrar session per M2 set) |

The reference labs (prplmesh-lab, meta-cmf-bananapi-vcpe) run their
controllers on their own patched kernels. EMOSA joins their controller LAN over
L2 instead of running their stacks in its own VM.

## Limits

- One radio per agent (2.4 GHz, operating class 81) and WPA2-PSK only.
- The pod's Wi-Fi uplink is not represented.
- Sustained AP/STA metrics are not reported, because the pods' OVSDB has no
  qualified measurements. See the run record.
- The fleet front port is plain TCP, as in the lab. A pod that insists on its
  vendor TLS trust needs EMOSA to hold a certificate the pod accepts.
