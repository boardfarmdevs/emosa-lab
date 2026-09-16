# Optional mac80211_hwsim radio and LXD station

The model and current OVSDB simulator do not require radios. Use this standalone
harness when testing Linux Wi-Fi association, WPA2 authentication or independent
client observations. The VM kernel supplies two `mac80211_hwsim` radios; separate
unprivileged LXD containers run hostapd and wpa_supplicant. The kernel module is
loaded in the **dedicated VM**, never the workstation or a pod.

This harness is not connected to the EMOSA simulator manager. It does not yet
prove OVSDB-to-radio actuation or real EasyMesh provisioning. Runtime status and
actual execution evidence are recorded separately from static checks.

**Executed:** a clean setup and smoke passed in the dedicated Ubuntu 24.04 VM
with kernel 6.8.0-139-generic and inner LXD 5.21.7 (snap revision 40585).
The station reached WPA2-PSK `COMPLETED`, all three interface-bound pings passed,
and 112 virtual-medium packets were captured. The initial package-started
supplicant conflict and its retest are also retained. See
[qualification evidence](../../doc/evidence/hwsim/qualification-summary.json),
[result](../../doc/evidence/hwsim/result.json) and
[station observation](../../doc/evidence/hwsim/station-status.txt).

## Setup

First create the dedicated `emosa-lab` VM per [deployment](../README.md). Inside
that VM, initialize its own LXD daemon with the `default` storage pool and copy
the repository. Install these VM-local prerequisites explicitly:

```sh
sudo apt-get update
sudo apt-get install -y python3 iw iproute2 kmod tcpdump
# If modinfo mac80211_hwsim fails for the running guest kernel:
sudo apt-get install -y "linux-modules-extra-$(uname -r)"
```

The module/package must match the **running** kernel; record any guest reboot
and resulting kernel before continuing. Initialize/qualify the selected LXD snap
inside this new VM as described in the deployment guide. Do not run `lxd init`
against an existing shared daemon. Retain snap revision, package inventory and
image exports for repeatability. The script uses the pinned container fingerprint
in `deploy/images.lock.json` but resolves Ubuntu packages during setup and records
their versions; this is not a package-locked reproducible image yet.

```sh
# From the repository inside emosa-lab
python3 deploy/hwsim/lab.py check
sudo python3 deploy/hwsim/lab.py setup
sudo python3 deploy/hwsim/lab.py smoke
```

`check` is read-only and fails outside the named VM or when prerequisites are
missing. Mutation commands also require VM root. LXD commands force the local
daemon and default project. Setup refuses existing target names, a preloaded
hwsim module or a previous lab directory. It creates only:

- `em-radio-setup`: temporary package-install network.
- `emosa-radio`: unprivileged profile with a root disk in the VM's default pool.
- `em-radio-ap` and `em-radio-client`: pinned Ubuntu 24.04 containers.
- Two guest hwsim PHYs, moved as whole radios into those network namespaces.

Setup installs hostapd, wpa_supplicant, iw and IP utilities only in its new
containers. Each receives a fresh random WPA2 PSK through a private file, not a
command-line argument. Setup Ethernet is removed before the smoke test. The
client's ping is explicitly bound to `wlan0`, using documentation-range static
addresses `192.0.2.1/30` and `192.0.2.2/30`. This first smoke does not exercise DHCP.

## Evidence and interpretation

`smoke` waits up to 30 seconds for `wpa_state=COMPLETED` on the expected SSID and
WPA2-PSK key management, records `iw link`, sends three interface-bound pings and
captures the virtual medium. It returns nonzero on failed checks. Each execution
retains a private timestamped directory under `.lab/hwsim/` containing:

- Result with explicit `not_evaluated` EMOSA, wire and physical-pod fields.
- Station status, link, traffic output, AP/station logs and `wireless.pcap`.
- Artifact hashes; setup captures kernel, module, LXD, packages and image ID.

Raw credentials and captures are local and ignored by Git. Review any artifacts
before publishing them; the Pages build only consumes curated repository evidence.
For another smoke run, clean up and recreate the harness. Existing daemons are
refused to avoid accidental reuse of state from a prior experiment.

```sh
sudo python3 deploy/hwsim/lab.py cleanup
# Archive .lab/hwsim under another private local name before a fresh setup.
```

Cleanup validates ownership markers before deleting its containers/network/profile
and unloading its hwsim module; evidence stays on disk. An interrupted setup or
ownership mismatch may need manual inspection instead of automated deletion.
Never reload/unload a module in a VM being used by another radio experiment.

## How this advances the actual goal

The next integration is a separate manager binding accepted simulator Config to
hostapd and deriving State from daemon/driver observations. Keep client evidence
independent. Cover wrong credentials, SSID change, reconnect and data-path failure
before treating it as an EMOSA radio component backend. Native OpenSync reuse is
optional and must not delay a ready physical-pod experiment.

For physical-pod acceptance, pass a **real Wi-Fi adapter** through the dedicated
VM to the observer container, or use a separate physical station. Qualify the
specific adapter/driver, namespace permissions and recovery path. hwsim radios
have no RF path to an actual pod. Keep the physical pod firmware and software
unchanged; run the existing read-only pod qualification before any actuation.

Authoritative references:

- [Linux mac80211_hwsim: virtual radios, hostapd/station example and monitor interface](https://wireless.docs.kernel.org/en/latest/en/users/drivers/mac80211_hwsim.html).
- [Linux iw documentation](https://wireless.docs.kernel.org/en/latest/en/users/documentation/iw.html).
- [Upstream wpa_supplicant](https://w1.fi/wpa_supplicant/).
- [Canonical LXD network devices](https://documentation.ubuntu.com/lxd/latest/reference/devices_nic/).

For semantic EMOSA Config → actual radio → independent client checks, use the
[OVSDB/hwsim integration](../radio-manager/README.md). It reuses the native
baseline topology and must run separately from this standalone smoke lab.
