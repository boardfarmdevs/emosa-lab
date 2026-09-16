# Independent synthetic M1/M2 payloads

These are complete WSC payloads for an AP, **not IEEE 1905 frames or physical
captures**. All identities, capabilities, keys, passphrases, nonces and IVs are
public synthetic test values. Never use their deterministic entropy in a lab
exchange. Actual pod/controller credentials do not belong here.

`reference.c` includes the unmodified hostap 2.11 enrollee source so it can call
the native static M1 builder. M2 assembly uses unmodified hostap attribute and
crypto functions plus the explicitly encoded new WFA BSS_Index field. Independent
strict M1/M2/AP-settings validators check the payloads; native HMAC, encryption
and KWA functions verify their integrity. The harness checks negative validation
as well, so a disabled/no-op strict validator cannot produce a passing result.
`provenance.json` pins the official archive, source files, build defines, harness
and vector hashes. The full archive hash also covers included upstream headers.

The fixture also contains a separately authenticated teardown M2 alternative.
Native parsing verifies both fronthaul and teardown flags in decrypted ConfigData.
The teardown alternative omits ordinary AP fields; hostap checks its envelope and
crypto, not its normal AP-settings validator. The alternatives reuse synthetic
M1/N2 material for separate tests and must not be combined into a response batch.
See [radio payload interpretation](../../../../docs/wsc-radio.md).

The normative contract is [wsc-messages.md](../../../../docs/wsc-messages.md).
Hostap 2.11 predates the selected WFA editions. Its parser ignores BSS_Index as
an unknown attribute; the harness does not establish its semantic interpretation
by a real peer. These tests do not complete P0 or any physical-pod milestone.

Reproduce both crypto and payload references with:

```sh
python3 scripts/check-wsc-reference.py
uv run pytest tests/test_wsc.py tests/test_wsc_messages.py tests/test_wsc_radio.py
```

The script downloads the pinned official hostap archive into a temporary
directory, verifies its digest and builds offline executables. It needs a C
compiler and OpenSSL development headers. It starts no daemon, radio or network
exchange. Use `--archive /absolute/path/hostapd-2.11.tar.gz` for an existing
digest-matching archive or `--fixture wsc-messages` for only this harness.
