# Authenticated radio payload interpretation

`src/emosa/wsc_radio.py` interprets the encrypted Multi-AP roles in one received
M2 payload set. It distinguishes BSS configuration from radio teardown and can
extract candidate values for the existing fronthaul WPA2-PSK mapping. **It is not
an onboarding exchange, a complete radio mapping, or permission to configure a pod.**

The component selects the following rules from the obtained publisher editions;
the complete IEEE/EasyMesh procedure gate remains pending.

| Rule | Source |
| --- | --- |
| Verify every M2 using the original M1; one configuration payload per BSS, bounded by the declared radio limit | EasyMesh 6.1 §7.1, pp.67–69; WPS 2.0.10 §7.2–3 and §7.5 |
| Multi-AP role subelement 0x06, length 1, within WFA vendor 0x00372A in encrypted ConfigData | EasyMesh §7.1 Table 20, p.68 |
| Preserve fronthaul, backhaul, station, teardown and profile association flags; ignore reserved bits 1:0 | EasyMesh §3.1.2, p.16, and §7.1 Table 20 |
| Teardown consists of one M2 and ignores other configuration attributes | EasyMesh §7.1, pp.68–69 |
| Ordinary AP settings, optional password changes and ignored legacy Network Key Index | WPS §8.3.9 Table 20, p.81 |
| Passphrase and raw PSK representations; compatibility with one legacy trailing NUL | WPS §12, Network Key/Table 40, p.119 |

## Interfaces and interpretation order

`decode_radio_payloads(transcript, tuple_of_m2_payloads, max_bss=...)` first
authenticates and decrypts **every** payload, including its Key Wrap Authenticator.
Only then does it interpret encrypted role fields. It rejects missing, malformed,
duplicated or unencrypted role fields without returning a partial result.
Those ambiguity checks use internal EMOSA reasons; they are not wire error codes.

A teardown result contains no BSS settings. Ordinary AP fields are not required
or interpreted for that result; M2 envelope and cryptographic checks still apply.
A mixed teardown/configuration batch is rejected. Ordinary configuration results
retain every BSS, its BSS index, role flags and authenticated AP attributes.
Optional unknown WPS data remains subject to WPS compatibility rules.

`result.existing_fronthaul_candidate()` returns an in-memory SSID/passphrase pair
only for one pure fronthaul BSS, exact WPA2-Personal/AES, and corresponding
authentication/encryption support in the original M1. It refuses backhaul or
station roles, association restrictions, password-change attributes, multiple
BSSs, teardown and extended security requiring companion TLVs. It does not
reduce a larger request to one convenient BSS or downgrade security.

The candidate uses the current mapper's UTF-8 SSID and 8–63 printable ASCII
passphrase limits. A 64-hex raw PSK, a binary SSID or another unsupported
representation produces `UNSUPPORTED_OPERATION`, not a protocol-invalid claim.
A single legacy passphrase terminator is removed as specified; other padding
and overlong keys are not truncated. Candidate and result representations hide
secret data, but the objects still contain secrets and must not be serialized
into ordinary logs or reports.

`M1Transcript.authenticate_m2_envelopes()` is the supporting authentication layer.
The existing `authenticate_m2()`/`authenticate_m2_batch()` interfaces retain their
ordinary AP-field validation. Neither interface verifies controller trust or
prevents reuse across exchanges.

## Remaining binding and acceptance work

A single received M2 does not prove that the target radio has only one BSS. Before
using a candidate, the wire path must validate the entire CMDU, peer/exchange and
radio identifiers, all accompanying TLVs, profile requirements, and replay/retry
state. In particular, BSS indexes can correlate companion RSN or advanced BSS
configuration; extracting the M2 alone cannot validate that combined request.

The physical mapper must then establish actual radio/BSS inventory, exclusive
configuration ownership, backhaul and management dependencies, complete requested
scope and suitable transaction guards. It must store approved credentials through
the private secret mechanism before a supported operation can run. This module
does not use M2's MAC attribute to infer a pod, radio or VIF identity. No new path
to the operation engine or OVSDB writes is enabled.

These requirements remain in the [protocol matrix](protocol-matrix.json) and
[pod qualification instructions](../guides/pod-qualification.md). The end state still needs
real controller discovery/onboarding and independently observed behavior on an
unchanged physical OpenSync extender. The [acquisition checklist](specification-acquisition.md)
now marks both IEEE editions obtained and lists remaining dependencies.

## Independent evidence

The existing native hostap 2.11 fixture still generates the identical 440-byte
M1 and 594-byte configuration M2. It now checks the decrypted fronthaul role and
generates a separate 509-byte teardown alternative. Native message authentication,
decryption, KWA and role parsing check that alternative. Hostap's ordinary
AP-settings validator is deliberately not used to impose AP fields on teardown.
This older implementation does not qualify the selected EasyMesh procedure.

The [fixture provenance](../../tests/fixtures/protocol/wsc-messages/provenance.json)
records its source, harness and vector hashes. All entropy and credentials are
public synthetic values. The two M2 alternatives are separate tests, not a valid
combined response with reused N2. `tests/test_wsc_radio.py` adds 42 checks covering
positive interpretation, whole-set authentication, malformed/ambiguous roles,
reserved bits, teardown, multiple BSSs, unsupported scope/security, M1 capability
consistency and secret handling. Run:

```sh
uv run pytest tests/test_wsc_messages.py tests/test_wsc_radio.py
uv run python scripts/check-wsc-reference.py
```
