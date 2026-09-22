# Initial procedure audit: available WFA requirements

Reviewed 2026-09-21 against the privately retained publisher PDFs of **EasyMesh
6.1** (SHA-256 `21b86e1d951a97ea832029979872b19a0933976c27db0822ecb0eb1ef02842e0`)
and **WPS 2.0.10** (SHA-256
`49f7a566a5cc7996982d601b5607664c3cf79a2e7ae0d01d2b589fdd934080cf`). References and
access status are in the [protocol matrix](protocol-matrix.json). Page numbers
below are the documents' printed page numbers. The PDFs are not redistributed.

This review identifies WFA message inclusion rules, feature conditions, selected
field definitions, and their implementation consequences. It **does not freeze
P0**, qualify a complete profile, or replace the missing IEEE base/amendment
review. A fact that is readable in the WFA documents is not permission to emit
an otherwise incomplete IEEE message.

## Why the proposed Profile-1 subset needs care

EasyMesh §18, pp.197–198, requires all mandatory functions for an advertised
profile. Its Table 133 is an informative summary, not an exemption from the
normative sections. Section 9.1, p.80, applies supported-feature reporting to
Profile-1 devices that do not meet all Profile-2/3 requirements. A feature name
containing “Profile-2” does not by itself make its TLV absent from a Profile-1
exchange: §6.1 and §7.1 explicitly require that capability TLV in relevant cases.

The retained prplMesh baseline sends a Profile-2 Search, but its corresponding
Response carries Profile 1 in both reviewed wired/wireless captures. This is an
observed compatibility gap against §6.1's response-profile rule, not a qualified
profile selection for EMOSA. See the [offline capture review](../guides/onboarding-readiness.md#5-interpret-the-actual-findings-and-act-on-them)
and [retained observations](../evidence/onboarding-readiness/summary.json).
The controller also sends two M2 payloads plus M8 in the selected baseline policy;
that complete request is outside the proposed single-M2 mapping. The controller
trial must resolve the peer/build/profile and policy intersection.
Do not force old peer behavior into the normative matrix as an exception.

The [HE/Wi-Fi 6 review](../guides/he-wifi6.md) now traces the response mismatch
to the pinned controller's fixed Profile-1 assignment. It also records a native
Wi-Fi 6 zero-length value rejected under Table 95 and the relevant source paths.
These findings narrow the peer-build work; they do not qualify a substitute
profile or modify the native binaries.

## Message inclusion and applicability

“One” below describes the WFA rule for the specified circumstance. Base IEEE
TLVs remain additional and unreviewed. Feature-dependent fields must be checked
against the corresponding procedure and §18, rather than emitted with invented
zero-valued capabilities.

| Message / circumstance | WFA requirements reviewed | Source | EMOSA disposition |
| --- | --- | --- | --- |
| Agent searches for a controller | Registrar role; agent in supported services; controller in searched services; one profile TLV for the highest fully implemented profile. A device not meeting Profile-2/3 uses Profile-1 and includes supported feature bits in Profile-2 AP Capability. | §6.1 p.63; §17.1.1 p.111 | Service names/profile semantics reviewed; IEEE role/band fields, transport, trust and actual supported features remain pending. |
| Controller responds to that search | Registrar role, controller service, profile response matching the discovery rules; Controller Capability with KiB/MiB support and, when no DPP chirp was received, early capability support. The message list also includes Device 1905 Layer Security Capability; resolve feature applicability with §18. | §6.1 p.63; §17.1.2 p.111 | Must inspect the real peer response; neither an empty reply nor a presumed older response qualifies the trial. |
| Topology Query | Include profile information; obtain the remaining query structure from IEEE. | §6.2 pp.63–64; §17.1.38 p.116; §18 p.197 | No query encoder or response correlation implemented. |
| Agent Topology Response | Supported services, AP Operational BSS, profile; Associated Clients when direct clients exist. The message list also names BSS Configuration Report and conditional backhaul/MLD/TID information. | §6.2 p.64; §17.1.4 pp.111–112 | Local inventory provides some semantic facts, but lacks qualified complete topology, association time, MLD/backhaul semantics and wire encoding. |
| AP Capability Query | No required query TLVs; report uses the query MID and a one-second response deadline. | §9.1 pp.79–80; §17.1.6 p.112 | Protocol deadline is distinct from the pod apply deadline. MID and transport rules await IEEE. |
| AP Capability Report: core | AP Capability; Radio Basic Capabilities for every AP radio; Metric Collection Interval; Device Inventory; supported-feature TLVs under the Profile-1 rule. | §9.1 pp.79–80; §17.1.7 p.112 | Actual resource/capability evidence and complete applicability review required. Local config limits cannot be advertised as radio capabilities. |
| AP Capability Report: technology | HT/VHT capability TLVs for radios supporting each technology; HE plus Wi-Fi 6 per HE radio; Wi-Fi 7/EHT information when supported. Distinct virtual radios have distinct RUIDs and truthful per-radio capability data. | §9.1 pp.79–80; §17.1.7 p.112 | IEEE 802.11-2024 and qualified radio inventory required. An hwsim radio is not evidence about physical pod hardware. |
| AP Capability Report: other features | Review AKM Suite, Channel Scan, Device 1905 Layer Security, CAC, Profile-2 AP Capability and conditional Radio Advanced Capabilities with §9.1 and §18. Unsupported feature capability TLVs are omitted under §18. | §9.1 pp.79–80; §17.1.7 p.112; §18 pp.197–198 | No blanket “include everything” or “omit every newer TLV” policy. A future emitter needs a qualified feature-by-feature decision. |
| Agent WSC M1 envelope | One Radio Basic Capabilities, one WSC carrying M1, one Profile-2 AP Capability and one Radio Advanced Capabilities; separate exchange per radio. M1 MAC is the represented AL MAC. | §7.1 p.67; §17.1.3 p.111 | M1 payload component exists. Complete envelope, qualified RUID/capabilities and exchange lifecycle remain pending. |
| Controller WSC M2 envelope | One Radio Identifier and one or more WSC/M2 payloads; count cannot exceed the reported radio limit; RUID matches the initiating radio. Review conditional M8, VLAN/traffic-separation, MLD, RSN and advanced BSS configuration TLVs. | §7.1 pp.67–69; §17.1.3 p.111 | Authenticate and interpret the entire request before mapping. No supported partial application of a larger radio request. |
| M2 response and complete radio configuration | Controller response within one second; unique N2 for each M2; configure the requested BSS set and remove unmatched existing BSSs. Teardown indicates zero BSSs. | §7.1 pp.67–69 | Components validate M2 sets and distinguish teardown. Existing-BSS patch cannot provide general create/delete/teardown semantics. Sole-BSS scope still needs qualification. |
| M2 roles and credentials | Fronthaul/backhaul/teardown roles live in the WFA Multi-AP extension within encrypted ConfigData. Authenticate settings before releasing a candidate. | §7.1 pp.68–69 Table 20; WPS §7.2–3, §7.5, §8.3.9 Table 20 | Implemented bounded crypto/payload/role components; caller-to-controller/radio binding and replay protection are still missing. |
| BSS index and companion TLVs | Optional BSS_Index immediately precedes Authenticator; uniqueness within a radio; companion configuration binds using its index. A nonzero BSSID takes precedence in a TLV containing both. | §7.1 p.67 Table 19 | Payload placement/index checks exist. Complete companion-TLV mapping is unsupported and must not be discarded. |
| Autoconfiguration Renew | Per-radio WSC response within one second; retain policy not explicitly updated. Unicast transmission and possible two-second multicast fallback rely on the referenced IEEE procedures. | §7.1 p.69; §17.1.72 p.121 | Add to the implemented subset if the chosen controller requires it. No guessed IEEE timer/retransmission implementation. |

## Selected field definitions that can be resolved now

The [selected value component](easymesh-payloads.md) now implements service lists,
Radio Identifier, Operational BSS, Basic/AP/Profile-2/Advanced Capabilities and profile values, with independent native
capture checks. It also exposes the native sender's reserved `0xA1` service in
Search and Topology Response: the receiver ignores it as a role, while the encoder
refuses to emit it under §3.1.2. This is an additional bounded compatibility
observation, not a complete assessment of that native build.

The [executable profile audit](profile-readiness.md) now inventories 15 requirement
families and evaluates selected HT/VHT/HE/EHT/QoS inclusion conditions. It retains
unknown inputs, unresolved applicability and the distinction between a value
codec, truthful target input and a complete mandatory procedure. It does not
complete a clause-by-clause profile assessment. The AP/Profile-2/Advanced codecs
have independent native numeric-field examples; older dissector feature labels
are explicitly insufficient to establish the EasyMesh 6.1 semantics.

These are factual field summaries, **not a complete message encoder contract**. Outer
CMDU framing, TLV sequencing/end markers, base field byte order, fragmentation,
unknown-field processing and duplicate/replay handling still require the IEEE
review. Values below must not be confused with qualified values for a real pod.

| Field/TLV | Definition reviewed | Source | Remaining input |
| --- | --- | --- | --- |
| Supported Service / Searched Service | Types `0x80` / `0x81`; one-octet service count followed by one-octet service identifiers. Controller is `0`; agent is `1` in Supported Service. | §17.2.1–2 Tables 24–25 p.125 | Complete base discovery message and represented role ownership. |
| Radio Identifier | Type `0x82`, value length 6, one RUID. | §17.2.3 Table 26 pp.125–126 | Stable per-pod radio identity allocation and actual binding. |
| Operational BSS | Type `0x83`; radio and BSS counts, six-octet RUID/BSSID, one-octet SSID byte length and the corresponding bytes. | §17.2.4 Table 27 p.126 | Complete observed BSS set; no substitution of Config for observed State. |
| Radio Basic Capabilities | Type `0x85`; six-octet RUID, nonzero one-octet Max_BSS, operating-class count/list; signed one-octet maximum EIRP in dBm and non-operable channel lists. | §17.2.7 Table 30 pp.127–128 | IEEE 802.11 Table E-4, real regulatory domain, power/channel capabilities. |
| Multi-AP Profile | Type `0xB3`, value length 1; values 1/2/3 identify profiles. Reserved values follow the receiver-profile rule. | §17.2.47 Table 70 p.157; §18 p.197 | Full profile qualification; no guessed rejection rule for reserved values. |
| Profile-2 AP Capability | Type `0xB4`; prioritization-rule count, reserved octet, capability/counter-unit bit field, Max VIDs. Counter units distinguish bytes, KiB and MiB. | §17.2.48 Table 71 pp.157–158; §9.1 pp.79–80 | Negotiated counter semantics and supported feature values. ODH must preserve units rather than assuming every counter is bytes. |
| Radio Advanced Capabilities | Type `0xBE`; six-octet RUID and feature bits for combined BSS/traffic separation and supported QoS functions. | §17.2.52 Table 75 p.160 | Truthful feature evidence and interpretation with §7.1/§9.1/§18. |
| WSC authentication | Group-5 DH input padded to 192 bytes; KDK binds N1, enrollee MAC and N2; separate derived keys; randomized AES-CBC wrapping with verified Key Wrap Authenticator. | WPS §7.3 pp.51–53, §7.5 p.57 | Independent crypto vectors already exist; complete trusted protocol exchange does not. |

The [IEEE media input review](ieee-media-review.md) now records verified access to
802.11-2024, including selected Annex E/operating-class and capability-structure
references. This closes that document's access gap; actual target values and
complete field mappings remain pending. The supplied Ethernet document is
802.3-2022, with selected Clause 3 definitions reviewed. Equivalence to the
802.3-2015 edition cited by EasyMesh has not been established.

## Ambiguities retained instead of silently repaired

1. §17.1.1 permits zero/one Supported/Searched Service entries, while §6.1
   requires them for the controller-discovery case. Apply the procedure-specific
   condition; the generic message list is not a reason to omit them.
2. §6.1 explicitly calls for Profile-2 AP Capability in the stated Profile-1
   search case, although §17.1.1 does not list it. Retain both references in the
   eventual encoder contract and cross-check peer compatibility.
3. §6.2's Topology Query paragraph refers to placing Profile-2 AP Capability in
   an Autoconfiguration Search message. Do not silently rewrite that reference
   into a Topology Query requirement; obtain clarification if the trial depends
   on that interpretation.
4. §17.1.3 points to IEEE §6.3.8 for additions to WSC. The IEEE full text is
   unavailable, so this pointer is recorded without treating it as a verified
   WSC-envelope definition.
5. §9.1/§17.1.7 and §18 must be read together for newer feature capability
   TLVs. The structural message list alone does not establish support or permit
   invented capability data. The full feature-field audit remains open.
6. Table 133 has both older Profile-1 and “Profile-1 as of Release 4” columns.
   Reliability (§15.1) and higher-layer delivery (§16) differ between the columns,
   while the sections still define procedures and conditions. Preserve this
   distinction during the clause review; do not turn an informative blank into
   a normative exemption. The new audit marks these families `review_pending`.

## Feature inputs and unchanged-pod viability

AP capability bits describe behavior of the represented agent, including the
adapter and pod mapping. A pod's hardware support alone cannot justify setting
a bit if EMOSA cannot execute the associated complete procedure. In particular,
RSN Overriding combines the special WSC authentication value, companion RSN
parameter application and the referenced WPA3 behavior (§9.1, p.80); the new
`0xA1` codec does not implement those features.

For the actual unchanged pod, qualification must also resolve applicable WPS
advertisement and Multi-AP information elements (§5.2.2), complete-radio BSS
configuration (§7.1), Wi-Fi backhaul address handling (§14), and the manager's
existing configuration/reporting interfaces. Unsupported behavior here is a
viability finding, even if standalone controller-facing bytes are valid.

Counter-unit choice is now a tested pure component of §9.1: known Profile-1
without a KiB/MiB indication selects bytes; the other defined cases require an
explicit KiB/MiB selection. Unknown peer facts are not silently assumed. No
actual peer negotiation, traffic-statistics reporting or ODH delivery is added.

## What remains before wire code can be enabled

Obtain **IEEE 1905.1-2013 and IEEE 1905.1a-2014**, resolve applicable corrections,
and finish the exact base fields, Ethernet rules, addressing, MID lifecycle,
fragments, duplicate rules, timers and failure behavior. Resolve dependent
capability/security documents in the [single acquisition checklist](specification-acquisition.md).
Then qualify the actual peer/profile intersection and represented complete-radio
scope, bind the existing authenticated payload components to an exchange, and
validate independently generated full-message positive and negative vectors.

The first live controller trial deliberately retains a **negative control**:
the adapter's local directory contains the simulated virtual agent, while native
controller inventory does not. The actual peer capture and the semantic operation
are separate observations. Their presence in the same evidence directory cannot
be used to claim that a controller message caused the Config change.


## Selected technology and Device Inventory mapping

The [new walkthrough](../guides/technology-inventory.md) records HT/VHT normalized
inputs and Device Inventory projection through the existing read-only service.
EasyMesh 6.1 §17.2.8–10 (Tables 31–33, pp.128–131) and §17.2.76 (Table 99, p.172)
define the new value structures. IEEE 802.11-2024 §9.4.2.54 (pp.1122–1126),
§9.4.2.156.3/Figure 9-707 (p.1299), §9.4.2.247.4/Figure 9-901/Tables 9-377–378
(pp.1455–1457), and selected Table E-4 classes (pp.5658–5660) define the reviewed
field meanings. Class 128's center-channel numbers are distinct from primary
channels. No management-frame parser or driver-capability inference is added.

The HE field remains opaque: normalized Tx/Rx ordering and independent asymmetric
vectors need further work. The required Wi-Fi 6 companion now has a
[selected synthetic role mapper](../guides/wifi6-inputs.md) with its own readiness
result; this cannot make the complete HE technology set ready. Old
native dissector labels are cross-check material, not authority. Inventory
strings retain their original octets; synthetic UTF-8 input is an explicit local
representation. Current serial/firmware matching cannot prove lifetime identity,
active-image semantics or the execution environment on physical firmware.


## Unresolved HE field reordering

EasyMesh 6.1 §17.2.10 Table 33 names a Tx/Rx HE MCS support field and requires
big-endian reordering. The reviewed IEEE 802.11-2024 Figure 9-901 field is a
sequence of little-endian Rx/Tx map words, with optional width pairs. The
selected references do not yet give this implementation an unambiguous reviewed
choice of reordering group. The pinned native implementation reverses each
four-octet pair on its little-endian host; that is an implementation observation,
not sufficient normative authority to select the conversion.

For the invented IEEE bytes `e41bc6e4fafffdffc0fff6ff`, the choices differ:

| Comparison operation | Resulting octets |
| --- | --- |
| Reverse each two-octet map word | `1be4e4c6fffafffdffc0fff6` |
| Reverse each four-octet Rx/Tx pair | `e4c61be4fffdfffafff6ffc0` |
| Reverse the entire twelve-octet field | `fff6ffc0fffdfffae4c61be4` |

These are comparison examples, **not selected `0x88` encodings**. A publisher
clarification or authoritative worked example must resolve map direction and
width-group order for all 4/8/12-octet cases; an independent asymmetric peer
example can then cross-check the result. Repeating the same symmetric native
sample cannot distinguish these choices. The mapper continues withholding the
entire HE technology set. The separately referenced Figure-9-901 representation
in Table 95 supports the bounded `0xAA` role mapping without resolving Table 33.

## Native candidate follow-up

The [isolated native compatibility trial](../guides/native-compatibility.md) now
demonstrates a rebuilt HAL whose advertised `0xAA` MCS length matches its reported
HE width flags. Nine injected parser cases pass; the original library fails six.
Two native wired runs with a sole-fronthaul agent policy pass client and inventory
checks and contain one M2 with no M8. The native Profile-2 Search/Profile-1 Response
mismatch and shutdown abort remain. No MCS ordering, complete request mapping,
profile conformance or EMOSA wire claim follows from the length fix.

The [acquisition checklist](specification-acquisition.md) includes the exact
clarification questions for Table 33 and Controller Capability Table 117. This
keeps the remaining source questions together with document access requests.
