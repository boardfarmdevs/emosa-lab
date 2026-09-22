# Independent report value references

`native-values.json` contains five independently extracted fields from the public
synthetic native wired baseline, frames 3 and 20. It is not a specification or a
complete positive conformance vector. The original capture is unchanged.

- Capture: `doc/evidence/peer-baseline/samples/wired/ethernet.pcap`
- Capture SHA-256: `6cf3010c059058c6c8a9abeb39bc6eedf46c68cca65c8e8e0010694c0c36b964`
- Fixture SHA-256: `a390028d7920a5835a1e9b71efcfc27ccd0bbb6128603d6b26620d324754db04`
- Extractor: [check-report-reference.py](../../../../scripts/check-report-reference.py),
  using tshark PDML and no EMOSA imports. Checked with tshark 3.6.2 and 4.2.2.

Run on HOST from the checkout root:

```bash
python3 scripts/check-report-reference.py \
  --report-capture doc/evidence/reports/run-01/right.pcap \
  --report-capture doc/evidence/reports/run-02/right.pcap
uv run pytest tests/test_wire_reports.py -q
```

Normal mode compares to the committed fixture. `--record` creates a new fixture
exclusively; it does not overwrite this file. Review new reference provenance
before changing tests. Synthetic SSIDs and identities in this fixture are public
lab values, not physical-pod credentials.

The AKM lists are empty, which does not demonstrate PSK support. Device Information
contains Wi-Fi 6 media entries with ten specific bytes; selected EasyMesh 6.1
Table 14 requires zero, so EMOSA tests retain this as a **negative**. The separate
complete-message inspector also finds missing cipher/bridge TLVs. Read the
[report contract and normative sources](../../../../doc/protocol/reports.md).

The optional `--report-capture` arguments independently check the two retained
AF_PACKET receiver traces: types, MIDs, security values, required TLV inventory,
media lengths, bridge tuple and decoded BSS fields. They are not native controller
acceptance checks.

Use `--coordinator-directory doc/evidence/coordinator/run-01` (and `run-02`)
to independently inspect the newer database-backed coordinator traces: both
directions, Early Report retries, matching Ack, State-derived old/old/new SSIDs
and the Query after disconnection with no captured response. No native controller
is used in that experiment.
