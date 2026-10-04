# Handover

EMOSA (EasyMesh to OpenSync Adapter) makes unchanged OpenSync pods EasyMesh agents of an
existing EasyMesh controller. It has two implementations of one specification: the
Python reference (`src/emosa`), which generates the conformance vectors, and the C
implementation (`c/`), taken to production quality so that it can be evaluated in full
and carried by an RDK-B gateway. This is what a team taking it over needs, without
access to the labs it was developed in (the easymesh-labs alignment plan's step 8.6).

## Read in this order

1. [spec/README.md](../../spec/README.md): what EMOSA does (the contract).
2. [architecture.md](architecture.md): the system, the C programs and where they run;
   then [spec/design.md](../../spec/design.md) for the component design.
3. [modules.md](modules.md): every C source file, its reference module and its tests.
4. [coding-standard.md](coding-standard.md) and [c/QUALITY.md](../../c/QUALITY.md): the
   standard, the gates and where each stands.
5. [testing.md](testing.md): every test tier, how to run it, how a change of behaviour
   flows from the specification to the vectors to the C.
6. [traceability.md](traceability.md): each section of the specification with the tests
   that check it.
7. [decisions.md](decisions.md): why it is as it is, and the decisions still open.

## Where it stands

- **Contract:** the specification, the schemas (`schemas/`) and the vectors
  (`spec/conformance`, 21 sets). Both implementations reproduce every vector.
- **The C's gates** ([c/QUALITY.md](../../c/QUALITY.md) §3): warnings as errors on gcc
  and clang and on 32-bit x86, sanitizer-clean tests, the clang analyzer and
  clang-tidy's CERT checks without findings, a fuzz target per parser of untrusted input,
  88 % line coverage, the lab in a box passed by both implementations, a package with an
  SPDX bill of materials, the gateway image's opt-in recipe.
- **Acceptance in the labs**, recorded in the repository: the OpenSync lab's 900 s
  reference workload with the whole adapter in C
  ([opensync-lab-proof](../records/evidence/opensync-lab-proof/README.md)); the RDK
  lab's room suite with the pods' agents in C, and EMOSA's footprint inside the RDK
  gateway's container ([rdk-lab](../records/evidence/rdk-lab/README.md) §3).

## What is open

- EMOSA's own license (the owner's decision; [decisions.md](decisions.md)).
- Plan decisions 5.1 to 5.3: the gateway's 1905 arrangement, the GTP's role, how pods
  find EMOSA in the field.
- Six modules of the agent's core below 85 % line coverage, and two inputs not fuzzed
  yet: the OVSDB framing and the configuration (QUALITY §3).
- What the specification leaves out (spec §9) and the design's known limitations
  (design §14): among them backhaul link metrics, complete EasyMesh AP and station
  metrics from a hwsim pod, 5/6 GHz and WPA3.
- An idle agent's CPU (2.4 to 2.8 % of a core per pod in the RDK lab) grows with its
  journal: both implementations parse every operation again several times a second, and
  the journal keeps every operation. Parse once and keep, search linearly, and bound the
  journal's retention (the specification has no bound yet) before a gateway carries many
  pods ([the footprint record](../records/evidence/rdk-lab/README.md)).
- The status file still names the C `c-lab-prototype` (the labs read it); rename it with
  the labs' tools when the C is released.
