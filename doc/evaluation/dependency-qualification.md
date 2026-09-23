# Dependency qualification

The recorded development host is Ubuntu 22.04.5 x86-64, with CPython 3.13.7,
uv 0.11.17 and LXD client/server 6.9. The pinned Ubuntu 24.04 image fingerprints
in `deploy/images.lock.json` were resolved from the Ubuntu LXD remote. The later
[dedicated-VM evidence](../evidence/peer/qualification-summary.json) records Ubuntu
24.04 component runtime compatibility, installed packages and a retained base
image export. Final runtime image exports and full procedure reruns remain
pending. Only the project-owned `emosa-lab` VM and its inner containers were
configured for this experiment.

## Open vSwitch experiment

The maintained upstream `ovs==4.0.0` Python package is used for JSON-RPC,
connections, echo, reconnect and OVSDB datum/schema validation. One bounded
worker per session isolates its synchronous interface. No alternative client or
custom full OVSDB protocol stack was needed. See the upstream
[OVSDB connection documentation](https://docs.openvswitch.org/en/stable/ref/ovsdb.7/)
and [RFC 7047](https://www.rfc-editor.org/rfc/rfc7047.html).

The locally built `ovsdb-server` and `ovsdb-tool` are version 4.0.0. Source archive
and binary digests are in `doc/evidence/bootstrap.json`. Building just these
targets first requires the upstream `BUILT_SOURCES`; the supplied build script
generates them before linking. There is no `make install`, system daemon or
switching datapath. The original bootstrap binary had TLS disabled; those hashes
remain historical evidence. The current build enables OpenSSL and requires
`libssl-dev` for the [secure-fleet tests](../guides/secure-fleet.md). New host and
clean-runtime binary hashes are retained with that experiment.

The integration suite checks schema retrieval, initial monitor snapshots,
incremental update/delete/reference handling, set/map/optional values, guarded
transactions, row counts, reconnect, independent manager application, lost
responses, conflicts, resource exhaustion and four independent sessions.
Both dialing via Unix sockets and database-initiated TCP to a listening manager
are exercised. The [connecting-pod demo](../guides/connecting-pod.md) also verifies a Unix
listener in a separate service process. Three narrow upstream compatibility
accommodations are required:

- Python `PassiveStream` interprets `ptcp:HOST:PORT`, unlike the C client's
  `ptcp:PORT:HOST`. The public session configuration retains the latter syntax;
  its adapter converts to the Python form.
- Python 4.0.0 creates a blocking TCP listening socket. The wrapper makes that
  socket nonblocking immediately after creation, preserving the documented
  nonblocking accept behavior and preventing a worker from stalling.
- Unix listeners register socket-unlink hooks through upstream signal handling.
  The wrapper initializes that machinery with the public `add_hook` API on the
  main thread before its worker opens the listener. Otherwise first use in a
  fresh service process raises Python's main-thread-only signal error. Upstream
  preserves existing application signal handlers; no dependency files are edited.

These workarounds live in `src/emosa/opensync/session.py`, with a real
listening-manager regression test. No files in the installed dependency are
patched. Application/connection generations are independent of reusable wire
request IDs; the durable attempt ID is assigned before submission.

The measured schema response was 131,161 decoded JSON bytes for the selected
server build; snapshots depend on cardinality and enabled columns. The initial
16 MiB parser admission/decoded-message budget and 10,000-row cache bound are
engineering resource limits, **not** OpenSync or EasyMesh protocol limits.
Exhaustion marks the observation cache unready and forces full resynchronization.
Larger physical schemas/snapshots and actual pod trust remain unqualified.
Authenticated pod-initiated TLS is now tested using the bounded accept boundary
in `src/emosa/opensync/tls_listener.py`, followed by the upstream OVS stream/session.
It rejects invalid clients before they can replace an active connection, bounds
pending handshakes and avoids outgoing TLS's process-global certificate settings.
The [secure-fleet guide](../guides/secure-fleet.md) describes its explicit bindings,
resource limits and 4/8/16/32-session service workload. This adds a fourth narrow
upstream compatibility boundary without modifying installed OVS package files.

## Provisioning crypto

The separately scoped [WSC component](../protocol/wsc-component.md) selects WPS 2.0.10 and
pins `cryptography==50.0.1` for DH group 5 and AES-CBC. CPython's standard
library supplies SHA-256, HMAC and randomness. The group-5 exchange, KDF,
authentication tags and encrypted settings match two independently generated
hostap 2.11 cases. Negative tests cover malformed TLVs, nonce/MAC binding,
out-of-range/nonmember public keys, altered tags, IV/ciphertext and padding.

[Cryptography 50.0.1](https://cryptography.io/en/50.0.1/hazmat/primitives/asymmetric/dh/)
still supports finite-field DH but deprecates it for removal in a future release.
WPS requires this group; silently substituting ECDH would change the protocol.
The dependency is pinned, its deprecation warnings remain visible, and upgrading
requires requalification. This bounded compatibility result is not a security
audit or completion of P0. The component is not yet wired to an exchange state
machine or pod operations. HMAC used by the operation journal remains separate
from the WSC session keys.

## R0 native OpenSync experiment

The [native manual](../../deploy/native/README.md) records the completed bounded
investigation on Ubuntu 24.04. The pinned OWM/OW/OSW target builds from a fresh
source extraction with two small lab patches and passes 39 selected upstream
units. A C dummy-driver module receives Config through the normal EMOSA OVSDB
boundary and publishes simulated feedback; OWM creates State. Applied and
withheld-feedback checks work with the explicitly scoped native PSK interpretation.

N03 remains blocked: after a database restart EMOSA reconnects and commits a new
Config change, but the surviving native manager produces no new driver callback
or observed change within the deadline. The application native backend stays
disabled. See [the qualification evidence](../evidence/native/qualification.json).
The earlier `protoc-c` failure and initial Kconfig/build logs in `evidence/r0-*`
are retained as history. The current dependency versions, source/Kconfig/patch
hashes, failed assumptions, resource measurements and source rebuild are in
`../../doc/evidence/native`. No physical pod or EasyMesh exchange was exercised.
