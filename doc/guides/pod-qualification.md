# Read-only physical-pod qualification

`emosa qualify-pod` runs on the controller machine. It uses only schema retrieval,
basic OVSDB monitor requests and echo/connection handling. Its session rejects
modifying transactions. It installs nothing on the pod and never changes Config,
State, manager endpoints, cloud settings or firmware.

No actual endpoint or authentication material has been supplied. Physical-pod
connection remains **pending** until the operator populates a private file and
provides its absolute path. EMOSA does not create or infer pod credentials.

The separate [radio-capability input mapper](radio-capabilities.md) now exercises
explicit capacity, operating-class/channel and maximum-EIRP claims in simulation.
This collector does not discover those supported limits automatically. A physical
profile needs reviewed evidence for the actual model, firmware and regulatory
context; observed channel, transmit power or BSS count cannot supply the maximum.
`physical_pod_draft` capability inputs remain blocked by the mapper.

## Private file location and examples

Keep the populated configuration, secrets and collected pod evidence **outside
the repository**, on the machine/user account running EMOSA. For user `rev`, use
`/home/rev/.config/emosa/pods/pod-1/connection.json` and its sibling `secrets/`
directory. If EMOSA runs in a container, these are paths inside that container;
mount/copy the private files there with the same ownership restrictions.

The following checked-in files contain no credentials and match the implemented
`qualification` configuration loader. Choose the transport actually provided by
the lab; the examples are not evidence that the pod supports it:

| Existing authorized access | Example | Private references |
| --- | --- | --- |
| Mutual TLS | [qualification.example.json](../../deploy/qualification.example.json) | `certificate_ref`, `private_key_ref`, `ca_ref` resolve to files below `secret_directory`; replace the placeholder peer pin with a trusted certificate SHA-256 |
| Pod-initiated mutual TLS | [qualification-tls-listen.example.json](../../deploy/qualification-tls-listen.example.json) | `direction: listen`, `pssl:PORT:IPv4`, existing adapter certificate/key, pod CA and trusted pod certificate pin; replace the example loopback address only with the authorized local listening interface |
| Authenticated tunnel to local TCP | [qualification-tunnel.example.json](../../deploy/qualification-tunnel.example.json) | `evidence_ref` identifies a private description of the tunnel's authenticated endpoint binding; tunnel credentials stay with the tunnel tool |
| Private local Unix socket | [qualification-unix.example.json](../../deploy/qualification-unix.example.json) | Existing socket in an owned private directory; no invented password field |

Prepare the directory and copy **one** example. This does not contact a pod:

```sh
umask 077
emosa_private_dir="$HOME/.config/emosa/pods/pod-1"
mkdir -p "$emosa_private_dir/secrets" "$emosa_private_dir/sockets"
chmod 700 "$emosa_private_dir" "$emosa_private_dir/secrets" "$emosa_private_dir/sockets"
# Use the tunnel or Unix example instead if that is the actual authorized path.
cp -n deploy/qualification.example.json "$emosa_private_dir/connection.json"
chmod 600 "$emosa_private_dir/connection.json"
```

Edit that private copy locally: actual endpoint/direction/database, pod ID,
absolute `secret_directory`, trusted pin and any known expected identifiers.
Example hostnames, usernames and all-zero pin values must be replaced. Install
existing certificates/key/trust files locally with mode `0600`; do not paste
their contents into chat. `*_ref` values are **file basenames**, not absolute
paths. `~` and environment variables inside JSON paths are not expanded.

For an SSH/VPN-backed access path, configure that external tunnel using its local
key/password/agent mechanism and verify its remote binding. EMOSA does not create
the tunnel or change the pod. The loader supports the three trust kinds above;
it does not implement an OVSDB username/password login or an arbitrary vendor
authentication flow. If the pod requires another mechanism, qualify that access
path explicitly instead of inserting unsupported JSON fields or disabling TLS.

Validate the private JSON without loading keys or opening a connection:

```sh
uv run python -c 'from emosa.config import load; import sys; load("qualification", sys.argv[1]); print("Configuration shape valid; endpoint/trust still unverified")' \
  "$HOME/.config/emosa/pods/pod-1/connection.json"
```

Only send the populated file's absolute path to the coding agent. No populated
private file has been created by this implementation work.

## Read-only collection

Use the private configuration prepared above. Referenced files must be owned by
the CLI user, regular files, mode `0600`, with no symlinks. For mutual TLS, obtain
the expected peer certificate SHA-256 from the trusted provisioning channel;
do not learn a pin by accepting an unauthenticated connection.

```sh
uv run emosa qualify-pod \
  --connection "$HOME/.config/emosa/pods/pod-1/connection.json" \
  --output "$HOME/.local/state/emosa/qualification/pod-1-first-read"
```

The output directory must be new. It contains `schema.json`,
`draft-profile.json` and `artifact-manifest.json`. Treat identifiers and SSIDs as
private lab information; review/sanitize them before publishing. Wi-Fi keys and
security maps are not selected by the monitor, and certificate/private-key
contents are never placed in the report. Keep the original schema artifact/hash
for future mapping qualification.

The draft reports available firmware/model/serial fields, schema version and
fingerprint, Config/State radio/VIF relationships, active security flags and
candidate configuration representations. Missing columns and unknown fields
remain explicit. Column presence does not prove usable behavior. Expected
identifier mismatches retain the draft and give a nonzero CLI result.

Supported connection preparation:

- Dialing mutual TLS uses the upstream OVS client with mandatory CA validation
  and an explicit peer certificate pin. A pin supplies endpoint identity binding
  because the upstream client disables DNS hostname matching. Minimum TLS is
  1.2; there is no insecure fallback. TLS qualification runs in its own short-lived
  process because upstream certificate file settings are process-global.
- An existing owned private Unix socket can be read locally, for example an
  already configured authorized tunnel. Its remote-pod binding still needs
  qualification.
- A database-initiated listening-manager connection can use an existing
  authenticated tunnel into a loopback listener. Set direction `listen`, endpoint
  `ptcp:PORT:127.0.0.1`, trust kind `existing-tunnel`, and a private `evidence_ref`
  describing the existing trust setup. The report hashes that reference and
  explicitly leaves external tunnel verification pending. The collector creates
  no tunnel or pod endpoint setting.

Listening TLS now uses a bounded EMOSA TLS accept boundary followed by the
selected upstream OVS JSON-RPC implementation. Use the listening example for an
already authorized pod-initiated connection. It requires mutual TLS and the
expected leaf certificate pin before admitting a session. The collector does
not redirect a cloud endpoint, change pod settings or enroll certificates.
The four-pending-handshake/three-second limits and invalid-client checks are
covered by [synthetic transport tests](secure-fleet.md); physical connection
qualification remains pending until the operator supplies the private path.
A remote plaintext TCP endpoint is rejected. Test certificates are generated
only for local regression tests, never for a physical pod.

The draft is always `writable=false`. Remaining M0 work includes physical
identity/trust binding, specific managed resources, existing writer controls and
their reboot/reconnect behavior, shared radio/management dependencies, recovery,
actual manager/security semantics and independent client observations. EasyMesh
6.1 §7.1 provisioning operates on the radio's requested BSS set: the one-BSS
experiment needs a sole existing BSS on that radio or a complete-radio mapping.

The eventual acceptance path remains **real EasyMesh messages → EMOSA → unchanged
physical pod → independent observed behavior**. This collector and the simulator
are preparation and component evidence, respectively.

## Facts needed for controller-facing reports

The [report components](../protocol/reports.md) require the complete actual
interface/bridge/neighbor inventory, RUID/BSSID bindings, capability evidence,
matching Config/State BSS facts and known client association ages. Qualification
must distinguish the virtual agent's adjacency from the physical pod backhaul.
Unknown powered-off, L2-neighbor, MLD/backhaul or other conditional features must
remain pending rather than be recorded as absent.

The pinned upstream `Wifi_Associated_Clients` table has client identities and
security/state fields but no association age. EasyMesh requires seconds since
association, saturated at 65535. Identify an existing authorized pod source or a
qualified observation of the actual association episode and its restart rules.
Time since EMOSA first saw a row cannot substitute. The current read-only draft
collector does not qualify this source; record it among remaining checks. Do
not install a pod agent, alter firmware or fabricate zero to fill the gap.

## Supplementary root-pod/cloud packet capture

An operator-provided passive capture can help establish transport initiation,
endpoints, bootstrap ordering, steady-state traffic and reconnect behavior. It
can also test whether the actual cloud interface uses OVSDB JSON-RPC at all.
Do not assume a cloud connection and the direct OVSDB interface used by EMOSA
are the same path. A root-pod capture may show the root's own management or
traffic forwarded for extenders; identify which is being observed. A paired
extender capture would help establish differences.

Collect on an existing authorized network observation point so the pod remains
unchanged. Include normal startup or naturally occurring reconnect if available;
an outage, reboot, configuration change or cloud redirection requires its own
lab plan. Capture provision is not authorization for EMOSA to change the pod.

Keep these items outside Git, with access restricted to the lab operator:

- The original pcap/pcapng and its SHA-256, with capture interface/location,
  time zone, time synchronization and known capture gaps.
- Pod model/firmware, root-versus-extender role, wired/wireless management path,
  and an event timeline with timestamps. Pseudonymize identities in public copies.
- The actual schema or read-only qualification output, when available. Schema
  requests may be absent from a capture that starts after synchronization.
- Whether payloads are encrypted and whether existing authorized endpoint logs
  provide the corresponding message sequence. TLS captures alone usually expose
  transport behavior, not JSON-RPC bodies. Do not weaken TLS or provide private
  keys merely to make a trace readable.

Where plaintext OVSDB messages are available, useful evidence includes
`get_schema`, `monitor`/other monitor variants, initial table snapshots, update
notifications, transactions, replies/errors and echo handling. Preserve message
IDs, ordering, relative timing, types and row/reference consistency in sanitized
fixtures. Wi-Fi keys, tokens, certificates, device/client identifiers and SSIDs
may require redaction; a capture must be reviewed before publication. Share only
the local file paths through chat, not credentials or raw message bodies.

The next analysis would identify the actual protocol and roles, compare observed
columns and sequencing with the [connecting-pod simulator](connecting-pod.md),
and derive reviewed fixtures or explicit incompatibilities. Replaying recorded
success responses is not a substitute for a stateful simulator, pod qualification
or independent evidence that an EMOSA-driven change actually took effect.
