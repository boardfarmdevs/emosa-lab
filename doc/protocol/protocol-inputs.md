# Protocol input research and proposed P0 scope

Reviewed 2026-09-15 against architecture 3.6, especially WIRE-01, WIRE-07,
WIRE-08 and the P0 gate. The machine-readable record is
[protocol-matrix.json](protocol-matrix.json).

Update 2026-09-21: operator-supplied 802.11-2024 and 802.3-2022 PDFs are now
verified and hashed. See the [bounded media-clause review](ieee-media-review.md)
for what was inspected and the unresolved 2015/2022 Ethernet edition decision.
The IEEE 1905 base and amendment remain missing; the historical research below
does not imply that the newly supplied radio document is still unavailable.

**P0 remains blocked.** The Wi-Fi Alliance documents below were obtained from
its public publisher site. IEEE full text, a complete rule matrix and independent
vectors are still missing. The proposed editions and profile are not a frozen
selection: `specifications` and `selected_profile` remain null in the matrix.
The [WSC cryptographic component](wsc-component.md) separately selects WPS 2.0.10
for its bounded payload rules. This does not satisfy P0 or establish interoperability.

## Proposed compatible corpus

| Input | Proposed exact edition | Evidence and access |
| --- | --- | --- |
| IEEE base | IEEE Std 1905.1-2013 | [Publisher record](https://standards.ieee.org/ieee/1905.1/4995/) and [IEEE Xplore record](https://ieeexplore.ieee.org/document/6502164/) are accessible. The publisher offers purchase or subscription access; full normative text was not obtained. |
| IEEE amendment | IEEE Std 1905.1a-2014 | [Publisher record](https://standards.ieee.org/ieee/1905.1a/5820/) is accessible. Full amendment text was not obtained. |
| EasyMesh | Wi-Fi EasyMesh Specification 6.1, dated 2025-12-15 | [Publisher entry](https://www.wi-fi.org/file/wi-fi-easymesh-specification); [accessible publisher PDF](https://www.wi-fi.org/system/files/Wi-Fi%20EasyMesh%20Specification%20v6.1.pdf), 229 pages. |
| WSC/WPS | Wi-Fi Protected Setup Specification 2.0.10, dated 2025-12-15 | [Publisher entry](https://www.wi-fi.org/file/wi-fi-protected-setup-specification); [accessible publisher PDF](https://www.wi-fi.org/system/files/Wi-Fi%20Protected%20Setup%20Specification%20v2.0.10.pdf), 155 pages. |

The compatibility proposal follows explicit references rather than choosing a
version merely because it is recent. EasyMesh 6.1 section 2 names the 1905 base
and amendment separately and references WPS without a fixed version. WPS
2.0.10's revision history, page 3, records extensions used by EasyMesh 6.1,
including BSS indexing. The matching WFA release dates and documented extensions
support proposing this pair; complete dependency and profile review is still
required. IEEE lists the proposed base and amendment as Inactive-Reserved.
That lifecycle status does not change the fact that EasyMesh 6.1 names them.

Propose a **Profile-1 procedure subset under EasyMesh 6.1**, one virtual agent
and one controller on isolated Ethernet. Begin with topology, controller
discovery, qualified capability reporting and WSC provisioning of a sole existing
fronthaul BSS using WPA2-Personal/AES where the physical pod supports that
representation. This is an evaluation scope, not full Profile-1 conformance.
Section 18 requires additional mandatory functions for a profile claim, including
functions outside the initial architecture subset. The current specification
also adds profile/capability reporting requirements to Profile-1; older release
TLV lists cannot simply be reused.

## Verified section pointers

Page numbers below are printed PDF pages. IEEE section pointers are **references
found in the EasyMesh text**, not claims that the IEEE clauses were read.
The JSON matrix records that distinction on each entry.

| Procedure | Accessible normative sections | IEEE material still needed |
| --- | --- | --- |
| Topology discovery/query/response | EasyMesh 6.1 sections 6.2, 7.2, 17.1.4, 17.2.1, 17.2.4–5 and 17.2.47; pages 63–64, 71, 111–112, 125–126 and 157. | EasyMesh cites IEEE section 8 and response section 6.3.3. Retrieve discovery framing, timers and the complete base TLV rules. |
| AP autoconfiguration search/response | EasyMesh 6.1 sections 6.1, 17.1.1–2, 17.2.1–2, 17.2.47–48 and 18; pages 62–63, 111, 125, 157 and 197. | IEEE section 10 and message sections 6.3.7–8, including transmission, relaying and retry behavior. |
| AP capability query/report | EasyMesh 6.1 sections 9.1, 17.1.6–7 and relevant 17.2 capability definitions; pages 79–80 and 112. Read these with section 18. | IEEE CMDU framing and any referenced 802.11 definitions needed to encode actual capabilities. |
| AP autoconfiguration WSC M1/M2 | EasyMesh 6.1 sections 7.1 and 17.1.3, pages 66–69 and 111; WPS 2.0.10 sections 7.1–3, 7.5, 7.9, 8.1, 8.3.1–2, Table 20 in 8.3.9 and Tables 28–29 in section 12. | IEEE section 10.1 and the WSC message/TLV, procedure, ordering and failure definitions. |
| Shared framing and reliability | EasyMesh 6.1 sections 15, 17 and 18; pages 104–105, 107 and 197–198. Appendix A.3.3, page 214, is an informative legacy-fragment note. | IEEE sections 6.2 and 7, including 7.1.1–2 and the relevant multicast/unicast send/receive clauses; all amendment effects. |

The partial matrix includes source-backed message IDs from EasyMesh Table 22
and the one-second response deadlines in sections 9.1 and 7.1. These are protocol
response deadlines, independent of a pod's configuration-application deadline.
They are not a complete timer/retry matrix. The discovery message ID remains
unset because the inspected normative WFA table does not define it; knowing it
from a dissector is insufficient to qualify the IEEE procedure.

WPS 2.0.10 supplies the registration-message attributes, nonce and authenticator
handling, key derivation, authenticated key wrapping and encrypted AP settings.
The selected IEEE/EasyMesh transport and procedure rules must be applied around
those definitions. Reading the crypto specification does not establish crypto
validation. The [bounded crypto component](wsc-component.md) now has independent
synthetic hostap vectors and negative decryption tests. Full M1/M2 validation,
exchange state, profile semantics and packet/peer tests are still required.
The additional [M1/M2 payload component](wsc-messages.md) now builds M1,
checks required M2 fields and authenticates/decrypts an entire payload set using
the original M1. Its independent full-payload fixtures are not IEEE frames. The [radio payload
interpreter](wsc-radio.md) adds selected encrypted-role and teardown semantics,
plus a narrow candidate projection; complete CMDU and radio mapping remain pending.

## Consequence for the one-BSS experiment

EasyMesh 6.1 section 7.1 specifies a **complete radio BSS configuration** for
WSC M1/M2. It requires validation of the received M2 configurations and removal
of operating BSSs that do not match any configuration in the request. It also
defines an explicit radio teardown request. This makes a component-level patch
to an arbitrary single existing VIF insufficient evidence of WSC provisioning.

The proposed narrow hardware experiment therefore requires a qualified radio
with exactly the sole existing fronthaul BSS being represented, exclusive
ownership of that radio's configuration, and no unaccounted management or
backhaul dependency. Alternatively, implement and qualify the complete requested
radio configuration. That broader mapping is outside the initial one-BSS patch.
A request requiring creation, deletion, radio teardown or changes to another BSS
must be identified before writing, with its wire behavior taken from the completed
protocol matrix. Never apply a convenient subset and report full success.

Capability and topology messages have similar evidence requirements. Actual
supported operating classes, HT/VHT/HE capabilities, client inventory and
physical backhaul cannot be derived merely from OVSDB table names. The pinned
OpenSync schema is a simulation reference; it is not a qualified physical-pod
profile. A virtual agent's Ethernet adjacency describes its protocol link and
must be kept distinct from the actual pod's physical topology.

## Access and review still required

All document requests and access status are consolidated in the
[specification acquisition checklist](specification-acquisition.md). The operator
confirmed that the exact IEEE 1905 base/amendment editions have no local copies
or supplied access mechanism. The follow-up dependency audit also records
IEEE 802.3-2015 (EasyMesh §5.2.6 and §14.1.3) and Wi-Fi Alliance Security
Requirements (EasyMesh §13), whose revision must be identified. No missing
document is replaced by a peer implementation or an older fixture.

1. Resolve the complete mandatory/optional/conditional TLV and field matrix for
   the proposed profile. IEEE base rules, timers, retry/duplicate behavior,
   rollover, reassembly and error semantics remain incomplete. Hardware-only
   capability values remain dependent on pod qualification.
2. Obtain independently derived packet and cryptographic vectors with permitted
   provenance, exact expected outcomes and a pinned independent parser or peer.
   The separate crypto component supplies synthetic payload vectors only;
   complete message vectors and independently interpreted captures are pending.

The WFA PDFs are currently downloadable without user credentials. If those
publisher URLs change or become unavailable, supply the exact digest-matching
editions locally through an authorized route. Do not replace them with an
unverified repost or silently switch editions. No publisher account was created,
contact form submitted, or third party messaged during this work.

Two document details need explicit review. EasyMesh section 2 reference [2]
names the 2013 base but links to an amendment URL, while reference [11] names
the amendment separately. Also, section 17.1.3 points to IEEE section 6.3.8 for
WSC, whereas EasyMesh section 6.1 identifies that pointer as the autoconfiguration
response. Preserve these observations and verify against the IEEE text; do not
silently infer a correction and call it normative.

## Provenance and independent cross-check

The public WFA specification catalog exposed the current publisher entries.
Their public download pages (`/wi-fi-download/35509` and
`/wi-fi-download/35517`) supplied the PDF URLs. Ordinary HTTPS GET requests
retrieved PDF content even though the web research tool reported fetch errors
for the same WFA host. Cover pages, revision histories and the cited sections
were read with local PDF text extraction. Only references and research findings
are committed; the PDFs are not redistributed in this repository.

| Artifact | Size | SHA-256 |
| --- | ---: | --- |
| EasyMesh 6.1 publisher PDF | 4,297,070 bytes | `21b86e1d951a97ea832029979872b19a0933976c27db0822ecb0eb1ef02842e0` |
| WPS 2.0.10 publisher PDF | 1,265,445 bytes | `49f7a566a5cc7996982d601b5607664c3cf79a2e7ae0d01d2b589fdd934080cf` |

[Wireshark's IEEE 1905 dissector](https://github.com/wireshark/wireshark/blob/009a163470b581c7d3ee66d89c819cef1f9e50fe/epan/dissectors/packet-ieee1905.c)
was inspected only to cross-check the initial message-name/ID definitions.
The `v4.4.0` tag resolves to commit
`009a163470b581c7d3ee66d89c819cef1f9e50fe`; the file's SHA-256 is
`5da6141b2be50312be153a929775c625db726049a5fb6ed37fecdba67849ec50`.
This is a non-normative implementation reference that predates EasyMesh 6.1,
not proof of support for that edition and not an independent-vector test result.
No prplMesh build-time or runtime dependency is introduced.

The eventual acceptance chain remains **real EasyMesh messages → EMOSA adapter
→ unchanged physical pod → independently observed behavior**. Model and OVSDB
simulation can continue while the missing protocol, hardware and independent-peer
inputs are resolved; their results do not complete that acceptance path.
