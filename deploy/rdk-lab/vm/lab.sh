#!/bin/bash
# Inside an RDK EasyMesh lab VM (meta-cmf-bananapi-vcpe, root): EMOSA and an
# unchanged OpenSync pod next to the RDK lab. Staged by ../lab.sh into /opt/emosa-lab.
# Design: docs/concepts/rdk-lab.md.
#
#   lab.sh lanport                    the RDK lab's wired LAN port, a VM bridge that is a port of
#                                     the controller's brlan0 (bpibroadband eth2): the EasyMesh LAN
#                                     for EMOSA and the GTP (meta-cmf gen/wired-extender.sh lanport;
#                                     here, bridge br-emosa, with an older lab checkout)
#   lab.sh emosa                      container emosa: the adapter kit; the pods' redirector
#                                     address 10.101.0.40 on br-wan101, forwarded to it
#   lab.sh fleet                      EMOSA fleet on the front port 10.101.0.40:6640 (the pod
#                                     image's redirector) and agents on 6651-6690
#   lab.sh agent POD python|c         which implementation runs POD's agent: the Python
#                                     reference or the C agent (same config and status)
#   lab.sh implementation python|c    the implementation of every pod's agent (a pod's own
#                                     choice from lab.sh agent is dropped); the agents restart
#   lab.sh gtp                        container em-gtp: the pods' onboarding SSID (the pod image's
#                                     backhaul credentials) and their GRE, LAN leg on the lab's wired LAN port
#   lab.sh pod [NAME]                 the unchanged OpenSync pod image on two pool radios; its
#                                     backhaul to the GTP is a fixed link (EMOSA_BACKHAUL_SNR, 45)
#   lab.sh backhaul wired|wifi [POD...]
#                                     the pods' uplink: the GTP path (wired), or the gateway's
#                                     5 GHz mesh_backhaul (wifi: EMOSA's option 1, a fixed link
#                                     like the lab's own backhaul, EMOSA_WIFI_BACKHAUL_SNR 50).
#                                     Releases an uplink hold (a failed switch): the pod
#                                     switches on its next OpenSync start, restarted here
#   lab.sh move POD TARGET            the controller moves POD's backhaul station to TARGET (a mesh
#                                     node's container, its 5 GHz backhaul BSS, or a BSSID): the
#                                     controller's SteerWiFiBackhaul(), a Backhaul Steering Request
#                                     EMOSA carries out (a pod on Wi-Fi backhaul)
#   lab.sh repod [NAME]               the pod again from the staged image (its radios are
#                                     returned to the VM first, so they survive)
#   lab.sh client NAME SSID KEY       a Wi-Fi client on one pool radio
#   lab.sh medium                     regenerate wmediumd's radios (guests: user.wmediumd.guest)
#   lab.sh telemetry [POD...]         MQTT broker (mutual TLS) for the pods' own statistics; every
#                                     agent has its pod publish 5 s client reports every 5 s
#   lab.sh rooms pods|native          the room service with the pods as APs: the lab's standard rooms
#                                     (four Wi-Fi extenders and the wired extender extender_5,
#                                     bpiap-004) with the pods (meta-cmf worlds-pods), or the lab's
#                                     own standard rooms (pods stopped, controller rows removed;
#                                     meta-cmf worlds-wired)
#   lab.sh up [python|c]              the EMOSA option of the RDK lab, every step in order: lanport,
#                                     emosa, (implementation), fleet, gtp, pod-1 and pod-2, medium,
#                                     telemetry, backhaul wifi, rooms pods (meta-cmf
#                                     gen/vm/lxd/build.sh emosa runs it, with EASYMESH_EMOSA_AGENT)
#   lab.sh status
set -euo pipefail
exec </dev/null
export PATH=/snap/bin:$PATH
ART=/opt/emosa-lab
IMAGE=${EMOSA_IMAGE:-ubuntu:24.04}
CTL=${EMOSA_RDK_CONTROLLER:-bpibroadband}
WAN_BRIDGE=${EMOSA_WAN_BRIDGE:-br-wan101}
WAN_HOST=${EMOSA_WAN_HOST:-10.101.0.40}    # the pod image's redirector (service provider mvx-local)
FLEET_PORT=${EMOSA_FLEET_PORT:-6640}
AGENTS=(6651 "${EMOSA_FLEET_LAST_PORT:-6690}")
BHAUL_SSID=${EMOSA_PODBH_SSID:-opensync-lab-bhaul}          # also from the pod image
BHAUL_KEY=${EMOSA_PODBH_KEY:-opensync-lab-bhaul-psk}
LABREPO=${EMOSA_RDK_LAB_REPO:-/home/easymesh/git/meta-cmf-bananapi-vcpe}
# the lab's room service: easymesh-room-service with its rooms in gen/rooms, or in a VM built
# before meta-cmf-bananapi-vcpe of 30 September, easymesh-room-demo with gen/demo
if [ -d "$LABREPO/gen/rooms" ]; then ROOM=easymesh-room-service ROOMS=gen/rooms; else ROOM=easymesh-room-demo ROOMS=gen/demo; fi
# The lab's RF medium: easymesh-medium as the gen/medium submodule, or, in a lab
# built before it, its own gen/wmediumd.
if [ -d "$LABREPO/gen/medium/wmediumd" ]; then
    MEDIUM_WMEDIUMD=gen/medium/wmediumd MEDIUM_CONFIGURATOR=gen/medium/configurator
else
    MEDIUM_WMEDIUMD=gen/wmediumd MEDIUM_CONFIGURATOR=gen/wmediumd/configurator
fi
LOCK=/run/easymesh-hwsim-allocator.lock                     # the RDK lab's own allocator lock
UPSTREAM=${EMOSA_POD_UPSTREAM:-bpibroadband/wifi1.1}          # the backhaul BSS a pod on Wi-Fi joins
LOG_TAG=emosa-rdk
# the pods' telemetry: 5 s client reports and survey, published every 5 s
TELEMETRY_OPTIONS='"reporting_interval": 5, "sampling_interval": 5, "publish_interval": 5, "survey": true'
# shellcheck source-path=SCRIPTDIR source=../../lib/emosa-vm.sh
source "$(cd "$(dirname "$0")/../.." && pwd)/lib/emosa-vm.sh"    # EMOSA's steps shared with the OpenSync lab

lan_bridge() {    # the bridge of the RDK lab's wired LAN port (meta-cmf gen/wired-extender.sh lanport)
    local d n
    for d in $(lxc config device list "$CTL" 2>/dev/null); do
        [ "$(lxc config device get "$CTL" "$d" name 2>/dev/null)" = eth2 ] || continue
        lxc config device get "$CTL" "$d" parent
        return
    done
    for n in $(lxc network list -f csv 2>/dev/null | cut -d, -f1); do
        [ "$(lxc network get "$n" user.easymesh.lanport 2>/dev/null)" = true ] && { echo "$n"; return; }
    done
    echo br-emosa    # this script's own port, before the RDK lab owned one
}

# --- radios: the RDK lab's allocator contract (gen/gen-util.sh) ---------------------------
# A pool radio is free when its virt-wlan* netdev is the only netdev of its phy in the VM's
# namespace and no LXD profile or instance references it. Allocation holds the lab's lock.
radios_free() {
    local p nds vw
    for p in /sys/class/ieee80211/phy*; do
        nds=$(ls "$p/device/net" 2>/dev/null)
        vw=$(printf '%s\n' "$nds" | grep -m1 '^virt-wlan[0-9]' || true)
        [ -n "$vw" ] && [ "$(printf '%s\n' "$nds" | grep -c .)" -eq 1 ] && echo "$vw"
    done | sort -V
}
radios_reserved() {
    local p d
    for p in $(lxc profile list -f csv -c n); do
        for d in $(lxc profile device list "$p"); do lxc profile device get "$p" "$d" parent 2>/dev/null || true; done
    done
    for p in $(lxc list -f csv -c n); do
        for d in $(lxc config device list "$p"); do lxc config device get "$p" "$d" parent 2>/dev/null || true; done
    done
}
# radios_take PROFILE N: add N free radios to PROFILE as wlan0..wlanN-1, under the lock
radios_take() {    # radios_take PROFILE N: wlan0..wlanN-1
    local profile=$1 n=$2 fd free i
    exec {fd}>"$LOCK"
    flock "$fd"
    mapfile -t free < <(comm -23 <(radios_free | sort) <(radios_reserved | grep "^virt-wlan" | sort -u) | sort -V)
    [ "${#free[@]}" -ge "$n" ] || { flock -u "$fd"; die "only ${#free[@]} free pool radios, need $n"; }
    for i in $(seq 0 $((n - 1))); do
        lxc profile device add "$profile" "wlan$i" nic nictype=physical parent="${free[$i]}" name="wlan$i" >/dev/null
        log "$profile: wlan$i <- ${free[$i]}"
    done
    flock -u "$fd"
}
# radios_reclaim PROFILE: after its container was deleted, the profile's phys come back to
# the VM carrying the VIFs the container made, and without their virt-wlanN (an OpenSync pod
# renames it): LXD then cannot start the container again. Give each phy back exactly one
# netdev, virt-wlanN. Only the profile's own radios are touched (phyN carries virt-wlanN).
radios_reclaim() {
    local profile=$1 d parent n p nd keep
    for d in $(lxc profile device list "$profile"); do
        parent=$(lxc profile device get "$profile" "$d" parent 2>/dev/null) || continue
        case "$parent" in virt-wlan[0-9]*) ;; *) continue ;; esac
        n=${parent#virt-wlan}
        p=/sys/class/ieee80211/phy$n
        [ -d "$p" ] || continue    # inside a container: nothing to reclaim
        keep=
        [ -e "$p/device/net/$parent" ] && keep=$parent
        for nd in $(ls "$p/device/net"); do
            [ "$nd" = "$keep" ] && continue
            if [ -z "$keep" ]; then
                ip link set "$nd" down
                if ip link set "$nd" name "$parent"; then keep=$parent; continue; fi
            fi
            iw dev "$nd" del
        done
        [ -n "$keep" ] || iw phy "phy$n" interface add "$parent" type managed
        log "$profile: reclaimed phy$n as $parent"
    done
}
# radios_await PROFILE: a deleted container's phys return to the VM only when its network
# namespace is torn down, which the kernel does asynchronously (seconds, sometimes more).
# Wait for them before a restart, so they are reclaimed rather than replaced as lost.
radios_await() {
    local profile=$1 d parent missing i
    for i in $(seq 60); do
        missing=0
        for d in $(lxc profile device list "$profile"); do
            parent=$(lxc profile device get "$profile" "$d" parent 2>/dev/null) || continue
            case "$parent" in virt-wlan[0-9]*) ;; *) continue ;; esac
            [ -d "/sys/class/ieee80211/phy${parent#virt-wlan}" ] || missing=$((missing + 1))
        done
        [ "$missing" -eq 0 ] && return 0
        sleep 2
    done
    log "$profile: $missing radio(s) did not come back within 120 s"
}
# radios_replace_lost PROFILE: a profile radio whose phy no longer exists is swapped for a
# free one under the same device name (wlan0 stays the 2.4 GHz radio, wlan1 the 5 GHz one)
radios_replace_lost() {
    local profile=$1 d parent fd free=() lost=()
    for d in $(lxc profile device list "$profile"); do
        parent=$(lxc profile device get "$profile" "$d" parent 2>/dev/null) || continue
        case "$parent" in virt-wlan[0-9]*) ;; *) continue ;; esac
        [ -d "/sys/class/ieee80211/phy${parent#virt-wlan}" ] || lost+=("$d:$parent")
    done
    [ "${#lost[@]}" -gt 0 ] || return 0
    exec {fd}>"$LOCK"
    flock "$fd"
    mapfile -t free < <(comm -23 <(radios_free | sort) <(radios_reserved | grep "^virt-wlan" | sort -u) | sort -V)
    [ "${#free[@]}" -ge "${#lost[@]}" ] || { flock -u "$fd"; die "only ${#free[@]} free pool radios"; }
    for d in "${!lost[@]}"; do
        lxc profile device remove "$profile" "${lost[$d]%%:*}" >/dev/null
        lxc profile device add "$profile" "${lost[$d]%%:*}" nic nictype=physical \
            parent="${free[$d]}" name="${lost[$d]%%:*}" >/dev/null
        log "$profile: ${lost[$d]%%:*}: radio ${lost[$d]#*:} is lost (no wiphy), now ${free[$d]}"
    done
    flock -u "$fd"
    log "$profile: new radios are not on the medium yet: run medium"
}
guest_profile() {    # guest_profile NAME RADIOS [memory]: a container on the medium
    local name=$1
    lxc profile show "$name" >/dev/null 2>&1 && return
    lxc profile create "$name" >/dev/null
    lxc profile set "$name" user.wmediumd.guest=true limits.memory="${3:-512MiB}" limits.cpu=2 \
        boot.autostart=false
    lxc profile device add "$name" root disk path=/ pool="$(lxc profile device get default root pool)" >/dev/null
    radios_take "$name" "$2"
}
# The regulatory domain is VM-wide and the RDK lab depends on its own: a guest that changes
# it is stopped at once (reference-lab guard).
# (every reader here consumes all of its input: with pipefail, one that stops early
# kills the writer with SIGPIPE and set -e ends the script)
regdom() { iw reg get 2>/dev/null | awk '/^global/{g=1} g && /^country/ && !n {print; n=1}'; }
start_guarded() {
    local name=$1 before
    before=$(regdom)
    if ! running "$name"; then
        if lxc profile show "$name" >/dev/null 2>&1; then
            radios_reclaim "$name"
            radios_replace_lost "$name"
        fi
        # a radio moving into a container can fail the first start: once more, then stop
        lxc start "$name" 2>/dev/null || { sleep 3; lxc start "$name"; } || die "$name did not start"
    fi
    sleep "${EMOSA_REGDOM_SETTLE:-20}"
    if [ "$(regdom)" != "$before" ]; then
        lxc stop -f "$name"
        die "$name changed the VM's regulatory domain ($before -> $(regdom)); stopped"
    fi
}

lanport() {
    # The RDK lab owns its wired LAN port (a VM bridge as eth2 of the gateway's brlan0) since
    # its wired extender needs it too; this script's own port is for an older lab checkout.
    if grep -q '^    lanport)' "$LABREPO/gen/wired-extender.sh" 2>/dev/null; then
        "$LABREPO/gen/wired-extender.sh" lanport
        return
    fi
    LAN=br-emosa
    lxc network show "$LAN" >/dev/null 2>&1 ||
        lxc network create "$LAN" ipv4.address=none ipv6.address=none >/dev/null
    # A bridge without a fixed address takes its lowest port MAC: LXD's 00:16:3e:... would
    # become brlan0's MAC (the gateway's LAN address). A locally administered MAC above the
    # lab's 02:... radios leaves brlan0 on its own address.
    has_device "$CTL" emosa-lan ||
        lxc config device add "$CTL" emosa-lan nic nictype=bridged parent="$LAN" name=eth2 \
            hwaddr="${EMOSA_LANPORT_MAC:-0a:e0:5a:00:00:01}" >/dev/null
    # brlan0 membership is not persistent: the gateway rebuilds brlan0 from its own config when
    # it restarts (RDK self-heal reboots it, e.g. CPU_THRESHOLD under load). A timer puts the
    # port back within 15 s.
    cat > /usr/local/sbin/emosa-lab-lanport <<EOF
#!/bin/sh
# re-attach $CTL eth2 to brlan0 after the gateway rebuilt it (emosa-lab deploy/rdk-lab)
lxc info $CTL 2>/dev/null | grep -q '^Status: RUNNING' || exit 0
lxc exec $CTL -- sh -c 'brctl show brlan0 2>/dev/null | grep -qw eth2 && exit 0
    ip link show brlan0 >/dev/null 2>&1 || exit 0
    ip link set eth2 up && brctl addif brlan0 eth2 && echo "eth2 re-attached to brlan0"'
EOF
    chmod 755 /usr/local/sbin/emosa-lab-lanport
    cat > /etc/systemd/system/emosa-lab-lanport.service <<EOF
[Unit]
Description=emosa-lab: keep $CTL eth2 (the EMOSA LAN port) in brlan0
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/emosa-lab-lanport
EOF
    cat > /etc/systemd/system/emosa-lab-lanport.timer <<EOF
[Unit]
Description=emosa-lab: keep $CTL eth2 in brlan0
[Timer]
OnBootSec=60
OnUnitActiveSec=15
AccuracySec=1
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now emosa-lab-lanport.timer >/dev/null 2>&1
    /usr/local/sbin/emosa-lab-lanport
    cx "$CTL" sh -c 'brctl show brlan0 | grep -w eth2 >/dev/null'
    log "lanport: $CTL eth2 in brlan0 (kept there by emosa-lab-lanport.timer), VM bridge $LAN"
}

emosa() {
    adapter_container isc-dhcp-client mosquitto mosquitto-clients
    has_device emosa emlan ||
        lxc config device add emosa emlan nic nictype=bridged parent="$(lan_bridge)" name=emlan >/dev/null
    # The pods' redirector: the VM owns br-wan101 (10.101.0.1); add .40 and forward the
    # front port and the agent ports into the container's loopback (agents listen there only).
    cat > /etc/systemd/system/emosa-lab-wan-address.service <<EOF
[Unit]
Description=EMOSA front address $WAN_HOST on $WAN_BRIDGE (the pods' redirector)
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'ip addr show dev $WAN_BRIDGE | grep -q " $WAN_HOST/" || ip addr add $WAN_HOST/24 dev $WAN_BRIDGE'

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable -q --now emosa-lab-wan-address
    proxy emosa front "$FLEET_PORT"
    proxy emosa agents "${AGENTS[0]}-${AGENTS[1]}"
    adapter_kit
    log "emosa: front $WAN_HOST:$FLEET_PORT"
}

telemetry() {    # telemetry [POD...]: the pods' own statistics (by default every running pod)
    local pods
    [ $# -gt 0 ] || { mapfile -t pods < <(pods_running); set -- "${pods[@]}"; }
    telemetry_on "$@"
}

agent() {    # agent POD python|c: the implementation of POD's agent
    local pod=${1:?usage: lab.sh agent POD python|c} bin
    bin=$(agent_binary "${2:-}") || die "usage: lab.sh agent POD python|c"
    exists emosa || die "run: lab.sh emosa"
    cx emosa test -f "/etc/emosa/$pod.json" || die "no agent for $pod (lab.sh status lists them)"
    implementation_one "$pod" "$bin"
    log "agent: $pod runs $bin"
}

implementation() {    # implementation python|c: every pod's agent
    local bin
    bin=$(agent_binary "${1:-}") || die "usage: lab.sh implementation python|c"
    implementation_all "$bin"
}

controller_al() {    # the RDK controller's AL MAC, from the lab's own topology API
    [ -n "${EMOSA_CONTROLLER_AL:-}" ] && { echo "$EMOSA_CONTROLLER_AL"; return; }
    # Not from a captured Topology Discovery: the extenders' discoveries cross brlan0 too.
    curl -fsS "${EMOSA_RDK_TOPOLOGY:-http://127.0.0.1:8888/api/v1/topology}" |
        jq -er '[.nodes[] | select(.name == "Controller") | .id] | if length == 1 then .[0] else error("no single controller") end'
}

fleet() {
    local al
    al=$(controller_al) || die "the lab's topology names no single controller"
    fleet_config "$FLEET_PORT" "${AGENTS[0]}" "${AGENTS[1]}" "$al" r1 true shared "$(pods_json)"
    log "fleet: front $WAN_HOST:$FLEET_PORT, agents ${AGENTS[0]}-${AGENTS[1]}, controller $al (r1, multi-BSS)"
}

gtp() {
    local channel=${EMOSA_PODBH_CHANNEL:-44}
    if ! exists em-gtp; then
        guest_profile em-gtp 1
        lxc init "$IMAGE" em-gtp --network lxdbr0 -p default -p em-gtp >/dev/null
        lxc config set em-gtp user.emosa.role gtp
        lxc config device add em-gtp eth1 nic nictype=bridged parent="$(lan_bridge)" name=eth1 >/dev/null
    fi
    start_guarded em-gtp
    wait_net em-gtp
    # each part only when missing, so a run that stopped half-way is completed by the next
    cx em-gtp sh -c 'command -v hostapd' >/dev/null 2>&1 ||
        cx em-gtp sh -ec 'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
            export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y iproute2 iw hostapd dnsmasq-base tcpdump >/dev/null'
    # The pod's onboarding SSID, where its 5 GHz backhaul station looks for it. 3-address,
    # into the underlay bridge podbh. No country_code: the regulatory domain is VM-wide.
    cx em-gtp sh -c "umask 077; cat > /etc/hostapd/hostapd.conf" <<EOF
interface=wlan0
bridge=podbh
driver=nl80211
ctrl_interface=/run/hostapd
ssid=$BHAUL_SSID
hw_mode=a
channel=$channel
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$BHAUL_KEY
EOF
    gtp_bridges
    gtp_hostapd
    gtp_termination
    log "em-gtp: '$BHAUL_SSID' on channel $channel, GRE into br-gtp, LAN leg on $(lan_bridge)"
}

pod() {
    local name=${1:-pod-1} meta rootfs fp
    meta=$(ls "$ART"/pod/*.metadata.tar.gz | head -1)
    rootfs=$(ls "$ART"/pod/*.rootfs.tar.gz | head -1)
    fp=$(sha256sum "$rootfs" | cut -c1-12)
    lxc image info "mvx-pod-$fp" >/dev/null 2>&1 ||
        lxc image import "$meta" "$rootfs" --alias "mvx-pod-$fp" >/dev/null
    if ! exists "$name"; then
        guest_profile "$name" 2
        lxc profile set "$name" security.privileged=true security.nesting=true
        # no wired network: the pod's only way out is its Wi-Fi backhaul
        lxc init "mvx-pod-$fp" "$name" -p "$name" >/dev/null
    fi
    lxc config set "$name" user.emosa.role=pod user.opensync-lab.image="$fp"
    pod_links "$name"
    start_guarded "$name"
    if [ -f /opt/emosa-lab/telemetry ]; then    # its lab device certificate, once OpenSync is up
        for _ in $(seq 60); do [ -n "$(pod_serial "$name")" ] && break; sleep 2; done
        provision "$name"
    fi
    log "pod: $name from mvx-pod-$fp (wlan0 2.4 GHz, wlan1 5 GHz backhaul station); run lab.sh medium once it is up"
}

pod_links() {    # the pod's fixed backhaul links on the medium (the lab's gen-config pins them)
    # Wired-equivalent: its station to the GTP, outside any room geometry. Every OpenSync start
    # comes up there. On Wi-Fi backhaul (EMOSA option 1) its station is also pinned to the
    # upstream backhaul BSS's radio, as strong as the lab's own gateway-extender backhaul.
    local links="bhaul-sta-50=em-gtp/wlan0:${EMOSA_BACKHAUL_SNR:-45}"
    [ "$(lxc config get "$1" user.emosa.backhaul)" = wifi ] &&
        links="$links bhaul-sta-50=$UPSTREAM:${EMOSA_WIFI_BACKHAUL_SNR:-50}"
    lxc profile set "$1" user.wmediumd.links="$links"
}

upstream_bssid() { cx "${UPSTREAM%%/*}" cat "/sys/class/net/${UPSTREAM#*/}/address"; }

pods_json() {    # the fleet's per-pod settings: a pod on Wi-Fi backhaul, pinned to the upstream BSS
    local pod serial first=1 bssid=
    printf '{'
    for pod in $(pods_running); do
        [ "$(lxc config get "$pod" user.emosa.backhaul)" = wifi ] || continue
        [ -n "$bssid" ] || bssid=$(upstream_bssid) || die "no upstream backhaul BSS $UPSTREAM"
        serial=$(pod_serial "$pod")
        [ -n "$serial" ] || die "$pod: no serial yet (OpenSync not up)"
        [ "$first" = 1 ] || printf ', '
        first=0
        printf '"%s": {"uplink": {"mode": "multi-ap", "bssid": "%s"}}' "$serial" "$bssid"
    done
    printf '}'
}

agent_reconfigure() {    # rewrite a bound pod's agent configuration from the fleet's, restart it
    # and release its uplink hold (EMOSA's option 2 after a failed switch): choosing the pod's
    # backhaul is the operator's decision a hold waits for. Prints "released" if it held.
    cx emosa systemctl stop "emosa-agent@$1"
    cx emosa /opt/emosa-adapter/venv/bin/python - "$1" <<'PY'
import json, sys
from pathlib import Path
from emosa.agent.fleet import agent_config
from emosa.config import validate
from emosa.store import Store
fleet = json.load(open("/etc/emosa-fleet.json"))
entry = json.load(open(Path(fleet["state_root"]) / "fleet.json"))[sys.argv[1]]
config = agent_config(entry, fleet)
validate("agent-config", config)
(Path(fleet["config_dir"]) / f"{sys.argv[1]}.json").write_text(json.dumps(config, indent=1) + "\n")
uplink = Path(config["state_dir"]) / "uplink"
if (uplink / "journal.db").exists():
    store = Store(uplink)
    if store.ownership(sys.argv[1]):
        store.release(sys.argv[1])
        print("released")
PY
    cx emosa systemctl start "emosa-agent@$1"
}

backhaul() {    # backhaul wired|wifi [POD...]: the pods' uplink
    local mode=${1:-} pods pod serial
    case $mode in wired|wifi) ;; *) die "usage: lab.sh backhaul wired|wifi [POD...]" ;; esac
    shift
    pods=${*:-$(pods_running)}
    [ -n "$pods" ] || die "no pod is running"
    for pod in $pods; do
        running "$pod" || die "$pod is not running"
        lxc config set "$pod" user.emosa.backhaul "$mode"
        pod_links "$pod"
    done
    fleet     # the per-pod uplink settings
    medium    # the links
    for pod in $pods; do
        serial=$(pod_serial "$pod")
        released=$(agent_reconfigure "$serial")
        # EMOSA never switches a pod back: an OpenSync restart returns it to its bootstrap (GTP)
        # path. A released hold switches on the pod's next start: one switch per start.
        if [ "$mode" = wired ] || [ -n "$released" ]; then
            [ -z "$released" ] || log "$pod: uplink hold released"
            cx "$pod" systemctl restart opensync
        fi
    done
    if [ "$mode" = wifi ]; then
        log "backhaul: $(echo $pods) on Wi-Fi to $UPSTREAM (EMOSA switches once the agent is provisioned)"
    else
        log "backhaul: $(echo $pods) on the GTP path"
    fi
}

rbus() {    # rbus GET|METHOD ARGS...: the controller's data model, CR LF stripped
    cx "$CTL" rbuscli "$@" 2>/dev/null | tr -d '\r'
}
agent_al() {    # the AL MAC of POD's EMOSA agent
    local serial
    serial=$(pod_serial "$1")
    [ -n "$serial" ] || die "$1: no serial (OpenSync not up)"
    cx emosa python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["agent_al"])' \
        "/var/lib/emosa/$serial/status.json" 2>/dev/null
}
move() {    # move POD TARGET: the controller steers POD's backhaul station to TARGET
    local pod=${1:-} target=${2:-} bssid al n i id
    [ -n "$pod" ] && [ -n "$target" ] || die "usage: lab.sh move POD CONTAINER|BSSID"
    running "$pod" || die "$pod is not running"
    [ "$(lxc config get "$pod" user.emosa.backhaul)" = wifi ] || die "$pod is not on Wi-Fi backhaul"
    case $target in
        *:*:*:*:*:*) bssid=$(echo "$target" | tr 'A-F' 'a-f') ;;
        *) bssid=$(cx "$target" cat /sys/class/net/wifi1.1/address 2>/dev/null) ||
            die "$target has no 5 GHz backhaul BSS (wifi1.1)"
            # A Wi-Fi extender starts its backhaul BSS only for a child (the RDK room forces it
            # up for its geometry rooms): up before the move, as the room does
            cx "$target" sh -c 'iw dev wifi1.1 info | grep -q ssid ||
                rbuscli setvalues Device.WiFi.AccessPoint.14.ForceApply boolean true >/dev/null
                for _ in $(seq 10); do iw dev wifi1.1 info | grep -q ssid && exit 0; sleep 1; done; exit 1' ||
                die "$target's backhaul BSS did not start" ;;
    esac
    al=$(agent_al "$pod") && [ -n "$al" ] || die "$pod: its agent has no status yet"
    n=$(rbus get Device.WiFi.DataElements.Network.DeviceNumberOfEntries | awk '/Value :/ {print $3}')
    for i in $(seq 1 "${n:-0}"); do
        id=$(rbus get "Device.WiFi.DataElements.Network.Device.$i.ID" | awk '/Value :/ {print tolower($3)}')
        [ "$id" = "$al" ] && break
        id=
    done
    [ -n "$id" ] || die "the controller has no device $al ($pod)"
    log "move: $pod (agent $al, Device.$i) to $bssid"
    rbus method_values "Device.WiFi.DataElements.Network.Device.$i.MultiAPDevice.Backhaul.SteerWiFiBackhaul()" \
        TargetBSS string "$bssid" Channel int32 36 TimeOut int32 30 | tail -3
    for _ in $(seq 24); do
        sleep 5
        [ "$(cx emosa python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["uplink"]["parent"])' \
            "/var/lib/emosa/$(pod_serial "$pod")/status.json" 2>/dev/null)" = "$bssid" ] &&
            { log "move: $pod on $bssid"; return; }
    done
    die "$pod did not move to $bssid within 2 minutes"
}
repod() {
    local name=${1:-pod-1}
    if exists "$name"; then
        lxc delete -f "$name"
        radios_await "$name"
    fi
    pod "$name"
}

client() {    # client NAME SSID KEY [BSSID]: pinned to BSSID when given (every RDK AP has the SSID)
    local name=$1 ssid=$2 key=$3 bssid=${4:-}
    if ! exists "$name"; then
        guest_profile "$name" 1
        lxc init "$IMAGE" "$name" --network lxdbr0 -p default -p "$name" >/dev/null
        lxc config set "$name" user.emosa.role client
    fi
    start_guarded "$name"
    if ! cx "$name" sh -c 'command -v wpa_supplicant && command -v dhclient' >/dev/null 2>&1; then
        wait_net "$name"
        cx "$name" sh -ec 'export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y wpasupplicant iw isc-dhcp-client iputils-ping wget >/dev/null'
    fi
    has_device "$name" eth0 || { lxc config device add "$name" eth0 none >/dev/null; lxc restart "$name"; }
    local conf
    conf=$(umask 077; mktemp)
    {
        printf 'ctrl_interface=/run/wpa_supplicant\nnetwork={\n ssid="%s"\n psk="%s"\n' "$ssid" "$key"
        [ -z "$bssid" ] || printf ' bssid=%s\n' "$bssid"
        printf '}\n'
    } > "$conf"
    lxc file push -q --mode 0600 "$conf" "$name/etc/wpa_supplicant/wlan0.conf"
    rm -f "$conf"
    cx "$name" sh -c 'pkill wpa_supplicant; sleep 1
        wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/wlan0.conf >/dev/null
        for i in $(seq 30); do iw dev wlan0 link | grep Connected >/dev/null && break; sleep 2; done
        dhclient -1 wlan0 >/dev/null 2>&1; iw dev wlan0 link | head -1; ip -br -4 addr show wlan0'
    log "client: $name on '$ssid'${bssid:+ at $bssid}"
}

medium() {
    # the lab's own generator (as root: it reads LXD), with guests (user.wmediumd.guest=true).
    # The room service drives wmediumd live: it stops for the restart and starts again.
    # A restart drops every Wi-Fi backhaul (the extenders lost their APs with it until OneWifi
    # and em_agent were restarted): only when the generated configuration differs.
    local room= cfg=/run/meta-cmf-wmediumd/wmediumd.cfg
    if [ -f "$cfg" ] && kill -0 "$(cat /run/meta-cmf-wmediumd/wmediumd.pid 2>/dev/null)" 2>/dev/null &&
            cmp -s "$cfg" <(cd "$LABREPO" && bash "$MEDIUM_WMEDIUMD/gen-config.sh" "${SNR:-40}" 2>/dev/null); then
        log "medium: already current"
        return
    fi
    systemctl is-active --quiet "$ROOM" && room=1 && systemctl stop "$ROOM"
    (cd "$LABREPO" && bash "$MEDIUM_WMEDIUMD/wmediumd-up.sh" up) | tail -2
    [ -z "$room" ] || systemctl start "$ROOM"
}

pods_running() { lxc list -c n -f csv | grep -E '^pod-[0-9]+$' || true; }

agents_provisioned() {    # the number of EMOSA agents whose session is provisioning
    cx emosa sh -c 'grep -l "\"state\": \"provisioning\"" /var/lib/emosa/*/status.json 2>/dev/null | wc -l'
}
wait_agents() {    # every running pod's agent provisioned (at most 5 minutes)
    local n
    n=$(pods_running | wc -l)
    for _ in $(seq 60); do [ "$(agents_provisioned)" -ge "$n" ] && return; sleep 5; done
    die "not every pod's agent is provisioned ($(agents_provisioned) of $n)"
}

forget_pods() {    # remove the EMOSA agents' rows from the controller's model (em_ctrl stopped)
    local als
    als=$(cx emosa sh -c '/opt/emosa-adapter/venv/bin/emosa-fleet list /etc/emosa-fleet.json 2>/dev/null' |
        jq -r '.[].al_mac' | tr '\n' ' ')
    [ -n "$als" ] || return 0
    cx bpibroadband sh -ec "systemctl stop em_ctrl
        for al in $als; do for t in PolicyList OperatingClassList BSSList RadioList DeviceList; do
            mysql -N -ubpi -proot OneWifiMesh -e \"delete from \$t where ID like '%@\$al@%'\" 2>/dev/null; done; done
        systemctl start em_ctrl"
    log "controller model: EMOSA agents $als removed"
}

room_manifest() {    # the room service's manifest: the standard rooms with the pods, or the lab's own
    local dropin=/etc/systemd/system/$ROOM.service.d/90-emosa-pods.conf
    if [ "$1" = pods ]; then
        mkdir -p "${dropin%/*}"
        printf '[Service]\nEnvironment=EASYMESH_ROOM_MANIFEST=%s/manifests/private-client-room-walk-%s.json\n' "$ROOMS" "$1" > "$dropin"
    else
        rm -f "$dropin"
    fi
    systemctl daemon-reload
}

rooms() {    # rooms pods|native: which rooms the lab's room service runs
    local pod n
    # The lab's wired extender (bpiap-004) is in all of its rooms: elsewhere it is an unbound
    # AP every client hears at the medium's default SNR. pods: the standard rooms with the
    # pods (worlds-pods); native: the standard rooms (worlds-wired, which the RDK lab's
    # gen/wired-extender.sh selects with its own room drop-in).
    if [ "${1:-}" = pods ]; then
        [ "$(lxc config get bpiap-004 user.easymesh.backhaul 2>/dev/null)" = wired ] && running bpiap-004 ||
            die "no wired extender bpiap-004 (meta-cmf gen/wired-extender.sh up 4)"
    elif [ "${1:-}" = native ] && exists bpiap-004 &&
            [ ! -e "/etc/systemd/system/$ROOM.service.d/80-wired-extender.conf" ]; then
        die "bpiap-004 is present but the room service has no rooms with it (meta-cmf gen/wired-extender.sh up 4)"
    fi
    case ${1:-} in
    pods)
        systemctl stop "$ROOM"
        for pod in $(lxc list -c n -f csv | grep -E '^pod-[0-9]+$'); do start_guarded "$pod"; done
        cx emosa systemctl start emosa-fleet
        n=$(pods_running | wc -l)
        wait_agents
        medium    # pins the pods' backhaul links now that their stations exist
        room_manifest "$1"
        if [ -x "$LABREPO/gen/lab-bringup.sh" ]; then
            # The pods' radios restarted the medium, which drops the extenders' Wi-Fi
            # backhauls: the lab's own bring-up then (OneWifi where a station is down, the
            # controller, every agent, the room settled), twice when an agent came back
            # not ready for candidate queries (rdk-emosa-1001, 1 Oct).
            "$LABREPO/gen/lab-bringup.sh" up || "$LABREPO/gen/lab-bringup.sh" up ||
                die "the lab did not settle after two bring-ups ($LABREPO/gen/lab-bringup.sh status)"
        else
            systemctl reset-failed "$ROOM"; systemctl start "$ROOM"
        fi
        log "rooms: $1 ($n pods); suite: EASYMESH_ROOM_WORLDS_ROOT=$MEDIUM_CONFIGURATOR/worlds-$1" ;;
    native)
        systemctl stop "$ROOM"
        cx emosa sh -c 'systemctl stop emosa-fleet "emosa-agent@*"'
        for pod in $(pods_running); do lxc stop "$pod"; done
        forget_pods
        room_manifest native
        systemctl reset-failed "$ROOM"; systemctl start "$ROOM"
        log "rooms: the lab's own$(exists bpiap-004 && echo ', with the wired extender') (pods stopped, their controller rows removed)" ;;
    *) die "usage: lab.sh rooms pods|native" ;;
    esac
}

uplinks_applied() {    # every running pod's agent on its Wi-Fi uplink (EMOSA's option 1 applied)
    cx emosa python3 -c '
import glob, json, sys
for path in glob.glob("/var/lib/emosa/*/status.json"):
    uplink = json.load(open(path)).get("uplink") or {}
    if uplink.get("uplink") != "multi-ap" or (uplink.get("operation") or {}).get("state") != "OBSERVED_APPLIED":
        sys.exit(1)'
}
wait_uplinks() {    # the pods' Wi-Fi uplinks applied (at most 3 minutes)
    for _ in $(seq 36); do uplinks_applied && return; sleep 5; done
    return 1
}

up() {    # up [python|c]: the EMOSA option of the RDK lab, every step in order (each completes a partial run)
    local pod impl=${1:-}
    [ -z "$impl" ] || agent_binary "$impl" >/dev/null || die "usage: lab.sh up [python|c]"
    [ "$(lxc config get bpiap-004 user.easymesh.backhaul 2>/dev/null)" = wired ] ||
        die "the option needs the lab's wired extender (meta-cmf EASYMESH_WIRED_EXTENDERS=1)"
    [ -f "$ART/adapter-kit.tar.gz" ] && ls "$ART"/pod/*.rootfs.tar.gz >/dev/null 2>&1 ||
        die "nothing staged in $ART: deploy/rdk-lab/lab.sh stage (host side)"
    lanport
    emosa
    [ -z "$impl" ] || implementation "$impl"    # before the fleet starts the agents
    fleet
    gtp
    for pod in pod-1 pod-2; do pod "$pod"; done
    medium
    telemetry
    wait_agents    # the pods bound to their agents over the GTP path, then onto Wi-Fi
    backhaul wifi
    rooms pods
    # A pod whose first switch was not confirmed in time is held on the GTP path (seen on
    # rdk-emosa-1001 after an in-place redeploy, 1 Oct, the medium restarting under it):
    # once more on the settled lab, rather than report an option that is not on Wi-Fi.
    if ! wait_uplinks; then
        log "a pod's Wi-Fi uplink did not apply (held on the GTP path): switching again"
        backhaul wifi
        rooms pods
        wait_uplinks || die "the pods' Wi-Fi uplinks did not apply (lab.sh status)"
    fi
    log "uplinks: every pod on its Wi-Fi backhaul"
    status
}

status() {
    lxc list -c ns4 -f csv | grep -E '^(emosa|em-gtp|pod-|emc-)' || true
    local pod
    for pod in $(pods_running); do
        echo "$pod backhaul: $(lxc config get "$pod" user.emosa.backhaul | sed 's/^$/wired/')"
    done
    exists emosa && cx emosa sh -c '/opt/emosa-adapter/venv/bin/emosa-fleet list /etc/emosa-fleet.json 2>/dev/null
        for f in /etc/default/emosa-*; do [ -f "$f" ] && echo "${f#/etc/default/emosa-}: $(cat "$f")"; done' || true
    echo "regulatory: $(regdom)"
}

case ${1:-} in
    lanport|emosa|fleet|gtp|medium|status|controller_al) "$1" ;;
    up) shift; up "$@" ;;
    implementation) shift; implementation "$@" ;;
    backhaul) shift; backhaul "$@" ;;
    move) shift; move "$@" ;;
    pod) shift; pod "$@" ;;
    agent) shift; agent "$@" ;;
    rooms) shift; rooms "$@" ;;
    telemetry) shift; telemetry "$@" ;;
    repod) shift; repod "$@" ;;
    client) shift; client "$@" ;;
    *) sed -n '2,47p' "$0"; exit 1 ;;
esac
