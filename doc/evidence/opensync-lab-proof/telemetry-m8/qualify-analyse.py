"""Compare EMOSA's summed per-period counters with the driver's cumulative ones.

emosa.log: <poll time> (measured_at, epoch_start, periods, tx_bytes, rx_bytes, tx_frames,
           rx_frames) of one station, from the agent's status
iw.log:    <sample time> tx_bytes rx_bytes tx_packets rx_packets (AP side, toward the station)
marks:     the stimulus's start and end times

Usage: qualify-analyse.py DIR (with emosa.log, iw.log and marks)
"""

import ast
import json
import sys
from pathlib import Path

d = Path(sys.argv[1])
start, end = (float(line.split()[0]) for line in (d / "marks").read_text().splitlines())
samples = {}
for line in (d / "emosa.log").read_text().splitlines():
    _, rest = line.split(" ", 1)
    value = ast.literal_eval(rest.strip())
    if value is not None:
        samples[value[0]] = value  # keyed by the period end
iw = []
for line in (d / "iw.log").read_text().splitlines():
    f = line.split()
    if len(f) == 5:
        iw.append((float(f[0]), *map(int, f[1:])))

before = [s for s in samples.values() if s[0] < start and s[3] is not None]
after = [s for s in samples.values() if s[0] > end + 2 and s[3] is not None]
pairs = [(b, a) for b in before for a in after if a[1] == b[1]]  # same counter epoch
if not pairs:
    sys.exit(f"no before/after pair in one epoch: {sorted(samples.values())}")
b, a = max(pairs, key=lambda p: (p[0][0], -p[1][0]))  # the tightest window


def at(t):
    return min(iw, key=lambda s: abs(s[0] - t))


ib, ia = at(b[0]), at(a[0])
NAMES = ("tx_bytes", "rx_bytes", "tx_frames", "rx_frames")
emosa = {k: a[3 + i] - b[3 + i] for i, k in enumerate(NAMES)}
driver = {k: ia[1 + i] - ib[1 + i] for i, k in enumerate(NAMES)}
difference = {k: round(100 * (emosa[k] - driver[k]) / driver[k], 3) for k in NAMES if driver[k]}
result = {
    "window": [b[0], a[0]],
    "periods": a[2] - b[2],
    "iw_sample_offsets_s": [round(ib[0] - b[0], 2), round(ia[0] - a[0], 2)],
    "emosa": emosa,
    "driver": driver,
    "difference_percent": difference,
}
print(json.dumps(result, indent=1))
