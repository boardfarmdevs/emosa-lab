# Authenticated WSC provisioning component — 2026-09-22

An independent synthetic hostap payload peer supplies fresh M2 settings. EMOSA
authenticates the complete request, creates a durable `wsc-component` operation
and changes a real disposable OpenSync-schema database. A separate simulated
manager publishes State. No semantic request supplies the change.

- [Summary](summary.json): source/tool hashes, scope, findings and remaining work.
- [Unit](unit-results.xml) and [OVSDB](ovsdb-results.xml): **920 / 53** passing tests;
  22 new handoff units and four real-database cases.
- [Run 1](run-01/summary.json) and [run 2](run-02/summary.json): each contains
  `configure.json`, `lost-reply.json`, `identity-race.json` and
  `crash-after-commit.json`. Frames are serialized Ethernet bytes delivered in
  memory, not packet-socket captures. Each run uses fresh exchange material.
- [Crash example](run-01/crash-after-commit.json): real SIGKILL after commitment,
  durable `SUBMITTED` recovery, zero writes after restart, fresh-State resolution
  with unknown commit attribution, and old M2 rejected against new M1.
- [Identity race](run-01/identity-race.json): one attempted transaction, zero
  configuration changes; original Config survives the atomic guard failure.
- [Installed wheel](wheel-check.json): all 71 Python source files and the new
  receipt schema match the wheel; all four cases pass outside the checkout with
  isolated Python and frozen dependencies.
- [Independent existing vectors](independent-reference.txt): unmodified hostap
  reproduces the retained cryptographic and M1/M2 vectors. The new peer's archive,
  harness and binary digests are recorded in each run's summary.
- [Full-wire gate](wire-gate.json): still blocked, exit 5, zero operations.

The first three cases also reject invalid authentication, encrypted teardown,
multiple M2s and unsupported configuration companions before any operation or
credential file exists. Identical and re-encrypted valid retries reuse one
operation across MIDs. Changed authenticated duplicates do not create a second
operation. The public result excludes PSKs, secret references, keyed credential
fingerprints, raw journals and ephemeral private keys.

The retained host runs are development runs 06/07. Earlier findings, including
overly strict observation-generation pinning after a lost reply, are recorded in
the summary. Historical evidence in other collections is unchanged.

There is **no native controller, Ethernet socket, radio/client observation or
physical pod** in these runs. The separate discovery lifecycle still withholds
automatic Early Report/M1 admission; this component does not change that gate.
See the [walkthrough](../../protocol/wsc-provisioning.md) and
[manual §13.12](../../guides/team-manual.md#1312-turn-authenticated-wsc-input-into-a-durable-operation).
Next join admitted native-controller discovery/capabilities/WSC to this handoff,
then the existing hwsim/client path, before substituting a qualified unchanged pod.
