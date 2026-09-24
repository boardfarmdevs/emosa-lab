# Conformance vectors

Recorded inputs and the exact outputs the [specification](../README.md)
requires. The Python reference implementation reproduces all of them
(`tests/test_conformance.py`). An implementation in another language
conforms when its own harness reproduces them too.

| File | Spec | Input | Expected |
| --- | --- | --- | --- |
| `al-mac.json` | §2.2 | a serial number and the AL MACs already taken | the agent's AL MAC |
| `operation-transitions.json` | §5 | none | the legal operation state transitions and the active states |
| `fleet.json` | §4 | a fleet configuration and pods arriving in order (their `AWLAN_Node` rows) | per arrival: the registry entry (without timestamps), the agent configuration, and the `manager_addr` update |
| `translation-northbound.json` | §2.6, §3.3 | the pod's OVSDB rows, plus the agent's parameters | the device view, the capability TLVs, the Device Inventory TLV, and the Topology Response TLVs for each message set |
| `translation-southbound.json` | §3.2, §3.4 | the pod's OVSDB rows, a pod profile, and an accepted M2 intent | the result status and every OVSDB transaction sent, operation by operation |

Formats:
- **OVSDB rows** use raw RFC 7047 JSON, `{table: {uuid: row}}`, exactly as a
  monitor or select returns them. They are real rows from an opensync-lab
  pod (OpenSync 6.6.1.0), with keys replaced by placeholders, plus two
  derived cases:
  - `fronthaul-and-backhaul`: a backhaul slot VIF is added;
  - `cold-pod`: the fronthaul VIF is removed.
- **TLVs** are `{"type": "0x..", "value": "<hex of the value only>"}`, in
  message order.
- **MAC addresses** are lowercase `aa:bb:cc:dd:ee:ff`. Other byte strings are
  plain hex.
- **The device view** lists radios by RUID and BSSes by interface name. The
  topology vectors report every BSS of the view in that order.
- **Passphrases** are public test values, given by reference in `passphrases`.
  A vector never contains a real one.
- **Station ages** are seconds since association. Values above 65535 are
  reported as 65535.

To regenerate the vectors after an intended change of behaviour, which must
start with a change to the specification:

```sh
uv run python -m emosa_lab.conformance generate
uv run python -m emosa_lab.conformance check
```

Not covered by vectors yet, but by the reference implementation's tests:
- the WSC M1/M2 cryptography, which has independent hostap vectors in
  `tests/fixtures/protocol/wsc-messages`;
- onboarding message sequences;
- the operation engine's timing rules.
