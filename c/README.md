# EMOSA in C

The second implementation of the EMOSA adapter (its agent, fleet and GTP),
interchangeable with the Python reference, written with an AI assistant. It is being taken to production
quality so that it can be evaluated in full and owned by a team without access
to the labs (the easymesh-labs
alignment plan (in [easymesh-labs](https://mesh.vcpe.dev/)),
phase 8: the bar, CI with a lab in a box, the basic adapter with the fleet and the
GTP in C, then feature by feature). The bar is [QUALITY.md](QUALITY.md): the coding
standard (CERT C), the gates and where each stands. Until every gate is met it
is not production code; its status file still says `c-lab-prototype`.

What makes it interchangeable with the Python reference (`src/emosa`):
- the **same contract**: the specification (`spec/README.md`) and its
  conformance vectors (`spec/conformance`), which `emosa-vectors` replays;
- the **same interfaces**: the agent configuration
  (`schemas/agent-config.schema.json`), the status file it writes, the pod's
  OVSDB and the controller's 1905 LAN; the fleet's files (its registry and the
  agent configurations, byte for byte) and the GTP's links. Each program has both
  implementations (`emosa-agent-c`, `emosa-fleet-c`, `emosa-gtp-c`); either fleet
  takes over from the other and starts either agent, and the adapter kit installs
  them without Python (`EMOSA_IMPLEMENTATION=c`, `deploy/adapter`).

## Scope and order

| Part | Reference | Vectors | State |
| --- | --- | --- | --- |
| 1905 envelope | `emosa.wire.cmdu` | `cmdu.json` | done |
| WSC M1/M2, onboarding | `emosa.wsc`, `emosa.wsc_messages`, `emosa.wire.autoconfiguration` | `onboarding.json` | done |
| Onboarding attempts, discovery, back-off, renewals | `emosa.wire.onboarding.OnboardingRecovery`, `emosa.agent.renew` | `session-timing.json` | done (`lifecycle.c`) |
| Early AP Capability Report retries | `emosa.wire.coordinator.ReportCoordinator` | `early-report.json` | done (`early.c`) |
| Pod view and 1905 TLVs | `emosa.opensync.easymesh_view`, `emosa.wire.reports` | `translation-northbound.json` | done |
| Control plane (channel, policy, steering, unassociated) | `emosa.wire.channel`, `.reporting_policy`, `.steering`, `.unassociated` | `control.json` | done |
| Link Metric and AP Metrics answers, periodic AP metrics | `emosa.wire.link_metrics`, `.ap_metrics`, `.pod_metrics` | `metrics.json` | done (`reporting.c`) |
| Backhaul Steering across sessions | `emosa.wire.backhaul_steering` | `backhaul-steering.json` | done (`bhsteer.c`) |
| OVSDB writes (M2, steering, uplink, wired uplink, telemetry, probe watch) | `emosa.opensync.pod_profile`, `.steering`, `.uplink`, `.wired`, `.telemetry`, `.probe_watch` | `translation-southbound.json`, `steering.json`, `uplink.json`, `scope-writes.json` | done |
| Operation engine and durable journal | `emosa.reconcile`, `emosa.store`, `emosa.secrets` | `engine.json`, `operation-transitions.json` | done (`engine.c`, `journal.c`, `vault.c`) |
| Client steering queue | `emosa.agent.steering` | `steering-queue.json` | done (`scope_steering.c`) |
| Probe watch | `emosa.agent.probe_watch` | `probe-watch.json` | done (`scope_watch.c`) |
| Pod statistics | `emosa.opensync.stats` | `telemetry.json` | done |
| Agent runtime (config, OVSDB session, Ethernet, status) | `emosa.agent.pod` | live in the RDK lab | done |
| Fleet: the front port, the registry, the agents' units, `forget` | `emosa.agent.fleet` | `fleet.json`, `fleet-sessions.json` | done (`fleet.c`, `fleetd.c`, `jsonrpc.c`, `proc.c`) |
| GRE termination point: dnsmasq's configuration, one gretap per lease | `emosa.gtp` | `gtp.json` | done (`gtp.c`, `gtpd.c`) |
| Secret store interface: the policy over a backend (files now, a platform's secure storage later) | `emosa.secrets` | units | done (`vault.c`) |
| Packaging: logging through RDK's logger, the version, the package's layout and units, the bill of materials; the Yocto recipe (meta-cmf-bananapi-vcpe, opt-in) | | units (`log-rdk`), CI `c-package`, `c-i686` | done (`log.c`, `packaging/`) |

## The agent runtime

`emosa-agent-c CONFIG.json [--profiles DIR]` takes the Python agent's
configuration and writes the same status file, marked
`"implementation": "c-lab-prototype"`. Both are validated against the schemas
(`schemas/agent-config.schema.json`, `agent-status.schema.json`), found in
`EMOSA_SCHEMAS`, else the build's `EMOSA_SCHEMAS_DIR` (`/opt/emosa-adapter/share/schemas`
by default); an invalid configuration is refused. Profiles come from `--profiles`, else
`EMOSA_PROFILES`, else the build's `EMOSA_PROFILES_DIR` (`/usr/share/emosa/profiles`). One owner thread runs a poll
loop over the pod's OVSDB connection (`ovsdb.c`: the pod dials in, as it does to
the Python agent), the 1905 socket (`ethernet.c`: AF_PACKET, ethertype
`0x893A`) and, with telemetry, the MQTT broker (`mqtt.c`); the pod state is
refreshed every 0.5 s under a 1.5 s lease.

It keeps the Python agent's state directory, file for file, so either can take
over a pod from the other: the journal (`journal.db`: operations, events,
ownership, WSC receipts), the secret store (`secrets/`, keyed fingerprints), the
kept Multi-AP policy (`reporting-policy.sqlite`), the accepted channel policy
(`channel-policy.sqlite`), the uplink target (`target.json`) and the telemetry
scope's records.

Scopes, as in the Python agent: the AP (the M2's BSSes), telemetry (the pod's
statistics over MQTT, with them AP metrics, probe requests and the Unassociated
STA Link Metrics answer), client steering (a queue of eight, one window at a
time), the probe watch, and the uplink (the pod's EasyMesh backhaul station,
reported in the Topology Response and the Backhaul STA Capability Report, and
moved by Backhaul Steering, whose move survives a session renewal), or for a wired pod
its Ethernet uplink port bridged into `br-home` (`scope_wired.c`, spec §8.4).

Checked in the RDK lab (`docs/concepts/rdk-lab.md`), in place of the
Python agent: onboarding and the journal taken over from Python, telemetry,
periodic AP metrics, the probe watch, the uplink on the EasyMesh backhaul,
Backhaul Steering (the move and its `0x801A` answer), and the lab's room suite
with both pods on C and with one pod on each implementation (the record
`docs/records/evidence/rdk-lab/README.md` §3).
The reference workload (`deploy/opensync-lab`, `lab.sh workload`) passed alike
with every agent on Python, on C, and mixed
(`docs/records/evidence/opensync-lab-proof`).

Known differences from the Python agent:
- the report source: Python correlates a report with its source by a token
  (instance, epoch, database generation and revision); C by the database
  generation and revision it last refreshed. The channel policy's `context`
  differs accordingly (C: the journal's process ID and the generation);
- `format: date-time` in the schemas (operation and event records, the status): C
  checks it (RFC 3339); the reference's jsonschema checks it only with the optional
  `rfc3339-validator`, which it does not install, so it accepts any string there. Both
  write RFC 3339 timestamps, so only a record altered on disk is judged differently;
- a journal operation record that is not valid against its schema (one altered on
  disk): the C skips it as it reads the journal and logs it; the reference raises;
- the controller-side tools and the lab are in Python only. The adapter kit
  installs either implementation of every program; `EMOSA_AGENT` in
  `/etc/default/emosa` (every pod) or `/etc/default/emosa-POD` (one pod) picks the
  one `emosa-agent@POD` runs (in the RDK lab: `lab.sh agent POD python|c`).

## Logging, version and package

**Logging** (`log.c`). Every message has a level and the reference's logger name
(`emosa.agent.uplink`, `emosa.fleet`, ...). By default it is one line on stderr,
`LEVEL component: text`, which systemd's journal keeps. Built with
`-DEMOSA_RDK_LOGGER=ON`, the programs log through RDK's logger (rdk-logger:
`rdk_logger.h`, `librdkloggers`) instead: module `LOG.RDK.EMOSA`, its level from
`debug.ini` (`LOG.RDK.DEFAULT` unless the file names the module), each program into
its own rolling file in `EMOSA_RDK_LOG_DIR` (`/rdklogs/logs/`): `EMOSAFleetLog.txt`,
`EMOSAGtpLog.txt`, and per agent `EMOSA_<pod>.txt` (`EMOSAAgentLog.txt` for a pod name
longer than 21 characters), at most `EMOSA_RDK_LOG_MAXCOUNT` files of
`EMOSA_RDK_LOG_MAXSIZE` bytes each (2 of 256 KiB). No change to the image's
`log4crc` is needed (`rdk_logger_ext_init`). A process opens its own file, so no two
roll the same one. The fleet's `list` and `forget` and the GTP's `list` answer on the
terminal (stderr).

**Version.** `--version` on each program prints the release (the reference's, from
`pyproject.toml`, or the adapter kit's `VERSION`) and the source revision (`git
describe`, `+dirty` for a changed tree; a distribution passes `-DEMOSA_REVISION`); the
programs' start messages carry both.

**The package's layout** (`-DEMOSA_INSTALL_DATA=ON`, as the Yocto recipe builds it):
the four programs in `bin`; the schemas and pod profiles in `EMOSA_SCHEMAS_DIR` and
`EMOSA_PROFILES_DIR`; the units `emosa-fleet.service`, `emosa-agent@.service`,
`emosa-gtp.service` and `emosa-forward.service` (`packaging/systemd`) in
`EMOSA_SYSTEMD_UNIT_DIR`; the agent's
link helper in `libexec/emosa`; `/etc/default/emosa` (`EMOSA_TRUNK=emlan` with
`EMOSA_BRIDGE=brlan0`: the helper makes the trunk a veth pair into the gateway's LAN
bridge); the example configurations and the bill of materials
(`emosa-c.spdx.json`) in `share/emosa`. The fleet and the GTP are inert until their
configuration exists (`ConditionPathExists`); the fleet enables an agent's unit for
each pod handed to it. The GTP's unit runs `EMOSA_DNSMASQ`. The forwarder,
`emosa-forward-c`, is spec 3.1's forwarder on the gateway itself: with `"forward": true` in
the fleet configuration it exposes the front port and the agent ports, which listen on
loopback, on the `advertise` address (the gateway's LAN address), each connection spliced to
`127.0.0.1` on its port; without it, it exits at once.

**Where the configuration and state go** (CMake, written into `/etc/default/emosa` for the
tools that configure EMOSA): the units read `EMOSA_FLEET_CONFIG`, `EMOSA_AGENT_CONFIG_DIR`
(the fleet configuration's `config_dir`) and `EMOSA_GTP_CONFIG`; a fleet configuration
names `EMOSA_STATE_ROOT` as its `state_root` and `EMOSA_RUN_ROOT`, a RAM disk, as its
`run_root`, where each agent writes its status once a second (spec §6). By default
`/etc/emosa-fleet.json`, `/etc/emosa`, `/etc/emosa-gtp.json`, `/var/lib/emosa` and no run
root. A gateway keeps the configuration and state on its persistent storage, which survives
an image upgrade, and the status in RAM: meta-cmf-bananapi-vcpe's recipe builds with
`/nvram/emosa/fleet-config.json`, `/nvram/emosa/agents`, `/nvram/emosa/gtp-config.json`,
`/nvram/emosa/state` and `/run/emosa`.

**The agents' network namespace** (CMake `EMOSA_NETNS`, written into `/etc/default/emosa`;
empty by default). Each agent's 1905 interface carries the agent's AL MAC. On a gateway whose
EasyMesh controller runs alongside, that controller must not find those MACs on its own
interfaces: RDK's em_ctrl takes an agent whose AL MAC is the MAC of a local interface for its
co-located agent, and with EMOSA in the gateway it named a pod's agent so; its native
backhaul steering then had the wrong root and measured no candidate (rdk-1004, 5 October
2026). With `EMOSA_NETNS` the link helper puts the agents' trunk end and interfaces in that
namespace (the bridge port stays in the gateway's), and each agent opens its socket there and
returns to its own namespace for its pod connection and broker. meta-cmf-bananapi-vcpe's
recipe builds with `emosa`.

**Bill of materials** (`packaging/sbom.py`, SPDX 2.3, JSON): every file of this
repository that goes into the programs or the package with its SHA-1 and SHA-256, the
libraries they link (versions from pkg-config where it was built), and the programs the
units run, each with its license; EMOSA's own is Apache-2.0 ([LICENSE](../LICENSE)), also
installed with the package (`share/doc/emosa-c`).

## Build and check

```sh
cmake -S c -B c/build -DEMOSA_STRICT=ON && cmake --build c/build && ctest --test-dir c/build
```

`ctest` runs `emosa-vectors spec/conformance`, `emosa-units`, the RDK logger
backend against a stub of rdk-logger (`emosa-log-rdk`) and the fuzz targets over their
seeds. The sanitizer and analyzer runs are in [QUALITY.md](QUALITY.md) §4; CI runs all
of them (`.github/workflows/checks.yml`, jobs `c`, `c-analyzer`, `c-fuzz`, `c-cert`),
the same on 32-bit x86, the gateway images' target (`c-i686`), and the package's
layout (`c-package`).

Needs cJSON, OpenSSL (libcrypto) and SQLite 3, all in the RDK-B images.
