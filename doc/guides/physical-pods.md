# Physical OpenSync pods in opensync-lab and the EMOSA lab

[Guides](README.md)

Both labs run virtual pods: LXD containers with OpenSync built from source and
`mac80211_hwsim` radios. This guide covers the usual steps for adding a real,
operator-provisioned pod that has been in a drawer for a while. It names no
device and holds no credentials. Keep the plan for a specific unit, and
anything that identifies it, outside the repository, next to its private
qualification files (`~/.config/emosa/pods/<pod>/`, see
[pod qualification](pod-qualification.md)).

## 1. What to expect from a pod that has been offline for years

A pod that has sat unused for years usually still behaves as follows when you
give it an Ethernet cable:

- It takes DHCP on Ethernet, pings the gateway, fetches time over HTTP
  (htpdate style) from the operator's time name, and connects to the
  operator's redirector with TLS.
- It checks the server certificate against the CA bundle in its image. A
  self-signed server gets a fatal `unknown_ca`.
- The operator's cloud asks for a client certificate. The pod presents a
  device certificate from the operator's issuing CA, often with the Ethernet
  MAC as common name. These are commonly valid for decades, so a stale pod
  can still authenticate.
- Over Wi-Fi it only probes for its onboarding backhaul SSID and joins
  nothing else.
- Its firmware, and so its OpenSync version, is as old as its last update.

A passive capture on an isolated segment shows all of this safely: DHCP and DNS
from a host you control, no forwarding, one monitor-mode adapter for probe
requests. Give it internet access only as a separate, deliberate step,
restricted to the pod's MAC and address, with all private destinations
blocked. The capture shows the device certificate (TLS 1.2 sends it in the
clear); its private key never leaves the pod.

A handshake that completes proves that the device is authenticated. It does not
prove that an account owns the pod or that the operator will serve it.

## 2. What to ask the operator's provisioning team

1. **Account and location.** Is the pod claimed? Can it join the location
   of the lab's test gateway? Stage A needs only this.
2. **Firmware.** Upgrade to a current build and report its OpenSync version and
   Wi-Fi chipset and driver. EMOSA's profiles need OpenSync 6.x tables and
   columns (`Wifi_Credential_Config.onboard_type`, `Wifi_VIF_Config.multi_ap`,
   `wds`); older schemas lack them. The driver decides whether the Multi-AP
   backhaul option works.
3. **Engineering access** to that one unit: SSH with your key, installed
   through their device-management path, or a serial console. Stage B needs
   it.
4. **Trust for a lab cloud**, only if 3 is refused: an engineering image that
   accepts a lab CA. Having the cloud write a lab address into
   `AWLAN_Node.redirector_addr` does not help on its own, because the pod
   still checks the lab server against its own CA bundle.
5. **Return path.** Reset and re-provisioning when the tests end.

Do not ask for the operator's CA keys or for server certificates for their
names. Neither is needed.

## 3. Physical attachment

Radios in both labs are simulated, so a physical pod connects by **Ethernet**:

- a physical NIC on the lab host, passed into the lab VM
  (`lxc config device add <vm> podnic nic nictype=physical parent=<if>`);
- inside the VM, attach it to the gateway's LAN segment. The `lan-p*` bridges
  filter VLANs, so the port takes the same PVID as the gateway's LAN port
  (`bridge vlan show`). With a wrong PVID, nothing passes;
- no other cable on the pod, so that its uplink is unambiguous.

The pod keeps probing for its onboarding SSID over the air. Nothing in the lab
answers.

## 4. opensync-lab

**Stage A: the operator's cloud manages the physical pod next to the virtual
gateway.** If the gateway is on the same operator cloud environment (see
`deploy-mvx.sh opensync --cloud plume`) and the pod joins the gateway's
location, the pod comes up as a wired extender: Ethernet uplink, no GRE.
This needs no lab changes. Record the firmware version, radios and channels
the cloud reports.

**Stage B: local-noc manages the physical pod.** local-noc, the lab's cloud
stand-in, speaks plain TCP. Virtual pods reach it because their image is built
for it. A physical pod needs one of these:

- with shell access, set `AWLAN_Node.redirector_addr` to local-noc as the
  gateway scripts do. If `cm` refuses `tcp:` targets, run local-noc behind TLS
  with a lab CA and add that CA to the pod's trust bundle. The pod keeps its
  device certificate as client certificate. local-noc trusts the operator's
  issuing CA or pins the device certificate. Keep the original values so you
  can restore them;
- an engineering image that trusts the lab CA.

A relay between the pod and the real cloud is not an option: it would need a
server certificate that the pod trusts for the operator's names.

Lab changes for Stage B stay additive and default-off:

- an optional TLS listener in local-noc, with its own CA and client-CA
  settings;
- mesh orchestration for Ethernet-uplinked extenders;
- a deploy step that attaches the NIC, sets the PVID and marks the pod as
  external. Lab scripts never restart, reset or reflash an external pod.

## 5. EMOSA lab

EMOSA takes a physical pod the same way it takes a virtual one. The pod's
`manager_addr` points at the fleet's front port, the fleet admits the serial,
and an agent presents the pod to the EasyMesh controller. Do these in order:

1. **Qualify first.** Run the read-only [pod qualification](pod-qualification.md)
   against the upgraded pod, reached through Stage B. Keep the results private.
2. **Pod profile.** Write `src/emosa/profiles/<model>-<opensync version>-v1.json`
   from the qualified schema and inventory: radios, bands, interface names,
   BSS limits, Multi-AP support. Take radio limits (maximum EIRP, operating
   classes) for the pod's regulatory domain from the model's documentation,
   never from observed channels ([radio capabilities](radio-capabilities.md)).
3. **TLS on the front port.** The fleet listens on `ptcp:` today
   (`schemas/fleet-config.schema.json`). A physical pod needs a `pssl:` front
   port: a server certificate from the lab CA that the pod trusts, plus
   client verification against the operator's issuing CA or a pin. Agents stay
   on local `ptcp:127.0.0.1`. This is an adapter change: schema, fleet, kit
   and spec.
4. **Handover.** The manager, local-noc or the operator's cloud, writes
   `manager_addr` and ends the session. `cm` ignores a new address while it is
   connected.
5. **Data plane** (see [the data plane contract](../architecture/data-plane.md)):
   - Ethernet backhaul first. The pod's uplink goes into the EasyMesh
     gateway's LAN, and the agent reports an IEEE 802.3 backhaul;
   - option 2 (GRE to a GTP) over the air needs a real AP next to the GTP: a
     physical Wi-Fi adapter given to the GTP container, and the correct
     regulatory domain (§6). The pod gets the pod-backhaul SSID as
     a `gre` credential, through the manager;
   - option 1 (Multi-AP backhaul) needs the pod's platform to report
     `multi_ap` in `Wifi_VIF_State`. Test it on the upgraded pod. The
     opensync-lab patch covers only the cfg80211 platform on hwsim.

## 6. Guards

- The reference RDK and prpl labs stay in their own VMs. EMOSA reaches their
  controllers over an L2 link, as it does for virtual pods.
- The regulatory domain is kernel-wide. A physical radio needs the correct
  country: set it deliberately on the host or VM that owns the radio, check
  that the hwsim pods still start, and never set it from a container.
- Pod identifiers, certificates, captures and PSKs stay in the private
  directory. Commits and evidence refer to the pod by a lab name.
