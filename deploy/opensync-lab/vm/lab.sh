#!/bin/bash
# Inside an opensync-lab VM (root): add the EasyMesh side next to the unchanged
# opensync-lab stack. Staged by ../lab.sh into /opt/emosa-lab.
#
#   lab.sh bridge                     LXD network em-1905 (no IP): the EasyMesh LAN
#   lab.sh controller                 container em-ctl: prplMesh controller + colocated agent
#   lab.sh emosa                      container emosa: EMOSA source + Python 3.13 venv
#   lab.sh agent POD N                EMOSA virtual agent N for opensync-lab pod POD:
#                                     macvlan emN (MAC = AL) on em-1905, OVSDB 10.101.0.1:665N,
#                                     service, then local-noc hands the pod over
#   lab.sh release POD                hand POD back to local-noc, stop its agent
#   lab.sh policy SSID KEY            controller fronthaul policy for the gateway + all agents
#   lab.sh client NAME POD SSID KEY   opensync-lab wireless client on POD's fronthaul
#   lab.sh topology                   the controller's DataElements (JSON)
#   lab.sh ui                         controller UI (prplmesh-lab) on the VM, port 8093 (8091 is boardfarm's)
#   lab.sh status
set -euo pipefail
exec </dev/null
export PATH=/snap/bin:$PATH
HERE=$(cd "$(dirname "$0")/.." && pwd)          # /opt/emosa-lab/deploy/opensync-lab
ART=/opt/emosa-lab/artifacts
OSL=${MVX_GUEST_ROOT:-/opt/opensync-lab}
NET=em-1905
WAN_HOST=${EMOSA_WAN_HOST:-10.101.0.1}             # the VM on boardfarm's WAN (br-wan101)
CTL_AL=02:00:00:e0:00:01
IMAGE=${EMOSA_IMAGE:-ubuntu:24.04}

log() { printf '\033[1;36m[emosa %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[1;31m[emosa %s] FATAL\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }
exists() { lxc info "$1" >/dev/null 2>&1; }
cx() { local c=$1; shift; lxc exec "$c" -- "$@"; }
al_of() { printf '02:00:00:5e:00:%02x' "$1"; }    # virtual agent N's AL (= its em-1905 MAC)
port_of() { echo $((6650 + $1)); }

bridge() {
    lxc network show $NET >/dev/null 2>&1 && return
    lxc network create $NET ipv4.address=none ipv6.address=none >/dev/null
    log "network $NET (EasyMesh LAN, no IP)"
}

wait_net() {    # wait_net CT: until the container can resolve and reach the archive
    for _ in $(seq 60); do cx "$1" getent hosts archive.ubuntu.com >/dev/null 2>&1 && return; sleep 2; done
    die "$1 has no network"
}

controller() {
    bridge
    if ! exists em-ctl; then
        lxc init "$IMAGE" em-ctl --network lxdbr0 >/dev/null
        lxc start em-ctl
        wait_net em-ctl
        log "em-ctl: packages"
        cx em-ctl sh -ec 'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
            export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y iproute2 iputils-ping iw tcpdump ebtables ethtool libcap-ng0 \
                libevent-2.1-7 libjson-c5 libnl-3-200 libnl-genl-3-200 libnl-route-3-200 libssl3 \
                liburiparser1 libyajl2 psmisc >/dev/null'
        log "em-ctl: prplMesh 6.0.0 (prplmesh-lab artifacts) + EMOSA controller candidate"
        cx em-ctl mkdir -p /opt/emosa-baseline/hostap
        for f in prpl-install-nl80211-6.0.0.tar.gz prpl-runtime-deps-6.0.0.tar.gz emosa-prpl-bwl-6.0.0.tar.gz \
                 emosa-hostap-wps-2.10.tar.gz; do
            lxc file push -q "$ART/$f" em-ctl/opt/
        done
        cx em-ctl sh -ec 'tar -C /opt -xzf /opt/prpl-install-nl80211-6.0.0.tar.gz
            tar -C / -xzf /opt/prpl-runtime-deps-6.0.0.tar.gz
            tar -C /opt/prpl-install-nl80211 -xzf /opt/emosa-prpl-bwl-6.0.0.tar.gz
            tar -C /opt/emosa-baseline/hostap -xzf /opt/emosa-hostap-wps-2.10.tar.gz'
        lxc file push -q "$ART/candidate-ap-esp-02/stage/bin/beerocks_controller" em-ctl/opt/prpl-install-nl80211/bin/
        lxc file push -q "$ART/candidate-ap-esp-02/stage/lib/libbpl.so.6.0.0" em-ctl/opt/prpl-install-nl80211/lib/
        cx em-ctl sh -c 'chmod 755 /opt/prpl-install-nl80211/bin/beerocks_controller; ldconfig'
        # one hwsim radio from opensync-lab's pool, the EasyMesh LAN, and no setup NIC
        # shellcheck source=/dev/null
        radio=$(source "$OSL/guest/common.sh"; hwsim_free | head -1)
        [ -n "$radio" ] || die "no free hwsim radio"
        lxc stop em-ctl
        lxc config device add em-ctl wlan0 nic nictype=physical parent="$radio" name=wlan0 >/dev/null
        lxc config device add em-ctl eth1 nic network=$NET name=eth1 >/dev/null
        lxc config device remove em-ctl eth0 >/dev/null
        lxc config device add em-ctl eth0 none >/dev/null     # mask the profile's eth0 too
        lxc config set em-ctl user.emosa.role controller
        lxc start em-ctl
        log "em-ctl: radio $radio as wlan0, eth1 on $NET"
    fi
    lxc file push -q "$HERE/files/em-ctl-node.sh" em-ctl/usr/local/sbin/em-ctl-node
    cx em-ctl chmod 755 /usr/local/sbin/em-ctl-node
    cx em-ctl em-ctl-node stop >/dev/null 2>&1 || true
    cx em-ctl em-ctl-node prepare
    cx em-ctl em-ctl-node start
    for _ in $(seq 60); do
        cx em-ctl em-ctl-node topology 2 >/dev/null 2>&1 && break; sleep 1
    done
    cx em-ctl em-ctl-node topology 2 >/dev/null 2>&1 || die "controller data model not available"
    log "em-ctl: controller running (AL $CTL_AL)"
}

emosa() {
    bridge
    if ! exists emosa; then
        lxc init "$IMAGE" emosa --network lxdbr0 >/dev/null
        lxc config set emosa user.emosa.role adapter
        lxc start emosa
        wait_net emosa
        cx emosa sh -ec 'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
            export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y build-essential iproute2 tcpdump >/dev/null'
    fi
    log "emosa: source + venv (uv, CPython 3.13 from .python-version, uv.lock)"
    lxc file push -q "$ART/uv" emosa/usr/local/bin/uv
    cx emosa chmod 755 /usr/local/bin/uv
    cx emosa rm -rf /opt/emosa.new
    cx emosa mkdir -p /opt/emosa.new
    tar -C /opt/emosa-lab/source -cf - . | lxc exec emosa -- tar -C /opt/emosa.new -xf -
    cx emosa sh -ec 'rm -rf /opt/emosa.old; [ -d /opt/emosa ] && mv /opt/emosa /opt/emosa.old
        mv /opt/emosa.new /opt/emosa
        [ -d /opt/emosa.old/.venv ] && mv /opt/emosa.old/.venv /opt/emosa/.venv; rm -rf /opt/emosa.old
        cd /opt/emosa && UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --frozen --no-dev -q'
    # one trunk NIC on the EasyMesh LAN; each virtual agent adds its own macvlan
    # (emN, MAC = its AL). LXD allows one NIC per managed network per instance.
    lxc config device show emosa | grep -q '^emlan:' ||
        lxc config device add emosa emlan nic network=$NET name=emlan >/dev/null
    lxc file push -q "$HERE/files/emosa-agent-link" emosa/usr/local/sbin/
    cx emosa chmod 755 /usr/local/sbin/emosa-agent-link
    lxc file push -q "$HERE/files/emosa-agent@.service" emosa/etc/systemd/system/
    cx emosa systemctl daemon-reload
    log "emosa: $(cx emosa /opt/emosa/.venv/bin/python -c 'import sys,emosa;print(sys.version.split()[0], emosa.__file__)')"
}

pod_id() { cx "$1" /usr/opensync/tools/ovsh -r s AWLAN_Node id 2>/dev/null | tr -d '[:space:]'; }
pod_serial() { cx "$1" /usr/opensync/tools/ovsh -r s AWLAN_Node serial_number 2>/dev/null | tr -d '[:space:]'; }

agent() {       # agent POD N
    local pod=$1 n=$2 al port serial id
    exists "$pod" || die "no container $pod"
    exists emosa || die "run: lab.sh emosa"
    al=$(al_of "$n") port=$(port_of "$n")
    serial=$(pod_serial "$pod") id=$(pod_id "$pod")
    [ -n "$serial" ] || die "$pod: no AWLAN_Node serial"
    lxc config device show emosa | grep -q "^ovsdb$n:" ||
        lxc config device add emosa "ovsdb$n" proxy bind=host listen="tcp:$WAN_HOST:$port" \
            connect="tcp:127.0.0.1:$port" >/dev/null
    cx emosa mkdir -p /etc/emosa
    cx emosa sh -c "cat > /etc/emosa/$pod.json" <<EOF
{
  "pod_id": "$pod",
  "serial": "$serial",
  "ovsdb": "ptcp:$port:127.0.0.1",
  "interface": "em$n",
  "al_mac": "$al",
  "controller_al": "$CTL_AL",
  "vif": "home-ap-24",
  "state_dir": "/var/lib/emosa/$pod",
  "run_id": "$pod"
}
EOF
    cx emosa systemctl enable --now "emosa-agent@$pod" >/dev/null 2>&1
    cx emosa systemctl restart "emosa-agent@$pod"
    log "agent $n: $pod ($serial) as AL $al on em$n, OVSDB tcp:$WAN_HOST:$port -> emosa 127.0.0.1:$port"
    docker exec local-noc noc-ctl redirect "$id" "tcp:$WAN_HOST:$port" >/dev/null
    log "local-noc: $id handed to tcp:$WAN_HOST:$port (its fronthaul is now EMOSA's; GRE on mv3 kept)"
}

release() {     # release POD
    local pod=$1 id
    id=$(pod_id "$pod")
    docker exec local-noc noc-ctl redirect "$id" - >/dev/null
    cx emosa systemctl disable --now "emosa-agent@$pod" >/dev/null 2>&1 || true
    log "$pod ($id) given back to local-noc"
}

policy() {      # policy SSID KEY
    local als=() f
    for f in $(cx emosa sh -c 'ls /etc/emosa/*.json 2>/dev/null' || true); do
        als+=("$(cx emosa python3 -c "import json;print(json.load(open('$f'))['al_mac'])")")
    done
    cx em-ctl em-ctl-node policy "$1" "$2" "${als[@]}"
}

client() {      # client NAME POD SSID KEY
    MVX_CLIENT_SSID=$3 MVX_CLIENT_PSK=$4 MVX_GUEST_ROOT=$OSL bash "$OSL/guest/80-client.sh" "$1" "$2"
}

topology() { cx em-ctl em-ctl-node topology "${1:-6}"; }

ui() {          # the controller's own topology: prplmesh-lab topology adapter + controller UI
    local port=${EMOSA_UI_PORT:-8093}
    lxc file push -q "$ART/controller-ui/topology-adapter.py" em-ctl/usr/local/sbin/em-topology-adapter
    cx em-ctl sh -c 'systemctl stop em-topology 2>/dev/null; systemctl reset-failed em-topology 2>/dev/null
        systemd-run --quiet --unit em-topology python3 /usr/local/sbin/em-topology-adapter --listen 127.0.0.1 --port 8092'
    lxc config device show em-ctl | grep -q '^nbapi:' ||
        lxc config device add em-ctl nbapi proxy bind=host listen=tcp:127.0.0.1:8092 connect=tcp:127.0.0.1:8092 >/dev/null
    install -d /opt/emosa-lab/controller-ui
    cp -a "$ART/controller-ui/easymesh-controller" "$ART/controller-ui/config" /opt/emosa-lab/controller-ui/
    printf '%s\n' '[Unit]' 'Description=EasyMesh controller UI (prplmesh-lab) on the controller NBAPI' \
        'After=network-online.target' '' '[Service]' 'WorkingDirectory=/opt/emosa-lab/controller-ui' \
        "ExecStart=/opt/emosa-lab/controller-ui/easymesh-controller -listen 0.0.0.0:$port -source http://127.0.0.1:8092/api/topology" \
        'Restart=always' '' '[Install]' 'WantedBy=multi-user.target' > /etc/systemd/system/emosa-controller-ui.service
    systemctl daemon-reload
    systemctl enable -q emosa-controller-ui
    systemctl restart emosa-controller-ui
    for _ in $(seq 20); do curl -fs -o /dev/null "http://127.0.0.1:$port/" && break; sleep 1; done
    curl -fs "http://127.0.0.1:8092/api/topology" >/dev/null || die "topology adapter not answering"
    log "controller UI on the VM at :$port (adapter em-ctl:8092 via proxy)"
}

status() {
    lxc list -f csv -c ns em-ctl emosa 2>/dev/null || true
    cx em-ctl systemctl list-units --no-legend --plain 'em-*' 2>/dev/null | awk '{print "em-ctl", $1, $3, $4}' || true
    cx emosa sh -c 'for f in /var/lib/emosa/*/status.json; do [ -f "$f" ] && python3 -c "
import json,sys; s=json.load(open(sys.argv[1])); p=s.get(\"pod\") or {}
print(s[\"pod_id\"], \"agent\", s[\"agent_al\"], \"session\", (s.get(\"session\") or {}).get(\"state\"),
      \"ssid\", p.get(\"ssid\"), \"stations\", len(p.get(\"stations\") or []),
      \"ops\", [(o[\"state\"], o[\"ssid\"]) for o in s[\"operations\"]])" "$f"; done' 2>/dev/null || true
    docker exec local-noc noc-ctl redirects 2>/dev/null || true
}

cmd=${1:-}; shift || true
case $cmd in
    bridge|controller|emosa|agent|release|policy|client|topology|ui|status) "$cmd" "$@" ;;
    *) sed -n '2,17p' "$0"; exit 2 ;;
esac
