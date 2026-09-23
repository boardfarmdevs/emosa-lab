# Independent IEEE 1905 envelope vectors

`native-envelope.json` contains tshark's projection of all 59 frames from the
already published synthetic native wired capture. It includes complete header
fields and, at the frame that completes a message, TLV types and lengths.
There are 58 messages; one WSC message spans two frames. No TLV values, WSC
settings or specification text are copied into this fixture.

`provenance.json` records source/vector/extractor hashes and the two independently
run dissector versions. Reproduce with `python3 scripts/check-ieee1905-reference.py`.
The extractor imports no EMOSA module. The Python tests then compare EMOSA's
headers and completed boundaries against that independent projection.

The hand-derived literal discovery vector and malformed/fragment cases are in
`tests/test_ieee1905.py`. Normative authority is the
[reviewed IEEE/WFA contract](../../../../doc/protocol/ieee1905-envelope.md), not
tshark or the native peer. Full WSC/profile/procedure conformance is not asserted.
