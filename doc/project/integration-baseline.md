# Frozen source and image baseline

**Baseline ID: `opensync-integration-20260923-01`; preserved and hash-verified on
2026-09-23.** The [public manifest](integration-baseline.json) contains selected
identifiers and hashes. Raw source archives, applied patches, image archives,
provider values and runtime observations remain private, outside both repos.
No integration, rebuild, VM snapshot, service restart or configuration handover
was executed to create this baseline.

“Frozen” means a separately retained, hash-checked artifact/source snapshot.
Neither active checkout was reset, stashed or made read-only. The running VMs
continue to operate and may evolve; future runs compare them against this record
and declare deviations. The copies are not write-once storage or off-host backups.

## Selected system and preserved material

| Host | Preserved material | Interpretation |
| --- | --- | --- |
| rev150 | EMOSA source/public-evidence archive at `4dece3c1c7581bb7eec0372c587e4f10cf8e05d0`; code unchanged from green `58763f3b1b570230d8f80d5e5f2139304801b851` | Subsequent documentation changes do not change the implementation baseline. |
| rev150 | Three pinned prplMesh input archives, two candidate controller/BPL pairs, metadata and regression results | The lifecycle-soak candidate and sparse-ESP candidate remain different artifacts with different evidence. Neither was installed during preparation. |
| rev140 | opensync-lab `77318dc2e812319b5ddae41ef17491c34d2e6d3c` plus captured working-tree modifications and nonignored untracked files | Commit alone is insufficient. Git bundles, working-tree archives, status, binary diffs and per-file hashes preserve the inspected source state. |
| rev140 | Core, cfg80211 platform, OpenWrt-template vendor and local-provider Git snapshots; applied `mvx-local` overlay | Exact commits are in the manifest; core and vendor trees contain local changes. Ignored build output is excluded. |
| rev140 | Pod rootfs/metadata from `20260923012050`, separately copied and hash-checked | Selected existing image; no rebuild and no substitution of the newer available image. |
| rev140 | Actual schema, expanded container placements, existing status files, selected pod binaries/startup helpers and NOC image identity | Read-only observations, not a complete live filesystem/database backup or a fresh connectivity test. |

The selected VM is **`mvx-opensync-0922`** with its existing `mv3`, `pod` and
`wclient`. The additional **`opensync-lab-0923`** VM had only `mv3` at capture time;
its name does not make it the evaluated replacement. Re-inventory a later renamed
or completed VM before changing the selected target.

| Pod image identity | Digest |
| --- | --- |
| LXD `volatile.base_image` | `68f3f495fb9306c9896b35ad7b11db7c9f2ccd80b7ea9492ad27010203ce8f51` |
| Rootfs SHA-256 | `f9545d744f3ffae707db0bd3f28151de4d4b3110f47c53ad85574770de868160` |
| Metadata SHA-256 | `62c723b7c4653611e68b74f31ee2f1c1ae8463d0fc9fabb1f501750fda65f49f` |

The runtime `cm`, `owm` and `nm` hashes still match the evaluation. Startup-helper
hashes are recorded separately because image-time transformations and later
source changes exist. This baseline preserves both facts; it does not assert
that rebuilding the dirty checkout recreates the running image bit for bit.
The [patch register](opensync-lab-integration-plan.md#32-patch-and-adaptation-register)
explains the OpenSync band filter, hostap changes, native target/provider, startup
transformations and gateway HAL fixes.

## Private locations and read-only verification

On **each host**, the private directory is:

```text
/home/rev/.local/state/emosa/baselines/opensync-integration-20260923-01
```

The identical path names contain **different host-specific collections**. Directories
are owner-only; retained regular files are mode `0400`. Each collection contains
`manifest.json`, the capture procedure and `SHA256SUMS`. The public JSON pins the
hashes of the private manifest and checksum list, so replacing both a file and its
local checksum list is detectable against the committed record.

On rev150, read-only verification is:

```bash
cd /home/rev/.local/state/emosa/baselines/opensync-integration-20260923-01
sha256sum manifest.json SHA256SUMS
sha256sum --quiet --check SHA256SUMS
```

For rev140, run the equivalent command through SSH:

```bash
ssh rev140 'cd /home/rev/.local/state/emosa/baselines/opensync-integration-20260923-01 && sha256sum manifest.json SHA256SUMS && sha256sum --quiet --check SHA256SUMS'
```

Compare the first two digests with the corresponding host fields in the public
manifest. A successful quiet check has exit status zero. During preparation Git working-tree
archive members were checked against their recorded inventory, each
source tree was unchanged across its capture, Git bundles verified, image copies
matched the selected originals, and both complete checksum lists passed. This
checks preservation, not functional compatibility.

## Using the baseline later

Before implementation, follow [integration phase 0](opensync-lab-integration-plan.md#phase-0--freeze-an-identifiable-baseline).
The archive work is complete; resolve any new VM/source drift and choose an
exclusive experiment or isolated complete-VM copy. Do not import an image or
restore files over the active lab as part of read-only qualification.

To inspect preserved sources, extract into a new **private** work directory.
A Git bundle contains committed objects; the corresponding working-tree archive
contains the captured edited files. Follow its inventory for deleted files and
symlinks. Do not apply a dirty patch over an already edited checkout. Provider
and inventory material may contain lab secrets, so these archives are not public
release artifacts.

A future build must record all extra inputs and generated transformations. The
selected image and native archives are preserved, but gateway/NOC/Boardfarm images,
Yocto caches, mutable package repositories and the complete VM are not copied by
this task. Full environment reproduction and disaster recovery therefore remain
separate work. A mismatch should create a new baseline/deviation record, not
replace this baseline's files or silently update its hashes.

[Acceptance levels](integration-acceptance.md) keep warm onboarding, cold start,
operational recovery and complete sustained service separate. Every OpenSync-lab
integration level remains **not evaluated** in the preparation register.
