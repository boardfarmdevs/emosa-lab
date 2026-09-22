"""Planning audit of the proposed EasyMesh 6.1 Profile-1 subset.

An input declares conditions to inspect, not device or profile qualification.
This component never builds messages, advertises a profile or permits writes.
"""

import json

from emosa.config import validate
from emosa.counter_units import select_counter_units
from emosa.errors import EmosaError, Reason
from emosa.evaluation.payloads import read_value

RADIO_FEATURES = ("ht", "vht", "he", "eht", "advanced_qos")

# A reviewed requirement-family inventory, not an assertion that every clause
# or applicability question is resolved. See the associated normative matrix.
REQUIREMENTS = (
    (
        "onboarding",
        "Layer-2 onboarding and backhaul roles",
        "5.1–5.2",
        "partial_component",
        "Native peers are a separate baseline; no EMOSA peer exchange or qualified pod backhaul",
    ),
    (
        "discovery",
        "Controller discovery and topology",
        "6.1–6.2; Table 4",
        "partial_component",
        "Value codecs/topology exist; IEEE processing and controller inventory proof are absent",
    ),
    (
        "profile",
        "Profile indication and mandatory functions",
        "6.1–6.2; 17.2.47; 18",
        "partial_component",
        "A profile-value codec is not qualification of the mandatory functions",
    ),
    (
        "configuration",
        "Radio configuration and policy",
        "7",
        "partial_component",
        "WSC payloads and an existing-BSS operation do not implement general radio configuration",
    ),
    (
        "channel_selection",
        "Channel preferences and selection",
        "8.1–8.2",
        "not_implemented",
        "Supported-class input is not a preference report or channel-selection procedure",
    ),
    (
        "ap_capability",
        "Complete AP capability reporting",
        "9.1; 17.1.7",
        "partial_component",
        "Basic radio mapping/codecs lack conditional fields and the complete report",
    ),
    (
        "client_capability",
        "Client capability reporting",
        "9.2",
        "not_implemented",
        "Client inventory does not supply the association frame or query/error procedure",
    ),
    (
        "metrics",
        "Link and station metrics",
        "10.1; 10.2.1; 10.3–10.4",
        "not_implemented",
        "Counter units alone lack measurements, reporting and the ODH delivery path",
    ),
    (
        "steering",
        "Client steering",
        "11",
        "not_implemented",
        "No qualified steering request, policy, outcome or failure mapping",
    ),
    (
        "backhaul",
        "Backhaul optimization",
        "12",
        "not_implemented",
        "No qualified backhaul steering or management-path recovery mapping",
    ),
    (
        "four_address",
        "Wi-Fi backhaul address handling",
        "14",
        "qualification_pending",
        "Path-dependent; native-peer hwsim results do not qualify unchanged OpenSync behavior",
    ),
    (
        "reliability",
        "Control-message reliability",
        "15.1; 18",
        "review_pending",
        "Table 133 columns differ; resolve normative procedures and missing IEEE rules",
    ),
    (
        "higher_layer",
        "Higher-layer payload delivery",
        "16; 18",
        "review_pending",
        "Table 133 columns differ; HLE trigger and receive/ack obligations remain unimplemented",
    ),
    (
        "message_format",
        "Complete message formats",
        "17",
        "blocked_external",
        "IEEE 1905.1-2013/1905.1a-2014 are pending; value codecs do not define the envelope",
    ),
    (
        "release4_features",
        "Additional current Profile-1 conditions",
        "6.3; 7.3; 9.1; 10.2.2; 11.4–11.7; 13; 19; 8.2.4; 18",
        "review_pending",
        "Resolve data elements, scans and conditional functions for the actual target",
    ),
)


def unknown_features():
    return {
        "schema_version": 1,
        "scope": "unqualified_planning_input",
        "controller": {"profile": None, "kib_mib": None, "preferred_scaled_unit": None},
        "radios": [{"radio_id": "unqualified-radio", **dict.fromkeys(RADIO_FEATURES)}],
    }


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def read_features(path):
    try:
        value = json.loads(read_value(value_file=path), object_pairs_hook=_object)
    except (ValueError, RecursionError):
        raise EmosaError(Reason.INVALID_INPUT, "invalid profile planning JSON") from None
    return validate_features(value)


def validate_features(value):
    validate("profile-features", value)
    ids = [r["radio_id"] for r in value["radios"]]
    if len(ids) != len(set(ids)):
        raise EmosaError(Reason.INVALID_INPUT, "duplicate planning radio identity")
    for key in ("profile", "preferred_scaled_unit"):
        if value["controller"][key] is not None and type(value["controller"][key]) is not int:
            raise EmosaError(Reason.INVALID_INPUT, "controller planning values must be integers")
    return value


def _condition(supported):
    if supported is None:
        return "unresolved"
    return "required" if supported else "omit_if_unsupported"


def _any(values):
    # One known supporting radio already triggers an agent-wide obligation.
    return (
        True
        if any(v is True for v in values)
        else None
        if any(v is None for v in values)
        else False
    )


def audit(features=None):
    inputs = validate_features(features if features is not None else unknown_features())
    rows = []

    def add(kind, name, scope, decision, source, component, remaining):
        rows.append(
            {
                "type": kind,
                "name": name,
                "scope": scope,
                "decision": decision,
                "source": "EasyMesh 6.1 " + source,
                "value_component_available": component,
                "qualified_value_available": False,
                "remaining": remaining,
            }
        )

    for kind, name, component, remaining in (
        (
            "0xa1",
            "AP Capability",
            True,
            "Qualified feature evidence for adapter and represented pod",
        ),
        (
            "0xb4",
            "Profile-2 AP Capability",
            True,
            "Qualified feature/limit claims and actual peer-dependent counter-unit choice",
        ),
        (
            "0xc5",
            "Metric Collection Interval",
            False,
            "Qualified measurement interval and reviewed field encoding",
        ),
        (
            "0xd4",
            "Device Inventory",
            False,
            "Stable qualified serial, active firmware/environment and per-radio vendor data",
        ),
    ):
        add(kind, name, "agent", "required", "§9.1 / §17.1.7", component, remaining)
    for radio in sorted(inputs["radios"], key=lambda r: r["radio_id"]):
        identifier = radio["radio_id"]
        add(
            "0x85",
            "Radio Basic Capabilities",
            identifier,
            "required",
            "§9.1 / §17.2.7",
            True,
            "Existing synthetic input mapper is not qualified physical capability evidence",
        )
        for feature, kind, name in (
            ("ht", "0x86", "HT Capabilities"),
            ("vht", "0x87", "VHT Capabilities"),
            ("he", "0x88", "HE Capabilities"),
            ("he", "0xaa", "Wi-Fi 6 Capabilities"),
            ("advanced_qos", "0xbe", "Radio Advanced Capabilities"),
        ):
            add(
                kind,
                name,
                identifier,
                _condition(radio[feature]),
                "§9.1 / §18",
                kind == "0xbe",
                "Planning only; verify complete feature implementation and truthful field inputs",
            )
    eht = _any([r["eht"] for r in inputs["radios"]])
    for kind, name in (("0xdf", "Wi-Fi 7 Agent Capabilities"), ("0xe7", "EHT Operations")):
        add(
            kind,
            name,
            "agent",
            _condition(eht),
            "§9.1 / §17.1.7",
            False,
            "Required if any radio supports EHT; qualified feature and operation inputs pending",
        )
    for kind, name in (
        ("0xcc", "AKM Suite Capabilities"),
        ("0xa5", "Channel Scan Capabilities"),
        ("0xa9", "1905 Layer Security Capability"),
        ("0xb2", "CAC Capabilities"),
    ):
        add(
            kind,
            name,
            "agent",
            "applicability_review_pending",
            "§9.1 / §17.1.7 / §18",
            False,
            "Reconcile message inclusion, supported features and current Profile-1 conditions",
        )
    peer = inputs["controller"]
    try:
        unit = select_counter_units(
            controller_profile=peer["profile"],
            controller_kib_mib=peer["kib_mib"],
            preferred_scaled_unit=peer["preferred_scaled_unit"],
        )
        units = {
            "decision": "conditional_choice",
            "code": unit,
            "unit": ("bytes", "KiB", "MiB")[unit],
            "basis": "unqualified planning inputs; actual exchange is not observed",
        }
    except EmosaError as exc:
        units = {"decision": "unresolved", "code": None, "unit": None, "reason": str(exc)}
    return {
        "schema_version": 1,
        "scope": "EasyMesh 6.1 proposed Profile-1 requirement-family and selected inclusion audit",
        "input_assurance": "planning only; no live device or evidence verification",
        "default_unknown_radio": features is None,
        "profile_advertisement_ready": False,
        "qualified_easymesh_profile": None,
        "full_clause_audit_complete": False,
        "requirements": [
            {"id": i, "family": n, "sections": s, "status": st, "remaining": gap}
            for i, n, s, st, gap in REQUIREMENTS
        ],
        "ap_capability_report": {"ready": False, "obligations": rows},
        "wsc_m1_companions": {
            "source": "EasyMesh 6.1 §7.1 / §17.1.3",
            "ready": False,
            "required_per_radio": ["0x85", "0x11 (WSC M1)", "0xb4", "0xbe"],
            "note": (
                "M1 requires Advanced Capabilities independently of AP report QoS conditions. "
                "IEEE envelope and trusted exchange remain pending."
            ),
        },
        "counter_units": units,
        "blockers": [
            "IEEE_1905_base_and_amendment_pending",
            "mandatory_functions_incomplete",
            "qualified_target_feature_inputs_pending",
            "native_peer_profile_intersection_unresolved",
        ],
        "easymesh_wire_state": "blocked_P0",
        "controller_onboarding_proven": False,
        "physical_pod_proven": False,
    }


def markdown(report):
    lines = [
        "# EMOSA Profile-1 readiness audit",
        "",
        "Profile advertisement: **blocked**. Inputs are unqualified planning conditions.",
        "",
        "| Requirement family | Sections | Status | Remaining |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {r['family']} | {r['sections']} | {r['status']} | {r['remaining']} |"
        for r in report["requirements"]
    ]
    lines += [
        "",
        "| Capability value | Scope | Inclusion decision | Value component available |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {r['type']} {r['name']} | {r['scope']} | {r['decision']} | "
        f"{r['value_component_available']} |"
        for r in report["ap_capability_report"]["obligations"]
    ]
    lines += [
        "",
        "Counter units: " + json.dumps(report["counter_units"]),
        "",
        "M1 companions: 0x85, WSC M1, 0xb4, 0xbe per radio; AP report inclusion differs.",
        "",
        "Full clause audit, wire onboarding and physical-pod proof remain pending.",
    ]
    return "\n".join(lines) + "\n"
