# Implementation decisions

- Add opt-in complete synthetic topology bindings with explicit per-pod logical
  IDs, RUIDs, names/modes and expected MACs. Pin canonical bindings in versioned
  local journal metadata; retain removed allocations and reject silent changes.
  Rebuild UUID references from each monitored snapshot. All configured resources
  must have unambiguous observed counterparts before producing an AP Operational
  BSS value. State supplies operational facts, and stations remain separate.
  The read-only diagnostic grants no actuation authority and keeps full protocol,
  capabilities, physical topology, MLD and hardware qualification pending. A
  deliberate identity migration/retirement interface is deferred and documented.
- I0: CPython 3.13.7 is the installed bootstrap runtime and is pinned in
  `.python-version`. Ubuntu 22.04 x86-64 is the inspected development host;
  it is not the required Ubuntu 24.04 nested-LXD reference deployment.
- Dependencies: upstream `ovs` is evaluated first, with a bounded worker per
  session; `jsonschema` is the single JSON Schema validator. Tool versions and
  all transitive dependencies are pinned in `uv.lock`.
- Use real disposable `ovsdb-server` processes without a switching datapath.
  No host packages, networking, cloud settings or existing LXD instances are
  changed by tests. Build test binaries into an ignored local dependency tree.
- The only initial mapping is explicitly synthetic: one existing AP BSS with
  modern WPA2-PSK fields in the pinned upstream OpenSync schema. It cannot
  qualify a physical pod. Missing P0 prevents wire/provisioning implementation.
- Secrets are private files; keyed intent fingerprints use a persistent private
  HMAC key. Public records contain secret references and redacted predicates.
- Default runner initiation is `easymesh-wire`. The runnable intermediate
  scenario explicitly selects `semantic` and never emits protocol success.
- The user requested specification resolution in parallel and confirmed that no
  physical profile exists. A bounded research subtask produced the proposed
  corpus in `../protocol/protocol-matrix.json`; exact selection and full P0 validation remain
  pending. The radio-wide WSC scope finding is an additional mapping check.
- Actual pod preparation is a separate read-only collector with a restricted
  monitor allowlist. It never retrieves PSKs/security maps and never enables
  writes. Dialing TLS requires existing client credentials, verified CA chain
  and a trusted peer certificate pin. A bounded per-session TLS accept boundary
  now supplies authenticated streams to upstream OVS for listening-manager mode.
  [Synthetic TLS/fleet tests](../guides/secure-fleet.md) qualify that component;
  actual pod trust, direction and physical behavior remain pending.
- Per-pod modifying queue capacity is zero in this foundation: one operation
  may be active and excess requests receive `BUSY`. This is a finite queue and
  avoids retaining stale queued intents; a bounded waiting queue can be added
  after its scheduling policy is selected.
- WPS 2.0.10 is selected only for the bounded cryptographic component, with its
  rules recorded before implementation. Complete IEEE/EasyMesh procedures stay
  gated by P0. Upstream hostap 2.11 generates independent synthetic expected
  bytes in a test-only C harness; it adds no EMOSA runtime/build dependency.
- No populated pod connection file exists. Operator examples use local secret
  references; actual configuration, credentials and raw pod evidence belong
  outside the repository on the EMOSA machine. Collection remains read-only.
- Prepare a named independent prplMesh controller in the dedicated peer
  container. Pin its upstream/build/patch and binary hashes separately from
  EMOSA. Its controller-only helper requires hwsim for startup. Outbound
  discovery and empty inventory are baseline evidence; its repeatable shutdown
  aborts and all actual EMOSA exchanges remain unqualified.
- Record the operator's confirmation that IEEE Std 1905.1-2013 and IEEE Std
  1905.1a-2014 have no local copies or supplied subscription mechanism. Keep
  both pending external inputs. Maintain one specification acquisition checklist,
  including direct and conditional dependencies; independent implementation and
  testing continue without enabling unvalidated wire procedures.
- Implement the obtained WPS M1/M2 payload rules independently of missing IEEE
  transport rules. Preserve exact authenticated bytes and optional settings,
  validate a whole M2 payload set, and keep the result separate from write
  authorization. Use the native hostap M1 builder and strict message/crypto
  checks as independent evidence, with their older-version limits recorded.
- Select EasyMesh 6.1 sections 3.1.2 and 7.1 and the applicable WPS AP/Network Key
  rules for independent M2 radio-payload interpretation. Authenticate the entire
  set before interpreting roles, recognize teardown without ordinary AP settings,
  preserve unsupported roles, and expose only a narrow non-authorizing candidate.
  The full CMDU, controller/radio binding and actual pod mapping remain required;
  this component opens no path to writes and does not satisfy P0.

- Add a separate lab radio manager using the pinned hostapd binary and actual
  hwsim interfaces. It publishes State only from agreeing hostapd/nl80211 reads,
  uses a single existing BSS/PSK profile, and exposes client success separately
  from radio State. Reuse the stopped native baseline containers and retained
  database/Python tools; keep the native OpenSync backend and wire gates closed.

- Bind AP Radio Basic Capabilities inputs to the persisted synthetic topology,
  model/firmware, schema and fresh regulatory context. Require pinned evidence
  files, explicit completeness and an expiring validity window. A separate
  read-only diagnostic produces values only after these checks; hash verification
  establishes input identity, not the truth of physical claims. Limit class
  mapping to reviewed IEEE 802.11-2024 Table E-4 entries, never silently omit
  unsupported classes, and keep full profile/AP Capability Report and wire
  admission pending. See the [input walkthrough](../guides/radio-capabilities.md).

- Keep profile readiness separate from component readiness. The offline
  [profile audit](../protocol/profile-readiness.md) inventories mandatory
  requirement families and selected conditional capability inclusions. Unknown
  conditions remain unknown and no planning declaration qualifies a feature.
  AP/Profile-2/Advanced value codecs preserve reserved fields on receipt and
  reject reserved output; counter units require explicit peer facts. Older
  dissector labels never replace current normative bit meanings.


Technology and Device Inventory mapping reuse the validated Basic context but
expose separate readiness results. Unknown support remains unknown. Selected
HT/VHT claims use explicit normalized inputs; unsupported HE mapping blocks that
extension instead of fabricating capabilities. Inventory represents the pod,
not the adapter host. IEEE center-channel classes never validate an observed
primary channel. See [the walkthrough](../guides/technology-inventory.md).
