# Synthetic WSC cryptographic vectors

These are **payload fragments**, not complete M1/M2 messages, EasyMesh CMDUs,
controller captures or conformance vectors. Every key, nonce, IV and identifier
in `vectors.json` is a deliberately public test constant. None is a pod secret.

Normative rules are selected from WPS 2.0.10 in
[the component contract](../../../../doc/protocol/wsc-component.md). Expected bytes were
generated separately by compiling the unmodified WPS routines from the official
[hostap 2.11 release](https://w1.fi/releases/hostapd-2.11.tar.gz), with OpenSSL
3.0.2 and GCC 11.4.0 on Ubuntu 22.04. `provenance.json` records the archive and
source digests. The hostap source is not redistributed here.

`reference.c` calls upstream `wps_derive_keys`, `wps_build_authenticator`,
`wps_process_authenticator`, `wps_build_key_wrap_auth`,
`wps_build_encr_settings`, `wps_decrypt_encr_settings` and
`wps_process_key_wrap_auth`. Public keys are generated using OpenSSL's RFC 3526
group constant and big-number API. A test-only random callback supplies a fixed
IV so the ciphertext is reproducible. No EMOSA code is linked or imported.

The cases exercise ordinary synthetic exponents and a small-exponent boundary
case whose shared secret needs 191 leading zero bytes before hashing. The
authenticated fragment contains an unknown attribute, which remains in the MAC
input. The harness verifies valid tags and rejects altered message/KWA tags;
it also emits bad-KWA ciphertext with valid padding and independently encrypted
zero-padding ciphertext for EMOSA's rejection tests. It does not claim that
hostap's permissive padding decoder rejects the latter.

Reproduce using a C compiler and OpenSSL development headers:

```sh
uv run python scripts/check-wsc-reference.py
# Or use an existing publisher archive matching the pinned digest:
uv run python scripts/check-wsc-reference.py --archive /absolute/path/hostapd-2.11.tar.gz
uv run pytest tests/test_wsc.py
```

The check downloads only the pinned publisher archive when needed, builds in a
temporary directory and compares every emitted value with the retained JSON.
It starts no daemon, opens no lab connection and writes no pod settings. Normal
unit tests use the retained vectors without downloading or compiling hostap.
