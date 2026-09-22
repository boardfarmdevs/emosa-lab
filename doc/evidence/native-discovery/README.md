# EMOSA Search answered by the native controller — 2026-09-22

Two clean runs of the pinned native controller answer EMOSA's bounded Profile-1
Search with Profile 1. The controller creates a device object for AL
`02:00:00:00:30:01`; that object has **zero radios and zero BSSs**. This establishes
initial native discovery visibility. It does not establish WSC onboarding, a
qualified profile, OpenSync configuration or physical-pod behavior.

- [Summary](summary.json) records scope, source hashes, native source review,
  selected runs, 937 passing unit tests and development failures.
- [Run 06](run-06/result.json) and [run 07](run-07/result.json) retain transport
  readiness, probe outcomes, controller inventory projections and cleanup.
- Each run includes `controller-before.json`, `controller-after.json`,
  `probe.json` and an independent `ethernet.pcap`. The two captured frames are
  Search and Response with MID 65535. Capture timestamps are actual tcpdump
  times, unlike the earlier synthetic-timestamp packet-worker captures.
- [HOST check](independent-host.json) and [VM check](independent-vm.json) reproduce
  the independent tshark assertions on versions 3.6.2 and 4.2.2. The checker
  imports no EMOSA implementation.
- [Full-wire gate](wire-gate.json) remains blocked with zero operations.

The new device object can already say `EasyMeshAgentOperationMode: Running`.
That string is not a complete onboarding verdict: its radio count is zero, no
BSS exists and the capture has no WSC. The controller's actual response still
has capability byte `0x40` and no Security Capability TLV. No native binary or
source was modified to produce this result.

The implementation waits for a packet socket owned by the native transport;
the inventory API can be ready before the transport. It also waits for the
independent capture to retain the Response. Earlier attempts exposed both
readiness and capture buffering problems and are not counted as accepted runs.
One early assertion incorrectly expected no device entry after Search; that
failed attempt led to the explicit discovered-entry versus radio/BSS distinction.
The detailed development history remains in the summary and private VM run folders.

Temporary probe namespaces/veths are removed. The owned controller services are
stopped and collected; their existing shutdown aborts remain explicitly reported.
All containers, assigned radios and earlier evidence are preserved. The public
allowlist contains no native logs, hostap configurations, database or credentials.
The native controller's colocated helper uses the existing hwsim radio for startup;
no external native agent, simulated OpenSync pod or physical device participates.

Follow the [step-by-step guide](../../guides/native-discovery.md) to reproduce the
measurement. Next resolve native capability behavior and the selected procedure
contract, then join admitted discovery to the tested WSC-to-radio path and require
complete controller radio/BSS inventory and independent client behavior.
