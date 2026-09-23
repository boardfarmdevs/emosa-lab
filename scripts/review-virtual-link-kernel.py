"""Reconstruct the exact owned Ubuntu scheduler/framing source; no kernel changes."""

import argparse
import json
import runpy
from pathlib import Path

FILES = {
    "include/net/sch_generic.h": "53d8058fc31483d15b07178f647cd15a417ccc763f64e271e7f0dec479631d55",
    "net/sched/sch_api.c": "a66256b92f3edc3a447f2cd4497d16a7e84637625ab9b6fbb09b24996928e5ac",
    "net/sched/sch_fifo.c": "48347b4fc78321796f9d4493b3c9e1d02bcf36e0396cf215f6e8cb28ca90106c",
    "net/sched/sch_generic.c": "8435004271d4e1bcdaad66093a8c6301c14d44141cff7e9e4fd5fc6e54d97451",
    "net/sched/sch_tbf.c": "23e0a1830d6d547e2613a953b9bb1f9c630118caad19e7cbdefae8162b612793",
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path, default=Path(".cache/upstream/linux-6.8.0-139.139"))
    args = parser.parse_args()
    review = runpy.run_path(str(Path(__file__).with_name("review-station-kernel.py")))["review"]
    print(json.dumps(review(args.cache, args.output, FILES), indent=2))
