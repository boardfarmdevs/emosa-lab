#!/bin/bash
# Inside an RDK EasyMesh lab VM with the EMOSA option (vm/lab.sh up c), root: EMOSA's fleet
# and agents in the RDK controller's own container, as on a gateway that carries the
# adapter (meta-cmf-bananapi-vcpe's recipe emosa), and its footprint there (easymesh-labs
# plan 5.4). The broker and the GTP stay where vm/lab.sh put them.
#
#   gateway.sh on [EMOSA_IPK]     the fleet and agents into the controller container: the
#                                 package emosa (EMOSA_IPK, built with the image's recipe,
#                                 over what the gateway has; without it, the package the
#                                 image or an earlier run put there), the adapter container's
#                                 registry and state taken over into the places the gateway's
#                                 package names (/etc/default/emosa: /nvram/emosa on RDK, the
#                                 status in /run), its fleet configuration written there, the
#                                 front and agent ports forwarded to the gateway, the agents'
#                                 broker reached through the host; the agents' trunk is the
#                                 package's veth pair into brlan0. Again while EMOSA runs in
#                                 the gateway (after it was deployed anew): the forwarding back;
#                                 with EMOSA_IPK, that package over the running one
#   gateway.sh off                back into the adapter container, the state taken back; the
#                                 package stays installed in the gateway, unconfigured (inert)
#   gateway.sh status             where EMOSA runs, its agents, the gateway's memory
#   gateway.sh measure LABEL SECONDS [INTERVAL]
#                                 the gateway's memory and CPU, and EMOSA's and the
#                                 controller's processes (wherever EMOSA runs), every INTERVAL
#                                 seconds (60) -> /var/lib/emosa-lab/footprint/LABEL
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
exec </dev/null
export PATH=/snap/bin:$PATH
CTL=${EMOSA_RDK_CONTROLLER:-bpibroadband}
WAN_HOST=${EMOSA_WAN_HOST:-10.101.0.40}
FLEET_PORT=${EMOSA_FLEET_PORT:-6640}
AGENTS=(6651 "${EMOSA_FLEET_LAST_PORT:-6690}")
BROKER_HOST_PORT=${EMOSA_GATEWAY_BROKER_PORT:-11883}    # the agents' broker on the VM's loopback
FOOTPRINT=/var/lib/emosa-lab/footprint
LOG_TAG=emosa-gateway
# shellcheck source-path=SCRIPTDIR source=../../lib/emosa-vm.sh
source "$(cd "$(dirname "$0")/../.." && pwd)/lib/emosa-vm.sh"

# where EMOSA runs (in_gateway, emosa_where) and a container's places (emosa_layout,
# emosa_state_root): deploy/lib/emosa-vm.sh

provisioned() { agents_provisioned_in "$1"; }    # provisioned CT: agents provisioning in CT

wait_provisioned() {    # wait_provisioned CT N: N agents provisioning in CT (at most 5 minutes)
    local i
    for i in $(seq 1 60); do
        [ "$(provisioned "$1")" -ge "$2" ] && return 0
        sleep 5
    done
    die "$1: $(provisioned "$1") of $2 agents provisioned after 5 minutes"
}

pods_running() { lxc list -c n -f csv | grep -cE '^pod-[0-9]+$' || true; }

stop_emosa() {    # stop_emosa CT: the fleet and every agent in CT stopped and disabled
    cx "$1" sh -c 'systemctl disable -q --now emosa-fleet 2>/dev/null || true
        for u in $(systemctl list-units --all --plain --no-legend "emosa-agent@*" | cut -d" " -f1); do
            systemctl disable -q --now "$u" 2>/dev/null || true
        done'
}

move_state() {    # move_state FROM TO: the registry and the agents' state, into TO's places
    local from_config to_config to_agents to_root to_run config from_root
    read -r from_config _ _ _ <<<"$(emosa_layout "$1")"
    read -r to_config to_agents to_root to_run <<<"$(emosa_layout "$2")"
    config=$(cx "$1" cat "$from_config")
    from_root=$(jq -r .state_root <<<"$config")
    # TO's own: gone, but for emosa's broker PKI (pki in its state root), which stays
    cx "$2" sh -c "rm -rf '$to_agents'; mkdir -p '$to_root' '$(dirname "$to_config")'
        for f in '$to_root'/* '$to_root'/.[!.]*; do
            [ -e \"\$f\" ] && [ \"\$f\" != '$to_root/pki' ] && rm -rf \"\$f\"; done; true"
    cx "$1" sh -c "cd '$from_root' && tar -cf - \$(ls -A | grep -vx pki)" | lxc exec "$2" -- tar -xf - -C "$to_root"
    # FROM's fleet configuration with TO's places; the agents' configurations are the
    # fleet's to write again, for its registry's agents when it starts (spec 4)
    jq --arg root "$to_root" --arg agents "$to_agents" --arg run "$to_run" \
        '.state_root = $root | .config_dir = $agents | if $run == "-" then del(.run_root) else .run_root = $run end' \
        <<<"$config" | lxc exec "$2" -- sh -c "cat > '$to_config'"
    # an agent's status is its own process's: none until it runs in TO
    cx "$2" sh -c "rm -f '$to_root'/*/status.json"
}

forward() {    # forward CT: the front and agent ports into CT (from the other container)
    local other=emosa dev
    [ "$1" = emosa ] && other=$CTL
    for dev in front agents; do
        ! has_device "$other" "$dev" || lxc config device remove "$other" "$dev" >/dev/null
    done
    proxy "$1" front "$FLEET_PORT"
    proxy "$1" agents "${AGENTS[0]}-${AGENTS[1]}"
}

install_package() {    # install_package [IPK]: the package emosa in the gateway, IPK over what is there
    local ipk=${1:-} d
    if [ -z "$ipk" ]; then
        cx "$CTL" test -x /usr/bin/emosa-fleet-c || die "the gateway has no EMOSA: give the package (emosa_*.ipk)"
        return 0
    fi
    [ -f "$ipk" ] || die "$ipk: no such package"
    d=$(mktemp -d)
    (cd "$d" && ar x "$ipk" && mkdir root && tar -xf data.tar.* -C root)
    tar -C "$d/root" -cf - . | lxc exec "$CTL" -- tar -xf - -C /
    rm -rf "$d"
    cx "$CTL" systemctl daemon-reload
}

plumbing() {    # the front and agent ports into the gateway, the agents' broker (in emosa) through the host
    forward "$CTL"
    if [ -f "$STATE/telemetry" ]; then
        has_device emosa mqtt-agents || lxc config device add emosa mqtt-agents proxy bind=host \
            listen="tcp:127.0.0.1:$BROKER_HOST_PORT" connect=tcp:127.0.0.1:1883 >/dev/null
        has_device "$CTL" mqtt-agents || lxc config device add "$CTL" mqtt-agents proxy bind=instance \
            listen=tcp:127.0.0.1:1883 connect="tcp:127.0.0.1:$BROKER_HOST_PORT" >/dev/null
    fi
}

on() {
    running "$CTL" || die "$CTL is not running"
    if in_gateway; then    # again, e.g. after the gateway was deployed anew: its state is its own
        if [ -n "${1:-}" ]; then    # a new package over the running one: fleet and agents restarted
            install_package "$1"
            stop_emosa "$CTL"
            log "gateway: $(cx "$CTL" /usr/bin/emosa-agent-c --version) installed over"
        fi
        cx "$CTL" test -x /usr/bin/emosa-fleet-c || die "$CTL has no EMOSA: deploy an image with it, or off"
        plumbing
        cx "$CTL" systemctl enable -q --now emosa-fleet
        log "gateway: EMOSA in $CTL again ($(emosa_layout "$CTL"))"
        wait_provisioned "$CTL" "$(pods_running)"
        log "gateway: $(provisioned "$CTL") agents provisioning in $CTL"
        return
    fi
    exists emosa && cx emosa test -f "$(emosa_layout emosa | cut -d" " -f1)" || die "the EMOSA option first: lab.sh up c"
    install_package "${1:-}"
    log "gateway: $(cx "$CTL" /usr/bin/emosa-agent-c --version); $(cx "$CTL" sh -c '. /etc/default/emosa; echo "trunk $EMOSA_TRUNK into ${EMOSA_BRIDGE:-(none)}"'); places $(emosa_layout "$CTL")"
    stop_emosa emosa
    move_state emosa "$CTL"
    plumbing
    touch "$STATE/gateway"
    cx "$CTL" systemctl enable -q --now emosa-fleet
    log "gateway: fleet started in $CTL, with its registry's agents"
    wait_provisioned "$CTL" "$(pods_running)"
    log "gateway: $(provisioned "$CTL") agents provisioning in $CTL"
}

off() {
    in_gateway || die "EMOSA does not run in $CTL"
    stop_emosa "$CTL"
    move_state "$CTL" emosa
    # the gateway as it was: no configuration (the package inert), no trunk
    local config agents root run
    read -r config agents root run <<<"$(emosa_layout "$CTL")"
    [ "$run" != - ] || run=
    cx "$CTL" sh -c "rm -rf '$config' '$agents' '$root' ${run:+'$run'}
        . /etc/default/emosa 2>/dev/null; [ -z \"\${EMOSA_BRIDGE:-}\" ] || ip link del \"\${EMOSA_TRUNK:-emlan}\" 2>/dev/null || true"
    forward emosa
    ! has_device "$CTL" mqtt-agents || lxc config device remove "$CTL" mqtt-agents >/dev/null
    ! has_device emosa mqtt-agents || lxc config device remove emosa mqtt-agents >/dev/null
    rm -f "$STATE/gateway"
    cx emosa systemctl enable -q --now emosa-fleet
    log "gateway: fleet back in emosa"
    wait_provisioned emosa "$(pods_running)"
    log "gateway: $(provisioned emosa) agents provisioning in emosa"
}

status() {
    local ct
    ct=$(emosa_where)
    echo "EMOSA runs in: $ct"
    cx "$ct" sh -c 'for f in '"'$(emosa_state_root "$ct")'"'/*/status.json; do [ -e "$f" ] || continue
        case $f in *.released-*) continue ;; esac
        printf "%s %s\n" "$(basename "$(dirname "$f")")" "$(grep -oE "\"state\":[[:space:]]*\"[a-z_]+\"" "$f" | head -1)"; done'
    cx "$CTL" sh -c 'printf "gateway memory: %s MiB of %s MiB\n" $(($(cat /sys/fs/cgroup/memory.current) / 1048576)) \
        $(($(cat /sys/fs/cgroup/memory.max) / 1048576))'
}

# One sample of a container: its cgroup's memory and CPU, and the followed processes'
# memory (smaps_rollup) and CPU time (clock ticks).
PROBE='
import json, os, sys, time
def rd(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""
out = {"t": round(time.time(), 1), "container": sys.argv[1]}
cg = {k: rd("/sys/fs/cgroup/" + k).strip() for k in ("memory.current", "memory.max", "memory.peak")}
stat = dict(line.split() for line in rd("/sys/fs/cgroup/memory.stat").splitlines() if line)
cg.update({k: int(stat[k]) for k in ("anon", "file", "shmem") if k in stat})
cpu = dict(line.split() for line in rd("/sys/fs/cgroup/cpu.stat").splitlines() if line)
cg["cpu_usage_usec"] = int(cpu.get("usage_usec", 0))
out["cgroup"] = cg
procs = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    comm = rd("/proc/%s/comm" % pid).strip()
    if not comm.startswith(("emosa-", "onewifi_em_ctrl", "onewifi_em_agen")):
        continue
    fields = rd("/proc/%s/stat" % pid).rsplit(")", 1)[-1].split()
    roll = {}
    for line in rd("/proc/%s/smaps_rollup" % pid).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            roll[parts[0].rstrip(":")] = int(parts[1])
    if len(fields) > 12:
        procs.append({"pid": int(pid), "comm": comm, "ticks": int(fields[11]) + int(fields[12]),
                      "rss_kib": roll.get("Rss"), "pss_kib": roll.get("Pss"),
                      "cmd": rd("/proc/%s/cmdline" % pid).replace("\0", " ").strip()[:160]})
out["procs"] = procs
print(json.dumps(out, sort_keys=True))
'

measure() {
    local label=${1:?usage: gateway.sh measure LABEL SECONDS [INTERVAL]} seconds=${2:?seconds} every=${3:-60}
    local dir=$FOOTPRINT/$label end ct
    [[ $label =~ ^[A-Za-z0-9._-]+$ && $seconds =~ ^[0-9]+$ && $every =~ ^[1-9][0-9]*$ ]] || die "usage: gateway.sh measure LABEL SECONDS [INTERVAL]"
    [ ! -e "$dir" ] || die "$dir exists"
    mkdir -p "$dir"
    ct=$(emosa_where)
    { echo "emosa_in=$ct"; echo "pods=$(pods_running)"; date -u +"started=%FT%TZ"
      # the agent's version where it runs (an agent from before --version answers with its usage)
      if [ "$ct" = "$CTL" ]; then cx "$CTL" /usr/bin/emosa-agent-c --version || true
      else cx emosa sh -c '. /etc/default/emosa-implementation; echo "agent $EMOSA_AGENT"; "$EMOSA_AGENT" --version 2>/dev/null' || true
      fi
    } > "$dir/run.txt"
    end=$((SECONDS + seconds))
    while :; do
        cx "$CTL" python3 -c "$PROBE" "$CTL" >> "$dir/samples.jsonl"
        [ "$ct" = "$CTL" ] || cx emosa python3 -c "$PROBE" emosa >> "$dir/samples.jsonl"
        [ $SECONDS -lt "$end" ] || break
        sleep "$every"
    done
    date -u +"finished=%FT%TZ" >> "$dir/run.txt"
    python3 - "$dir" <<'EOF' | tee "$dir/summary.json"
import json, statistics, sys
from pathlib import Path
d = Path(sys.argv[1])
rows = [json.loads(line) for line in (d / "samples.jsonl").read_text().splitlines()]
out = {}
for ct in sorted({r["container"] for r in rows}):
    rs = [r for r in rows if r["container"] == ct]
    mem = [int(r["cgroup"]["memory.current"]) / 1048576 for r in rs]
    span = rs[-1]["t"] - rs[0]["t"] or 1
    entry = {
        "samples": len(rs),
        "seconds": round(span),
        "memory_current_mib": {"min": round(min(mem)), "median": round(statistics.median(mem)), "max": round(max(mem))},
        "memory_max_mib": round(int(rs[0]["cgroup"]["memory.max"]) / 1048576) if rs[0]["cgroup"]["memory.max"].isdigit() else rs[0]["cgroup"]["memory.max"],
        "cpu_percent_of_one_core": round(100 * (rs[-1]["cgroup"]["cpu_usage_usec"] - rs[0]["cgroup"]["cpu_usage_usec"]) / 1e6 / span, 1),
        "processes": {},
    }
    by = {}
    for r in rs:
        for p in r["procs"]:
            key = p["comm"] if not p["comm"].startswith("emosa-agent") else "emosa-agent-c (each)"
            by.setdefault(key, []).append(p)
    first = {p["pid"]: p for p in rs[0]["procs"]}
    last = {p["pid"]: p for p in rs[-1]["procs"]}
    for key, ps in sorted(by.items()):
        pss = [p["pss_kib"] for p in ps if p["pss_kib"] is not None]
        rss = [p["rss_kib"] for p in ps if p["rss_kib"] is not None]
        ticks = [last[pid]["ticks"] - first[pid]["ticks"] for pid in first
                 if pid in last and (first[pid]["comm"] == key or (key.startswith("emosa-agent") and first[pid]["comm"].startswith("emosa-agent")))]
        entry["processes"][key] = {
            "pss_kib": {"median": round(statistics.median(pss)), "max": max(pss)} if pss else None,
            "rss_kib_max": max(rss) if rss else None,
            "cpu_percent_of_one_core": round(100 * statistics.mean(ticks) / 100 / span, 2) if ticks else None,
            "processes_seen": len({p["pid"] for p in ps}),
        }
    out[ct] = entry
print(json.dumps(out, indent=2, sort_keys=True))
EOF
    log "footprint: $dir"
}

case ${1:-} in
    on) on "${2:-}" ;;
    off) off ;;
    status) status ;;
    measure) shift; measure "$@" ;;
    *) sed -n '2,20p' "$0"; exit 2 ;;
esac
