# Specification acquisition checklist

This is the single acquisition list for the proposed **EasyMesh 6.1 Profile-1
procedure subset**. The operator confirmed on 2026-09-15 that neither
**IEEE Std 1905.1-2013** nor **IEEE Std 1905.1a-2014** is available locally and
that no subscription-access mechanism has been supplied. Both are **pending
external inputs**. Their exact editions remain recorded; no replacement edition
is selected by this checklist.

## Obtain for the initial procedure review

| Done | Document | Authoritative access route | Why it is needed / current status |
| --- | --- | --- | --- |
| ☐ | **IEEE Std 1905.1-2013** | [IEEE publisher](https://standards.ieee.org/ieee/1905.1/4995/), [Xplore](https://ieeexplore.ieee.org/document/6502164/) | Base framing, discovery, autoconfiguration, WSC transport and reliability. No local copy or supplied access mechanism. |
| ☐ | **IEEE Std 1905.1a-2014** | [IEEE publisher](https://standards.ieee.org/ieee/1905.1a/5820/) | Amendment effects on those procedures and media definitions. No local copy or supplied access mechanism. Must be reviewed with the base. |
| ☑ (reported) | **IEEE Std 802.11-2024** | [IEEE publisher](https://standards.ieee.org/ieee/802.11/10548/), [IEEE GET](https://ieeexplore.ieee.org/browse/standards/get-program/page/series?id=68) | Operator reports obtaining `80211-2024.pdf`. Absolute local path, edition/content verification, digest and clause review are pending. Needed for operating classes and radio capability fields in EasyMesh §9.1 and applicable §17.2 definitions. |
| ☐ | **IEEE Std 802.3-2015** | [IEEE Xplore record cited by EasyMesh](https://ieeexplore.ieee.org/document/7428776/) | Ethernet LINK_UP/LINK_DOWN in EasyMesh §5.2.6; Ethernet handling in §14.1.3 points to §3 of this standard. Exact cited edition not obtained. Operator instead obtained the 2022 edition; relevant-clause comparison and any substitution decision remain pending. |
| ☐ | **Wi-Fi Alliance Security Requirements — revision to be identified** | [Member route cited by EasyMesh](https://www.wi-fi.org/members/wi-fi-alliance-security-requirements) | EasyMesh §13 refers to its Personal AP/STA requirements. Reference [22] gives no revision. The publisher route redirects to a Wi-Fi Alliance login; no account access was supplied. Obtain an authorized copy and record its revision/date before resolving the proposed WPA2-Personal scope. |

These dependencies come from [EasyMesh 6.1](https://www.wi-fi.org/system/files/Wi-Fi%20EasyMesh%20Specification%20v6.1.pdf)
§2 and the cited procedure sections. Document acquisition does not establish
that the proposed subset satisfies all mandatory profile requirements. The
[protocol matrix](protocol-matrix.json) retains the incomplete applicability
audit and unfrozen procedure selection.

On 2026-09-21 the operator reported obtaining **IEEE Std 802.11-2024** and
**IEEE Std 802.3-2022**, with no other new documents obtained. This records
acquisition, not inspection: neither absolute local path has been supplied and
neither new PDF has been read or hashed by the coding agent. The 802.11 filename
matches the requested edition by operator report. The newer Ethernet edition
can support comparison once available; it does not automatically replace the
2015 reference. First inspect the obtained material and identify the relevant
clause correspondence and remaining gaps before deciding whether another
Ethernet purchase is needed. Both IEEE 1905 documents remain the primary missing
inputs for the wire procedure.

The [2026-09-21 available-document audit](procedure-audit.md) now records WFA
message inclusion conditions, selected field definitions and unresolved source
ambiguities. It also distinguishes the existing independent WSC payload/crypto
vectors from the still-missing complete IEEE message vectors. It adds no new
mandatory acquisition beyond the consolidated entries below.

## Resolve only if the selected fields require it

| Done | Document or input | Trigger / disposition |
| --- | --- | --- |
| ☐ | **ISO 3166-1 country-code reference** | CAC reporting in EasyMesh §9.1 uses two-letter country codes. Reference [6] omits an edition but links to [ISO 3166-1:2013](https://www.iso.org/standard/63545.html). Resolve the edition and authorized maintained code source if CAC is emitted; do not silently substitute the newer edition or infer the pod's regulatory domain. Full standard not obtained. |
| ☐ | **IEEE Std 802.11-2012** | WPS 2.0.10 §2 reference [17] selects this older edition. Review affected WPS definitions if used; the current adapter does not implement WPS over 802.11 management frames. Do not treat 802.11-2024 as an automatic replacement for every historical WPS reference. |
| ☐ | **Applicable errata/corrigenda and further normative dependencies** | Check publisher records for the acquired editions, then audit the full IEEE texts. Exact additional documents cannot be determined before reading them. Record either identified corrections or the dated result of the check; no guessed amendment list. |

Feature-dependent bibliography entries remain deferred: IEEE 802.1Q-2018 for
traffic separation; Agile Multiband 1.2, Optimized Connectivity 1.0, WMM 1.2.0
and QoS Management 3.0 for their features; Data Elements 3.0 and the cited TR-181
models for corresponding telemetry; Easy Connect for DPP; 802.11be/D5.0 for
EHT/MLO; WPA3 3.5-or-later, OWE 1.2 and AFC 1.5 for their features. WPS NFC,
UPnP, EAP/802.1X, P2P and certificate-request documents are also outside the
current Ethernet M1/M2 component. A complete mandatory-field audit may promote
an item into the required list; EMOSA must not advertise an unreviewed feature.
This is not an instruction to purchase every document in both bibliographies.

## Already obtained or available for independent work

| Document | Status |
| --- | --- |
| IEEE Std 802.11-2024 — `80211-2024.pdf` | Operator reports obtaining it; local path, content verification, digest and review pending. |
| IEEE Std 802.3-2022 — `IEEE_Standard_for_Ethernet.pdf` | Operator reports obtaining it; local path, content verification and digest pending. Candidate comparison material, not a selected replacement for the cited 2015 edition. |
| [Wi-Fi EasyMesh 6.1](https://www.wi-fi.org/system/files/Wi-Fi%20EasyMesh%20Specification%20v6.1.pdf), 2025-12-15 | Publisher PDF obtained; digest in the protocol matrix. Full procedure/profile selection remains proposed. |
| [Wi-Fi Protected Setup 2.0.10](https://www.wi-fi.org/system/files/Wi-Fi%20Protected%20Setup%20Specification%20v2.0.10.pdf), 2025-12-15 | Publisher PDF obtained; digest recorded. Selected for the bounded WSC cryptographic component only. |
| Public cryptographic references | RFC [2104](https://www.rfc-editor.org/rfc/rfc2104), [3526](https://www.rfc-editor.org/rfc/rfc3526), [2785](https://www.rfc-editor.org/rfc/rfc2785) and the NIST/PKCS references cited in WPS can be reviewed independently. Pin the referenced versions when used; these are not requests for operator subscription credentials. |

The two obtained WFA PDFs are retained outside the repository under
`/home/rev/.local/share/emosa/specifications/`, named `Wi-Fi-EasyMesh-6.1.pdf`
and `Wi-Fi-Protected-Setup-2.0.10.pdf`. They are not redistributed by this repo
or its Pages site. This location is separate from private pod connection files.

For newly acquired documents, provide **absolute local paths only**, or the
name/path of an authorized local access mechanism. Keep credentials out of chat
and outside the repository. We will verify edition/title, hash the full artifact,
record provenance and applicable corrections, and complete the field/rule matrix
before enabling dependent wire procedures. Open-source peers and dissectors
remain independent cross-checks, never substitutes for the specifications.

P0 and specification-dependent wire validation remain **pending**. Model,
OVSDB simulation, evaluation, the selected WSC crypto component and independent
peer baseline work can continue. The acceptance path remains real EasyMesh
messages → EMOSA → unchanged physical pod → independently observed behavior.
