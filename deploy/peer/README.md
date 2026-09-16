# Independent controller candidate

The pinned prplMesh 6.0.0 build in
[prplmesh.reference.json](prplmesh.reference.json) runs as an **external peer**
inside `em-controller`. It is not linked into EMOSA and adds no prplMesh package
dependency to the adapter. Its upstream revision and 22 lab patches are pinned
separately. This is a candidate for the first integration, not a claim of
certification or interoperability with EMOSA.

The baseline now has real controller-generated topology-discovery frames,
captured on the VM's isolated `em-protocol` bridge and on `em0` in the EMOSA
container. Matching packet bytes establish delivery between those points.
Wireshark independently decodes the frames. **EMOSA has not answered or onboarded
an extender; the controller inventory contains zero agents.** IEEE normative
validation, actual search/WSC exchanges and a physical-pod profile remain pending.

That statement concerns the EMOSA-facing endpoint. The separate
[native controller–agent baseline](../peer-baseline/README.md) now exercises this
peer build against a normal native prplMesh agent, including wired and wireless
onboarding and client traffic. It uses its own topology, explicit BSS policy and
hostap runtime; it does not turn the EMOSA-facing experiment into a pass.

## Prepare the peer

Create the dedicated VM and inner containers using [the deployment guide](../README.md).
Use the base-image fingerprint in `deploy/images.lock.json`. Commands below run
only inside `emosa-lab` or its `em-controller` container. Keep the other lab VMs
and all physical pods separate.

The local build used these two archives from
`/home/rev/git/prplmesh-lab-0913-clean/artifacts/`:

- `prpl-install-nl80211-6.0.0.tar.gz`
- `prpl-runtime-deps-6.0.0.tar.gz`

They are not distributed in EMOSA. To rebuild them, use the recorded build
repository commit and its build instructions; verify the archive hashes before
using a result. Different build bytes require a new candidate profile. The
hostap archive is not needed for this controller-only baseline.

Copy these archives to `/opt/peer-artifacts/` in the VM and check them against
`prplmesh.reference.json`, then copy them to `/opt/` in `em-controller`:

```sh
# Inside emosa-lab, with this repository at /opt/emosa:
python3 - <<'PY'
import hashlib, json
from pathlib import Path
reference = json.loads(Path('/opt/emosa/deploy/peer/prplmesh.reference.json').read_text())
for name, expected in reference['archives'].items():
    assert hashlib.sha256((Path('/opt/peer-artifacts') / name).read_bytes()).hexdigest() == expected, name
print('Pinned archive digests match')
PY
lxc file push --quiet /opt/peer-artifacts/prpl-install-nl80211-6.0.0.tar.gz \
  /opt/peer-artifacts/prpl-runtime-deps-6.0.0.tar.gz em-controller/opt/
```

In the **fresh `em-controller` container**, install the runtime packages and
extract the verified archives. Do not run this installation over another peer:

```sh
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y iproute2 iputils-ping tcpdump ebtables libcap-ng0 \
  libevent-2.1-7 libjson-c5 libnl-3-200 libnl-genl-3-200 libnl-route-3-200 \
  libssl3 liburiparser1 libyajl2
tar -C / -xzf /opt/prpl-runtime-deps-6.0.0.tar.gz
tar -C /opt -xzf /opt/prpl-install-nl80211-6.0.0.tar.gz
ldconfig
```

The retained package inventory records the tested versions. These commands use
the available Ubuntu repositories, so a fresh install must compare its packages
and repeat qualification. Copy `controller.py` and `prplmesh.reference.json`
from this directory to `/opt/emosa/deploy/peer/` in the container.

## Radio and startup

The helper exits before configuring the transport if no radio interface exists,
even in `Multi-AP-Controller` mode. One real `mac80211_hwsim` interface resolves
that check. The mode then skips fronthaul management: the radio stays down, with
no hostapd, wpa_supplicant or wireless client. This is an observed peer-build
constraint, not a requirement for EMOSA or for an EasyMesh controller generally.

```sh
# Inside emosa-lab. Requires the qualified hwsim kernel module and a running peer.
python3 /opt/emosa/deploy/peer/attach-radio.py
lxc exec em-controller -- python3 /opt/emosa/deploy/peer/controller.py start
```

The radio command refuses an existing hwsim module; inspect its owner instead of
unloading someone else's radios. The launcher checks the container, executable
digests, hwsim device and bridge membership. It binds the synthetic controller
AL MAC `02:00:00:e0:00:01` to `br-lan`, whose only member is `em0`. It disables
the UCC listeners and starts only the bus, transport, controller and its helper
under named `emosa-peer-*` systemd units. LXD administration remains in the VM.
Existing processes or loaded units cause an explicit stop before any new start.

Check actual readiness independently of the launcher's return code:

```sh
lxc exec em-controller -- systemctl show emosa-peer-bus emosa-peer-transport \
  emosa-peer-controller emosa-peer-agent -p Id -p ActiveState -p SubState -p MainPID
lxc exec em-controller -- ubus call Device.WiFi.DataElements.Network _get \
  '{"rel_path":"","depth":1}'
mkdir -p /opt/emosa/.lab/peer
timeout --signal=INT 70 tcpdump -i em-protocol -nn -s 0 -c 1 \
  -w /opt/emosa/.lab/peer/controller.pcap 'ether proto 0x893a'
tshark -r /opt/emosa/.lab/peer/controller.pcap -T fields \
  -e eth.src -e eth.dst -e ieee1905.message_type
```

Install `tshark` in the VM if absent. A capture timeout without a frame is a
failed observation, not readiness. The tested parser was Ubuntu's Wireshark
4.2.2 package; its interpretation does not replace IEEE text. To verify delivery,
run another tcpdump on `em0` in `emosa` concurrently and compare captured packet
bytes. Keep the capture setup independent from any future EMOSA packet parser.

Stop only the owned units with:

```sh
lxc exec em-controller -- python3 /opt/emosa/deploy/peer/controller.py stop
```

The controller and helper repeatedly ended shutdown with `SIGABRT`; journal entries are
retained. The launcher reports abnormal shutdown with a nonzero result and
retains failed units. Clean shutdown/recovery is not qualified. Inspect a failed unit before using
`systemctl reset-failed UNIT` for a subsequent experiment; the launcher does not
erase that failure automatically. The VM is stopped after the baseline; start
the existing containers and attach a fresh hwsim radio after a VM reboot.

## Before onboarding

The baseline has **no BSS policy configured**. Empty policy must not become an
implicit radio teardown in an EMOSA trial. Select the supported fronthaul BSS
policy and its private credentials before sending M1; inspect the controller's
actual M2 behavior and validate the complete requested radio scope before any
OVSDB operation. This work still depends on P0 and the qualified mapping.

Controller baseline captures and semantic OVSDB runs are separate experiments.
Their simultaneous presence does not establish causality. The acceptance path
remains real EasyMesh discovery/onboarding and provisioning → EMOSA → unchanged
physical pod → independently observed client behavior.
