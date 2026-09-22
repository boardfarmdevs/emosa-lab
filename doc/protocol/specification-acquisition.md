# Specification acquisition checklist

This is the single acquisition list for the proposed **EasyMesh 6.1 Profile-1
procedure subset**. On **2026-09-22** the operator supplied both exact IEEE
1905 editions. They are verified and hashed; the [envelope implementation and
review](ieee1905-envelope.md) records selected clauses and working packet code.
The earlier acquisition blocker is closed. Remaining documents and full
procedure/profile validation are listed below.

## Obtain for the initial procedure review

| Done | Document | Authoritative access route | Why it is needed / current status |
| --- | --- | --- | --- |
| ☑ | **IEEE Std 1905.1-2013** | [IEEE publisher](https://standards.ieee.org/ieee/1905.1/4995/), [Xplore](https://ieeexplore.ieee.org/document/6502164/) | Obtained at `/home/rev/19051-2013.pdf`; edition/hash verified. Selected framing, discovery and autoconfiguration clauses reviewed; complete procedure integration remains pending. |
| ☑ | **IEEE Std 1905.1a-2014** | [IEEE publisher](https://standards.ieee.org/ieee/1905.1a/5820/) | Obtained at `/home/rev/19051a-2014.pdf`; edition/hash verified and selected changes reviewed with the base, including visual inspection of amended reserved-field rules. |
| ☑ | **IEEE Std 802.11-2024** | [IEEE publisher](https://standards.ieee.org/ieee/802.11/10548/), [IEEE GET](https://ieeexplore.ieee.org/browse/standards/get-program/page/series?id=68) | Operator supplied `80211-2024.pdf`; cover/metadata verified, digest recorded and selected clauses reviewed. See [media review](ieee-media-review.md). Complete field/profile audit remains pending. Needed for operating classes and radio capability fields in EasyMesh §9.1 and applicable §17.2 definitions. |
| ☐ | **IEEE Std 802.3-2015** | [IEEE Xplore record cited by EasyMesh](https://ieeexplore.ieee.org/document/7428776/) | Ethernet LINK_UP/LINK_DOWN in EasyMesh §5.2.6; Ethernet handling in §14.1.3 points to §3 of this standard. Exact cited edition not obtained. Operator supplied the 2022 edition; it is verified and hashed, with selected Clause 3 definitions reviewed. Direct comparison to 2015 and any substitution decision remain pending. |
| ☐ | **Wi-Fi Data Elements 3.0 package, including TR-181-2-17_DEr3.xlsx** | [Member package cited by EasyMesh reference 10](https://www.wi-fi.org/file-member/wi-fi-data-elements-specification-package) | EasyMesh §9.1 requires Metric Collection Interval; §17.2.59 Table 82 p.164 points to Device.CollectionInterval. Obtain the definition, units and valid values before implementing `0xC5`. The publisher route redirected to Wi-Fi Alliance login on 2026-09-22; no authorized package/access supplied. This is a capability-report dependency, separate from choosing ODH transport. |
| ☐ | **Wi-Fi Alliance Security Requirements — revision to be identified** | [Member route cited by EasyMesh](https://www.wi-fi.org/members/wi-fi-alliance-security-requirements) | EasyMesh §13 refers to its Personal AP/STA requirements. Reference [22] gives no revision. The publisher route redirects to a Wi-Fi Alliance login; no account access was supplied. Obtain an authorized copy and record its revision/date before resolving the proposed WPA2-Personal scope. |

These dependencies come from [EasyMesh 6.1](https://www.wi-fi.org/system/files/Wi-Fi%20EasyMesh%20Specification%20v6.1.pdf)
§2 and the cited procedure sections. Document acquisition does not establish
that the proposed subset satisfies all mandatory profile requirements. The
[protocol matrix](protocol-matrix.json) retains the incomplete applicability
audit and unfrozen procedure selection.

On 2026-09-21 the operator supplied local paths for **IEEE Std 802.11-2024**
and **IEEE Std 802.3-2022**. Both PDFs are accessible, their editions verified,
and hashes recorded. The [bounded media review](ieee-media-review.md) identifies
selected sections inspected and dated publisher correction checks. The newer
Ethernet edition does not automatically replace the 2015 reference; a direct
comparison remains unavailable. The later IEEE 1905 acquisition is recorded above.

The [2026-09-21 available-document audit](procedure-audit.md) now records WFA
message inclusion conditions, selected field definitions and unresolved source
ambiguities. It also distinguishes the existing independent WSC payload/crypto
vectors from the new independent IEEE envelope vectors and still-pending complete procedure validation. The later [HE/Wi-Fi 6 review](../guides/he-wifi6.md) promotes Data Elements 3.0
from a deferred telemetry dependency to the initial capability-report checklist.

## Resolve only if the selected fields require it

| Done | Document or input | Trigger / disposition |
| --- | --- | --- |
| ☐ | **ISO 3166-1 country-code reference** | CAC reporting in EasyMesh §9.1 uses two-letter country codes. Reference [6] omits an edition but links to [ISO 3166-1:2013](https://www.iso.org/standard/63545.html). Resolve the edition and authorized maintained code source if CAC is emitted; do not silently substitute the newer edition or infer the pod's regulatory domain. Full standard not obtained. |
| ☐ | **IEEE Std 802.11-2012** | WPS 2.0.10 §2 reference [17] selects this older edition. Review affected WPS definitions if used; the current adapter does not implement WPS over 802.11 management frames. Do not treat 802.11-2024 as an automatic replacement for every historical WPS reference. |
| ☐ | **ISO 80000-1:2009** | EasyMesh reference [26] is cited by Table 71's byte-counter units. See the [publisher reference](https://www.iso.org/en/contents/data/standard/03/06/30669.html). Full text/access not supplied; retain for the complete units/telemetry review. The current component only exposes the WFA codes and conventional binary-prefix metadata; it implements no traffic-counter conversion or ODH transport. |
| ☐ | **IEEE Std 802.1AB-2009** | Base §6.1 and §8.2.1.2 require accompanying bridge-discovery LLDP. Its encoding/procedure dependency is identified during the new IEEE review; no local copy supplied. The current envelope/packet check sends no discovery advertisements or LLDP. |
| ☐ | **Applicable errata/corrigenda and further normative dependencies** | IEEE 1905 publisher records/search checked 2026-09-22; no separate correction identified there. Continue complete dependency/applicability review; this is not a claim that no corrections exist. |

Feature-dependent bibliography entries remain deferred: IEEE 802.1Q-2018 for
traffic separation; Agile Multiband 1.2, Optimized Connectivity 1.0, WMM 1.2.0
and QoS Management 3.0 for their features; additional TR-181 model dependencies
for corresponding telemetry; Easy Connect for DPP; 802.11be/D5.0 for
EHT/MLO; WPA3 3.5-or-later, OWE 1.2 and AFC 1.5 for their features. WPS NFC,
UPnP, EAP/802.1X, P2P and certificate-request documents are also outside the
current Ethernet M1/M2 component. A complete mandatory-field audit may promote
an item into the required list; EMOSA must not advertise an unreviewed feature.
This is not an instruction to purchase every document in both bibliographies.

The [feature/profile audit](profile-readiness.md) now makes the deferred triggers
more explicit: the DPP, traffic-separation, RSN Overriding and QoS bits require
the corresponding complete behavior and referenced documents before support can
be advertised. Their standalone value codecs do not promote those features into
the implemented profile. The remaining acquisition priorities are the cited Ethernet/LLDP
texts, Data Elements package and applicable WFA Security Requirements.

## Already obtained or available for independent work

### Include these clarification questions in the same acquisition effort

The documents and source ambiguities can be resolved together. No request has
been sent to IEEE, Wi-Fi Alliance or another party by this agent. Keep authorized
answers and worked examples outside Git until their redistribution terms are
reviewed; record their provenance and digest in the protocol matrix.

| Source | Precise clarification needed |
| --- | --- |
| EasyMesh 6.1 §17.2.10 Table 33, compared with IEEE 802.11-2024 Figure 9-901 | Define `0x88` Rx/Tx direction, width-group ordering and the unit of big-endian reordering. Supply asymmetric 4-, 8- and 12-octet examples that distinguish word, pair and whole-field reversal. The alternatives are listed in the [procedure audit](procedure-audit.md#unresolved-he-field-reordering). |
| EasyMesh 6.1 §17.2.94 Table 117 | Resolve the overlap between the named Early AP Capability bit and the stated reserved-bit range. We must not silently repair a normative table when encoding controller capability. |
| EasyMesh reference [10] and §17.2.59 Table 82 | Supply the selected Data Elements 3.0 package, including the referenced spreadsheet, to establish CollectionInterval units and valid values. |
| EasyMesh reference [22] and §13 | Identify the applicable Security Requirements revision and provide authorized access for the selected WPA2-Personal procedure review. |

The [isolated native HE-length fix](../guides/native-compatibility.md) does not
resolve these questions. Native source and captures can cross-check an answer;
they do not replace its normative authority.

### Available documents

| Document | Status |
| --- | --- |
| IEEE Std 802.11-2024 — `80211-2024.pdf` | Local PDF verified and hashed; selected operating-class and capability structures inspected. Full field/profile review remains pending. |
| IEEE Std 802.3-2022 — `IEEE_Standard_for_Ethernet.pdf` | Local PDF verified and hashed; selected Clause 3 definitions inspected. Candidate comparison material, not a selected replacement for the cited 2015 edition. |
| [Wi-Fi EasyMesh 6.1](https://www.wi-fi.org/system/files/Wi-Fi%20EasyMesh%20Specification%20v6.1.pdf), 2025-12-15 | Publisher PDF obtained; digest in the protocol matrix. Full procedure/profile selection remains proposed. |
| [Wi-Fi Protected Setup 2.0.10](https://www.wi-fi.org/system/files/Wi-Fi%20Protected%20Setup%20Specification%20v2.0.10.pdf), 2025-12-15 | Publisher PDF obtained; digest recorded. Selected for the bounded WSC cryptographic component only. |
| Public cryptographic references | RFC [2104](https://www.rfc-editor.org/rfc/rfc2104), [3526](https://www.rfc-editor.org/rfc/rfc3526), [2785](https://www.rfc-editor.org/rfc/rfc2785) and the NIST/PKCS references cited in WPS can be reviewed independently. Pin the referenced versions when used; these are not requests for operator subscription credentials. |

The two obtained WFA PDFs are retained outside the repository under
`/home/rev/.local/share/emosa/specifications/`, named `Wi-Fi-EasyMesh-6.1.pdf`
and `Wi-Fi-Protected-Setup-2.0.10.pdf`. They are not redistributed by this repo
or its Pages site. This location is separate from private pod connection files. The newly supplied IEEE PDFs remain at their operator-provided paths outside Git; exact artifact digests are recorded in the media review and matrix.

For newly acquired documents, provide **absolute local paths only**, or the
name/path of an authorized local access mechanism. Keep credentials out of chat
and outside the repository. We will verify edition/title, hash the full artifact,
record provenance and applicable corrections, and complete the field/rule matrix
before enabling dependent wire procedures. Open-source peers and dissectors
remain independent cross-checks, never substitutes for the specifications.

The IEEE access blocker is **closed**; full P0 procedure/profile validation remains
**pending**. Envelope and packet implementation can now use the actual IEEE texts. Model,
OVSDB simulation, evaluation, the selected WSC crypto component and independent
peer baseline work can continue. The acceptance path remains real EasyMesh
messages → EMOSA → unchanged physical pod → independently observed behavior.
