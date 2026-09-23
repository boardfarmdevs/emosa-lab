# Protocol specifications and components

[Documentation index](../README.md)

| Document | Scope |
| --- | --- |
| [Public BBF metric definitions](bbf-data-elements.md) | Pin USP/CWMP definitions, encode explicit units, reject unavailable/overflowing values and identify remaining source gaps |
| [Native sparse ESP reception](native-ap-esp.md) | Validate optional AP service fields and reproduce a controller parser fix |
| [Specification inputs](protocol-inputs.md) | Authoritative sources, proposed editions and selected procedure scope |
| [Acquisition checklist](specification-acquisition.md) | Required, obtained and pending external documents |
| [IEEE 1905 envelope implementation](ieee1905-envelope.md) | Obtained base/amendment, audited frame/TLV rules, bounded reassembly, native vectors and isolated Ethernet checks |
| [Controller discovery and WSC exchanges](autoconfiguration.md) | Search/M1 construction, Response/M2 binding, lifetime/replay controls, native compatibility findings and beginner exercise |
| [Capability and topology reports](reports.md) | Restricted Early Report and Topology Response, facts/freshness checks, offline and Ethernet exercises, native compatibility findings |
| [Read-only report coordinator](report-coordinator.md) | Real database source, Topology Query loop, Early Report Ack/retry, source withdrawal and isolated Ethernet reproduction |
| [Discovery before reporting](discovery-session.md) | Bounded Search/Response lifecycle, required-field diagnostics, reconnect invalidation and read-only topology |
| [Authenticated WSC to durable operation](wsc-provisioning.md) | Independent hostap M2 drives real owned OVSDB; atomic receipt, duplicate, lost-reply, identity-race and process-crash cases |
| [Ethernet WSC to observed Wi-Fi](wsc-wire-radio.md) | Actual packet receiver, authenticated operation, real OVSDB, separate hwsim manager and client evidence; synthetic peer, native admission pending |
| [IEEE media input review](ieee-media-review.md) | Verified 802.11-2024 and 802.3-2022 PDFs, selected clauses, corrections and Ethernet edition gap |
| [Protocol matrix](protocol-matrix.json) | Rules, sections, implementation evidence and unresolved procedure requirements |
| [EasyMesh value components](easymesh-payloads.md) | Service, radio identity, Operational BSS and profile values; offline CLI, exact references and independent native-capture checks |
| [Profile readiness and feature values](profile-readiness.md) | Inspect mandatory-function gaps, conditional report obligations, AP/Profile-2/Advanced capability bits and counter-unit decisions |
| [Technology and Device Inventory mapping](../guides/technology-inventory.md) | Selected technology codecs, explicit synthetic input mapping and HE conversion limits |
| [HE MCS and Wi-Fi 6 capability values](../guides/he-wifi6.md) | Inspect direction/width/role fields, reproduce a native negative case and understand remaining report/peer gaps |
| [Wi-Fi 6 role inputs through the service](../guides/wifi6-inputs.md) | Map explicit AP/STA capabilities, inspect separate readiness, and test two-pod withdrawal/isolation/crash recovery |
| [WSC cryptographic component](wsc-component.md) | Bounded cryptography, independent vectors and validation limits |
| [WSC M1/M2 payloads](wsc-messages.md) | Payload construction, authentication and required-field checks |
| [WSC radio interpretation](wsc-radio.md) | Authenticated BSS roles, teardown and complete-radio admission |
| [Native simulated-pod onboarding](native-onboarding.md) | Real controller discovery/WSC through EMOSA, independent radio manager and client traffic |
| [Sustained operation](sustained-operation.md) | 15-minute operational recovery, reporting gaps and full acceptance criteria |
| [Final session statistics](final-session-statistics.md) | Implemented sender, guarded handoff and remaining measurement qualification |
| [Station-removal observation](station-removal-observations.md) | Raw final kernel records, radio reasons and EasyMesh leave correlation |
| [Station counter accounting](station-counter-accounting.md) | Capture health, exact Ubuntu source reconstruction, measured byte/packet boundaries and reproduction |
| [Counters under medium loss](medium-loss-accounting.md) | Optional pinned wmediumd, captured kernel status and failure accounting; retry and airtime qualification limits |
| [Kernel completion flags](tx-status-accounting.md) | Explain retry suppression using a passive exact-kernel probe and independently correlated completions |
| [Telemetry freshness](telemetry-freshness.md) | Withhold stale observations without restarting a healthy control session; reproduce the native gap and both recovery faults |
| [Reporting policy receipt](reporting-policy.md) | Durable native policy, same-MID Ack, recovery-preserved schedule and explicit missing reports |
| [AP/radio/client report assembly](ap-metric-reports.md) | Complete selected companions, guarded measurement handoff and durable periodic dispatch; native AP measurements pending |
| [Neighbor link metrics](neighbor-link-metrics.md) | Direction-specific IEEE 1905 responses, guarded measurement handoff and the remaining pod/peer interface qualification |
| [Forwarding observations](forwarding-observations.md) | Actual pod interfaces, raw counter windows, independent backhaul capture and recovery baselines |
| [Live neighbor binding](neighbor-discovery-binding.md) | Pod-side discovery through OVSDB into represented topology, with observer pause checks |
| [Backhaul counter audit](backhaul-counter-accounting.md) | Packet/byte reconciliation and the controlled loss absent from ordinary interface counters |
| [Egress accounting source](egress-accounting-source.md) | Passive action/driver loss observations through OVSDB, configuration epochs, controlled loss and live recovery checks |
| [Receive and common-window accounting](receive-counter-accounting.md) | Interface arrival versus client delivery, selected ingress losses, exact Ubuntu source review and common transmit/receive intervals |
| [Virtual-link capacity](virtual-link-capacity.md) | Explicit software service, framing calibration, independently captured work and OVSDB recovery; full neighbor metrics pending |
| [Combined shaped-backhaul accounting](shaped-backhaul-accounting.md) | Selected action, scheduler and interface losses with common service intervals; actual queue overflow and native OVSDB recovery |
| [Native peer-metric delivery](native-peer-metrics.md) | Isolated owned peer profile, observed OVSDB inputs, real IEEE 1905 replies, controller statistics and fault withdrawal |
| [Native lifecycle and 15-minute workload](native-lifecycle.md) | BPL ownership, actual loaded libraries, graceful main-process exits and candidate restoration |
| [Live final-session reason join](live-session-reasons.md) | Actual radio reasons and removal-time raw counters joined by association, with independent capture checks |

Component checks retain their individual scope. The native onboarding guide
establishes the bounded simulated-pod exchange; full-profile sustained reporting
and physical-pod qualification remain incomplete. Open-source reference behavior
supplements the normative specifications.
