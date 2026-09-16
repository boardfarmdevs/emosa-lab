# WSC cryptographic component

This component selects **WPS 2.0.10** for the payload cryptography described
below. Its publisher PDF and digest are recorded in
[protocol-matrix.json](protocol-matrix.json). This selection is limited to the
component; the proposed IEEE/EasyMesh procedure profile is still unfrozen and
P0 remains blocked. No Ethernet messages or pod writes are enabled by it.

The separate [M1/M2 payload component](wsc-messages.md) now uses these primitives
to build M1 and authenticate/decrypt sets of M2 AP settings. Its own required
field checks and independent payload vectors remain separate from full wire
procedure and physical-pod qualification.

| Rule | WPS 2.0.10 reference | Component behavior |
| --- | --- | --- |
| Attribute encoding | §8 and §8.1, p.67; §12 Table 28, pp.100–104 | Two-byte type and length, big endian; preserve unknown attributes and original authenticated bytes; reject truncation |
| Group and key derivation | §7.3, pp.51–53 | 1536-bit MODP group 5, generator 2; 192-byte public/shared values; SHA-256, KDK bound to N1/enrollee MAC/N2; 640-bit WPS KDF split into AuthKey, KeyWrapKey and EMSK |
| Message authenticator | §7.2, pp.48–49; §8.3.2 Table 9, p.75 | HMAC-SHA256 over previous message and current message without its final Authenticator attribute; compare first eight bytes |
| Encrypted settings | §7.2, p.49; §7.5, p.57; §12, pp.114–115 | Fresh random IV, AES-128-CBC, PKCS#5 v2 padding (16-byte block); final Key Wrap Authenticator TLV, verified before returning plaintext |
| Identity for eventual EasyMesh use | EasyMesh 6.1 §7.1, pp.66–69 | The future procedure supplies the represented agent's AL MAC as the enrollee MAC, and a unique registrar nonce for each M2 |

The library supplies Diffie-Hellman and AES; Python's maintained standard
library supplies SHA-256, HMAC, constant-time tag comparison and OS randomness.
No private-key operation or block cipher is implemented from scratch. Group-5
parameters are fixed by WPS. Peer range and subgroup membership are checked
before the library exchange: the qualification test found that supplying `q`
alone did not make the library reject a nonmember. The membership predicate uses
Python's built-in modular arithmetic on **public values only**, following
[RFC 2785 §3.1](https://www.rfc-editor.org/rfc/rfc2785.html#section-3.1).
This extra check and rejection of duplicate authentication trailers are local
defensive admission rules, not new protocol error codes.

The decoder has a 1 MiB input and 4096-attribute component resource budget.
These are bounded admission limits, not IEEE fragmentation or WSC message-size
rules. Encoders reserve capacity for the authentication trailer, and encrypted
settings including IV and padding must fit the TLV's two-byte length field.
Authentication trailers must be unique and final. Unknown attributes
remain byte-for-byte in authentication inputs. Decryption failures have one
public error and return no plaintext. Key containers and attribute values are
excluded from default object representations; Python cannot guarantee erasure
of immutable secret bytes, so production secret-memory handling remains a
qualification item.

This is **not a complete M1/M2 validator or onboarding state machine**. It does
not establish controller trust, enforce profile-specific fields/order, bind a
response to a live exchange, detect replay, interpret BSS settings, or authorize
OVSDB writes. Those checks must precede any semantic operation. Full-radio BSS
scope, mandatory capability evidence, IEEE framing/timers and independent
controller testing remain required. A valid payload authenticator alone does
not authenticate an otherwise untrusted controller.

Validation uses separately generated, synthetic hostap 2.11 reference vectors
for DH derivation, message authentication and encrypted settings. The pinned
reference harness and provenance live under `tests/fixtures/protocol/wsc/`.
Its C code is test-only and is not an EMOSA build/runtime dependency. hostap is
an implementation cross-check; WPS remains the normative source. These payload
vectors are not complete EasyMesh frames or interoperability captures.
