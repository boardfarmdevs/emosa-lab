# EMOSA Profile-1 readiness audit

Profile advertisement: **blocked**. Inputs are unqualified planning conditions.

| Requirement family | Sections | Status | Remaining |
| --- | --- | --- | --- |
| Layer-2 onboarding and backhaul roles | 5.1–5.2 | partial_component | Native peers are a separate baseline; no EMOSA peer exchange or qualified pod backhaul |
| Controller discovery and topology | 6.1–6.2; Table 4 | partial_component | Value codecs/topology exist; IEEE processing and controller inventory proof are absent |
| Profile indication and mandatory functions | 6.1–6.2; 17.2.47; 18 | partial_component | A profile-value codec is not qualification of the mandatory functions |
| Radio configuration and policy | 7 | partial_component | WSC payloads and an existing-BSS operation do not implement general radio configuration |
| Channel preferences and selection | 8.1–8.2 | not_implemented | Supported-class input is not a preference report or channel-selection procedure |
| Complete AP capability reporting | 9.1; 17.1.7 | partial_component | Basic radio mapping/codecs lack conditional fields and the complete report |
| Client capability reporting | 9.2 | not_implemented | Client inventory does not supply the association frame or query/error procedure |
| Link and station metrics | 10.1; 10.2.1; 10.3–10.4 | not_implemented | Counter units alone lack measurements, reporting and the ODH delivery path |
| Client steering | 11 | not_implemented | No qualified steering request, policy, outcome or failure mapping |
| Backhaul optimization | 12 | not_implemented | No qualified backhaul steering or management-path recovery mapping |
| Wi-Fi backhaul address handling | 14 | qualification_pending | Path-dependent; native-peer hwsim results do not qualify unchanged OpenSync behavior |
| Control-message reliability | 15.1; 18 | review_pending | Table 133 columns differ; resolve normative procedures and missing IEEE rules |
| Higher-layer payload delivery | 16; 18 | review_pending | Table 133 columns differ; HLE trigger and receive/ack obligations remain unimplemented |
| Complete message formats | 17 | blocked_external | IEEE 1905.1-2013/1905.1a-2014 are pending; value codecs do not define the envelope |
| Additional current Profile-1 conditions | 6.3; 7.3; 9.1; 10.2.2; 11.4–11.7; 13; 19; 8.2.4; 18 | review_pending | Resolve data elements, scans and conditional functions for the actual target |

| Capability value | Scope | Inclusion decision | Value component available |
| --- | --- | --- | --- |
| 0xa1 AP Capability | agent | required | True |
| 0xb4 Profile-2 AP Capability | agent | required | True |
| 0xc5 Metric Collection Interval | agent | required | False |
| 0xd4 Device Inventory | agent | required | False |
| 0x85 Radio Basic Capabilities | radio-1 | required | True |
| 0x86 HT Capabilities | radio-1 | required | False |
| 0x87 VHT Capabilities | radio-1 | omit_if_unsupported | False |
| 0x88 HE Capabilities | radio-1 | required | False |
| 0xaa Wi-Fi 6 Capabilities | radio-1 | required | False |
| 0xbe Radio Advanced Capabilities | radio-1 | omit_if_unsupported | True |
| 0x85 Radio Basic Capabilities | radio-2 | required | True |
| 0x86 HT Capabilities | radio-2 | required | False |
| 0x87 VHT Capabilities | radio-2 | required | False |
| 0x88 HE Capabilities | radio-2 | omit_if_unsupported | False |
| 0xaa Wi-Fi 6 Capabilities | radio-2 | omit_if_unsupported | False |
| 0xbe Radio Advanced Capabilities | radio-2 | required | True |
| 0xdf Wi-Fi 7 Agent Capabilities | agent | unresolved | False |
| 0xe7 EHT Operations | agent | unresolved | False |
| 0xcc AKM Suite Capabilities | agent | applicability_review_pending | False |
| 0xa5 Channel Scan Capabilities | agent | applicability_review_pending | False |
| 0xa9 1905 Layer Security Capability | agent | applicability_review_pending | False |
| 0xb2 CAC Capabilities | agent | applicability_review_pending | False |

Counter units: {"decision": "conditional_choice", "code": 0, "unit": "bytes", "basis": "unqualified planning inputs; actual exchange is not observed"}

M1 companions: 0x85, WSC M1, 0xb4, 0xbe per radio; AP report inclusion differs.

Full clause audit, wire onboarding and physical-pod proof remain pending.
