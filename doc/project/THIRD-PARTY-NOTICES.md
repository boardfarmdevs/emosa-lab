# Third-party material

`tests/fixtures/opensync/opensync.ovsschema` is the unmodified schema from
plume-design/opensync commit `78d8a7194d5e77635877cc456231e7be5cf03d68`.
The original license is retained as `tests/fixtures/opensync/LICENSE.opensync`;
the corresponding source path, hash and provenance are in `provenance.json`.
The native Kconfig evidence is also from that pinned source. No full OpenSync
source checkout is included in the Python package. The separate native lab
retains upstream LICENSE/NOTICE in its source extraction and in `deploy/native/`.
Its two documented lab patches and C glue are separately recorded; native binaries
and runtime image exports are not distributed in the Python wheel.

Python dependencies are distributed under their upstream licenses: Open vSwitch
(`ovs`, Apache-2.0), jsonschema (MIT), and their locked transitive dependencies.
The OVS C source archive is fetched separately by an explicit build script; its
COPYING/NOTICE files remain in that archive/build tree. Retain them with any
distributed binary bundle. Ruff/pytest/uv and the native experiment's
kconfiglib/Jinja build tools retain their respective upstream licenses.

The secure-fleet listener uses Python's standard `ssl` module and the runtime's
OpenSSL; the current OVSDB tools are also built with OpenSSL enabled. Synthetic
certificate generation uses the already locked `cryptography` dependency.
The retained clean runtime includes the upstream OVS source/notice files and
installed Debian/Python license metadata. Its exact package inventory and image
digest are recorded in [reliability evidence](../evidence/reliability/README.md).

No prplMesh dependency is linked, imported or fetched by the Python package build. Normative
IEEE/Wi-Fi Alliance specification documents are referenced, not redistributed.

The native peer baseline builds a separate hostapd/wpa_supplicant runtime from
hostap commit `cff80b4f7d3c0a47c052e8187d671710f48939e4`, with the explicit
fixed-BSS file-update patch under `deploy/peer-baseline/patches/`. That patch
contains upstream context; its BSD license and copyright notice are retained in
`deploy/peer-baseline/LICENSE.hostap`. The build/configuration/binary digests and
source URL are recorded in that directory's `reference.json`. Preserve the
upstream license with any distributed source or binary bundle. These native
artifacts are separate from EMOSA's Python package.

The owned WSC provisioning experiment separately builds a small payload helper
against unmodified hostapd 2.11 WPS source files. Its pinned archive and source
hashes come from `tests/fixtures/protocol/wsc-messages/provenance.json`; the build
retains the upstream archive and `COPYING` in `.cache/wsc-registrar/`. Preserve
that BSD license and upstream notices with any redistributed helper binary.
The helper uses OpenSSL and is not included in the EMOSA wheel. The experiment's
C harness is EMOSA lab code, not a complete upstream controller implementation.

The separate native peer experiment also rebuilds prplMesh's NL80211 `libbwl`
with a recorded primary-BSS identity patch. It uses the pinned upstream source
and companion patchset identified in `deploy/peer/prplmesh.reference.json`.
The additional patch retains upstream context; the BSD+Patent license is in
`deploy/peer-baseline/LICENSE.prplmesh`. Keep that license and the source's
copyright notices with redistributed artifacts. The native controller/agent
executables and other native libraries remain the pinned companion build.
