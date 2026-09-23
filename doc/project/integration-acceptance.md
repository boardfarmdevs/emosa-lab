# Acceptance levels for OpenSync integration

**Review contract, 2026-09-23. These levels do not enable any backend or modify
runtime verdict logic.** Use them with the [integration plan](opensync-lab-integration-plan.md),
[frozen baseline](integration-baseline.md) and [current status](current-status.md).

A working client, a controller inventory entry and a 15-minute run answer
separate questions. Record each result for its exact controller/adapter/pod
profile. Success on the simulated manager cannot be transferred to actual
OpenSync by changing a label.

## Common record and verdict rules

Before executing a new level, record the baseline ID; actual source/image/library
hashes and deviations; controller and represented identities; selected radio/BSS
and security scope; trust and writer ownership; mandatory procedure/report list;
protocol deadlines and separate lab recovery budgets; observer and capture plan;
run directory; and scoped restoration procedure. A VM rename changes a locator,
not the image or qualification identity. A different image or source diff needs
a new baseline/deviation record and relevant qualification.

Use separate verdicts per level:

- `not_evaluated`: no attempt against this declared target.
- `blocked`: a named prerequisite or source is missing; record it explicitly.
- `failed`: attempted execution or independent review violated a required check.
- `passed`: every requirement of that level has referenced independent evidence.

Retain original runner output and a separate subsequent review. Do not rewrite
failed attempts, combine observations from unrelated runs into a single causal
pass, treat missing data as zero, or treat a successful OVSDB transaction as
observed Wi-Fi application. Restoration failure makes the attempt unsuccessful
as a whole even if a narrower functional observation was useful.

These are documentary review fields. Existing CLI result strings and schemas
remain unchanged; a future runner must implement an explicit mapping rather than
assume that this document already added a verdict API.

## Levels and exit evidence

| ID | Question answered | Required exit evidence | Does not establish |
| --- | --- | --- | --- |
| Q — Read-only qualification | What actual OpenSync instance can we safely observe? | Bound trusted endpoint, actual schema and two-radio graph, firmware/image identity, security representation review, writer/bootstrap/recovery inventory, draft `writable=false` profile; transcript confirms no Config writes | Writable mapping, controller membership or onboarding |
| A — Semantic application | Can EMOSA cause and observe the intended native OpenSync change? | Reviewed container profile, writer exclusion, guarded selected-BSS SSID/PSK change, real manager Config→State causality, independent hostap/client result, backhaul preserved, negative/conflict/lost-reply checks and restoration | Real EasyMesh initiation |
| W — Warm onboarding | Can the real controller onboard an already bootstrapped OpenSync pod through EMOSA? | A's prerequisites; real discovery/Early/WSC, authenticated M2 is the only source of target credentials, correlated durable operation, actual OpenSync apply, truthful radio/BSS inventory, independent client authentication and nonce traffic | Cold-start creation, fulfilled ongoing reporting, physical firmware or whole-profile conformance |
| C — Cold-start onboarding | Can that path work after a declared cold pod start? | W plus reviewed initial storage/persistence state, no NOC-supplied target fronthaul, actual bootstrap/manager routing and missing-resource creation where needed; repeat the complete causal wire/application/client path | Factory reset unless separately tested; sustained reporting |
| R — Operational recovery | Can the established target remain operational and recover through the workload? | At least 900 seconds active after onboarding, repeated client joins/leaves, independent traffic, actual OVSDB transport interruption and adapter process restart, fresh contexts, no duplicate effects, bounded resources, timed outages and restoration | Complete sustained acceptance when mandatory reports remain absent |
| S — Complete sustained operation | Does the target fulfill all selected mandatory procedures while it operates and recovers? | W and separately reviewed C; all R workload checks in the same acceptance run, complete qualified AP/STA/neighbor and final-session reporting, controller receipt/Ack where required, no unanswered mandatory procedure or unfulfilled reporting period, resource/recovery/restoration checks | Arbitrary features, multiple pods, another controller, physical-pod or universal conformance claims |

The [machine-readable preparation register](integration-acceptance.json) records
all actual opensync-lab levels as `not_evaluated`. Its baseline preparation is
verified; qualification and application are not. Existing simulator evidence is
listed separately, without retroactively awarding these new levels.

## W — Make warm onboarding causal

The OpenSync pod already has a working 5 GHz backhaul and a selected existing
2.4 GHz BSS. Preserve their pre-run state and choose a target different from that
initial fronthaul state. Only the controller receives the desired SSID/key; EMOSA
must obtain them through the authenticated wire exchange. Local-NOC must be
excluded from pod fronthaul writes while retaining gateway AP/GRE duties.

Required checks include all of the following:

1. Capture actual controller Search/Response, selected Early behavior and M1/M2;
   bind the M1 receipt, controller identity and selected pod/radio to one operation.
2. Observe the guarded transaction and native `owm` application. Fresh State and
   independent hostapd/nl80211 facts must agree; withholding or stale State cannot
   pass. Retain a deliberate failure/withholding check from this qualified profile.
3. Observe the controller's represented radio/BSS, not only its initial AL entry.
   Declare the represented 2.4 GHz scope honestly and preserve the 5 GHz underlay.
4. Prove the wrong key fails and the correct key joins the pod BSSID. Use a fresh
   nonce and interface-bound traffic; reject mv3 association or a wired bypass.
5. Correlate evidence timestamps, record unsupported procedures, and restore only
   owned changed resources under fresh guards. Keep secret material private.

A warm run can be useful while full reporting is incomplete. Its claim must name
that limitation. If the represented scope cannot satisfy a requirement essential
to the selected onboarding procedure, the W result is blocked or failed; a narrow
label does not waive a mandatory onboarding rule.

## C — Define what “cold” means before restarting

Record whether the test is a process restart, container stop/start, loss of the
runtime database, or a factory-reset scenario. They are different tests. Capture
which settings PSM restores and which the boot image creates. In the evaluated
pod, startup reconstructs a runtime database from `conf.db.bck`, bootstrap creates
radio/backhaul resources and PSM can restore configuration. Discover actual
behavior rather than treating a container restart as a factory reset.

A cold-start pass must show that pod bootstrap reaches the designated EMOSA
session, gateway backhaul/GRE services remain available, and the authenticated
controller operation supplies the target fronthaul policy. If the selected test
starts without a fronthaul BSS, prove guarded row/reference creation or declare
and review a neutral resource bootstrap. NOC must not secretly create the target
SSID/key first. Retain the before-start storage state and complete W evidence
after the restart. An unchanged existing BSS surviving PSM is not proof of
missing-BSS creation.

## R and S — Keep recovery distinct from full service

The selected active workload lasts at least **900 seconds after initial
onboarding**, with the adapter running during client activity. Schedule the two
required faults separately: pod OVSDB transport loss and actual adapter process
restart. Record fault/recovery timestamps, stable identities, fresh monitor and
wire generations, operation/write counts and manual interventions. Repeat client
joins/leaves and run continuous independent data traffic in connected phases.

For the OpenSync VM's wireless management path, also require an independently
identified backhaul-loss recovery case. A relay/TCP interruption is not that case:
backhaul loss affects both management and data, including GRE reconstruction.
Cold-pod recovery may be scheduled as a separate case with its own startup budget;
do not hide its outage inside a transport-fault allowance.

R must include a report ledger listing fulfilled and missing mandatory work. A
passed R with missing AP reports has `complete_sustained_operation=false`.
S additionally requires real qualified publisher input, correct membership and
counter epochs, complete selected responses, periodic/triggered reports, observed
controller consumption and final-session statistics. No synthetic parser values,
last periodic sample masquerading as final counters, or unqualified hwsim survey
values may fill a missing field. Review any selected procedure's Ack requirement
explicitly, including retained Early/AP interpretation questions.

Use the [sustained-operation criteria](../protocol/sustained-operation.md) as the
minimum existing workload/protocol contract. S adds the actual OpenSync target,
its separate cold-start result and Wi-Fi/GRE management-path checks; it does not
weaken the earlier criteria. Re-run the integrated workload after required source
work is complete. Passing components at different revisions does not complete S.

## Physical proof and later expansion

Every level above is scoped to a named profile. Passing S on the patched hwsim
container still does not establish the final unchanged-physical-pod objective.
For that objective, separately qualify the physical target and repeat real
EasyMesh → EMOSA → existing pod managers → independent physical-client behavior,
with the declared recovery and reporting obligations. Multiple pods, another
controller, optional features and router deployment each need their own scope and
evidence. Preserve the baseline result when expanding scope.
