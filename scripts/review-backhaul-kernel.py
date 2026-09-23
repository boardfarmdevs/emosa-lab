"""Reproduce the selected veth/TC source from the actual Ubuntu kernel package.

Reuses pinned upstream archives; does not build, install or modify a kernel.
"""

import argparse
import json
import runpy
from pathlib import Path

FILES = {
    "drivers/net/veth.c": "77c6e3e963a1b504c3d328e4dba0a4b72006b2225348dfd76e56213734d096ac",
    "include/linux/netdevice.h": "995a40b4d3ac4de8234cc7017660ecd6287cd0ef2e637a30cbbef34e20b1a96c",
    "net/core/dev.c": "bc604c4b51b4283ff327f00c4284617c7a598ebceb4243c558d0e0f0f62f4c2e",
    "net/sched/act_gact.c": "dd7d856da94e90e74330f91ca939fc02b9a4bd2b9f8a4fe1b28f2111094f15a5",
    "net/sched/cls_api.c": "d924d2e016e797982bc1ec1e16a5404bdc086e4e9dbb572b0a9c5b401d7f42cf",
    "net/sched/sch_api.c": "a66256b92f3edc3a447f2cd4497d16a7e84637625ab9b6fbb09b24996928e5ac",
    "net/sched/sch_ingress.c": "54a58937e85f34f829056efabf72007278295c7986e8093a67aa9260672a7e57",
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path, default=Path(".cache/upstream/linux-6.8.0-139.139"))
    args = parser.parse_args()
    review = runpy.run_path(str(Path(__file__).with_name("review-station-kernel.py")))["review"]
    print(json.dumps(review(args.cache, args.output, FILES), indent=2))
