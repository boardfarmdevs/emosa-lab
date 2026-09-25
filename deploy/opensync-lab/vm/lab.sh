#!/bin/bash
# Inside an opensync-lab VM (root): add the EasyMesh side next to the unchanged
# opensync-lab stack. Staged by ../lab.sh into /opt/emosa-lab.
#
#   lab.sh bridge                     LXD network em-1905 (no IP): the EasyMesh LAN
#   lab.sh controller                 container em-ctl: prplMesh controller + colocated agent
#   lab.sh emosa                      container emosa: the adapter kit (deploy/adapter) installed
#   lab.sh agent POD N                EMOSA virtual agent N for opensync-lab pod POD:
#                                     macvlan emN (MAC = AL) on em-1905, OVSDB 10.101.0.1:665N,
#                                     service, then local-noc hands the pod over
#   lab.sh fleet                      EMOSA fleet: one front port (10.101.0.1:6650); every pod
#                                     handed to it gets its own virtual agent, started on demand
#   lab.sh admit POD...               local-noc hands each POD to the fleet front port; the
#                                     saved controller policy is re-applied for the new agents
#   lab.sh release POD                hand POD back to local-noc, stop its agent
#   lab.sh policy SSID KEY            controller fronthaul policy for the gateway + all agents
#   lab.sh client NAME POD SSID KEY   opensync-lab wireless client on POD's fronthaul
#   lab.sh topology                   the controller's DataElements (JSON)
#   lab.sh ui                         controller UI (prplmesh-lab) on the VM, port 8093 (8091 is boardfarm's)
#   lab.sh telemetry [POD...]         MQTT broker (mutual TLS) for the pods' own statistics; every
#                                     agent then has its pod publish them (a lab device
#                                     certificate for each POD, and for pods admitted later)
#   lab.sh provision POD              lab device certificate into POD:/var/certs
#   lab.sh gtp                        container em-gtp: pod-backhaul SSID + GRE termination point
#                                     (data plane option 2), its LAN leg on mv3's LAN (lan-p4)
#   lab.sh uplink POD MODE            move POD's uplink by hand: gtp | multi-ap | restore | show
#   lab.sh option1 POD on|off [config|m2]
#                                     POD's EMOSA agent switches its uplink to the controller's
#                                     backhaul BSS itself (em-gtp, 5 GHz), and keeps it switched;
#                                     credentials from its config, or from the controller's M2 set
#   lab.sh status
#   lab.sh workload LABEL             900 s recovery workload under faults (vm/workload.py)
set -euo pipefail
exec </dev/null
export PATH=/snap/bin:$PATH
HERE=$(cd "$(dirname "$0")/.." && pwd)          # /opt/emosa-lab/deploy/opensync-lab
ART=/opt/emosa-lab/artifacts
OSL=${MVX_GUEST_ROOT:-/opt/opensync-lab}
NET=em-1905
WAN_HOST=${EMOSA_WAN_HOST:-10.101.0.1}             # the VM on boardfarm's WAN (br-wan101)
CTL_AL=${EMOSA_CONTROLLER_AL:-02:00:00:e0:00:01}    # the EasyMesh controller agents bind to
IMAGE=${EMOSA_IMAGE:-ubuntu:24.04}
FLEET_PORT=6650                                     # fleet front port; agents on the ports after it
FLEET_LAST=${EMOSA_FLEET_LAST_PORT:-6690}

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
    lxc info emosa | grep -q '^Status: RUNNING' || lxc start emosa    # e.g. after a VM reboot
    # one trunk NIC on the EasyMesh LAN; each virtual agent adds its own macvlan
    # (emN, MAC = its AL). LXD allows one NIC per managed network per instance.
    lxc config device show emosa | grep -q '^emlan:' ||
        lxc config device add emosa emlan nic network=$NET name=emlan >/dev/null
    log "emosa: installing the adapter kit (deploy/adapter), trunk emlan"
    lxc file push -q /opt/emosa-lab/adapter-kit.tar.gz emosa/root/adapter-kit.tar.gz
    cx emosa sh -ec 'rm -rf /root/adapter-kit && mkdir /root/adapter-kit
        tar -C /root/adapter-kit --strip-components=1 -xzf /root/adapter-kit.tar.gz
        /root/adapter-kit/install.sh'
    cx emosa sh -c 'grep -q "^EMOSA_TRUNK=emlan$" /etc/default/emosa || sed -i "s/^EMOSA_TRUNK=.*/EMOSA_TRUNK=emlan/" /etc/default/emosa'
    log "emosa: $(cx emosa cat /root/adapter-kit/VERSION) in /opt/emosa-adapter"
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
  "message_set": "${EMOSA_MESSAGE_SET:-easymesh-6.1}",
  "multi_bss": ${EMOSA_MULTI_BSS:-false},
  "m2_session": "${EMOSA_M2_SESSION:-distinct}",
  "profile": "${EMOSA_POD_PROFILE:-opensync-lab-hwsim-6.6.1-v1}",
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

fleet() {
    exists emosa || die "run: lab.sh emosa"
    cx emosa sh -c 'ls /etc/emosa/pod-*.json' >/dev/null 2>&1 &&
        die "per-pod agents configured (/etc/emosa/pod-*.json): lab.sh release each POD first"
    local d
    for d in $(lxc config device list emosa | grep '^ovsdb[0-9]*$' || true); do
        lxc config device remove emosa "$d" >/dev/null    # superseded by the agents range
    done
    lxc config device show emosa | grep -q '^fleet:' ||
        lxc config device add emosa fleet proxy bind=host listen="tcp:$WAN_HOST:$FLEET_PORT" \
            connect="tcp:127.0.0.1:$FLEET_PORT" >/dev/null
    lxc config device show emosa | grep -q '^agents:' ||
        lxc config device add emosa agents proxy bind=host \
            listen="tcp:$WAN_HOST:$((FLEET_PORT + 1))-$FLEET_LAST" \
            connect="tcp:127.0.0.1:$((FLEET_PORT + 1))-$FLEET_LAST" >/dev/null
    cx emosa sh -c "cat > /etc/emosa-fleet.json" <<EOF
{
  "listen": "ptcp:$FLEET_PORT:127.0.0.1",
  "advertise": "$WAN_HOST",
  "ports": [$((FLEET_PORT + 1)), $FLEET_LAST],
  "controller_al": "$CTL_AL",
  "message_set": "${EMOSA_MESSAGE_SET:-easymesh-6.1}",
  "multi_bss": ${EMOSA_MULTI_BSS:-false},
  "m2_session": "${EMOSA_M2_SESSION:-distinct}",
  "profile": "${EMOSA_POD_PROFILE:-opensync-lab-hwsim-6.6.1-v1}",
  "state_root": "/var/lib/emosa",
  "config_dir": "/etc/emosa",
  "admit": "*",
  "telemetry": $(telemetry_json)
}
EOF
    cx emosa systemctl enable -q emosa-fleet
    cx emosa systemctl restart emosa-fleet
    log "fleet: pods handed to tcp:$WAN_HOST:$FLEET_PORT get agents on ports $((FLEET_PORT + 1))-$FLEET_LAST"
}

fleet_agents() { cx emosa /opt/emosa-adapter/venv/bin/emosa-fleet list /etc/emosa-fleet.json; }

fleet_al() {    # fleet_al SERIAL: its agent's AL MAC, empty until the fleet registered it
    fleet_agents | python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], {}).get("al_mac", ""))' "$1"
}

admit() {       # admit POD...
    local pod id serial al
    for pod in "$@"; do
        id=$(pod_id "$pod") serial=$(pod_serial "$pod")
        [ -n "$id" ] && [ -n "$serial" ] || die "$pod: no AWLAN_Node id/serial"
        [ ! -f /opt/emosa-lab/telemetry ] || provision "$pod"
        docker exec local-noc noc-ctl redirect "$id" "tcp:$WAN_HOST:$FLEET_PORT" >/dev/null
        log "local-noc: $pod ($id) handed to the fleet front port"
        al=
        for _ in $(seq 60); do
            al=$(fleet_al "$serial")
            [ -n "$al" ] && break
            sleep 1
        done
        [ -n "$al" ] || die "$pod: the fleet did not register $serial"
        log "fleet: $pod ($serial) is virtual agent $al"
    done
    if [ -f /opt/emosa-lab/policy ]; then
        # shellcheck disable=SC2046    # the saved SSID and KEY, split on purpose
        policy $(cat /opt/emosa-lab/policy)
    else
        log "no controller policy saved yet: run lab.sh policy SSID KEY"
    fi
}

release() {     # release POD
    local pod=$1 id serial
    id=$(pod_id "$pod") serial=$(pod_serial "$pod")
    docker exec local-noc noc-ctl redirect "$id" - >/dev/null
    if cx emosa test -f "/etc/emosa/$pod.json"; then
        cx emosa systemctl disable --now "emosa-agent@$pod" >/dev/null 2>&1 || true
        cx emosa rm -f "/etc/emosa/$pod.json"
    elif [ -n "$serial" ] && cx emosa test -f "/etc/emosa/$serial.json"; then
        cx emosa /opt/emosa-adapter/venv/bin/emosa-fleet forget /etc/emosa-fleet.json "$serial" >/dev/null
    fi
    log "$pod ($id) given back to local-noc"
}

policy() {      # policy SSID KEY
    local als=() f
    for f in $(cx emosa sh -c 'ls /etc/emosa/*.json 2>/dev/null' || true); do
        # an agent that takes its uplink credentials from M2 also gets the backhaul BSS
        als+=("$(cx emosa python3 -c "import json; c = json.load(open('$f'))
print(c['al_mac'] + ('+bh' if c.get('uplink', {}).get('credentials') == 'm2' and c.get('uplink', {}).get('mode') == 'multi-ap' else ''))")")
    done
    ( umask 077; printf '%s %s\n' "$1" "$2" > /opt/emosa-lab/policy )    # re-applied by admit
    cx em-ctl em-ctl-node policy "$1" "$2" "${als[@]}"
}

client() {      # client NAME POD SSID KEY
    MVX_CLIENT_SSID=$3 MVX_CLIENT_PSK=$4 MVX_GUEST_ROOT=$OSL bash "$OSL/guest/80-client.sh" "$1" "$2"
}

topology() { cx em-ctl em-ctl-node topology "${1:-6}"; }

telemetry() {   # MQTT broker for the pods' own statistics (OpenSync sm/qm: mutual TLS only)
    cx emosa sh -ec 'command -v mosquitto >/dev/null || { export DEBIAN_FRONTEND=noninteractive
            apt-get -qq update; apt-get -qq install -y mosquitto mosquitto-clients openssl >/dev/null; }
        P=/var/lib/emosa/pki; install -d -m 700 $P; cd $P
        [ -f ca.pem ] || { openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=EMOSA lab CA" \
                -keyout ca.key -out ca.pem 2>/dev/null; }
        [ -f broker.pem ] || { openssl req -newkey rsa:2048 -nodes -subj "/CN=10.101.0.1" -keyout broker.key \
                -out broker.csr 2>/dev/null
            printf "subjectAltName=IP:10.101.0.1,IP:127.0.0.1\n" > broker.ext
            openssl x509 -req -in broker.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 3650 \
                -extfile broker.ext -out broker.pem 2>/dev/null; }
        # the CA key stays with root; the broker gets its own copy (/var/lib/emosa is 0700)
        C=/etc/mosquitto/certs; install -d -o mosquitto -m 750 $C
        install -o mosquitto -m 644 ca.pem broker.pem $C/; install -o mosquitto -m 600 broker.key $C/
        printf "%s\n" "per_listener_settings true" "listener 8883 127.0.0.1" "cafile $C/ca.pem" \
            "certfile $C/broker.pem" "keyfile $C/broker.key" "require_certificate true" \
            "use_identity_as_username true" "listener 1883 127.0.0.1" "allow_anonymous true" \
            > /etc/mosquitto/conf.d/emosa.conf
        systemctl restart mosquitto'
    lxc config device show emosa | grep -q '^mqtt:' ||
        lxc config device add emosa mqtt proxy bind=host listen="tcp:$WAN_HOST:8883" \
            connect=tcp:127.0.0.1:8883 >/dev/null
    log "telemetry: broker tcp:$WAN_HOST:8883 (mutual TLS, lab CA) -> emosa; local subscriber 127.0.0.1:1883"
    local pod
    for pod in "$@"; do provision "$pod"; done
    touch /opt/emosa-lab/telemetry    # lab.sh fleet and admit keep it on
    # The fleet writes an agent's configuration when its pod is handed over:
    # the running agents get the setting directly.
    cx emosa python3 - "$(telemetry_json)" <<'EOF'
import glob, json, sys
telemetry = json.loads(sys.argv[1])
for path in ["/etc/emosa-fleet.json", *glob.glob("/etc/emosa/*.json")]:
    with open(path) as f:
        config = json.load(f)
    config["telemetry"] = telemetry
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
EOF
    cx emosa sh -c 'systemctl restart emosa-fleet
        for u in $(systemctl list-units --plain --no-legend "emosa-agent@*" | cut -d" " -f1); do
            systemctl restart "$u"
        done'
    log "telemetry: every agent has its pod publish client reports to emosa/stats/<serial>"
}

telemetry_json() {    # the fleet's telemetry setting
    if [ -f /opt/emosa-lab/telemetry ]; then
        printf '{"mode": "mqtt", "broker": "%s", "port": 8883}' "$WAN_HOST"
    else
        printf '{"mode": "off"}'
    fi
}

provision() {   # lab device certificate for POD, where OpenSync expects it (/var/certs)
    local pod=$1 serial t
    serial=$(pod_serial "$pod")
    t=$(mktemp -d)
    cx emosa sh -ec "cd /var/lib/emosa/pki; [ -f $serial.pem ] || { openssl req -newkey rsa:2048 -nodes \
            -subj /CN=$serial -keyout $serial.key -out $serial.csr 2>/dev/null
        openssl x509 -req -in $serial.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 3650 \
            -out $serial.pem 2>/dev/null; }"
    lxc file pull -q "emosa/var/lib/emosa/pki/ca.pem" "$t/ca.pem"
    lxc file pull -q "emosa/var/lib/emosa/pki/$serial.pem" "$t/client.pem"
    lxc file pull -q "emosa/var/lib/emosa/pki/$serial.key" "$t/client_dec.key"
    for f in ca.pem client.pem client_dec.key; do lxc file push -q --mode 0600 "$t/$f" "$pod/var/certs/$f"; done
    rm -rf "$t"
    log "provision: $pod ($serial) lab device certificate in /var/certs (signed by the EMOSA lab CA)"
}

ui() {          # the controller's own topology: prplmesh-lab topology adapter + controller UI
    local port=${EMOSA_UI_PORT:-8093}
    lxc file push -q "$ART/controller-ui/topology-adapter.py" em-ctl/usr/local/sbin/em-topology-adapter
    cx em-ctl sh -c 'systemctl stop em-topology 2>/dev/null; systemctl reset-failed em-topology 2>/dev/null
        systemd-run --quiet --unit em-topology python3 /usr/local/sbin/em-topology-adapter --listen 127.0.0.1 --port 8092'
    lxc config device show em-ctl | grep -q '^nbapi:' ||
        lxc config device add em-ctl nbapi proxy bind=host listen=tcp:127.0.0.1:8092 connect=tcp:127.0.0.1:8092 >/dev/null
    install -d /opt/emosa-lab/controller-ui
    systemctl stop emosa-controller-ui 2>/dev/null || true    # a running binary is "Text file busy"
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

gtp() {         # GRE termination point (data plane option 2) with a pod-backhaul SSID
    exists emosa || die "run: lab.sh emosa (it stages the adapter kit)"
    local lan=${EMOSA_GTP_LAN:-lan-p4} radio
    # Each part only when missing, so a run that stopped half-way is completed by the next.
    if ! exists em-gtp; then
        lxc init "$IMAGE" em-gtp --network lxdbr0 >/dev/null
        lxc config set em-gtp user.emosa.role gtp
    fi
    if ! cx em-gtp sh -c 'command -v hostapd' >/dev/null 2>&1; then
        lxc start em-gtp 2>/dev/null || true
        wait_net em-gtp
        cx em-gtp sh -ec 'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
            export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
            apt-get -qq install -y build-essential iproute2 iw hostapd dnsmasq-base tcpdump >/dev/null'
    fi
    if ! lxc config device show em-gtp | grep -q '^wlan0:'; then
        # shellcheck source=/dev/null
        radio=$(source "$OSL/guest/common.sh"; hwsim_free | head -1)
        [ -n "$radio" ] || die "no free hwsim radio"
        lxc stop em-gtp 2>/dev/null || true
        lxc config device add em-gtp wlan0 nic nictype=physical parent="$radio" name=wlan0 >/dev/null
        log "em-gtp: radio $radio as wlan0"
    fi
    if ! lxc config device show em-gtp | grep -q '^eth1:'; then
        # eth1: a port on mv3's LAN, standing in for the EasyMesh gateway's LAN. The lan-p*
        # bridges filter VLANs, and each mv3 port is an untagged access port in its own VLAN:
        # join that VLAN, or nothing (not even ARP) crosses. (`command bridge`: this script
        # has a function of that name.)
        local host pvid
        host=$(lxc config get mv3 "volatile.${EMOSA_GTP_MV3_PORT:-eth4}.host_name")
        [ -n "$host" ] || die "em-gtp: mv3 has no ${EMOSA_GTP_MV3_PORT:-eth4} on the host side"
        pvid=$(command bridge -j vlan show dev "$host" | python3 -c 'import json,sys
print([v["vlan"] for p in json.load(sys.stdin) for v in p["vlans"] if "PVID" in v.get("flags", [])][0])') ||
            die "em-gtp: no port VLAN on mv3's ${EMOSA_GTP_MV3_PORT:-eth4} ($host)"
        lxc stop em-gtp 2>/dev/null || true
        lxc config device add em-gtp eth1 nic nictype=bridged parent="$lan" name=eth1 vlan="$pvid" >/dev/null
        log "em-gtp: eth1 on $lan (VLAN $pvid, mv3 ${EMOSA_GTP_MV3_PORT:-eth4})"
    fi
    lxc start em-gtp 2>/dev/null || true
    wait_net em-gtp
    # the pod-backhaul SSID: a 3-address AP on 2.4 GHz channel 6 into the underlay bridge podbh.
    # No country_code: the regulatory domain is VM-wide and must not be changed from a container.
    cx em-gtp sh -c "cat > /etc/hostapd/hostapd.conf" <<EOF
interface=wlan0
bridge=podbh
driver=nl80211
ctrl_interface=/run/hostapd
ssid=${EMOSA_PODBH_SSID:-emosa-podbh}
hw_mode=g
channel=6
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${EMOSA_PODBH_KEY:-EmosaPodBh2026!}

# an EasyMesh backhaul BSS (data plane option 1): hostapd's standard Multi-AP backhaul,
# a 4-address station per backhaul STA, bridged into the LAN bridge (${EMOSA_MAP_BRIDGE:-br-gtp})
bss=wlan0_1
bridge=${EMOSA_MAP_BRIDGE:-br-gtp}
ssid=${EMOSA_MAP_SSID:-emosa-lab-bh}
multi_ap=1
wds_sta=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${EMOSA_MAP_KEY:-EmosaLabBh2026!}
EOF
    # The bridges exist before hostapd: a bridge hostapd creates is one it deletes when it
    # restarts, taking the GTP's tunnels and LAN leg with it.
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
    cx em-gtp sh -c 'systemctl daemon-reload; systemctl enable -q --now emosa-lab-bridges'
    local confs=/etc/hostapd/hostapd.conf
    if [ -f /opt/emosa-lab/policy ]; then
        # The controller's backhaul BSS, as the EasyMesh gateway would run it: 5 GHz, the
        # policy's "<ssid>-bh" and key (em-ctl-node policy), Multi-AP backhaul into br-gtp.
        # Data plane option 1 for the agents' uplink switch (lab.sh option1). Own radio.
        if ! lxc config device show em-gtp | grep -q '^wlan1:'; then
            # shellcheck source=/dev/null
            radio=$(source "$OSL/guest/common.sh"; hwsim_free | head -1)
            [ -n "$radio" ] || die "no free hwsim radio"
            lxc stop em-gtp
            lxc config device add em-gtp wlan1 nic nictype=physical parent="$radio" name=wlan1 >/dev/null
            lxc start em-gtp
            wait_net em-gtp
            log "em-gtp: radio $radio as wlan1 (5 GHz backhaul BSS)"
        fi
        local ssid key
        read -r ssid key < /opt/emosa-lab/policy
        cx em-gtp sh -c "umask 077; cat > /etc/hostapd/hostapd-bh.conf" <<EOF
interface=wlan1
bridge=${EMOSA_MAP_BRIDGE:-br-gtp}
driver=nl80211
ctrl_interface=/run/hostapd
ssid=$ssid-bh
hw_mode=a
channel=${EMOSA_BH_CHANNEL:-36}
multi_ap=1
wds_sta=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$key
EOF
        # its own hostapd: the packaged unit passes DAEMON_CONF as one argument
        cx em-gtp sh -c 'cat > /etc/systemd/system/emosa-lab-backhaul.service' <<'EOF'
[Unit]
Description=The controller's backhaul BSS (5 GHz Multi-AP) for data plane option 1
After=emosa-lab-bridges.service
Requires=emosa-lab-bridges.service

[Service]
ExecStart=/usr/sbin/hostapd /etc/hostapd/hostapd-bh.conf
Restart=always

[Install]
WantedBy=multi-user.target
EOF
        confs="$confs /etc/hostapd/hostapd-bh.conf"
    fi
    cx em-gtp sh -ec "sed -i '/^DAEMON_CONF=/d' /etc/default/hostapd 2>/dev/null || true
        echo DAEMON_CONF=/etc/hostapd/hostapd.conf >> /etc/default/hostapd
        systemctl unmask hostapd >/dev/null 2>&1; systemctl enable -q hostapd; systemctl restart hostapd"
    if [ "$confs" != /etc/hostapd/hostapd.conf ]; then
        cx em-gtp sh -ec 'systemctl daemon-reload; systemctl enable -q emosa-lab-backhaul
            systemctl restart emosa-lab-backhaul'
    fi
    lxc file push -q /opt/emosa-lab/adapter-kit.tar.gz em-gtp/root/adapter-kit.tar.gz
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
    sleep 2
    cx em-gtp systemctl is-active -q emosa-gtp || die "emosa-gtp did not start: $(cx em-gtp journalctl -u emosa-gtp -n 5 --no-pager)"
    log "em-gtp: pod-backhaul SSID ${EMOSA_PODBH_SSID:-emosa-podbh}, GTP 169.254.2.1 on podbh, tunnels into br-gtp ($lan)"
    log "em-gtp: Multi-AP backhaul BSS ${EMOSA_MAP_SSID:-emosa-lab-bh} into ${EMOSA_MAP_BRIDGE:-br-gtp}"
    [ "$confs" = /etc/hostapd/hostapd.conf ] ||
        log "em-gtp: the controller's backhaul BSS (5 GHz, channel ${EMOSA_BH_CHANNEL:-36}) into ${EMOSA_MAP_BRIDGE:-br-gtp}"
}

uplink() {      # uplink POD gtp|multi-ap|restore|show
    python3 "$HERE/vm/uplink.py" "$@"
}

option1() {     # option1 POD on|off [config|m2]: POD's agent switches its uplink to the EasyMesh backhaul
    local pod=$1 mode serial ssid key bssid= source=${3:-config}
    case ${2:-} in on) mode=multi-ap ;; off) mode=off ;; *) die "option1 POD on|off [config|m2]" ;; esac
    case $source in config|m2) ;; *) die "option1: credentials from config or m2" ;; esac
    serial=$(pod_serial "$pod")
    [ -n "$serial" ] || die "$pod: no AWLAN_Node serial"
    cx emosa test -f "/etc/emosa/$serial.json" || die "$pod ($serial) is not a fleet agent: lab.sh admit $pod"
    [ -f /opt/emosa-lab/policy ] || die "no controller policy saved yet: run lab.sh policy SSID KEY"
    read -r ssid key < /opt/emosa-lab/policy
    if [ "$mode" != off ]; then
        # The station is pinned to em-gtp's backhaul BSS: unpinned, a pod that also runs
        # the backhaul BSS (m2) joins itself and loops br-home (data-plane.md §5.6).
        bssid=$(cx em-gtp cat /sys/class/net/wlan1/address 2>/dev/null) ||
            die "no controller backhaul BSS on em-gtp wlan1: run lab.sh gtp"
    fi
    # The controller's backhaul credentials (em-ctl-node policy: "<ssid>-bh", same key), given
    # to the agent by configuration: its secret file, then the fleet's per-pod setting (kept
    # across handovers) and the running agent's configuration.
    printf '%s' "$key" | cx emosa sh -c "install -d -m 700 /var/lib/emosa/$serial/secrets
        umask 077; cat > /var/lib/emosa/$serial/secrets/backhaul"
    cx emosa python3 - "$serial" "$mode" "$ssid-bh" "$source" "$bssid" <<'EOF'
import json, sys
serial, mode, ssid, source, bssid = sys.argv[1:]
uplink = {"mode": mode}
own = {"uplink": uplink}
if mode != "off":
    uplink["bssid"] = bssid
if mode != "off" and source == "config":
    uplink.update(credentials="config", ssid=ssid, secret_ref="backhaul")
elif mode != "off":
    # from the controller's M2 set: the agent maps the set's BSSes (multi-BSS)
    uplink.update(credentials="m2")
    own["multi_bss"] = True
for path, update in (
    ("/etc/emosa-fleet.json", lambda c: c.setdefault("pods", {}).setdefault(serial, {}).update(own)),
    (f"/etc/emosa/{serial}.json", lambda c: c.update(own)),
):
    config = json.load(open(path))
    update(config)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
EOF
    cx emosa systemctl restart emosa-fleet "emosa-agent@$serial"
    if [ "$mode" = off ]; then
        policy "$ssid" "$key"    # a backhaul BSS given for M2 credentials goes with it
        log "option1: $pod ($serial) uplink off"
    elif [ "$source" = m2 ]; then
        policy "$ssid" "$key"    # the controller now gives this agent the backhaul BSS in its M2 set
        log "option1: $pod ($serial) uplink $mode to $bssid, credentials from the controller's M2 set"
    else
        log "option1: $pod ($serial) uplink $mode to $bssid (backhaul '$ssid-bh' from the controller policy)"
    fi
}

status() {
    lxc list -f csv -c ns em-ctl emosa 2>/dev/null || true
    cx em-ctl systemctl list-units --no-legend --plain 'em-*' 2>/dev/null | awk '{print "em-ctl", $1, $3, $4}' || true
    # configured agents only (a released agent's state stays on disk as history)
    cx emosa python3 -c '
import glob, json, os
for c in sorted(glob.glob("/etc/emosa/*.json")):
    f = os.path.join(json.load(open(c))["state_dir"], "status.json")
    if not os.path.exists(f):
        continue
    s = json.load(open(f)); p = s.get("pod") or {}
    print(s["pod_id"], "agent", s["agent_al"], "session", (s.get("session") or {}).get("state"),
          "ssid", p.get("ssid"), "stations", len(p.get("stations") or []),
          "last ops", [(o["state"], o["ssid"]) for o in s["operations"][-2:]])' 2>/dev/null || true
    docker exec local-noc noc-ctl redirects 2>/dev/null || true
    if cx emosa systemctl is-active -q emosa-fleet 2>/dev/null; then
        fleet_agents | python3 -c '
import json, sys
for serial, a in sorted(json.load(sys.stdin).items()):
    print("fleet", serial, a["al_mac"], a["interface"], a["port"], "handovers", a["handovers"])'
    fi
}

cmd=${1:-}; shift || true
case $cmd in
    bridge|controller|emosa|agent|fleet|admit|release|policy|client|topology|ui|telemetry|provision|gtp|uplink|option1|status) "$cmd" "$@" ;;
    workload) exec python3 "$HERE/vm/workload.py" "$@" ;;
    *) sed -n '2,26p' "$0"; exit 2 ;;
esac
