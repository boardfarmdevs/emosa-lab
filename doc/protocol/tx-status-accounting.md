# Explain the retry counter using kernel completion flags

Follow [the medium-loss exercise](medium-loss-accounting.md) first. That run
establishes a real discrepancy: the userspace medium returns more retry attempts
than the kernel's final station counter reports. Matching frame counts or a
successful client connection does not explain why those integers differ.

This exercise observes the missing context at the point where mac80211 processes
transmit completion. It is passive instrumentation of the owned VM kernel. It
does not modify radio configuration, packet contents, status flags or counters.
No physical pod is connected or changed, and kernel lockdown remains enabled.

## What the extra observation means

An A-MPDU combines MPDUs into an aggregate transmission. The mac80211 control flag
`IEEE80211_TX_CTL_AMPDU` indicates that a frame should be sent as part of one.
The completion flag `IEEE80211_TX_STAT_AMPDU` indicates aggregate status.
These are different bits with different meanings; neither is the retry bit in
an individual captured 802.11 header.

The reviewed Ubuntu kernel's `ieee80211_tx_get_rates()` has a deliberate rule:
when the control flag is set but aggregate completion status is absent, that
per-frame completion contributes zero retries. This prevents treating every
member of an aggregate as carrying the aggregate's complete retry information.
The hwsim userspace netlink API supplies attempt chains and ACK status but does
not expose that original control flag. Reading just netlink and final counters
therefore leaves an essential part of the calculation unobserved.

The additional probe records the fields at entry to `ieee80211_tx_status_ext()`,
before the kernel applies that rule:

- Original control/completion flags and whether a station context exists.
- The four packed rate entries and the hardware's report limit.
- MPDU length and the first 24 header bytes for frame correlation.
- Kernel monotonic timestamp.

The independent checker joins each completion to one captured medium status using
the frame header, length, association lifetime and timestamp. It checks that the
rate chains and ACK flags match, applies the exact kernel rule, and compares the
sum with the actual removal-time retry counter. Unmatched, ambiguous, late or
missing records fail the check. Capture and trace-buffer loss also fail it.

Explaining this arithmetic does not make the raw retry field suitable for
EasyMesh. A value that omits simulated attempts cannot silently become a complete
retry measurement. The online publisher and selected counter definitions still
need qualification. This probe is a diagnostic source, not a production pod
integration requirement.

## Why the probe is tied to an exact kernel

The probe dereferences typed kernel structures. Their offsets depend on the
kernel build and architecture. The helper therefore checks Ubuntu
`6.8.0-139-generic`, x86-64, and the SHA-256 hashes of the running `vmlinux` and
`mac80211` BTF metadata before creating the probe. BTF describes the actual
compiled type layouts. A matching release name alone would not be enough.

The selected layouts were inspected inside VM with `pahole` 1.25, package
`1.25-0ubuntu3`. For example:

```bash
pahole -C sk_buff /sys/kernel/btf/vmlinux
pahole --btf_base=/sys/kernel/btf/vmlinux \
  -C ieee80211_tx_status /sys/kernel/btf/mac80211
pahole --btf_base=/sys/kernel/btf/vmlinux \
  -C ieee80211_tx_info /sys/kernel/btf/mac80211
```

Install that optional layout-inspection utility with `apt-get install pahole`
inside VM if needed. The runtime helper itself checks BTF hashes using Python;
it does not install or rebuild a kernel. Another build requires a new layout and
source review. Do not remove the guard to make an unfamiliar kernel pass.

The [Linux kprobe event documentation](https://www.kernel.org/doc/html/v6.6/trace/kprobetrace.html)
describes the tracing mechanism. The exact source review remains the repository's
selected Ubuntu distribution source, rather than an assumed generic Linux tag.

## Run the focused native experiment — HOST

Prepare the existing native candidate and optional medium build using the preceding
guide. While the owned lab is idle, stage the new helper and harness:

```bash
lxc file push deploy/radio-manager/tx-status-trace.py \
  emosa-lab/opt/emosa-radio-manager/
lxc file push deploy/peer-baseline/native-onboarding.py \
  emosa-lab/opt/emosa-baseline/

lxc exec emosa-lab -- env \
  PYTHONPATH=/opt/emosa-radio-manager/source \
  EMOSA_OVS_BIN=/opt/emosa-radio-manager/ovsdb \
  /opt/emosa/.venv/bin/python /opt/emosa-baseline/native-onboarding.py \
  --build /opt/emosa-baseline/candidate-onboarding-01 \
  --label tx-learning-01 --active-seconds 210 --recovery-checks \
  --observe-station-removal --medium-loss --observe-tx-status

python3 scripts/collect-native-review.py tx-learning-01 .lab/tx-learning-01
python3 scripts/check-tx-status-accounting.py .lab/tx-learning-01
```

Use a new label. The helper owns one uniquely named probe and trace instance,
with an 8 MiB buffer per CPU. Its filter selects only the represented AP's unicast
completions to the known client. It leaves other tracing sessions intact. Normal
cleanup disables and removes its own instance/probe, even when decoding fails.
Check cleanup errors before starting another attempt. A forcibly killed outer
harness or VM failure can leave resources behind; preserve the failed run and
inspect the exact owned names before any manual cleanup. Never clear global
`kprobe_events` or another operator's tracing instance.

`tx-status-provenance.json` records the probe, filter, type hashes, kernel event
format, clock, trace-buffer statistics and raw-trace digest. `tx-status.jsonl`
contains the checked projection. The private `tx-status.raw` includes kernel
pointers and is deliberately excluded from the review collector. The projection
keeps only a boolean for station-context presence. Do not publish the raw trace.

The original 15-minute recovery evidence remains a separate experiment. This
focused run contains intentional radio loss and investigates measurement
semantics. It cannot establish completed AP/radio/STA/neighbor reporting or full
sustained acceptance merely by explaining a kernel counter.

## Retained result and next exercise

The [reviewed trace run](../evidence/tx-status/README.md) correlates 962
completions across ten station lifetimes. The observed aggregation flags explain
2,094 suppressed retries: 2,421 medium retry attempts become 327 in the kernel
counter. All capture and trace-loss checks pass.

That same run records an extra onboarding operation during a telemetry gap and
50% loss in its initial two-packet Wi-Fi probe. Its strict onboarding/policy gates
therefore do not pass; counter accounting does not erase those failures. Follow
[telemetry freshness](telemetry-freshness.md) to reproduce and verify the control
session fix separately, with ordinary hwsim traffic and both recovery faults.
