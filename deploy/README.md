# Reference deployment and qualification

The runnable local simulator uses the same session/mapping code in unprivileged
processes. It has been tested on the inspected Ubuntu 22.04 host and in the
dedicated Ubuntu 24.04 nested-LXD environment: 69 unit and 13 OVSDB tests passed,
and VM-driven component provisioning/lost-reply scenarios completed. See
[the retained runtime evidence](../doc/evidence/peer/qualification-summary.json).
Full wire/hardware deployment remains unqualified. `images.lock.json` records
the base image and its retained split export; wire/physical procedure reruns
remain pending. A newer, separately scoped
[secure-fleet reproduction](reliability/README.md) now supplies a retained installed
runtime image and clean TLS/4–32-pod/recovery exercises. Use that workflow for
priorities 1–4; the older numbers above describe the original deployment evidence.

## Dedicated VM and inner system containers

On the existing LXD host, `bash deploy/create-vm.sh` creates only `emosa-lab`.
It refuses to replace an existing instance. Keep the host daemon outside all
application containers. Copy this source tree and its lockfile to the new VM.

Inside that VM, install a selected LXD snap revision, record `snap list lxd`,
hold its refresh during the experiment, and initialize its **new** daemon with
`lxd init --minimal`. Record the resulting storage pool/profile and guest package
inventory. Then run `bash deploy/setup-inner.sh` from the copied source tree.
The supplied profile expects the minimal daemon's `default` storage pool.

The setup creates:

- `em-controller` and `emosa`, unprivileged Ubuntu 24.04 LXD system containers.
- `em-protocol`, an isolated bridge with no IP addressing or forwarding to pods.
- `em-mgmt`, a separate routed management bridge.

Never bridge `em-protocol` to the pod-facing network. `em0` is the future explicit
packet endpoint interface; real packet delivery, multicast and unicast remain P0
qualification work. The existing controller command reports that gate and does
not announce an invented protocol. Application services need no `CAP_NET_ADMIN`.
The current semantic adapter needs no raw capability either; only future actual
packet endpoint processes should receive `CAP_NET_RAW`.

Install a pinned `uv` executable in each Python container. Copy the source to
`/opt/emosa`, then run `uv python install 3.13.7` and `uv sync --frozen` there.
Ubuntu's default Python is not the selected runtime. Verify all CLI help and
unit checks inside each container. Build OVSDB tools explicitly with
`bash scripts/build-ovsdb.sh` for a component simulator hosted there; this needs
a qualified C compiler, make, libc/OpenSSL headers and pkg-config, but no switch daemon.
Capture exact packages using `dpkg-query -W`, compiler versions, build logs and
binary hashes. Do not treat this package list as already validated for Ubuntu 24.04.

For a long-lived adapter, create an `emosa` service user, private mounted secret
directory `/run/emosa-secrets`, writable state directory `/var/lib/emosa`, and a
validated `/etc/emosa/adapter.json`. The supplied systemd unit runs `emosa serve`
and restricts its Unix socket. Use explicit simulator endpoints per configured
pod; no endpoint discovery or physical network scanning is performed.

The VM-local runner invokes installed endpoint commands through the inner daemon:

```sh
emosa-lab --execution lxd run scenarios/component-bss-change.json --backend ovsdb-sim
emosa-lab --execution lxd report RUN_ID --format json
emosa-lab --execution lxd watch RUN_ID
emosa-lab --execution lxd inspect RUN_ID --operation OPERATION_ID
emosa-lab --execution lxd compare RUN_A RUN_B --format html
```

These commands expand to `lxc exec em-controller -- ... status` and
`lxc exec emosa -- ... emosa-lab ...`; the scenario is pushed to the EMOSA
container and its results stay under `/var/lib/emosa/lab`. The simulator manager
is a separate process and its database is disposable. Running a component lab
scenario does not start the future wire controller. Default `--execution local`
is the developer test mode and makes that scope explicit in the report.

Retain VM and container image exports with SHA-256 digests. Record installed
package/snap/runtime versions, actual topology and effective configs with each
reference qualification. No `opensync-native` container is enabled until N01–N04
pass. No Docker or Compose dependency is used.

## Physical and independent-controller gates

For Linux Wi-Fi component experiments, see the optional
[mac80211_hwsim AP and wpa_supplicant LXD client](hwsim/README.md). It runs only
inside the dedicated VM and is separate from the current OVSDB simulator manager.
The physical observer needs a real Wi-Fi interface; hwsim does not provide an RF
link to actual pods.

For an existing-controller baseline before inserting EMOSA, use the
[native controller–agent harness](peer-baseline/README.md). It owns four separate
unprivileged containers and three hwsim PHYs, removes setup Ethernet, and checks
wired/wireless onboarding and independent client traffic. Run it separately from
the two-radio smoke harness; both require exclusive ownership of hwsim in the VM.

Copy `doc/project/emosa-input-manifest.example.json` to the ignored local manifest and
complete M0 with actual pod/build, schema, trusted endpoint/direction, resource
bindings, writer controls and recovery/client evidence. Hardware configuration
currently rejects writes because no such target has been qualified. Do not use
a simulator profile against a physical endpoint.

Once P0 procedures work, the evaluator attaches its independent controller to
the isolated protocol bridge, stops the bundled controller, records its exact
build/scope and captures at an independent bridge point. This future X1 path
must use actual frames. There is no internal API substitute for third-party
interoperability. Wired and wireless pod management need separate qualification.

The [independent prplMesh candidate](peer/README.md) now has a tested startup
path and controller discovery captures delivered to the EMOSA container. Its
controller-only helper requires one hwsim radio, and both native controller and
helper abort on shutdown. No EMOSA discovery/onboarding or physical-pod
interoperability is established by that baseline.

The [OVSDB/hwsim integration](radio-manager/README.md) exercises semantic EMOSA
changes through an independent hostapd/nl80211 manager with both client types.
It reuses stopped native-baseline resources and does not enable wire or pod gates.
