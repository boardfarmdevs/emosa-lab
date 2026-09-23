# External IEEE 1905.1 implementations: reuse options for EMOSA

**Opinion and source review, 22 September 2026.** This document considers external
work as potential input to EMOSA, the **EasyMesh-to-OpenSync Adapter**. It proposes
evaluation options; it does not select, integrate or qualify a new dependency.

## 1. Recommendation

**None of the four reviewed repositories is established by this review as a
complete, off-the-shelf replacement for EMOSA's required 1905/EasyMesh boundary.**
There is nevertheless substantial reusable work:

| Project | Recommended role | Adoption judgment |
| --- | --- | --- |
| Broadband Forum **meshComms** | Historical C AL implementation, procedure reference and comparison source | Broad base implementation, but explicit omissions, legacy interfaces and hardening work prevent a drop-in recommendation |
| **TechnicolorEDGM/ieee1905** | C backend feasibility candidate if a C implementation is a deployment requirement | More suitable higher-layer API than meshComms, but requires ABI, protocol and platform work |
| **rdkcentral/ieee1905-rs** | **First candidate for an optional native-backend feasibility evaluation** | Useful standalone Rust architecture and higher-layer IPC; specific receive/reassembly and virtual-agent integration gaps must be resolved first |
| **evanslai/pyieee1905** | Independent packet construction/dissection and fault-fixture tool | Packet library, not a running AL service or a router performance replacement |

The Rust recommendation is an engineering preference based on the inspected
interface, component structure and recent development. It is **not** a conformance
finding or a measured performance ranking. Technicolor is the more relevant C
starting point among these candidates, but choosing C alone would not remove its
adaptation cost. The findings supporting these judgments follow below.

For the first bounded viability demonstration, retain the current implementation
as the reference path. Evaluate an alternative behind a defined boundary only if
that work helps resolve a concrete gap or a measured deployment constraint.

### Fitness as optional components inside the EMOSA system

**Yes: optional component reuse is a stronger fit than complete replacement.**
EMOSA can keep its EasyMesh-to-OpenSync logic while obtaining a particular
transport service or evaluation tool from another project. The useful question is
whether a component meets its assigned contract, rather than whether it implements
every possible 1905 and EasyMesh feature.

| Component | Optional place in EMOSA | Fitness judgment | Condition before enabling it |
| --- | --- | --- | --- |
| Rust AL service | Selectable native backend for packet I/O, reassembly, LLDP and explicitly assigned base discovery functions | **Strongest candidate for an integration prototype.** Existing serialized SAP and Linux service structure reduce the amount of interface design needed | Fix or otherwise resolve the identified reassembly/edition issues; qualify SAP identity/metadata, pod-derived facts and restart behavior |
| Technicolor `lib1905` | Selectable C backend in a helper process; particularly relevant when a router platform already supplies its platform library | **Conditional fit.** Real higher-layer callbacks make it more adaptable than starting with an undivided legacy AL | Correct the handle ABI and MID behavior; provide serialized IPC, required message coverage and target-platform hooks |
| meshComms | Isolated comparison peer, procedure reference, or selected C components behind an owned interface | **Useful evaluation/reference fit; weaker runtime fit.** Existing base procedures and test vectors can inform validation without replacing EMOSA | Establish the selected peer behavior and harden any code exposed to live traffic; related ancestry limits its independence from Technicolor |
| pyieee1905 | Optional developer packet generator/dissector and malformed-input fixture tool | **Good role fit as test tooling, with encoding checks.** Scapy makes unusual or intentionally invalid inputs convenient to express | Pin dependencies and verify the classes actually used, especially WSC; retain an independent decoder and specification-based expected results |

These judgments mean “worth adapting for this role,” not “already integrated” or
“safe to enable unchanged.” The review adds no backend selector, optional package
or runtime dependency. No candidate is currently qualified by this document.

For example, an eventual **Rust-backed EMOSA** would still use EMOSA to decide
whether a controller is admitted, authenticate and scope its request, map that
request to an OpenSync pod and verify the outcome. Rust would supply the selected
AL functions. An eventual **pyieee1905-enabled lab** would simply gain another
way to construct or inspect test packets; the deployed adapter would not need
that package at all. These are independent choices, so a native backend and a
Python test tool could both be useful.

The practical ranking depends on the purpose: **Rust first for optional runtime
reuse; Technicolor when C or an existing compatible router platform matters;
pyieee1905 for packet-test tooling; meshComms for historical and comparative
evaluation.** There is no measured basis yet to rank their performance.

## 2. What was inspected, and what was not proved

Public default-branch source snapshots were inspected, including packet codecs,
receive/reassembly paths, higher-layer interfaces, platform access, build files,
tests and licenses. Links below pin the reviewed revisions so that later upstream
changes do not silently change the basis of this opinion.

| Repository | Reviewed revision | Head commit date |
| --- | --- | --- |
| [BroadbandForum/meshComms](https://github.com/BroadbandForum/meshComms/tree/a5b0121d046d1081c3e5d980644e93b206932ec9) | `a5b0121d046d1081c3e5d980644e93b206932ec9` | 2019-05-30 |
| [TechnicolorEDGM/ieee1905](https://github.com/TechnicolorEDGM/ieee1905/tree/56742149f706345f998db38603842e2234bb2325) | `56742149f706345f998db38603842e2234bb2325` | 2019-05-17 |
| [rdkcentral/ieee1905-rs](https://github.com/rdkcentral/ieee1905-rs/tree/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4) | `9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4` — release 0.7.0 | 2026-09-15 |
| [evanslai/pyieee1905](https://github.com/evanslai/pyieee1905/tree/6e64856c21f7e12c356915e081b9e655c1e860d3) | `6e64856c21f7e12c356915e081b9e655c1e860d3` | 2022-12-02 |

These are commit dates, not promises about project support or activity in other
branches or downstream products. None was compiled, installed, benchmarked or
run against EMOSA for this review. Existing upstream tests were inspected as
evidence of test infrastructure, not reported as passing. This is a targeted
static review, not a full standards audit, security audit or certification.
Source-derived failure cases below require executable regression tests before
being treated as independently reproduced runtime findings.

EMOSA's comparison baseline is IEEE 1905.1-2013 plus IEEE 1905.1a-2014 and the
selected EasyMesh 6.1 procedure subset. The IEEE documents are now available
locally. Edition-specific rules and remaining dependencies are recorded in the
[envelope review](ieee1905-envelope.md), [protocol matrix](protocol-matrix.json)
and [acquisition checklist](specification-acquisition.md). Upstream code and
README claims do not replace these specifications. Supporting some newer-looking
message names or version constants does not establish compatibility with our
selected editions.

## 3. Which part of EMOSA could actually be replaced?

The **1905 Abstraction Layer (AL)** discovers neighboring devices and transports
control messages across the relevant links. A **CMDU** is a control message; a
**TLV** is one of its typed, length-delimited fields. An **AL-SAP** is a service
interface through which higher-layer software uses the AL. A packet encoder is
only one part of this system.

An **EasyMesh agent** additionally implements the selected EasyMesh profile:
capabilities, discovery/autoconfiguration procedures, radio and BSS reporting,
configuration semantics and required responses. WSC authentication and credential
handling add obligations beyond recognizing a WSC TLV.

EMOSA's **virtual agent** is the controller-facing representation of an OpenSync
pod. Its radio and BSS facts must describe that pod. Its accepted configuration
must pass through EMOSA's guarded operations and OpenSync mapping. An AL reporting
the gateway's own radios is not automatically representing a remote pod.

```mermaid
flowchart LR
    C[Real EasyMesh controller] <--> A[1905 AL and packet transport]
    A <--> V[EMOSA virtual agent and EasyMesh procedures]
    V --> G[Authenticated request and guarded operation]
    G --> O[OpenSync configuration and State mapping]
    O <--> P[Unchanged OpenSync pod]
    P --> I[Independent client and behavior observation]
```

An external implementation could supply all or part of **A**. It does not supply
the rest of this chain merely by decoding autoconfiguration messages. In
particular, none of these repositories establishes our pod qualification,
Config/State reconciliation, durable operation recovery, telemetry route to the
network-center data lake, or physical acceptance evidence.

The current Python implementation has bounded wire/WSC components and an
operation path exercised through simulated OpenSync state and hwsim clients.
Native-controller admission and controller-owned inventory through EMOSA remain
part of the unfinished demonstration. The
[first experiment guide](../guides/first-wire-experiment.md) defines that boundary;
this review does not advance its acceptance status.

## 4. Broadband Forum meshComms

### What is useful

This is a substantial C implementation, rather than a packet-format sketch. Its
tree separates common/platform utilities, packet factories, an AL and a sample
higher-layer entity. There are IEEE 1905, LLDP and AL management codecs, topology
and metric handling, AP autoconfiguration/WSC code, and factory unit tests. This
is useful material for understanding how a complete service was decomposed.
See the [source tree](https://github.com/BroadbandForum/meshComms/tree/a5b0121d046d1081c3e5d980644e93b206932ec9/src)
and [build/test targets](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/Makefile#L155-L219).

Its platform separation and historical embedded build variants make C reuse
plausible. However, the Linux builds depend on libraries including pthread,
libpcap and libcrypto; the existence of an old embedded target does not prove a
build against a current router SDK.
[Build configuration](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/Makefile).

### Interfaces and completeness limits

The AL management server uses TCP and binds to `INADDR_ANY`. That is an ALME
management interface, not an already suitable Python interface for arbitrary
EasyMesh agent procedures. A router integration would need an explicit local
access boundary and a decision about which AL functions remain autonomous.
[ALME server](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/src/al/src_linux/platform_alme_server.c#L153-L179).

The project's own TODO list includes amendment-related registrar detection,
U-key derivation, Linux forwarding commands and security analysis. Thus a broad
base implementation should not be described as complete selected-edition support,
let alone a complete contemporary EasyMesh agent.
[Recorded omissions](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/README.md#L2446-L2468).

Three code-level concerns matter directly to EMOSA:

1. **Reassembly capacity and lifetime:** the receive routine holds five messages
   with up to three fragments each. Its comment explicitly substitutes oldest
   entry eviction for a timer. This is not evidence of the deadline and resource
   behavior EMOSA requires under loss, concurrency or malicious input.
   [Reassembly implementation](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/src/al/src_independent/al_entity.c#L104-L135).
2. **Parser boundary:** `parse_1905_CMDU_from_packets()` takes a null-terminated
   collection of packet pointers, without their individual lengths; nested TLV
   parsing likewise receives a pointer without remaining length. This API shape
   needs a bounds audit and likely length-aware adaptation before accepting
   untrusted traffic. This observation is not an exploit claim.
   [CMDU parser](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/src/factory/src_independent/1905_cmdus.c#L384-L410),
   [TLV dispatch](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/src/factory/src_independent/1905_cmdus.c#L555-L560).
3. **Configuration ownership:** its WSC path can call
   `PLATFORM_CONFIGURE_80211_AP()` directly. EMOSA must retain the boundary between
   authenticated, scope-checked settings and guarded pod operations; reusing this
   path unchanged would bypass that design.
   [WSC application call](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/src/al/src_independent/al_wsc.c#L1006).

**Opinion:** valuable reference code and a possible foundation for a maintained C
fork, but not the shortest route to our first demonstration. The work needed is
more than linking a library and replacing Python packet I/O.

## 5. TechnicolorEDGM/ieee1905

### What improves on meshComms

Technicolor explicitly identifies its implementation as derived from the
Broadband Forum code. These are related implementations, so agreement between
them is weaker independent evidence than agreement between unrelated stacks.
[Project description](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/README.md).

The useful addition is a C `lib1905` interface: connect in agent/controller mode,
register message callbacks, send CMDUs, poll/read, get/set selected AL properties,
notify interface events and release messages. This is a much clearer potential
integration point for an EasyMesh higher layer.
[Public API](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.h#L138-L295).

### Why its socket is not ready-made Python IPC

`lib1905_connect()` starts the AL in a thread and opens an abstract Unix
`SOCK_SEQPACKET` socket. The transported `map_message_t` contains a `void *`;
`lib1905_read()` treats that value as a pointer to a CMDU in the same process.
An unrelated Python process cannot consume that pointer as a serialized message.
[Thread/socket setup](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.c#L84-L112),
[message structure](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/factory/interfaces/extensions/map/map_server.h#L34-L48),
[receive dispatch](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.c#L479-L522).

A possible integration is a small C helper that owns the library, copies/serializes
messages into a defined IPC format and releases their original allocations.
Direct FFI is another possibility, but requires precise callback, lifetime and
thread ownership. Neither option is implemented by this review.

### Concrete work required

| Finding in the reviewed code | Consequence for an EMOSA backend |
| --- | --- |
| The public handle is `int`; allocation stores a pointer through `(uintptr_t)` into it | Correct the handle ABI before relying on a 64-bit build; this is a static pointer-truncation risk, not a reproduced crash |
| `lib1905_send()` interprets a supplied MID of zero as “allocate a MID” | Add a way to transmit an explicitly selected zero MID where the procedure requires it; test rollover and response correlation |
| Message-filter `ack_required` is documented as unimplemented | Assign ACK/retry ownership explicitly; the API flag does not establish behavior |
| Named Multi-AP CMDUs and callback range end at `0x801a` | Audit newer EasyMesh messages and dispatch paths; adding a TLV constant alone is insufficient |
| Reassembly retains five messages, raises fragments per message to 26, and retains the eviction-instead-of-timer design | Qualify deadline, concurrency and malformed-input behavior; do not inherit meshComms assumptions silently |

Evidence: [handle declaration](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.h#L166),
[pointer assignment](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.c#L121-L131),
[MID selection](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.c#L403-L415),
[ACK contract](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/factory/interfaces/extensions/map/1905_lib_common.h#L28-L45),
[message identifiers](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/factory/interfaces/1905_cmdus.h#L91-L119),
and [reassembly](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/al_entity.c#L104-L136).

The embedded WRT1900ACX variant enables OpenWrt/Multi-AP platform abstractions and
links `libplatform_map`, whereas the generic build has a different configuration.
This needs a platform-port inventory, not an assumption that all router support
is self-contained. The public API also explicitly excludes exercising registrar
functionality through that interface.
[Build variants](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/Makefile#L63-L87),
[API scope](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/src/al/src_independent/extensions/map/1905_lib.h#L138-L158).

**Opinion:** the best C-oriented API starting point in this set, conditional on
funding a maintained port and conformance work. It is not a ready-to-load C
extension for EMOSA, nor evidence of complete EasyMesh 6.1 support.

## 6. rdkcentral/ieee1905-rs

### Why it deserves the first optional evaluation

The Rust project separates a core implementation from tests and optional RBus
integration. It includes codecs, a topology database, LLDP handling, interface
management, asynchronous packet processing and an AL-SAP. RBus, cryptographic
support, a topology UI and artifact exchange are separate feature choices; a
standalone core evaluation need not adopt all of RDK's management system.
[Core manifest](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/Cargo.toml),
[core modules](https://github.com/rdkcentral/ieee1905-rs/tree/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src).

The AL-SAP uses separate control and data Unix stream sockets with length-delimited
framing. Its SDU carries two MAC addresses, fragmentation fields and a byte-vector
payload. This is a real serialized boundary that Python could implement, unlike
Technicolor's pointer-carrying internal socket. It is still a specific upstream
protocol requiring version pinning and contract tests, not an assumed stable
cross-project ABI. The exposed Rust crate is not a supplied C ABI.
[SAP server](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/al_sap.rs#L148-L198),
[SDU layout](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/sdu_codec.rs#L30-L66),
[crate exports](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/lib.rs).

Unknown CMDU/TLV identifiers have representations, and TLVs retain byte-vector
values. That is useful for carrying EasyMesh extensions without implementing
their complete semantics inside the AL. It does not prove that every message
passes every dispatch path unchanged.
[Identifier codecs](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_codec.rs#L48-L255),
[generic TLV representation](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/tlv_cmdu_codec.rs#L43-L101).

### Source findings to resolve before trusting the boundary

**Final fragment arriving first.** `push_fragment()` removes the buffered context
and attempts reassembly immediately when the arriving fragment has the final bit.
It does not wait for earlier missing fragments. For a two-fragment message
received in order FID 1/final, then FID 0/non-final, static analysis predicts an
incomplete reassembly error followed by a stranded new context. EMOSA already
exercises reversed fragment arrival; this is a concrete regression case for any
candidate backend. The caller propagates the reassembly error.
[Reassembler](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_reassembler.rs#L110-L144),
[caller](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_handler.rs#L87-L110).

**Duplicate and resource behavior.** A duplicate fragment replaces the stored
value before the function returns a duplicate error. The context key is source
MAC plus MID, and this reassembler has no explicit aggregate context/byte cap.
Periodic expiration exists; it is not a substitute for an aggregate memory bound
or separation between virtual agents and link generations. Test conflicting
duplicates, cross-context collisions and sustained incomplete traffic.
[Buffer and expiry](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_reassembler.rs#L43-L105),
[insertion order](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_reassembler.rs#L116-L140).

**Selected-edition length semantics.** The TLV parser consumes the entire 16-bit
length field as a byte count. It does not mask off the two reserved high bits.
EMOSA's selected base/amendment interpretation uses the low 14 bits and ignores
reserved bits on reception. Resolve this mismatch against the exact editions
before calling the backend compatible; upstream version labels alone cannot
settle it.
[TLV parser](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/tlv_cmdu_codec.rs#L62-L91),
[EMOSA rule and specification sections](ieee1905-envelope.md#2-review-the-exact-rules-implemented).

**Receive metadata is normalized.** The higher-layer receive path requires a
known topology entry for the source. It then constructs an SDU whose source field
is the local AL and destination field is the remote source, rebuilding the CMDU
with version zero, fragment zero and flags `0x80`. This may be an intentional SAP
convention, but it is not a byte-exact packet indication. EMOSA must correctly
recover peer identity, distinguish interface MAC from AL MAC, and retain the
ingress/link-generation evidence needed for admission. A naive interpretation
of the field names would be unsafe for correlation.
[Receive-to-SAP transformation](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_handler.rs#L935-L1020).

**Transmit content is also managed by the AL.** The SDU transmit path injects
Topology Response TLVs and, in enrollee mode, AP Autoconfiguration Search TLVs.
This is useful when the AL owns a device's real interfaces. For EMOSA, it needs an
explicit policy so gateway-derived information does not overwrite or contradict
the proxied pod's qualified facts.
[Transmit transformations](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/cmdu_proxy.rs#L930-L949).

**One AL context is the starting design.** SAP state is singleton state; the
server accepts a control/data pair for that instance. It is not demonstrated as
a multiplexed service for many independent virtual AL identities. Multi-pod reuse
requires separate instances with isolated identity and sockets, or an upstream
context abstraction. Either choice needs testing.
[Singleton state](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/al_sap.rs#L53-L69),
[connection acceptance](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/src/al_sap.rs#L176-L198).

The upstream roadmap also leaves controller redundancy and message integrity/
encryption unfinished. Those features are not all prerequisites for our bounded
experiment, but their omission prevents a blanket “complete 1905 implementation”
claim. The project deliberately targets the functions useful to its RDK EasyMesh
architecture. Its crypto feature does not establish a complete EMOSA WSC or
EasyMesh security procedure.
[Roadmap](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/README.md#L10-L30),
[scope statement](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/README.md#L1421-L1423).

**Opinion:** the most promising optional native backend in this set, provided the
first evaluation is a small conformance/interface experiment. Start with the
findings above, not with a wholesale replacement of working EMOSA components.

## 7. evanslai/pyieee1905

This Python/Scapy package defines the Ethernet binding, CMDU fields, IEEE 1905
TLVs and a collection of Multi-AP TLVs. The latter includes more than initial
Multi-AP fields: extended metrics, Wi-Fi 6, CAC and scan capability definitions
are present. It can therefore contribute useful packet construction and
dissection examples.
[CMDU definition](https://github.com/evanslai/pyieee1905/blob/6e64856c21f7e12c356915e081b9e655c1e860d3/pyieee1905/multiap_msg.py#L60-L76),
[later capability TLVs](https://github.com/evanslai/pyieee1905/blob/6e64856c21f7e12c356915e081b9e655c1e860d3/pyieee1905/multiap_tlv.py#L773-L947).

The inspected package does not supply a running AL daemon, topology-aging service,
link management, fragment/retry lifecycle or authenticated WSC onboarding engine.
Its `answers()` method compares only the MID. That can assist interactive Scapy
use, but cannot establish EMOSA's peer, procedure and exchange authorization.
The package manifest depends on Scapy without pinning its version; the reviewed
tree contains no dedicated test suite or CI workflow.
[Package tree](https://github.com/evanslai/pyieee1905/tree/6e64856c21f7e12c356915e081b9e655c1e860d3),
[dependencies](https://github.com/evanslai/pyieee1905/blob/6e64856c21f7e12c356915e081b9e655c1e860d3/setup.py),
[response matching](https://github.com/evanslai/pyieee1905/blob/6e64856c21f7e12c356915e081b9e655c1e860d3/pyieee1905/multiap_msg.py#L72-L73).

There is a specific reason not to adopt its WSC output as an oracle: the WSC class
declares an extra `wsc_frame_size` field after the TLV length, before the WSC bytes.
Our selected IEEE WSC TLV carries the WSC frame directly as its value. The extra
field therefore needs correction or a specification-backed explanation before
using that class to generate acceptance fixtures. This is a static format
finding; no Scapy round-trip test was run here.
[WSC class](https://github.com/evanslai/pyieee1905/blob/6e64856c21f7e12c356915e081b9e655c1e860d3/pyieee1905/ieee1905_tlv.py#L308-L317),
[selected WSC references, including IEEE §6.4.18](ieee1905-envelope.md#2-review-the-exact-rules-implemented).

**Opinion:** useful as a separately checked lab tool, especially for malformed
packets and uncommon TLVs. Pin Scapy and validate each used encoding against the
specifications and another decoder. It is not a complete 1905 layer and offers no
established router performance advantage over the current Python implementation.

## 8. A possible optional-backend boundary

The following is a design proposal, not an implemented EMOSA interface. The goal
would be to compare backends without moving configuration authority accidentally.

“Optional backend” should mean selecting one qualified implementation for a
virtual agent when its runtime starts. Keep the current backend available for
reproduction and comparison. Do not run two autonomous ALs for the same virtual
identity, or silently switch backends during a live WSC exchange: a replacement
or restart must invalidate the old exchange generation. Separate read-only
capture/dissection tools can accompany either backend without owning its protocol
responses.

| Responsibility | Proposed owner |
| --- | --- |
| Ethernet/LLDP I/O, frame limits, fragmentation/reassembly and selected base AL duties | Backend, with explicit responsibility for each enabled procedure |
| Pod-to-virtual-AL identity, radio/BSS facts, controller/profile admission and EasyMesh procedures | EMOSA |
| WSC authentication, complete-request scope, replay/exchange lifetime and secret handling | EMOSA's qualified procedure boundary |
| Durable intent, guarded OpenSync transactions, fresh State and recovery | EMOSA operation engine |
| Packet capture, controller inventory and independent client behavior | Evaluation system |

A useful backend contract would include:

1. Explicit virtual-AL identity and link context; keep observed Ethernet source,
   originating AL, destination and ingress distinguishable.
2. Complete CMDUs with ordered TLV occurrences and exact WSC value bytes; preserve
   repeated M2 TLVs rather than collapsing them into a map keyed by TLV type.
3. Selected MID transmission, including zero; defined duplicate, timeout,
   fragmentation and relay ownership so two layers do not both retry or respond.
4. Bounded queues, messages, reassembly contexts and memory; observable rejection
   and send-failure behavior.
5. A connection/link generation that invalidates stale peer exchanges after
   restart, disconnect or interface replacement.
6. A fact-provider mechanism for pod inventory, with explicit control of any
   autonomous topology or Search TLV insertion.

For Rust, the existing SAP is a starting point, but the receive metadata and
transmit transformations require a wrapper or upstream changes. For Technicolor,
a C helper would need a genuine serialization boundary and corrected handle ABI.
For meshComms, higher-layer access and ownership would require more design work.

**Many pods do not require many complete EMOSA services.** A shared EMOSA service
can retain many pod/virtual-agent contexts. However, an upstream AL built around
one local identity may initially require one isolated helper instance per virtual
agent. That is an implementation option, not an architectural requirement.
Per-instance sockets, interfaces, topology state, MAC identities and resource
budgets would all need qualification. Refactoring to an explicit multi-AL service
could reduce overhead, but would be a larger upstream change.

On a gateway already running an EasyMesh controller, coexistence is also part of
the experiment: controller and virtual agents must not advertise the same AL
identity, answer for one another or generate duplicate autonomous responses. The
packet path must make each virtual agent discoverable to the real controller.
Neither “run another daemon on the bridge” nor namespace isolation by itself
proves this routing and identity behavior.

## 9. Router deployment and performance

### Where C or Rust could help

A native backend could plausibly reduce CPU and allocation overhead for packet
parsing, reassembly and many simultaneous control exchanges. It could also reuse
existing Linux discovery and interface integration. These are hypotheses to
measure. IEEE 1905 is a **control plane**: replacing its implementation does not
by itself increase Wi-Fi user-data throughput or make pod configuration converge
faster. IPC, WSC cryptography, OVSDB, pod managers and client reassociation can
dominate the end-to-end timing.

Retaining Python EMOSA with a native helper still requires Python on the router.
Replacing the AL is not a complete C/Rust port of EMOSA, and an extra daemon plus
IPC can increase total memory even if its parser is faster.

| Candidate | Router considerations to establish before selection |
| --- | --- |
| meshComms | Current compiler/libcrypto compatibility, length-aware parsing, platform hooks, management interface and cross-build reproducibility |
| Technicolor | The above plus 64-bit handle correctness, callback ownership, `libplatform_map` dependencies and a maintained serialization wrapper |
| Rust | Target toolchain/ISA/libc, Linux networking dependencies, feature selection, total helper-plus-Python footprint and multi-instance cost |
| pyieee1905 | Python and Scapy dependencies; appropriate for a lab image if needed, not an assumed smaller production runtime |

Rust's README specifies toolchain 1.91.1, and the core uses edition 2024. Its tests
and benchmark targets are promising evaluation infrastructure. The published RSS
workflow samples a short run after five seconds; those results are not an EMOSA
workload or a fleet of virtual agents. Do not compare that number directly with
an EMOSA service under load.
[Toolchain guidance](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/README.md#L395),
[manifest](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/Cargo.toml#L1-L15),
[tests, benchmarks and RSS method](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/README.md#L1000-L1043).

A fair future measurement would pin the target router, OS, compiler, features,
logging and packet corpus. Compare identical operations with one, eight and
32 virtual-agent contexts as **proposed test scales**, not capacity claims.
Record total process-tree RSS/PSS, installed footprint, idle and loaded CPU,
queue drops, reassembly memory, startup/recovery time and p50/p95/p99 latency.
Separate packet-to-validated-request timing from operation commit, State
convergence and independent client success. Include malformed traffic and a
disconnected higher layer so a fast happy path cannot conceal unbounded queues.

### Distribution inputs

The root licenses are also part of a deployment comparison: meshComms carries
redistribution conditions and a contributor patent grant; Technicolor includes
its own notice and inherited Broadband Forum/MaxLinear terms; Rust declares
Apache-2.0; pyieee1905 uses MIT. Preserve the actual notices and review the selected
files and transitive dependencies before a product distribution. A repository
license label does not establish rights to every implementation dependency or
standards-essential patent.
[meshComms license](https://github.com/BroadbandForum/meshComms/blob/a5b0121d046d1081c3e5d980644e93b206932ec9/LICENSE),
[Technicolor license](https://github.com/TechnicolorEDGM/ieee1905/blob/56742149f706345f998db38603842e2234bb2325/LICENSE),
[Rust manifest](https://github.com/rdkcentral/ieee1905-rs/blob/9856eb9feb5894fe7f33bd9dd0907cef65dd3fb4/ieee1905-core/Cargo.toml),
[Python license](https://github.com/evanslai/pyieee1905/blob/6e64856c21f7e12c356915e081b9e655c1e860d3/LICENSE).

## 10. Proposed evaluation gates and decision

This sequence is optional follow-on work. None of it was executed as part of
writing this document.

| Gate | Required evidence | What it establishes |
| --- | --- | --- |
| 1. Reproducible isolated build | Exact revision, features, compiler, dependency inventory and upstream test results on the intended ISA | Buildability, not protocol correctness |
| 2. Selected-edition packet corpus | Independent decoding; unknown/repeated TLVs; reserved length bits; EOM placement; malformed lengths; MID zero/rollover; AL/interface MAC distinction | Encoding and receive behavior for the declared subset |
| 3. Lifecycle and resource faults | Final-fragment-first, missing/conflicting/duplicate fragments, expiry, context/byte exhaustion, IPC disconnect, restart and stale peer generation | Bounded failure behavior and correlation |
| 4. Virtual-agent contract | Pod-derived facts survive the backend, no unintended local AP changes, one operation for a legitimate replay, multiple AL identities remain isolated | Suitability as an EMOSA component |
| 5. Native controller with simulated pod | Real Search/Response and WSC procedures, controller-owned agent/radio/BSS inventory, guarded OpenSync changes and independent hwsim client observations | Bounded controller-to-simulation interoperability |
| 6. Target-router comparison | Same workload and gates on current and candidate backends, complete process-tree resource and latency measurements | Whether the alternative improves the declared deployment constraint |
| 7. Qualified physical pod | Repeat the causal procedure with an unchanged pod, qualified actual schema/profile and independent physical behavior observation | The project's physical viability claim for that bounded case |

Scope and capability advertisements must match the implemented procedures at
every gate. An older peer accepting a message, or two related C stacks agreeing
on its encoding, cannot override the selected specification. Failures should be
classified as backend, EMOSA procedure, peer-profile or pod-mapping issues so the
comparison does not attribute every failure to the transport.

The recommended decision is therefore to **record all four projects, shortlist
Rust for the first optional backend experiment, retain Technicolor as the C
alternative, use meshComms as historical implementation evidence, and consider
pyieee1905 for independently checked packet tools**. A backend should earn adoption
by passing the relevant gates and reducing a demonstrated cost or gap.

The acceptance path remains **real EasyMesh messages → EMOSA → unchanged physical
OpenSync pod → independently observed behavior**. None of these repositories,
and no simulator-only result, completes that path by itself.
