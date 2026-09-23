# IEEE radio and Ethernet input review

Reviewed 2026-09-21 from the operator-supplied local PDFs. Cover pages and PDF
metadata agree on the editions below; both documents were extracted successfully
for selected-clause inspection. This is a bounded review, not an assertion that
all 12,981 PDF pages or all applicable protocol requirements have been audited.
The PDFs, extracted text and page images remain outside the repository.

| Supplied file | Verified edition | PDF pages | Bytes | SHA-256 of supplied artifact |
| --- | --- | ---: | ---: | --- |
| `80211-2024.pdf` | IEEE Std 802.11-2024, revision of 802.11-2020 | 5,956 | 53,718,551 | `4b263f76cc12a6aad0e8edbf65e5fc9ab9772520cdc1c0aac994b85449c9bd55` |
| `IEEE_Standard_for_Ethernet.pdf` | IEEE Std 802.3-2022, revision of 802.3-2018 | 7,025 | 54,473,053 | `e49257a2d250128afe894763a1216ed732d75c35d4e722d6407df4b123e6d425` |

The source is the operator's supplied artifact, not an independently downloaded
publisher master. These hashes identify those exact files. Private provenance
records their absolute local paths on `rev150`; publisher records are linked
below. No watermark, licensed page image or full-text extract is published here.

## 802.11-2024: the requested radio reference is available

This is the edition named by EasyMesh 6.1 reference [1]. The access gap is closed
for this document. It supplies definitions for interpreting radio observations;
it does not supply the capabilities of the actual OpenSync pod.

| Selected reference | What was inspected | Consequence for EMOSA |
| --- | --- | --- |
| §9.4.2.2, Figure 9-209, printed p.934; subsequent component review | SSID's 0–32-octet structure and character encoding conditional on UTF-8 capability information. | The [EasyMesh value component](easymesh-payloads.md) preserves original SSID bytes without assuming UTF-8. Structural parsing does not qualify an operational BSS or wildcard use in a procedure. |
| Annex E.1, printed p.5648; Table E-4 starts p.5656 | Operating classes combine frequency/channel, width and behavioral constraints; global and regional class tables are distinct. Inspected the table structure and the 2.4 GHz entries on p.5658. | Resolve class values from the referenced global table together with qualified target capabilities and regulatory restrictions. A configured channel or schema column alone cannot establish every supported class/channel. |
| §9.4.2.52, printed pp.1120–1121 | Supported Operating Classes element and its optional extension/duple structures. | This Wi-Fi element is a separate structure from an EasyMesh Radio Basic Capabilities TLV. Use EasyMesh §17.2.7 for its outer fields; do not copy a Wi-Fi element wholesale into it. |
| §9.4.2.54.1–2, starting printed p.1122 | HT element structure and capability-information fields. | Provides input definitions for the feature-by-feature HT mapping; actual support and complete mapping remain to be qualified. |
| §9.4.2.156.1, printed p.1292 | VHT element structure, including separate capability information and supported MCS/NSS data. | Do not infer VHT support, stream counts or MCS limits from a band name. Full field interpretation remains work. |
| §9.4.2.247.1, printed p.1437 | HE element structure, with separate MAC/PHY information, MCS/NSS data and optional PPE thresholds. | HE/Wi-Fi 6 reporting needs an explicit mapping and qualified evidence; the structure review does not establish feature support. |

Page numbers above are printed page numbers. In these supplied PDFs the physical
PDF page for those entries is the printed number plus one. No operating-class
table, capability encoder or physical capability value was added to the runtime
as part of this document intake.

## 802.3-2022: useful inspected material, with edition equivalence unresolved

EasyMesh 6.1 reference [3] names **802.3-2015**. The supplied file is **2022**.
The cited 2015 text remains unavailable, so a direct edition comparison has not
been performed and no normative substitution is selected.

| Selected 2022 reference | What was inspected | Consequence for EMOSA |
| --- | --- | --- |
| §3.1.1–2, printed pp.239–240 | Packet/frame boundaries and MAC service-interface mappings. | Distinguish what the standard places on the medium from what the chosen capture/socket interface actually exposes. Qualify that interface before assuming preamble, padding or FCS handling. |
| §3.2.3–6, printed pp.240–242 | Destination/source addressing and Length/Type interpretation and octet order. | These provide explicit 2022 definitions for a future Ethernet boundary review. They do not select IEEE 1905 addressing, EtherType use or CMDU rules. |
| §3.2.7–9, printed pp.242–243 | MAC client-data sizes, padding and frame check sequence. | Ethernet frame limits and padding do not establish IEEE 1905 fragment sizes or message termination. The later IEEE envelope review implements those rules from the acquired base/amendment. |
| EasyMesh §5.2.6 and §14.1.3, printed pp.30 and 103 | Link-triggered onboarding/failover and the reference to Ethernet Clause 3. | Complete the media-specific link-state and actual platform-observation binding separately. A Config `enabled` value is insufficient evidence of an observed link transition. |

The general frame definitions are available for implementation planning against
the supplied 2022 edition. Before claiming the proposed EasyMesh corpus is
qualified, resolve their relationship to the cited edition and the actual
interface. This review does not justify requiring the operator to purchase every
intervening Ethernet edition.

## Correction check and remaining inputs

The [802.3 publisher record](https://standards.ieee.org/ieee/802.3/10422/) links a
[9 February 2024 correction sheet](https://standards.ieee.org/wp-content/uploads/2024/02/802.3-2022_errata.pdf).
Its four pages were reviewed through the publisher's browser-accessible copy:
the changes concern LLDP management numbering and Energy-Efficient Ethernet
references in Clauses 30 and 78; it lists no Clause 3 change. A local download
returned HTTP 403, so no local errata PDF/hash is claimed. The same publisher
record describes **Cor 1-2024** as automotive PHY corrections to Clauses 149
and 165. Full corrigendum text was not obtained; applicability must be revisited
if those PHYs enter scope.

The [802.11 publisher record](https://standards.ieee.org/ieee/802.11/10548/) was
checked on the review date. Its main 2024 entry listed amendments but no separate
2024 errata/corrigendum link. This is a dated publisher-page observation, not a
claim that no correction or maintenance issue exists anywhere.

**Update 2026-09-22: both IEEE 1905 PDFs are obtained.** The
[envelope review and implementation](ieee1905-envelope.md) supersede that earlier
access gap. P0, complete wire
validation and controller onboarding remain blocked. WFA Security Requirements,
the Ethernet edition decision, full feature applicability, independent complete
message vectors and physical capabilities also remain unresolved. The
[acquisition checklist](specification-acquisition.md) keeps those inputs together;
the [protocol matrix](protocol-matrix.json) distinguishes acquired references
from a frozen protocol selection.

## Use complementary specification sources

Use EasyMesh 6.1 as the normative source for the extensions and procedures it
defines, WPS 2.0.10 for the selected WSC payload/authentication rules, and the
obtained 802.11 edition for relevant radio definitions. Standalone EasyMesh
payload components can progress where the available text completely defines
their fields and behavior. Each component needs exact section references,
independent expected values and explicit limits on what its tests establish.

The existing service, scope guards, native peer captures and hwsim client
observers can continue to be exercised alongside that work. Native peers and
dissectors supply implementation cross-checks; they do not resolve missing
normative procedure questions. EasyMesh references the IEEE 1905 base rather than
reproducing its complete framing, addressing, fragmentation, transmission,
retry and duplicate-handling contract. Keep those implementation/validation
dependencies open, with no complete wire-onboarding claim or semantic fallback.
