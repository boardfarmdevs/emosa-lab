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

No prplMesh dependency is linked, imported or fetched by the build. Normative
IEEE/Wi-Fi Alliance specification documents are referenced, not redistributed.
