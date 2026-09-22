# Protocol specifications and components

[Documentation index](../README.md)

| Document | Scope |
| --- | --- |
| [Specification inputs](protocol-inputs.md) | Authoritative sources, proposed editions and selected procedure scope |
| [Acquisition checklist](specification-acquisition.md) | Required, obtained and pending external documents |
| [IEEE 1905 envelope implementation](ieee1905-envelope.md) | Obtained base/amendment, audited frame/TLV rules, bounded reassembly, native vectors and isolated Ethernet checks |
| [Controller discovery and WSC exchanges](autoconfiguration.md) | Search/M1 construction, Response/M2 binding, lifetime/replay controls, native compatibility findings and beginner exercise |
| [Capability and topology reports](reports.md) | Restricted Early Report and Topology Response, facts/freshness checks, offline and Ethernet exercises, native compatibility findings |
| [Read-only report coordinator](report-coordinator.md) | Real database source, Topology Query loop, Early Report Ack/retry, source withdrawal and isolated Ethernet reproduction |
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

Component checks do not establish a complete IEEE 1905/EasyMesh exchange.
Specification-dependent wire validation and controller onboarding remain gated.
Open-source reference behavior supplements the normative specifications.
