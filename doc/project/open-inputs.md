# Open input gates

| Gate | Missing input | Affected work | Independent work |
| --- | --- | --- | --- |
| P0 | [Specification acquisition checklist](../protocol/specification-acquisition.md): IEEE 1905.1-2013, 1905.1a-2014, cited 802.3-2015 and WFA Security Requirements remain missing. Operator supplied 802.11-2024 and 802.3-2022; editions/hashes and selected clauses reviewed, with complete feature audit and Ethernet edition comparison unresolved. EasyMesh 6.1 and WPS 2.0.10 are obtained and inspected. Full rule/vector review remains pending. | I3, I4, wire tests/provisioning | I0–I2, selected WSC crypto component, component evaluation |
| M0 | Named pod/build, actual schema, endpoint direction/trust, managed radio/VIF, writer evidence, management/recovery and client profile | I5 and hardware writes | Simulators, package, reports |
| X1 | The prplMesh candidate now starts and sends discovery to the EMOSA container; actual EMOSA exchanges, profile/BSS policy and shutdown/recovery remain unqualified | I7 | [Peer baseline and startup commands](../../deploy/peer/README.md) |
| LXD | Ubuntu 24.04 component tests and VM-driven scenarios passed; base image export retained. Final runtime images and clean full-procedure reruns remain pending | Full reference deployment acceptance | Component and peer baselines now exercised in the dedicated VM |

`qualified` requires referenced evidence and compatible current configuration.
No boolean in an input manifest enables hardware writes. The upstream schema is
a simulation reference. Complete protocol frames remain pending; the separately
selected [WSC cryptographic component](../protocol/wsc-component.md) has synthetic payload
vectors independently checked with hostap 2.11.
The [M1/M2 payload component](../protocol/wsc-messages.md) also has independent required-field
and cryptographic checks; actual IEEE messages, exchange state and complete
radio mapping remain gated. The [radio payload interpreter](../protocol/wsc-radio.md)
now checks encrypted roles and whole-set candidate scope, without a write path.

The [native compatibility candidate](../guides/native-compatibility.md) now has a
tested HE-length fix and two wired sole-fronthaul runs. One M2/no M8 narrows the
observed policy; profile 2/1 mismatch, MCS ordering and shutdown abort remain.
The [first complete wire experiment contract](../guides/first-wire-experiment.md)
records the five-step status and the required physical evidence. Data Elements
3.0 and the WFA table clarification questions are consolidated in the acquisition
checklist, alongside the two exact IEEE 1905 editions.

The initial user input supplied no selected specification editions or actual pod
profile. See [the proposed corpus and access details](../protocol/protocol-inputs.md).
The operator subsequently confirmed that **IEEE Std 1905.1-2013** and
**IEEE Std 1905.1a-2014** are not available locally and no subscription-access
mechanism has been supplied. Both exact editions remain pending external inputs;
specification-dependent wire validation stays pending. Acquisition requests are
consolidated in the checklist above.

The operator supplied local paths on `rev150` for `80211-2024.pdf`
(**IEEE Std 802.11-2024**) and `IEEE_Standard_for_Ethernet.pdf`
(**IEEE Std 802.3-2022**). Both PDFs are verified and hashed; the
[media review](../protocol/ieee-media-review.md) records selected radio definitions,
Ethernet Clause 3 and correction checks. The 2015 Ethernet text remains unavailable,
so no normative substitution is selected. The IEEE 1905 base/amendment gate is
unchanged.

The read-only collector in [pod-qualification.md](../guides/pod-qualification.md) can produce
a draft once real connection inputs arrive. Existing direct access and the ability
to disable/redirect cloud writers are confirmed intentions, not verified lab setup.
The operator explicitly confirmed that **no private connection configuration has
been provided or created**. Credentials-free TLS, tunnel and Unix examples are
available; populate the selected copy outside this repository on the EMOSA
machine, then provide only its absolute path. Physical connection stays pending.

The [OVSDB/hwsim boundary](../evaluation/radio-manager.md) now passes semantic EMOSA changes
with live radio reads and independent clients. This advances I2/evaluation; it
does not satisfy P0, I3/I4 or M0. No new external inputs are needed for that
completed component experiment.

The bounded [native OpenSync R0 experiment](../../deploy/native/README.md) now
builds and runs, with 39 upstream units and the native apply/withhold path checked.
N03 recovery fails after database restart, so the backend remains disabled.
This is an implementation/qualification gap, not another external specification
or pod credential request.
