# Protocol specifications and components

[Documentation index](../README.md)

| Document | Scope |
| --- | --- |
| [Specification inputs](protocol-inputs.md) | Authoritative sources, proposed editions and selected procedure scope |
| [Acquisition checklist](specification-acquisition.md) | Required, obtained and pending external documents |
| [IEEE media input review](ieee-media-review.md) | Verified 802.11-2024 and 802.3-2022 PDFs, selected clauses, corrections and Ethernet edition gap |
| [Protocol matrix](protocol-matrix.json) | Rules, sections, implementation evidence and unresolved procedure requirements |
| [EasyMesh value components](easymesh-payloads.md) | Service, radio identity, Operational BSS and profile values; offline CLI, exact references and independent native-capture checks |
| [WSC cryptographic component](wsc-component.md) | Bounded cryptography, independent vectors and validation limits |
| [WSC M1/M2 payloads](wsc-messages.md) | Payload construction, authentication and required-field checks |
| [WSC radio interpretation](wsc-radio.md) | Authenticated BSS roles, teardown and complete-radio admission |

Component checks do not establish a complete IEEE 1905/EasyMesh exchange.
Specification-dependent wire validation and controller onboarding remain gated.
Open-source reference behavior supplements the normative specifications.
