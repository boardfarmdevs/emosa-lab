#!/bin/bash
# Inside an RDK EasyMesh lab VM (meta-cmf-bananapi-vcpe, root): EMOSA and an
# unchanged OpenSync pod next to the RDK lab. Staged by ../lab.sh into /opt/emosa-lab.
# Design: doc/architecture/rdk-lab.md.
#
#   lab.sh lanport                    bridge br-emosa, a wired port of the controller's brlan0
#                                     (bpibroadband eth2): the EasyMesh LAN for EMOSA and the GTP
#   lab.sh emosa                      container emosa: the adapter kit; the pods' redirector
#                                     address 10.101.0.40 on br-wan101, forwarded to it
#   lab.sh fleet                      EMOSA fleet on the front port 10.101.0.40:6640 (the pod
#                                     image's redirector) and agents on 6651-6690
#   lab.sh agent POD python|c         which implementation runs POD's agent: the Python
#                                     reference or the C lab prototype (same config and status)
#   lab.sh gtp                        container em-gtp: the pods' onboarding SSID (the pod image's
#                                     backhaul credentials) and their GRE, LAN leg on br-emosa
#   lab.sh pod [NAME]                 the unchanged OpenSync pod image on two pool radios; its
#                                     backhaul to the GTP is a fixed link (EMOSA_BACKHAUL_SNR, 45)
#   lab.sh repod [NAME]               the pod again from the staged image (its radios are
#                                     returned to the VM first, so they survive)
#   lab.sh client NAME SSID KEY       a Wi-Fi client on one pool radio
#   lab.sh medium                     regenerate wmediumd's radios (guests: user.wmediumd.guest)
#   lab.sh status
set -euo pipefail
exec </dev/null
export PATH=/snap/bin:$PATH
ART=/opt/emosa-lab
IMAGE=${EMOSA_IMAGE:-ubuntu:24.04}
CTL=${EMOSA_RDK_CONTROLLER:-bpibroadband}
LAN=br-emosa
WAN_BRIDGE=${EMOSA_WAN_BRIDGE:-br-wan101}
WAN_HOST=${EMOSA_WAN_HOST:-10.101.0.40}    # the pod image's redirector (service provider mvx-local)
FLEET_PORT=${EMOSA_FLEET_PORT:-6640}
AGENTS=(6651 "${EMOSA_FLEET_LAST_PORT:-6690}")
BHAUL_SSID=${EMOSA_PODBH_SSID:-opensync-lab-bhaul}          # also from the pod image
BHAUL_KEY=${EMOSA_PODBH_KEY:-opensync-lab-bhaul-psk}
LABREPO=${EMOSA_RDK_LAB_REPO:-/home/easymesh/git/meta-cmf-bananapi-vcpe}
LOCK=/run/easymesh-hwsim-allocator.lock                     # the RDK lab's own allocator lock

log() { printf '\033[1;36m[emosa-rdk %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[1;31m[emosa-rdk %s] FATAL\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }
exists() { lxc info "$1" >/dev/null 2>&1; }
running() { [ "$(lxc list "^$1\$" -c s -f csv)" = RUNNING ]; }
has_device() { lxc config device list "$1" | grep -x "$2" >/dev/null; }
cx() { local c=$1; shift; lxc exec "$c" -- "$@"; }
wait_net() {
    for _ in $(seq 60); do cx "$1" getent hosts archive.ubuntu.com >/dev/null 2>&1 && return; sleep 2; done
    die "$1 has no network"
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
    if ! exists emosa; then
        lxc init "$IMAGE" emosa --network lxdbr0 >/dev/null
        lxc config set emosa user.emosa.role adapter
        lxc start emosa
        wait_net emosa
        cx emosa sh -ec 'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
            export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y iproute2 tcpdump isc-dhcp-client mosquitto mosquitto-clients >/dev/null'
    fi
    # the kit builds the C lab prototype when these are there
    cx emosa sh -ec 'dpkg -s cmake pkg-config gcc libcjson-dev libssl-dev >/dev/null 2>&1 || {
            export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y cmake pkg-config gcc libcjson-dev libssl-dev >/dev/null; }'
    running emosa || lxc start emosa
    has_device emosa emlan ||
        lxc config device add emosa emlan nic nictype=bridged parent="$LAN" name=emlan >/dev/null
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
    has_device emosa front ||
        lxc config device add emosa front proxy bind=host listen="tcp:$WAN_HOST:$FLEET_PORT" \
            connect="tcp:127.0.0.1:$FLEET_PORT" >/dev/null
    has_device emosa agents ||
        lxc config device add emosa agents proxy bind=host listen="tcp:$WAN_HOST:${AGENTS[0]}-${AGENTS[1]}" \
            connect="tcp:127.0.0.1:${AGENTS[0]}-${AGENTS[1]}" >/dev/null
    lxc file push -q "$ART/adapter-kit.tar.gz" emosa/root/adapter-kit.tar.gz
    cx emosa sh -ec 'rm -rf /root/adapter-kit && mkdir /root/adapter-kit
        tar -C /root/adapter-kit --strip-components=1 -xzf /root/adapter-kit.tar.gz
        /root/adapter-kit/install.sh'
    cx emosa sh -c 'sed -i "s/^EMOSA_TRUNK=.*/EMOSA_TRUNK=emlan/" /etc/default/emosa'
    log "emosa: $(cx emosa cat /root/adapter-kit/VERSION) in /opt/emosa-adapter; front $WAN_HOST:$FLEET_PORT"
}

agent() {
    local pod=${1:?usage: lab.sh agent POD python|c} bin
    case ${2:-} in
        python) bin=/opt/emosa-adapter/venv/bin/emosa-agent ;;
        c) bin=/opt/emosa-adapter/bin/emosa-agent-c ;;
        *) die "usage: lab.sh agent POD python|c" ;;
    esac
    exists emosa || die "run: lab.sh emosa"
    cx emosa test -f "/etc/emosa/$pod.json" || die "no agent for $pod (lab.sh status lists them)"
    cx emosa test -x "$bin" || die "$bin is not installed (lab.sh emosa)"
    cx emosa sh -c "echo EMOSA_AGENT=$bin > /etc/default/emosa-$pod"
    cx emosa systemctl restart "emosa-agent@$pod"
    log "agent: $pod runs $bin"
}

controller_al() {    # the RDK controller's AL MAC, from the lab's own topology API
    [ -n "${EMOSA_CONTROLLER_AL:-}" ] && { echo "$EMOSA_CONTROLLER_AL"; return; }
    # Not from a captured Topology Discovery: the extenders' discoveries cross brlan0 too.
    curl -fsS "${EMOSA_RDK_TOPOLOGY:-http://127.0.0.1:8888/api/v1/topology}" |
        jq -er '[.nodes[] | select(.name == "Controller") | .id] | if length == 1 then .[0] else error("no single controller") end'
}

fleet() {
    exists emosa || die "run: lab.sh emosa"
    local al
    al=$(controller_al) || die "the lab's topology names no single controller"
    cx emosa sh -c "cat > /etc/emosa-fleet.json" <<EOF
{
  "listen": "ptcp:$FLEET_PORT:127.0.0.1",
  "advertise": "$WAN_HOST",
  "ports": [${AGENTS[0]}, ${AGENTS[1]}],
  "controller_al": "$al",
  "message_set": "${EMOSA_MESSAGE_SET:-r1}",
  "multi_bss": ${EMOSA_MULTI_BSS:-true},
  "m2_session": "${EMOSA_M2_SESSION:-shared}",
  "profile": "${EMOSA_POD_PROFILE:-opensync-lab-hwsim-6.6.1-v1}",
  "state_root": "/var/lib/emosa",
  "config_dir": "/etc/emosa",
  "admit": "*"
}
EOF
    cx emosa systemctl enable -q emosa-fleet
    cx emosa systemctl restart emosa-fleet
    log "fleet: front $WAN_HOST:$FLEET_PORT, agents ${AGENTS[0]}-${AGENTS[1]}, controller $al (r1, multi-BSS)"
}

gtp() {
    local channel=${EMOSA_PODBH_CHANNEL:-44}
    if ! exists em-gtp; then
        guest_profile em-gtp 1
        lxc init "$IMAGE" em-gtp --network lxdbr0 -p default -p em-gtp >/dev/null
        lxc config set em-gtp user.emosa.role gtp
        lxc config device add em-gtp eth1 nic nictype=bridged parent="$LAN" name=eth1 >/dev/null
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
    cx em-gtp sh -c 'cat > /etc/systemd/system/emosa-lab-bridges.service' <<'EOF'
[Unit]
Description=Lab bridges for em-gtp: podbh (underlay) and br-gtp (LAN), before hostapd
Before=hostapd.service emosa-gtp.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for b in podbh br-gtp; do ip link show $b >/dev/null 2>&1 || ip link add $b type bridge; ip link set $b up; done'

[Install]
WantedBy=multi-user.target
EOF
    cx em-gtp sh -ec "systemctl daemon-reload; systemctl enable -q --now emosa-lab-bridges
        sed -i '/^DAEMON_CONF=/d' /etc/default/hostapd 2>/dev/null || true
        echo DAEMON_CONF=/etc/hostapd/hostapd.conf >> /etc/default/hostapd
        systemctl unmask hostapd >/dev/null 2>&1; systemctl enable -q hostapd; systemctl restart hostapd"
    lxc file push -q "$ART/adapter-kit.tar.gz" em-gtp/root/adapter-kit.tar.gz
    cx em-gtp sh -ec 'rm -rf /root/adapter-kit && mkdir /root/adapter-kit
        tar -C /root/adapter-kit --strip-components=1 -xzf /root/adapter-kit.tar.gz
        /root/adapter-kit/install.sh >/dev/null'
    cx em-gtp sh -c 'cat > /etc/emosa-gtp.json' <<'EOF'
{
  "underlay": {"interface": "podbh", "address": "169.254.2.1/25", "mtu": 1600,
               "dhcp_range": ["169.254.2.10", "169.254.2.126"], "lease_time": "1h"},
  "lan": {"bridge": "br-gtp", "ports": ["eth1"]},
  "tunnel_mtu": 1562,
  "state_dir": "/var/lib/emosa-gtp"
}
EOF
    cx em-gtp sh -c 'systemctl enable -q emosa-gtp; systemctl restart emosa-gtp'
    log "em-gtp: '$BHAUL_SSID' on channel $channel, GRE into br-gtp, LAN leg on $LAN"
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
    # wired-equivalent backhaul: the pod's station to the GTP is a fixed link on the medium,
    # outside any room geometry (the lab's gen-config pins it; lab.sh medium applies it)
    lxc profile set "$name" user.wmediumd.links="bhaul-sta-50=em-gtp/wlan0:${EMOSA_BACKHAUL_SNR:-45}"
    start_guarded "$name"
    log "pod: $name from mvx-pod-$fp (wlan0 2.4 GHz, wlan1 5 GHz backhaul station); run lab.sh medium once it is up"
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
    # The room demo drives wmediumd live: it stops for the restart and starts again.
    local room=
    systemctl is-active --quiet easymesh-room-demo && room=1 && systemctl stop easymesh-room-demo
    (cd "$LABREPO" && bash gen/wmediumd/wmediumd-up.sh up) | tail -2
    [ -z "$room" ] || systemctl start easymesh-room-demo
}

status() {
    lxc list -c ns4 -f csv | grep -E '^(emosa|em-gtp|pod-|emc-)' || true
    exists emosa && cx emosa sh -c '/opt/emosa-adapter/venv/bin/emosa-fleet list /etc/emosa-fleet.json 2>/dev/null
        for f in /etc/default/emosa-*; do [ -f "$f" ] && echo "${f#/etc/default/emosa-}: $(cat "$f")"; done' || true
    echo "regulatory: $(regdom)"
}

case ${1:-} in
    lanport|emosa|fleet|gtp|medium|status|controller_al) "$1" ;;
    pod) shift; pod "$@" ;;
    agent) shift; agent "$@" ;;
    repod) shift; repod "$@" ;;
    client) shift; client "$@" ;;
    *) sed -n '2,22p' "$0"; exit 1 ;;
esac
