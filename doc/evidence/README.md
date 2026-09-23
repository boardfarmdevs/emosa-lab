# Retained evaluation evidence

[Documentation index](../README.md)

The [artifact manifest](manifest.json) records relative paths, SHA-256 digests and
sizes for reviewed evidence. Existing evidence files were moved here without
changing their contents. Dates, source hashes, test counts and paths inside
historical records describe their original executions; use the current guides
for navigation and commands.

| Collection | Entry point |
| --- | --- |
| Ethernet WSC to OVSDB, hwsim and independent clients | [Normal/fault runs, received captures and cleanup](wsc-wire/README.md) |
| Read-only database-backed report coordinator | [Ack/retry, State-derived reports, withdrawal and real Ethernet runs](coordinator/README.md) |
| Capability and topology report components | [Offline, isolated Ethernet and independent field checks](reports/README.md) |
| Bounded controller discovery and WSC exchanges | [Peer/radio binding, replay controls and native compatibility gaps](autoconfiguration/README.md) |
| Authenticated WSC to real owned OVSDB | [Durable operation, duplicate, lost-reply, identity-race and process-crash cases](wsc-provisioning/README.md) |
| IEEE 1905 envelope and packet transport | [Native frame comparison and isolated Ethernet runs](ieee1905/README.md) |
| Authenticated fleet, repeated recovery and clean runtime | [Workloads, measurements, retained failures and image provenance](reliability/README.md) |
| Connecting-pod diagnostic demonstration | [Qualification summary](connecting-pod/qualification-summary.json) |
| Service, two pods and process-crash recovery | [Run summary](service-integration/summary.json) |
| Sole-radio scope and native capture review | [Run summary](onboarding-readiness/summary.json) |
| Semantic EMOSA/OVSDB/hwsim integration | [Run summary](radio-manager/summary.json) |
| Native controller–agent baseline | [Run summary](peer-baseline/summary.json) |
| Isolated native HE-length fix and sole-fronthaul policy | [Candidate build, two live trials and retained failures](native-compatibility/summary.json) |
| Initial independent-controller experiment | [Qualification summary](peer/qualification-summary.json) |
| Standalone hwsim smoke | [Qualification summary](hwsim/qualification-summary.json) |
| Native OpenSync R0 | [Qualification](native/qualification.json) and [application/recovery summary](native/path-summary.json) |
| Retained model/OVSDB scenarios | [Run directories](runs/) and the [interactive explorer](https://boardfarmdevs.github.io/emosa-lab/#evidence) |
| WSC components | [M1/M2 evidence](wsc-messages.json) and [radio interpretation](wsc-radio.json) |
| EasyMesh value components | [Codec, CLI and independent native-capture checks](easymesh-payloads/summary.json) |
| Wi-Fi 6 role inputs through the service | [Two-pod mapping, withdrawal and installed-wheel checks](wifi6-inputs/summary.json) |
| HE/Wi-Fi 6 components and native source review | [Value checks, compatibility findings and limits](he-wifi6/summary.json) |
| Complete synthetic observed topology | [Stable bindings, full graph checks and two-pod service recovery](observed-topology/summary.json) |
| Native counter accounting | [Loss-checked six-session accounting, runtime source provenance and both recovery checks](counter-accounting/README.md) |

These collections have different scopes and include failures. They do not
establish EMOSA wire onboarding, physical-pod qualification or universal peer
compatibility. See the [evaluation guides](../evaluation/README.md) for context.

Do not put raw lab directories, credentials or unreviewed physical captures here.
Follow the [evidence curation procedure](../guides/team-manual.md#173-curate-evidence-instead-of-copying-a-lab-directory).
