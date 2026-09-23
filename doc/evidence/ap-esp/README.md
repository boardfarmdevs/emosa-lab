# Native sparse AP service-field parser evidence

The optional native controller candidate now receives all eight permitted ESP
presence combinations without reading absent entries. This fixes a demonstrated
SIGSEGV on BE+VI. **These synthetic parser diagnostics do not qualify AP sensors,
ESP conversion, complete reporting or sustained acceptance.**

Use the [reproduction guide](../../protocol/native-ap-esp.md),
[manual §13.32](../../guides/team-manual.md#1332-verify-that-the-native-controller-can-receive-sparse-ap-service-fields)
and learning-path step 22. The affected code is in the C++ evaluation controller;
EMOSA remains the separate EasyMesh-to-OpenSync adapter.

## Requirement and defect

EasyMesh 6.1 §17.2.22/Table 45 places mandatory BE, then optional BK, VO and VI
three-octet entries consecutively. An absent entry contributes no bytes. The
baseline controller uses offsets 0, 3, 6 and 9 regardless of presence. Its TLVF
accessor returns null for index 9 in a six-octet BE+VI array, and `set_esp` reads
that pointer without checking it.

The optional `0007-controller-ap-esp-presence.patch` validates BE and exact array
length before updating this TLV's BSS ESP/utilization values. It then copies each
present entry at the next available offset. It preserves the opaque octets and
the selected x86 model's existing low-byte integer representation. Semantic
IEEE-to-EasyMesh ESP conversion remains pending; this patch does not settle it.

## Compiled tests and retained failed build

[Build 02](build-02/candidate.json) records pinned archives, header commits,
patches, tools, source/recipe hashes and output binaries. Its
[independent provenance check](build-02/independent-provenance-check.json)
resolves eight patched sources and six recipes. The
[compiled regression](build-02/ap-esp-regression.json) parses real TLVF objects
using the same helper called by the controller:

- Eight valid presence combinations preserve each category's distinct bytes.
- 232 invalid presence/length combinations are rejected without changing output.
- The baseline fixed VI offset is independently observed to return null.

The existing 12 counter-conversion checks and actual baseline/candidate BPL
lifetime comparison also pass. These component tests establish selected behavior,
not a full native application or physical measurement result.

Build 01 compiled and its regression returned success, but the build wrapper
failed to parse stdout because the expected TLVF bounds diagnostic preceded
JSON. Its [exact raw output](build-01/ap-esp-regression-invalid-json.txt), build
log, exception and executed recipes are retained. Build 02 directs that expected
diagnostic to stderr and keeps JSON alone on stdout. No assertion or bounds check
was removed. Build 01 was never installed.

## Actual native A/B trials

Both trials first run real controller discovery/WSC through EMOSA to the owned
OpenSync-schema simulator, configure the separate hwsim manager and verify
independent clients. The worker then stops. A separate parser probe emits
synthetic single-TLV `0x800C` frames from the owned virtual-agent interface.
These frames deliberately isolate the AP Metrics parser; they are not complete
AP reports or a qualified telemetry source. The mode cannot accompany a soak.

| Trial | Result |
| --- | --- |
| [Baseline: native-ap-esp-old-01](native-ap-esp-old-01/independent-ap-esp-check.json) | The first BE+VI frame is captured; controller exits with core-dump/SIGSEGV 11 and inventory read fails |
| [Candidate: native-ap-esp-new-01](native-ap-esp-new-01/independent-ap-esp-check.json) | All 16 packet/inventory cases pass; controller/helper and supporting native services exit success/0 |

The baseline failure retains its one expected cleanup-observation error: the
process-mapping observer could no longer read the exited controller. The
controller wrapper still restores the original files. Actual post-restoration
hashes and an idle check pass for both trials. No core dump, process memory,
private configuration or credentials are published.

The corrected run has 38 Ethernet and 1,666 radio records, with matching
capture/filter totals and zero reported drops. The independent checker imports
no adapter code. It checks literal TLV layouts, actual packet timing, the correct
virtual device/BSS, later native inventory and observation before the next test.
Eight valid combinations update exactly the present categories. Eight malformed
arrays leave ESP and utilization unchanged. Missing categories retain their
previous model value; that behavior does not prove freshness of an absent field.

Both [baseline](native-ap-esp-old-01/independent-source-check.json) and
[candidate](native-ap-esp-new-01/independent-source-check.json) runtime audits
resolve all **108 input hashes** against the repository and actual trial reference.
The candidate controller digest is
`cec287b95d588ca715c7384a8bd6985f9b492e192becdf69b6e708067afc2b9d`.
Its rebuilt BPL digest is
`f25812f74db0996ae8bfb15360a775d536aee9f3c2ff44f5264f4dafe301582f`;
the baseline BPL restores to
`2e4d0b2ef7dc68e11ad8e6686418555100768634f7462a24f6bb454c77b42cf6`.

## Remaining work

The retained unit execution has **1,472 passing tests**, including corruption
checks for wrong categories, stale inventory, changed wire bytes, false values
and wrong peers. CI replays both native outcomes and the existing historical
regressions. The prior 60-test OVSDB evidence remains separate.

The selected native receiver can now handle sparse ESP arrays. AP utilization,
BE ESP estimation/subfield conversion, Data Elements-dependent AP/radio/STA
measurements, final-session counters and complete native reporting remain
unqualified. The acquisition checklist now explicitly includes an asymmetric
ESP conversion clarification. The pinned native nl80211 HAL's ESP implementation
is still a TODO, so it cannot supply that missing estimator by reuse alone.

After qualifying those sources, repeat the integrated 15-minute acceptance with
all required reports enabled. The eventual physical proof remains real EasyMesh
messages → EMOSA → unchanged OpenSync pod → independently observed behavior.
