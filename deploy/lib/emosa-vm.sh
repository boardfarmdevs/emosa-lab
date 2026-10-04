# EMOSA's deployment steps shared by the labs' VM scripts (deploy/opensync-lab/vm/lab.sh,
# deploy/rdk-lab/vm/lab.sh), sourced by them inside the lab VM (root), where both stage
# the repository's deploy/ layout under /opt/emosa-lab/deploy. What differs between the
# labs stays in their scripts: the EasyMesh LAN, the controller, the pods and their radios.
#
# The caller sets LOG_TAG (log prefix), IMAGE (container image) and WAN_HOST (the
# address the pods reach EMOSA on); TELEMETRY_OPTIONS optionally adds settings to the
# fleet's telemetry (JSON members, e.g. reporting intervals).
# shellcheck shell=bash disable=SC2016    # commands for the containers expand there
# SPDX-License-Identifier: Apache-2.0

KIT=${KIT:-/opt/emosa-lab/adapter-kit.tar.gz}    # the adapter kit (deploy/adapter), staged by lab.sh stage
STATE=${STATE:-/opt/emosa-lab}                   # the lab's markers: telemetry, policy

log() { printf '\033[1;36m[%s %s]\033[0m %s\n' "$LOG_TAG" "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[1;31m[%s %s] FATAL\033[0m %s\n' "$LOG_TAG" "$(date +%H:%M:%S)" "$*" >&2; exit 1; }
exists() { lxc info "$1" >/dev/null 2>&1; }
running() { [ "$(lxc list "^$1\$" -c s -f csv)" = RUNNING ]; }
has_device() { lxc config device list "$1" | grep -x "$2" >/dev/null; }
cx() { local c=$1; shift; lxc exec "$c" -- "$@"; }

wait_net() {    # wait_net CT: until the container can resolve and reach the archive
    for _ in $(seq 60); do cx "$1" getent hosts archive.ubuntu.com >/dev/null 2>&1 && return; sleep 2; done
    die "$1 has no network"
}

apt_install() {    # apt_install CT PACKAGE...: the packages CT lacks
    local ct=$1; shift
    cx "$ct" dpkg -s "$@" >/dev/null 2>&1 && return
    cx "$ct" sh -ec 'export DEBIAN_FRONTEND=noninteractive; apt-get -qq update
        apt-get -qq install -y "$@" >/dev/null' sh "$@"
}

pod_serial() { cx "$1" /usr/opensync/tools/ovsh -r s AWLAN_Node serial_number 2>/dev/null | tr -d '[:space:]'; }

# --- the adapter container (emosa) -------------------------------------------------------

adapter_container() {    # adapter_container PACKAGE...: container emosa, running, with them
    if ! exists emosa; then
        lxc init "$IMAGE" emosa --network lxdbr0 >/dev/null
        lxc config set emosa user.emosa.role adapter
        lxc start emosa
        wait_net emosa
        cx emosa systemctl mask --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
    fi
    running emosa || lxc start emosa    # e.g. after a VM reboot
    # what the kit's installer needs to build the C agent (deploy/adapter/install.sh)
    apt_install emosa iproute2 tcpdump cmake pkg-config gcc libcjson-dev libssl-dev libsqlite3-dev "$@"
}

install_kit() {    # install_kit CT: the adapter kit, installed in CT
    # EMOSA_IMPLEMENTATION=python|c chooses the adapter's (c: no Python); unset, the
    # installer keeps the one CT has
    lxc file push -q "$KIT" "$1/root/adapter-kit.tar.gz"
    cx "$1" env EMOSA_IMPLEMENTATION="${EMOSA_IMPLEMENTATION:-}" sh -ec 'rm -rf /root/adapter-kit && mkdir /root/adapter-kit
        tar -C /root/adapter-kit --strip-components=1 -xzf /root/adapter-kit.tar.gz
        /root/adapter-kit/install.sh'
}

fleet_cli() {    # fleet_cli COMMAND [ARGS...]: the installed fleet's command (list, forget)
    cx emosa sh -c 'set -a; . /etc/default/emosa-implementation 2>/dev/null || true
        . /etc/default/emosa 2>/dev/null || true
        exec "${EMOSA_FLEET:-/opt/emosa-adapter/venv/bin/emosa-fleet}" "$1" /etc/emosa-fleet.json ${2:+"$2"}' \
        sh "$1" "${2:-}"
}

python_adapter() { cx emosa test -x /opt/emosa-adapter/venv/bin/python; }

adapter_kit() {    # the kit in emosa, its agents' trunk the EasyMesh LAN NIC emlan
    install_kit emosa
    cx emosa sed -i 's/^EMOSA_TRUNK=.*/EMOSA_TRUNK=emlan/' /etc/default/emosa
    log "emosa: $(cx emosa cat /root/adapter-kit/VERSION) in /opt/emosa-adapter, trunk emlan"
}

proxy() {    # proxy CT DEVICE PORTS: tcp:$WAN_HOST:PORTS into CT's loopback (agents listen there only)
    has_device "$1" "$2" ||
        lxc config device add "$1" "$2" proxy bind=host listen="tcp:$WAN_HOST:$3" connect="tcp:127.0.0.1:$3" >/dev/null
}

# --- the fleet -----------------------------------------------------------------------------

telemetry_json() {    # the fleet's telemetry setting
    if [ -f "$STATE/telemetry" ]; then
        printf '{"mode": "mqtt", "broker": "%s", "port": 8883%s}' "$WAN_HOST" "${TELEMETRY_OPTIONS:+, $TELEMETRY_OPTIONS}"
    else
        printf '{"mode": "off"}'
    fi
}

# fleet_config FRONT FIRST LAST CONTROLLER_AL MESSAGE_SET MULTI_BSS M2_SESSION [PODS_JSON]:
# /etc/emosa-fleet.json (EMOSA_MESSAGE_SET, EMOSA_MULTI_BSS, EMOSA_M2_SESSION and
# EMOSA_POD_PROFILE override), then the fleet (re)started
fleet_config() {
    local pods=
    exists emosa || die "run: lab.sh emosa"
    [ -z "${8:-}" ] || pods=$',\n  "pods": '"$8"
    cx emosa sh -c "cat > /etc/emosa-fleet.json" <<EOF
{
  "listen": "ptcp:$1:127.0.0.1",
  "advertise": "$WAN_HOST",
  "ports": [$2, $3],
  "controller_al": "$4",
  "message_set": "${EMOSA_MESSAGE_SET:-$5}",
  "multi_bss": ${EMOSA_MULTI_BSS:-$6},
  "m2_session": "${EMOSA_M2_SESSION:-$7}",
  "profile": "${EMOSA_POD_PROFILE:-opensync-lab-hwsim-6.6.1-v1}",
  "state_root": "/var/lib/emosa",
  "config_dir": "/etc/emosa",
  "admit": "*",
  "telemetry": $(telemetry_json)$pods
}
EOF
    cx emosa systemctl enable -q emosa-fleet
    cx emosa systemctl restart emosa-fleet
}

# --- the agent implementation (deploy/adapter: EMOSA_AGENT) --------------------------------

agent_binary() {    # python|c -> the agent the kit installed for it
    case ${1:-} in
        python) echo /opt/emosa-adapter/venv/bin/emosa-agent ;;
        c) echo /opt/emosa-adapter/bin/emosa-agent-c ;;
        *) return 1 ;;
    esac
}

agent_names() { cx emosa sh -c 'for f in /etc/emosa/*.json; do [ -e "$f" ] && basename "$f" .json; done'; }

implementation_all() {    # implementation_all BIN: every agent (a pod's own choice is dropped)
    local name
    exists emosa || die "run: lab.sh emosa"
    cx emosa test -x "$1" || die "$1 is not installed (lab.sh emosa)"
    cx emosa sh -ec "sed -i '/^EMOSA_AGENT=/d' /etc/default/emosa; echo EMOSA_AGENT=$1 >> /etc/default/emosa"
    for name in $(agent_names); do
        cx emosa rm -f "/etc/default/emosa-$name"
        cx emosa systemctl try-restart "emosa-agent@$name"
    done
    log "implementation: every agent runs $1"
}

implementation_one() {    # implementation_one NAME BIN: agent NAME (a pod or its serial)
    exists emosa || die "run: lab.sh emosa"
    cx emosa test -x "$2" || die "$2 is not installed (lab.sh emosa)"
    cx emosa sh -c "echo EMOSA_AGENT=$2 > /etc/default/emosa-$1"
    cx emosa systemctl try-restart "emosa-agent@$1"
}

# --- the pods' telemetry: an MQTT broker (OpenSync sm/qm: mutual TLS only) -----------------

provision() {   # provision POD: its lab device certificate, where OpenSync expects it (/var/certs)
    local pod=$1 serial t f
    serial=$(pod_serial "$pod")
    [ -n "$serial" ] || die "$pod: no serial yet (OpenSync not up)"
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
    log "provision: $pod ($serial) lab device certificate in /var/certs (EMOSA lab CA)"
}

telemetry_on() {    # telemetry_on POD...: the broker, the PODs' certificates, every agent publishing
    local pod
    apt_install emosa mosquitto mosquitto-clients openssl
    cx emosa sh -ec 'P=/var/lib/emosa/pki; install -d -m 700 $P; cd $P
        [ -f ca.pem ] || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=EMOSA lab CA" \
            -keyout ca.key -out ca.pem 2>/dev/null
        [ -f broker.pem ] || { openssl req -newkey rsa:2048 -nodes -subj "/CN=$1" -keyout broker.key \
                -out broker.csr 2>/dev/null
            printf "subjectAltName=IP:%s,IP:127.0.0.1\n" "$1" > broker.ext
            openssl x509 -req -in broker.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 3650 \
                -extfile broker.ext -out broker.pem 2>/dev/null; }
        # the CA key stays with root; the broker gets its own copy (/var/lib/emosa is 0700)
        C=/etc/mosquitto/certs; install -d -o mosquitto -m 750 $C
        install -o mosquitto -m 644 ca.pem broker.pem $C/; install -o mosquitto -m 600 broker.key $C/
        printf "%s\n" "per_listener_settings true" "listener 8883 127.0.0.1" "cafile $C/ca.pem" \
            "certfile $C/broker.pem" "keyfile $C/broker.key" "require_certificate true" \
            "use_identity_as_username true" "listener 1883 127.0.0.1" "allow_anonymous true" \
            > /etc/mosquitto/conf.d/emosa.conf
        systemctl restart mosquitto' sh "$WAN_HOST"
    proxy emosa mqtt 8883
    touch "$STATE/telemetry"    # the fleet and later pods keep it on
    for pod in "$@"; do provision "$pod"; done
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
            systemctl restart "$u"; done'
    log "telemetry: broker tcp:$WAN_HOST:8883 (mutual TLS, lab CA) -> emosa; pods publish to emosa/stats/<serial>"
}

# --- the GRE termination point (em-gtp): the pods' underlay into the EasyMesh LAN ----------

gtp_bridges() {    # gtp_bridges: em-gtp's bridges podbh (underlay) and br-gtp (LAN)
    # They exist before hostapd: a bridge hostapd creates is one it deletes when it
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
}

gtp_hostapd() {    # gtp_hostapd: hostapd (re)started on /etc/hostapd/hostapd.conf
    cx em-gtp sh -ec "sed -i '/^DAEMON_CONF=/d' /etc/default/hostapd 2>/dev/null || true
        echo DAEMON_CONF=/etc/hostapd/hostapd.conf >> /etc/default/hostapd
        systemctl unmask hostapd >/dev/null 2>&1; systemctl enable -q hostapd; systemctl restart hostapd"
}

gtp_termination() {    # gtp_termination: the kit's GTP on podbh, tunnels into br-gtp with eth1
    # the adapter's implementation (the emosa container's) unless EMOSA_IMPLEMENTATION says
    local impl=${EMOSA_IMPLEMENTATION:-}
    [ -n "$impl" ] || impl=$(cx emosa sed -n 's/^EMOSA_IMPLEMENTATION=//p' /etc/default/emosa-implementation 2>/dev/null || true)
    EMOSA_IMPLEMENTATION=$impl install_kit em-gtp >/dev/null
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
    cx em-gtp systemctl is-active -q emosa-gtp ||
        die "emosa-gtp did not start: $(cx em-gtp journalctl -u emosa-gtp -n 5 --no-pager)"
}
