# Decision log

The decisions that shaped EMOSA as it is handed over, newest first, each with its reason
and where it is recorded. The earlier, Python-only implementation decisions are in
[docs/concepts/decisions.md](../concepts/decisions.md); the work program and its
status are in the easymesh-labs alignment plan ([mesh.vcpe.dev](https://mesh.vcpe.dev/)).

| Date | Decision | Why | Where |
| --- | --- | --- | --- |
| 3 Oct 2026 | On a gateway the agents' trunk is a veth pair whose other end is a port of the controller's LAN bridge (`EMOSA_BRIDGE=brlan0`), not a macvlan on the bridge device | what a macvlan on the bridge device sends leaves through the bridge's ports and never reaches the controller on that bridge; nor do the answers come back | `deploy/adapter/files/emosa-agent-link`, `c/packaging/default-emosa` |
| 3 Oct 2026 | EMOSA in the gateway image is opt-in through an image variable (`EMOSA_ADAPTER = "1"`, `EMOSA_GTP` for the GTP), not a `DISTRO_FEATURES` entry; the default image is unchanged | a distro feature changes nearly every recipe's signature; the variable changes only the image's package list | meta-cmf-bananapi-vcpe `recipes-emosa/emosa`, plan 5.5 |
| 3 Oct 2026 | One pin of emosa-lab for the RDK lab's EMOSA option and the image's recipe (`gen/vm/lxd/emosa-lab.env`) | the lab and the image deploy the same adapter | meta-cmf-bananapi-vcpe |
| 3 Oct 2026 | The GTP is a package of its own, its unit disabled | whether the gateway serves the pods' onboarding and ends their GRE is plan 5.2's decision, still open | the recipe |
| 3 Oct 2026 | Logging through RDK's logger with `rdk_logger_ext_init`: a rolling file per program in `/rdklogs/logs`, module `LOG.RDK.EMOSA`; stderr otherwise | no change to the image's `log4crc`; no two processes roll one file | `c/src/log.c`, c/README.md |
| 3 Oct 2026 | The package carries an SPDX 2.3 bill of materials generated at build time | the dependencies' versions are those of the build, not the repository's | `c/packaging/sbom.py` |
| 3 Oct 2026 | EMOSA is licensed under the Apache License 2.0: `LICENSE`, SPDX identifiers in its C, Python and shell sources, the recipe's `LICENSE` and the bill of materials say so | the owner's decision; the license of RDK-B's components and of rdk-logger | `LICENSE`, `docs/project/third-party-notices.md` |
| 3 Oct 2026 | A rate that is not a finite number of Mbit/s in 0 .. 2^32 − 1 is no measurement and is absent; an SNR beyond the RCPI's range gives RCPI 220 | found by the CERT review (FLP34-C, INT32-C on the pod's statistics); the reference failed on them too | spec §3.6 |
| 3 Oct 2026 | An uplink hold is released by a new admission (`forget`) | the operator's way to let EMOSA switch a pod again after a failed switch | spec §8.3 |
| 3 Oct 2026 | The fleet reads its registry file for each arriving pod | a `forget` run while the fleet served was undone at the pod's next handover (box finding 14) | spec §4 |
| 3 Oct 2026 | The fleet and GTP in C write the reference's files byte for byte (canonical JSON, `indent=2` as Python writes it) | either implementation takes over from the other; the labs switch per program | `c/src/canon.c`, spec §4 |
| 3 Oct 2026 | The secret store is an interface: its policy over a backend (files now, a platform's secure storage later) | a gateway's secure storage can replace the files without touching the policy | `c/src/vault.c`, `src/emosa/secrets.py` |
| 3 Oct 2026 | A lab in a box: the real binaries in a private user and network namespace with a real `ovsdb-server`, hostap's registrar and mosquitto, in CI for each implementation | the behaviours the rooms rely on and every suite finding stay checked without the labs | `lab/src/emosa_lab/box.py`, spec/box-scenarios.md |
| 30 Sep 2026 | The bar for the C (plan 8.1): CERT C (2016) rules required; warnings as errors on gcc and clang; sanitizer-clean tests; the clang analyzer; clang-tidy's CERT checks; a fuzz target per parser of untrusted input; 85 % line coverage | production code a team can own without the labs | [c/QUALITY.md](../../c/QUALITY.md) |
| 30 Sep 2026 | Errors are values; allocation failure ends the process; one owner thread; no truncation unnoticed; secrets never in a log | the agent is supervised and its journal makes a restart safe; one thread needs no locks | c/QUALITY.md §2 |
| 30 Sep 2026 | Two deviations from CERT rules: `sscanf` for bounded integers (ERR34-C), `_GNU_SOURCE` (DCL37-C) | each bounded and checked; the feature-test macro declares nothing of the program's | c/QUALITY.md §5 |
| 29 Sep 2026 | The AI takes the C implementation to production quality for a full evaluation; a separate team may take it over later, without access to the labs, and own all of it: the C, the reference, the specification and the vectors | evaluate EMOSA in C completely before a gateway carries it | plan 5.6 |
| 29 Sep 2026 | The Python reference is the vectors' oracle; the specification, schemas and vectors are the contract | two implementations, one behaviour, checked mechanically | c/QUALITY.md §1 |
| 29 Sep 2026 | The C and the Python are interchangeable: the same configuration, status file and state directory | either can be swapped in per pod in the labs, and compared there | c/README.md |
| 28 Sep 2026 | EMOSA becomes an option of the RDK lab, with two pods as access points in its rooms; EMOSA in C inside the gateway only after that | reconcile the labs before EMOSA becomes part of the router | the alignment plan |
| before 28 Sep 2026 | EMOSA reports only what it measured: a metric it cannot build completely from the pod's measurements is not sent | a guessed metric misleads the controller's decisions | spec §3.6, §9 |
| before 28 Sep 2026 | Each pod gets a virtual agent with its own AL MAC, derived from its serial | the controller sees one EasyMesh agent per pod, stable across restarts | spec §2.2 |
| before 28 Sep 2026 | The pods stay unchanged; everything new runs off the pods, through their OVSDB, MQTT and redirector | OpenSync pods in the field cannot be changed | spec §1 |

## Open decisions

| Decision | Why it matters | Where |
| --- | --- | --- |
| 1905 on the router: EMOSA's virtual agents next to the gateway's own 1905 daemon and agent on `brlan0` | the gateway integration; the lab's `gateway.sh` assumes it | plan 5.1 |
| The GTP role: the router serves the pods' onboarding and ends their GRE, or supports only Wi-Fi backhaul and Ethernet pods | whether `emosa-gtp` is installed | plan 5.2 |
| How pods find EMOSA: the fleet's front port and the redirect on the router's LAN | the pods' handover in the field | plan 5.3 |
| What the journal retains (operations, events) and for how long | an agent's CPU and disk grow with its journal | spec §6, the footprint record |
