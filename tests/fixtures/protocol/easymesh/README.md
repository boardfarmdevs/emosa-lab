# Selected native EasyMesh value fixtures

`native-values.json` contains nine values from frames 1, 2, 7 and 20 of the
already published synthetic native-peer wired capture. They cover Supported
Service, Searched Service, Radio Identifier, Operational BSS and Multi-AP Profile.
Wireshark supplies both bytes and expected field interpretation; EMOSA supplied
neither. These are native implementation cross-checks, not independent normative
full-message vectors or evidence of EMOSA onboarding.

The [provenance](provenance.json) pins the capture, extraction script, resulting
fixture and original tshark version/digest. Recheck with:

```bash
python3 scripts/check-easymesh-reference.py
```

This command uses a local tshark, verifies the pinned artifacts, and compares
results without importing EMOSA. A differing dissector is recorded and must
produce the same selected fields; its full-message behavior is not qualified by
this check. The script delegates frame 7's reassembly to tshark.

The captured native sender includes reserved service `0xA1`. The receiver tests
retain it but ignore it as a role; encoder tests require rejection under EasyMesh
6.1 §3.1.2. The fixture deliberately preserves the observed sender behavior.
Only already public synthetic MAC identities and SSIDs are included; no WSC
settings or credential fields are extracted.

See the [component contract and exercise](../../../../doc/protocol/easymesh-payloads.md)
for exact normative references and limitations. Additional hand-derived table
examples in `tests/test_easymesh_payloads.py` exercise empty counts, multi-radio
structure and opaque SSID bytes without pretending they came from a peer.
