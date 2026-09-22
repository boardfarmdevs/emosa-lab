# Reproduce the secure fleet in a clean nested-LXD runtime

[Learning sequence](../../doc/guides/learning-path.md) ·
[TLS/fleet/recovery concepts](../../doc/guides/secure-fleet.md)

This is priority 4: reproduce priorities 1–3 without relying on the developer's
editable checkout or existing radio lab. All commands in this guide are issued
on **HOST, at the repository root**. The driver invokes the outer VM's LXD and
then its inner containers. The runtime account is unprivileged; root is used
only to prepare packages and the disposable build container.

The owned layout is:

```text
development HOST: checkout, uv, LXD, retained base/export files
  └─ emosa-reliability VM: Ubuntu 24.04, 2 CPUs, 4 GiB RAM, 14 GiB disk
       ├─ emosa-runtime-build: pinned base + installed wheel + OVSDB tools
       └─ emosa-runtime-run: fresh image clone, emosa user, private experiments
```

Neither container needs hwsim, raw packet capabilities, prplMesh binaries or
physical-pod credentials. Simulated pods are database/manager processes inside
the run container. Existing `emosa-lab` and its radio containers are separate.

## 1. Check capacity and pinned inputs

The host needs working LXD VM support/KVM, an available 4 GiB memory budget,
storage for a 14 GiB VM disk, and export headroom. Inspect at least 10 GiB free
in both the LXD pool and host artifact filesystem. Other running labs consume
resources too. The preflight records capacity; review it before creating anything.

```sh
python3 deploy/reliability/lab.py preflight --directory .cache/reliability/clean-lxd
lxc image info 805f57b68c5eac9ef90fc2d0c435e01ad15ba98d57054b83ad83dac2ebc2f1e5
```

The VM and container base fingerprints are pinned in
[images.lock.json](../images.lock.json). `create` requires the retained container
split export and checks both hashes before mutation. On the reference host these
are `.cache/peer-evidence/ubuntu24-container` and the adjacent `.root` file.
These large files are local artifacts, not Git objects. Obtain the retained
exports from the lab owner on a new host; do not silently use today's `ubuntu:24.04`
alias and claim it is the same baseline. Import the recorded VM export/image
into the host's LXD if the pinned VM fingerprint is missing.

The driver runs on Python 3.10 or newer. Build/run Python is separately pinned to
3.13.7. Selected `uv` 0.11.17 must already be available on HOST; `uv sync --frozen`
and the ordinary OVSDB build should have completed in this checkout.

## 2. Create only the new owned environment

```sh
python3 deploy/reliability/lab.py create \
  --directory .cache/reliability/clean-lxd \
  --base-export .cache/peer-evidence/ubuntu24-container
```

The driver refuses an existing VM rather than replacing it. It waits for the new
VM agent/cloud-init, installs LXD 5.21 revision 40585, holds its refresh for the
24-hour experiment window, initializes a new `dir` pool and imports the verified
container base. Network access is needed for snap/package/Python downloads.
Record a changed snap revision as a different tuple if the retained one cannot
be obtained; do not edit an existing lab daemon's version or storage pool.

The ownership marker is `user.emosa-purpose=secure-fleet-reproduction` on the VM
and `secure-fleet-runtime` on the two named containers. Later commands verify
these markers. For another simultaneous experiment use a fresh name such as
`--vm emosa-reliability-team2` consistently on every command and a new directory.

## 3. Build the runtime from the selected checkout

```sh
python3 deploy/reliability/lab.py build --directory .cache/reliability/clean-lxd
```

This builds a wheel and exports the frozen dependency lock with hashes. It copies
only a named bundle: wheel, locked requirements, selected `uv`, OVS source archive,
build script and TLS regression tests. It does not copy `.lab`, credentials,
private journals or licensed specification PDFs. The bundle has `SHA256SUMS`.

Inside a fresh unprivileged Ubuntu system container,
[prepare-runtime.sh](prepare-runtime.sh) installs build/OpenSSL prerequisites,
creates `/opt/emosa-runtime` with Python 3.13.7, installs hash-checked dependencies
and the wheel, and builds only the pinned OVSDB tools. There is no OVS switching
daemon. It records actual Debian/Python packages, Python version, build logs and
binary hashes under `/opt/emosa-evidence`.

The build refuses an existing build container or runtime directory. A failure
is evidence: inspect it before discarding its owned container and retrying with
a new output directory. The apt repository can change; exact package inventories
and the retained runtime image identify the resulting environment. This is not a
claim of bit-identical recompilation from changing package repositories.

## 4. Publish and retain before creating experiment secrets

```sh
python3 deploy/reliability/lab.py publish --directory .cache/reliability/clean-lxd
```

“Publish” here means create a **private local LXD image**, not upload to a public
registry. The script stops the build container, publishes `emosa-secure-runtime`,
exports the image and pulls it onto HOST. `runtime-image.json` records the image
fingerprint and each export's SHA-256/size. The image is made before any tests
generate synthetic trust or journals. Its build bundle contains no pod secrets.

Keep both the export and manifest. An export digest is not a download link.
On another prepared VM, import the retained export with `lxc image import`,
verify that the fingerprint matches the manifest, and assign the same private
alias before the `run` step. `--image-alias emosa-secure-runtime-r3` selects the
final retained revision from this experiment; use the same option on `publish`
and `run` when reproducing under that alias. A new image revision needs a new
alias: do not overwrite an older experiment's identity. Retaining an image makes the installed package tuple
reproducible even if upstream apt repositories later move forward.

## 5. Run priorities 1–3 as an unprivileged account

```sh
python3 deploy/reliability/lab.py run \
  --directory .cache/reliability/clean-lxd --soak-cycles 12
```

The driver creates a new run container from that image and invokes the installed
Python with `-I` outside a checkout, as user `emosa`. The sequence is four TLS
regression tests, 4/8/16/32-pod runs with one recovery cycle each, and twelve
four-pod cycles with five-second intervals. The TLS tests include certificate
rejection and stalled handshakes. Fleet runs include trusted-certificate/wrong-
serial refusal, peer progress, real process kills/database restarts, late State
and durable ownership conflicts.

Observe progress in the invoking terminal. The multi-minute command stops on
failure. It copies only completed reports and test/version/package evidence back
to HOST. If it fails, preserve the run container and inspect private outputs under
`/home/emosa/runs`; do not publish that directory wholesale. A successful run
produces `tls-results.xml`, `fleet-4.json`, `fleet-8.json`, `fleet-16.json`,
`fleet-32.json`, `soak.json` and runtime inventories in the selected host directory.

Compare each report's declared scope and checks with the HOST run. Performance
will differ under the VM's two-CPU limit. The acceptance requirement here is the
same behavior with no hidden checkout dependency; there is no universal latency
threshold. Neither this image nor these reports qualify real EasyMesh wire
onboarding or a physical pod.

## 6. Retain evidence and clean up the owned run

Review the reports and make any needed private forensic copies first. Then:

```sh
python3 deploy/reliability/lab.py cleanup --directory .cache/reliability/clean-lxd
lxc list emosa-reliability
```

Cleanup deletes only the marked run container and stops the marked VM. It retains
the build container, private image and host exports for repeatability. It does
not modify `emosa-lab`, its containers, host radios or physical pods. To repeat,
start the VM with `lxc start emosa-reliability`, choose a new host evidence
directory and run the `run` command again. The new run generates fresh test keys.

For a demo, prepare/publish beforehand and show a short two-pod HOST run alongside
the reviewed clean-environment results. See the [learning sequence](../../doc/guides/learning-path.md)
for the step from these component observations to radio and eventual wire proof.
