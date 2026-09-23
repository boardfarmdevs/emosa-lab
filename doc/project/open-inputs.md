# Open inputs and qualification gates

**Current review: 2026-09-23.** See [current status](current-status.md) for proven
scope and [the roadmap](viability-roadmap.md) for implementation work. This page
lists outstanding inputs; it does not reopen completed acquisition requests or
turn implementation gaps into requests for credentials.

| Gate | Available now | Still needed / affected work |
| --- | --- | --- |
| Protocol references | IEEE 1905.1-2013 and 1905.1a-2014 obtained; EasyMesh 6.1, WPS 2.0.10, IEEE 802.11-2024 and 802.3-2022 inspected; selected public BBF TR-181 2.17 definitions pinned | Remaining exact-edition references and WFA documents/clarifications in the [single acquisition checklist](../protocol/specification-acquisition.md); full normative/profile audit, ESP and width/source review remain unfinished |
| OpenSync container | rev140 opensync-lab source, working pod and retained image; read-only evaluation and [frozen baseline](integration-baseline.md) | Formal qualified transport/profile, resource/security semantics, per-node routing and writer exclusion, native runner/bootstrap and telemetry qualification. These are planned integration tasks, not missing physical-pod credentials |
| Physical pod M0 | Read-only collector and credentials-free TLS/tunnel/Unix examples; operator previously confirmed OVSDB access is possible | Populated private connection file's absolute path, endpoint/authentication trust, named firmware/schema/resource scope, writer controls, management/recovery and independent physical client. No private physical connection file has been supplied |
| Native peer X1 | Patched prplMesh candidate completes bounded simulated-pod discovery/WSC/radio/client proof; lifetime fix and sparse-ESP receipt independently checked | Full selected procedure/reporting acceptance, other platform/controller qualification. Older baseline shutdown defects remain historical, not a blanket blocker on the fixed candidate |
| Deployment/reproduction | Nested-LXD component and secure-service reproductions; selected source/image copies now preserved | Actual OpenSync integrated rerun and complete environment reconstruction; no complete VM snapshot or general release image is claimed |
| ODH | Network-center data lake identified as destination | Ingestion/trust/schema/retention contract; not required for initial warm onboarding |

`qualified` requires current evidence for the declared target and scope. No input
manifest boolean, schema match, image hash or baseline archive enables writes.
The OpenSync simulation schema remains pinned to
`78d8a7194d5e77635877cc456231e7be5cf03d68`; the actual container's newer schema is
a separate prospective profile.

## Specification acquisition versus implementation

The two exact IEEE 1905 documents supplied on 2026-09-22 close their acquisition
request. Their presence does not establish full protocol conformance. Selected
envelope, discovery/WSC and report procedures are implemented and a bounded
native integration passes; complete procedure admission/reporting remains open.

Public BBF definitions permit selected metric conversions while the WFA Data
Elements package remains pending for exact comparison. Do not claim BBF 2.17 is
the acquired DEr3 spreadsheet. Missing telemetry values, actual counter meaning,
association age and final-session completeness require source qualification,
not simply another document download.

The older optional [native R0](../../deploy/native/README.md) experiment retains
its N03 database-restart failure and disabled backend. That result concerns its
own build/profile; it neither qualifies nor disqualifies opensync-lab's different
OpenSync 6.6.1 pod without an experiment.

## Supplying physical connection details

Follow [read-only pod qualification](../guides/pod-qualification.md). Populate a
credentials-free example outside the repository on the machine running EMOSA,
use local secret-file references supported by the loader, and supply only the
populated file's absolute path. Keep credentials out of chat and Git. The formal
collector always produces a draft; writer/resource qualification is a later gate.

For the container integration, SSH/LXD access on rev140 was sufficient for
read-only evaluation and preservation. A dedicated EMOSA transport and trust
binding still need to be designed and tested; shared NAT IP or self-reported pod
serial alone does not authenticate the device.
