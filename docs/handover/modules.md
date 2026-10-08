# Module guide: EMOSA in C

Every source file of `c/src`, by layer (the design's §4.1: wire, model, pod scopes),
with the design module it implements, the Python reference module it mirrors, and what
tests it. "Vectors" are `spec/conformance/*.json`, replayed by `c/tests/vectors.c`;
"units" are `c/tests/units.c`; "box" are the lab in a box's scenarios
([spec/box-scenarios.md](../../spec/box-scenarios.md)); "fuzz" the libFuzzer targets in
`c/fuzz`. Coverage per file is in [c/QUALITY.md](../../c/QUALITY.md) §3.

The reference (`src/emosa`) is the oracle: a change of behaviour starts in the
specification and the reference, the vectors are regenerated from the reference, and
the C follows until `emosa-vectors` passes again ([testing.md](testing.md)).

## Programs

| File | What | Reference | Tests |
| --- | --- | --- | --- |
| `agent.c` | `emosa-agent-c CONFIG.json`: one pod's agent. Configuration (schema `agent-config`), the poll loop over the pod's OVSDB connection, the 1905 socket and the MQTT broker, the onboarding session's states, the dispatch of every received message, the counters and the status file (schema `agent-status`) | `emosa.agent.pod`, `emosa.wire.onboarding` | box (every agent scenario), the labs |
| `fleetd.c` | `emosa-fleet-c serve\|list\|forget`: the front port's poll loop, one state machine per arriving pod, `systemctl` as reaped children | `emosa.agent.fleet` (main) | box `fleet-*`, fuzz `fleet` |
| `gtpd.c` | `emosa-gtp-c setup\|lease\|reconcile\|list`: the GTP's command line, dnsmasq's lease hook | `emosa.gtp` (main) | box `gtp`, fuzz `gtp` |
| `forwardd.c` | `emosa-forward-c CONFIG`: spec 3.1's forwarder on a gateway, the fleet's front and agent ports on its `advertise` address spliced to loopback; inert without `"forward": true` | none (deployment plumbing; the labs' containers use LXD proxies) | ctest `forward` |

## Wire

| File | What | Reference | Tests |
| --- | --- | --- | --- |
| `cmdu.c` | the IEEE 1905.1 envelope: encoding, fragmentation at TLV boundaries, reassembly with its budgets, TLV lists | `emosa.wire.cmdu` | vectors `cmdu.json`, fuzz `cmdu` |
| `ethernet.c` | the agent's packet socket (`AF_PACKET`, EtherType `0x893A`) on its own interface | `emosa.wire.ethernet` | box |
| `autoconf.c` | AP-Autoconfiguration: Search, the Response's admission (spec §2.5), M1 and M2 framing, the Early AP Capability Report's TLVs | `emosa.wire.autoconfiguration` | vectors `onboarding.json`, box `onboard`, `refuse` |
| `wsc.c` | WPS 2.0.10 M1 and M2: building M1, authenticating and decrypting M2 (OpenSSL), the M2 set's BSS settings | `emosa.wsc`, `emosa.wsc_messages`, `emosa.wsc_radio` | vectors `onboarding.json`, the hostap fixture, fuzz `wsc` |
| `lifecycle.c` | when an onboarding attempt starts and ends, the agent's own renewals, the re-announcement of clients | `emosa.wire.onboarding.OnboardingRecovery`, `emosa.agent.renew` | vectors `session-timing.json`, box `no-m2`, `silent-controller`, `unserved-pod` |
| `early.c` | the Early AP Capability Report's delivery: retries with new MIDs until its Ack | `emosa.wire.coordinator.ReportCoordinator` | vectors `early-report.json`, box `early-report` |
| `control.c` | the provisioned agent's control procedures: channel preference and selection, channel scan, the Multi-AP policy's receipt, client steering requests, unassociated station queries, capability queries | `emosa.wire.channel`, `.reporting_policy`, `.steering`, `.unassociated` | vectors `control.json`, box `answers`, `channel-*`, `steering-refusals`, fuzz `control` |
| `reporting.c` | the Multi-AP policy kept, the due-report schedule, AP Metrics Query answers and periodic AP metrics from the pod's statistics | `emosa.wire.reporting_policy`, `emosa.wire.pod_metrics` | vectors `metrics.json`, box `metrics`, fuzz `control` |
| `bhsteer.c` | Backhaul Steering across sessions: the request, the move, its answer once the pod shows it | `emosa.wire.backhaul_steering` | vectors `backhaul-steering.json`, box `backhaul-steering*`, fuzz `control` |

## Model

| File | What | Reference | Tests |
| --- | --- | --- | --- |
| `ovsdb.c` | the agent's OVSDB session (RFC 7047): the pod dials in, the schema is read, the tables monitored, transactions sent; its framing | `emosa.opensync.session`, `.schema` | box; the rows it yields through fuzz `rows` (its framing is not fuzzed yet) |
| `jsonrpc.c` | RFC 7047 JSON-RPC framing shared by the agent's session and the fleet's front port | `emosa.opensync.session` (framing) | fuzz `fleet` |
| `ovs.c` | reading RFC 7047 values (atoms, sets, maps) from rows without the schema | the `ovs` library's row decoding (`emosa.opensync.session`, `.mapping`) | fuzz `rows` |
| `view.c` | the pod as EasyMesh sees it: device, radios, BSSes, stations, uplinks; capability and topology TLVs | `emosa.opensync.easymesh_view`, `emosa.wire.reports` | vectors `translation-northbound.json`, `uplink.json`, fuzz `rows` |
| `stats.c` | the pod's own statistics: a proto2 decoder for the `sts.Report` fields EMOSA uses, counter epochs, freshness, RCPI | `emosa.opensync.stats` | vectors `telemetry.json`, box `telemetry`, fuzz `stats` |
| `mqtt.c` | a minimal MQTT 3.1.1 subscriber: the parser apart from the socket | paho-mqtt in the reference | units `mqtt_parser`, box `telemetry*`, fuzz `mqtt` |

## Pod scopes and their engine

| File | What | Reference | Tests |
| --- | --- | --- | --- |
| `operation.c` | operation states and their legal transitions | `emosa.operations` | vectors `operation-transitions.json` |
| `engine.c` | the operation lifecycle: request, validate, submit, observe, time out; idempotency keys; recovery after a restart | `emosa.reconcile` | vectors `engine.json`, box `adapter-restart` |
| `journal.c` | the durable journal (SQLite): operations, events, ownership, WSC receipts | `emosa.store.Store` | units, vectors `engine.json` |
| `vault.c` | the secret store: its policy (keyed fingerprints, references) over a backend (files) | `emosa.secrets.SecretStore` | units `vault`, `secret_backend` |
| `scope.c` | what the pod scopes read in common | `emosa.opensync.*` | vectors `scope-writes.json` |
| `southbound.c` | the scopes' guarded OVSDB transactions (AP scope, client steering) | `emosa.opensync.pod_profile`, `emosa.opensync.steering` | vectors `translation-southbound.json`, `steering.json`, `scope-writes.json` |
| `scope_ap.c` | the AP scope: an accepted M2 (set) to the pod's VIF, Inet and radio rows per profile | `emosa.opensync.pod_profile.PodBackend` | vectors `translation-southbound.json`, box `onboard`, `multi-bss` |
| `scope_telemetry.c` | the telemetry scope: the pod's MQTT settings and statistics rows | `emosa.opensync.telemetry`, `emosa.agent.telemetry` | vectors `scope-writes.json`, box `telemetry`, `foreign-broker`, `telemetry-broker-restart` |
| `scope_steering.c` | client steering: one window at a time, a queue of eight, kick and close | `emosa.opensync.steering`, `emosa.agent.steering` | vectors `steering-queue.json`, `scope-writes.json`, box `steering*` |
| `scope_watch.c` | the probe watch: the stations the controller asks about, watched for probe requests | `emosa.opensync.probe_watch`, `emosa.agent.probe_watch` | vectors `probe-watch.json`, box `unassociated` |
| `scope_uplink.c` | the uplink (data plane option 1): the switch to the EasyMesh backhaul, its hold, Backhaul Steering's re-pin | `emosa.opensync.uplink`, `emosa.agent.uplink` | vectors `uplink.json`, box `backhaul-*`, `uplink-*` |
| `scope_wired.c` | a wired pod's Ethernet uplink port bridged into br-home (`Connection_Manager_Uplink.bridge`, spec 8.4) | `emosa.opensync.wired`, `emosa.agent.wired` | vectors `scope-writes.json` |
| `channel_store.c` | the accepted channel policy's durable record | `emosa.wire.channel.ChannelPolicyStore` | box `channel-selection` |

## The adapter around the agents

| File | What | Reference | Tests |
| --- | --- | --- | --- |
| `fleet.c` | the fleet: admission, the registry (ports, interfaces, AL MACs), agent configurations, `forget` | `emosa.agent.fleet` | vectors `fleet.json`, `fleet-sessions.json`, box `fleet-*`, fuzz `fleet` |
| `gtp.c` | the GRE termination point: dnsmasq's configuration, a gretap per lease, reconciliation | `emosa.gtp` | vectors `gtp.json`, box `gtp`, fuzz `gtp` |
| `proc.c` | running other programs without a shell (`systemctl`, `ip`), output captured | `subprocess` in the reference | box `fleet-*`, `gtp` |

## Shared

| File | What | Tests |
| --- | --- | --- |
| `common.c` | reason codes, buffers, the allocator (`em_malloc`: abort on failure), bounded strings (`em_copy`, `em_format`, `EM_FORMAT_FIXED`), MAC parsing, file helpers | units, everything |
| `canon.c` | JSON as the reference writes it (`json.dumps` byte for byte, also `indent=2`), hashes, time stamps | units `canon` |
| `jschema.c` | a JSON Schema subset validator for the repository's own schemas | units `schema`, every configuration the box writes |
| `log.c` | the log: stderr, or RDK's logger (`-DEMOSA_RDK_LOGGER`) | `emosa-log-rdk` |
| `version.h.in` | the version and source revision the programs report | CI `c-package` |

## Data the programs read

`schemas/*.schema.json` (configurations, status, journal records: `EMOSA_SCHEMAS`),
`src/emosa/profiles/*.json` (pod profiles: `EMOSA_PROFILES`). Both are installed with
the programs ([c/README.md](../../c/README.md), "Logging, version and package").
