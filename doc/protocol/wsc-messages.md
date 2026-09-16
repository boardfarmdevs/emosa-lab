# WSC M1/M2 payload component

`src/emosa/wsc_messages.py` builds an M1 payload and authenticates M2 AP settings
against that exact M1. It accepts a bounded set of M2 payloads together and
returns settings only after every payload passes its checks. It has no socket,
OVSDB, journal or lab-controller dependency. **This is not yet an IEEE 1905
exchange, an onboarding state machine or permission to configure a pod.**

The component selects WPS 2.0.10 for the rules below, plus the explicitly named
EasyMesh 6.1 payload extensions. Publisher references/digests and this limited
selection are recorded in [the protocol matrix](protocol-matrix.json). The full
IEEE/EasyMesh procedure selection and P0 remain pending; see the consolidated
[acquisition checklist](specification-acquisition.md).

| Implemented rule | Normative source |
| --- | --- |
| Required M1/M2 fields and their byte lengths | WPS §8, §8.3.1–2 Tables 8–9, §12 Table 28; pp.67, 74–75, 100–103 |
| Fresh enrollee nonce/public key; exact M1 transcript in the M2 authenticator | WPS §7.2–3, §8.3.1–2; pp.48–53, 74–75 |
| Authenticate before releasing decrypted, KWA-verified settings | WPS §7.2, §7.5, §8.3.9 Table 20; pp.48–49, 57, 81 |
| AP SSID/authentication/encryption/key/MAC fields; password-change conditional field | WPS Table 20 and Table 28; pp.81, 100–103 |
| Ignore unrecognized optional attributes, preserving original authenticated bytes | WPS §8 and §12; pp.67, 100 |
| Emit Version 0x10 / Version2 0x20; do not reject a received payload solely for a version mismatch | WPS §7.9, §12 Table 29 and Version2; pp.66, 104, 130 |
| WFA vendor/subelement framing; output strings without NUL padding; OS reserved high bit | WPS §12 Tables 28–29 and field definitions; pp.100–104, 120, 130 |
| M1 enrollee MAC supplied as the represented AL MAC; RF-band extension 0x08 | EasyMesh §7.1; pp.67–68 |
| Optional BSS_Index 0x1BBC immediately before M2 Authenticator | EasyMesh §5.3.6 Table 10, §7.1 Table 19; pp.51, 67 |
| Distinct N2 values and, when present, distinct BSS indexes within one radio's received M2 set | EasyMesh §7.1; pp.67–69 |

## Interface and limits

`M1Transcript.create(M1Device(...))` requires explicit identity, device metadata
and capability fields. There are no synthetic device defaults. The constructor
generates fresh ephemeral key material and the nonce, encodes the complete M1,
and retains the exact bytes for authentication. It does not derive these facts
from schema column names or claim that caller-supplied capabilities are qualified.

`transcript.authenticate_m2(payload)` checks required field structure, the
enrollee nonce, the transcript/public-key binding, the message authenticator,
encrypted settings and their Key Wrap Authenticator. A normal WPS M2 without
AP settings returns `UNSUPPORTED_OPERATION`: the longer WPS registration flow
is not implemented here. This is an internal error reason, not an invented
EasyMesh response code.

`transcript.authenticate_m2_batch(tuple_of_payloads, max_bss=...)` additionally
checks count, nonce uniqueness and BSS-index uniqueness. It returns the entire
tuple or raises without returning partial settings. The caller must supply the
qualified radio limit; the component imposes an additional **16-payload local
admission ceiling**, which is not an advertised radio capability. Each payload
uses the crypto component's 1 MiB/4096-attribute budget, and WFA parsing has a
4096-subelement ceiling. Duplicate consumed singleton fields are rejected as a
local ambiguity rule. These limits are not IEEE fragmentation or retry rules.

`AuthenticatedM2` and `APSettings` preserve optional and unknown attributes,
including configuration-affecting extensions. SSIDs and keys remain bytes;
they are never truncated or implicitly converted into the simulator's UTF-8
representation. Default object representations hide payloads, identifiers and
key values. Returned objects still contain secrets: do not serialize them into
reports, log them, or use `dataclasses.asdict()` as a redactor. The later mapping
must place approved credentials in the local secret mechanism and use references.

## Checks still required before an operation

The future wire procedure must establish controller trust and bind its peer,
radio identifier, complete CMDU and current exchange to this transcript. It must
implement timers, retry/duplicate handling, restart and replay control. Repeating
a call to this pure authentication component is not replay protection.

The component checks payload structure and cryptographic integrity. It does not
approve every received capability/flag value or interpret all optional actions.
The full procedure must validate supported authentication, complete radio BSS
scope, Multi-AP role/teardown bits, password changes, backhaul dependencies,
traffic separation and all accompanying TLVs. Unknown optional WPS data remains
in authentication and output; this does not permit ignoring a mandatory EasyMesh
request. In particular, the one-existing-BSS OVSDB patch cannot apply a convenient
part of a larger radio configuration. No path from these results to OVSDB writes
is enabled. The separately selected [radio payload interpreter](wsc-radio.md)
now distinguishes encrypted roles/teardown and extracts a narrow candidate; all
peer/radio and complete-request mapping checks above still apply.

## Independent validation

[The retained fixture](../../tests/fixtures/protocol/wsc-messages/README.md) uses
unmodified hostap 2.11 source. Its native enrollee builder produces a 440-byte
M1 that EMOSA reproduces byte-for-byte under injected synthetic test entropy.
Its helpers produce a 594-byte M2, including encrypted AP settings, and its
strict M1/M2/settings validators and authenticator/KWA routines check the result.
Negative assertions confirm the strict validators reject a missing Version field.

Hostap is an independent implementation cross-check, not the specification.
Its older parser does not interpret the new BSS_Index: that field's placement
and value come from the selected WFA text and are tested separately. The fixture
does not establish full EasyMesh 6.1 field support or a live controller exchange.
No hostap code is linked into the EMOSA package.

Python tests also exercise correctly authenticated malformed payloads, identity
and nonce mismatches, duplicate/missing fields, unknown extensions, version
compatibility, conditional password fields, size bounds and multi-M2 rejection.
The [retained component evidence](../evidence/wsc-messages.json) states exact scope.
The physical acceptance path remains real EasyMesh messages → EMOSA → unchanged
OpenSync pod → independently observed behavior.
