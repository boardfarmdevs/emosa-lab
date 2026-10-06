#!/bin/bash
# Inside an RDK EasyMesh lab VM with the EMOSA option (vm/lab.sh up c gateway), root: EMOSA's
# fleet and agents, the pods' broker and their GRE termination point in the RDK controller's
# own container, as on a gateway that carries the adapter (meta-cmf-bananapi-vcpe's recipe
# emosa), and its footprint there (easymesh-labs plan 5.4).
#
#   gateway.sh on [EMOSA_IPK]     the fleet and agents into the controller container: the
#                                 package emosa (EMOSA_IPK, built with the image's recipe,
#                                 over what the gateway has; without it, the package the
#                                 image or an earlier run put there); an adapter container's
#                                 registry and state, if the lab has one, taken over into the
#                                 places the gateway's package names (/etc/default/emosa:
#                                 /nvram/emosa on RDK, the status in /run), else the lab's
#                                 fleet configuration (vm/lab.sh fleet) written there; the
#                                 front and agent ports on the gateway's LAN address through
#                                 the package's forwarder, the pods sent there by the
#                                 operator's redirect on the VM (operator-redirect.py, the
#                                 pods' redirector address); the pods' broker in the gateway
#                                 (the image's mosquitto, the lab's CA on the VM); their
#                                 bootstrap SSID as a VAP of the gateway's (OneWifi's
#                                 lnf_radius_5g on bridge brpodbh) and their GRE ended there
#                                 (the image's emosa-gtp, tunnels into brlan0); the agents'
#                                 trunk is the package's veth pair into brlan0. Again while
#                                 EMOSA runs in the gateway (after it was deployed anew): all
#                                 of that again; with EMOSA_IPK, that package over the running one
#   gateway.sh off                back into the adapter container and the GTP container em-gtp,
#                                 the state taken back (a lab that has them: the target
#                                 configuration has none); the package stays installed in the
#                                 gateway, unconfigured (inert)
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
FOOTPRINT=/var/lib/emosa-lab/footprint
LOG_TAG=emosa-gateway
HERE=$(cd "$(dirname "$0")" && pwd)
REDIRECT=$HERE/operator-redirect.py    # the operator's redirect (the VM's)
PKI=/var/lib/emosa-lab/pki    # the lab's CA on the VM (the pods' device certificates)
MQTT_DIR=/nvram/emosa/mqtt    # the gateway's broker: its CA, certificate and key
BHAUL_SSID=${EMOSA_PODBH_SSID:-opensync-lab-bhaul}    # the pods' bootstrap SSID (the pod image's)
BHAUL_KEY=${EMOSA_PODBH_KEY:-opensync-lab-bhaul-psk}
PODBH_BRIDGE=brpodbh    # the underlay: the bootstrap SSID's own bridge in the gateway
GTP_CONFIG=/nvram/emosa/gtp-config.json    # the package's (emosa-gtp.service)
PODBH_CONF=/nvram/emosa/podbh.conf    # the pods' SSID and key, the image's (emosa-podbh.service)
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

# how many pods run (their containers, stopped after a VM restart, do not count)
pods_running() { lxc list -c ns -f csv | grep -cE '^pod-[0-9]+,RUNNING$' || true; }

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

operator_redirect() {    # operator_redirect TARGET|off: the operator's redirect on the pods' redirector address
    # The operator's cloud hands each pod to the gateway's fleet once (plan 5.3); in this lab
    # the pod image's redirector is $WAN_HOST:$FLEET_PORT, on the WAN side: answered here, on
    # the VM, outside the home (operator-redirect.py), not by EMOSA.
    local unit=/etc/systemd/system/emosa-lab-operator-redirect.service
    if [ "$1" = off ]; then
        systemctl disable -q --now emosa-lab-operator-redirect 2>/dev/null || true
        rm -f "$unit"
        systemctl daemon-reload
        return
    fi
    cat > "$unit" <<EOF
[Unit]
Description=The operator's redirect on $WAN_HOST:$FLEET_PORT (the pods' redirector) to $1
After=emosa-lab-wan-address.service

[Service]
ExecStart=/usr/bin/python3 $REDIRECT $WAN_HOST $FLEET_PORT $1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable -q emosa-lab-operator-redirect
    systemctl restart emosa-lab-operator-redirect
}

gateway_ports() {    # the front and agent ports on the gateway's LAN address, its own forwarder
    local lan config ct dev
    lan=$(lan_address)
    [ -n "$lan" ] || die "$CTL has no IPv4 address on its LAN bridge"
    for ct in "$CTL" emosa; do    # the lab's proxies from the WAN address: none
        exists "$ct" || continue    # emosa: none in the target configuration
        for dev in front agents; do
            ! has_device "$ct" "$dev" || lxc config device remove "$ct" "$dev" >/dev/null
        done
    done
    # the fleet hands pods to tcp:LAN:PORT; its forwarder (emosa-forward-c) carries them to
    # the loopback ports the fleet and the agents listen on (spec 3.1)
    config=$(emosa_layout "$CTL" | cut -d" " -f1)
    cx "$CTL" cat "$config" | jq --arg lan "$lan" '.advertise = $lan | .forward = true' |
        lxc exec "$CTL" -- sh -c "cat > '$config.new' && mv '$config.new' '$config'"
    cx "$CTL" systemctl enable -q emosa-forward
    cx "$CTL" systemctl restart emosa-forward
    operator_redirect "tcp:$lan:$FLEET_PORT"
    log "gateway: the fleet and agents on $lan, the operator's redirect on $WAN_HOST:$FLEET_PORT"
}

lab_ca() {    # the lab's CA, on the VM: the operator's side, which signs the pods' device certificates
    [ -f "$PKI/ca.key" ] && return 0
    install -d -m 700 "$PKI"
    if exists emosa && cx emosa test -f /var/lib/emosa/pki/ca.key; then    # the one the pods trust
        cx emosa cat /var/lib/emosa/pki/ca.pem > "$PKI/ca.pem"
        cx emosa cat /var/lib/emosa/pki/ca.key > "$PKI/ca.key"
    else
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=EMOSA lab CA" \
            -keyout "$PKI/ca.key" -out "$PKI/ca.pem" 2>/dev/null
    fi
    chmod 600 "$PKI/ca.key"
}

gateway_broker() {    # gateway_broker LAN: the pods' broker in the gateway (mosquitto, the image's)
    # mutual TLS on the gateway's LAN address for the pods, plain on its loopback for the
    # agents (spec 3.6); the broker's certificate from the lab's CA, for the LAN address
    local lan=$1 t ct config agents
    cx "$CTL" test -x /usr/sbin/mosquitto || die "$CTL has no mosquitto: a gateway image with EMOSA's broker"
    lab_ca
    t=$(mktemp -d)
    openssl req -newkey rsa:2048 -nodes -subj "/CN=$lan" -keyout "$t/broker.key" -out "$t/broker.csr" 2>/dev/null
    printf 'subjectAltName=IP:%s,IP:127.0.0.1\n' "$lan" > "$t/broker.ext"
    openssl x509 -req -in "$t/broker.csr" -CA "$PKI/ca.pem" -CAkey "$PKI/ca.key" -CAcreateserial \
        -days 3650 -extfile "$t/broker.ext" -out "$t/broker.pem" 2>/dev/null
    cx "$CTL" sh -c "mkdir -p '$MQTT_DIR' && chmod 700 '$MQTT_DIR'"
    lxc exec "$CTL" -- sh -c "cat > '$MQTT_DIR/ca.pem'" < "$PKI/ca.pem"
    lxc exec "$CTL" -- sh -c "cat > '$MQTT_DIR/broker.pem'" < "$t/broker.pem"
    lxc exec "$CTL" -- sh -c "umask 077; cat > '$MQTT_DIR/broker.key'" < "$t/broker.key"
    rm -rf "$t"
    cx "$CTL" sh -c "printf '%s\n' 'per_listener_settings true' 'user root' \
        'listener 8883 $lan' 'cafile $MQTT_DIR/ca.pem' 'certfile $MQTT_DIR/broker.pem' \
        'keyfile $MQTT_DIR/broker.key' 'require_certificate true' 'use_identity_as_username true' \
        'listener 1883 127.0.0.1' 'allow_anonymous true' > /etc/mosquitto/mosquitto.conf"
    cx "$CTL" systemctl enable -q mosquitto
    cx "$CTL" systemctl restart mosquitto
    for ct in "$CTL" emosa; do    # the lab's broker proxies: none
        exists "$ct" || continue
        for dev in mqtt mqtt-agents; do
            ! has_device "$ct" "$dev" || lxc config device remove "$ct" "$dev" >/dev/null
        done
    done
    # the pods publish to the gateway's LAN address: the fleet's and the agents' setting
    read -r config agents _ _ <<<"$(emosa_layout "$CTL")"
    cx "$CTL" python3 - "$lan" "$config" "$agents" <<'EOF'
import glob, json, os, sys
lan, fleet, agents = sys.argv[1:4]
for path in [fleet, *glob.glob(agents + "/*.json")]:
    with open(path) as f:
        config = json.load(f)
    telemetry = config.get("telemetry")
    if isinstance(telemetry, dict) and telemetry.get("mode") == "mqtt" and telemetry.get("broker") != lan:
        telemetry["broker"] = lan
        with open(path + ".new", "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(path + ".new", path)
EOF
    log "gateway: the pods' broker in $CTL, tcp:$lan:8883 (mutual TLS, lab CA), the agents' on 127.0.0.1:1883"
}

pods_to_broker() {    # the pods publishing elsewhere: their OpenSync again, once the agents run
    # an agent writes the pod's broker once per start of its OpenSync (spec 3.6)
    local lan pod broker
    [ -f "$STATE/telemetry" ] || return 0
    lan=$(lan_address)
    for pod in $(lxc list -c ns -f csv | grep -E '^pod-[0-9]+,RUNNING$' | cut -d, -f1); do
        # shellcheck disable=SC2016 # expanded in the pod
        broker=$(cx "$pod" sh -c 'PATH=$PATH:/usr/opensync/tools; ovsh s AWLAN_Node mqtt_settings -r' |
            grep -o '"broker","[^"]*"' | cut -d'"' -f4) || true
        # none yet (not provisioned since its OpenSync started): its agent writes the gateway's
        if [ -z "$broker" ] || [ "$broker" = "$lan" ]; then continue; fi
        cx "$pod" systemctl restart opensync
        log "gateway: $pod published to $broker: its OpenSync again, for $lan"
    done
}

podbh_vap() {    # the pods' bootstrap SSID as one of the gateway's own VAPs (OneWifi), on its own bridge
    # A fifth VAP on the gateway's 5 GHz radio, wifi1.4: lnf_radius_5g on the underlay
    # bridge, run as a fronthaul-type PSK VAP (spec 8.2), beside the gateway's own ten. Not
    # lnf_psk_5g: the controller's networks configure that one (haul type Configurator, its
    # SSID lnf_radius). No haul type maps to lnf_radius, and with OneWifi's libwebconfig 0014
    # (meta-cmf) lnf_radius VAPs stay out of EasyMesh: the controller never sees it (an SSID
    # outside its networks failed the gateway's Topology Responses) and never resets it. The
    # image's pre-start makes the interfaces the map names beyond its fixed ten (the HAL
    # cannot: -EADDRNOTAVAIL). OneWifi reads its interface map from /nvram, which an image
    # upgrade keeps; the gateway's own map stays in .before-podbh, for off.
    local changed i ssid
    cx "$CTL" grep -q 'beyond the fixed ones above' /usr/ccsp/wifi/onewifi_pre_start.sh ||
        die "$CTL: its image makes no interface beyond its fixed ten (meta-cmf: the pods' VAP on wifi1.4)"
    cx "$CTL" test -x /usr/libexec/emosa/podbh-onewifi ||
        die "$CTL: its image has no emosa-podbh (meta-cmf: the pods' SSID kept across OneWifi's restarts)"
    changed=$(cx "$CTL" python3 - "$PODBH_BRIDGE" <<'EOF'
import json, os, shutil, sys
path = "/nvram/InterfaceMap.json"
if not os.path.exists(path + ".before-podbh"):
    shutil.copy(path, path + ".before-podbh")
# the gateway's own map (an earlier version of this step had the pods' VAP in the 5 GHz
# mesh station's slot), with the pods' VAP beside its 5 GHz VAPs
interface_map = json.load(open(path + ".before-podbh"))
for phy in interface_map["PhyList"]:
    for radio in phy["RadioList"]:
        vaps = [v for v in radio["InterfaceList"] if v["vapName"] != "lnf_radius_5g"]
        if any(v["vapName"].endswith("_5g") for v in vaps):
            vaps.insert(0, {"InterfaceName": "wifi1.4", "Bridge": sys.argv[1], "vlanId": 0,
                            "vapIndex": 11, "vapName": "lnf_radius_5g"})
        radio["InterfaceList"] = vaps
if interface_map == json.load(open(path)):
    print("")
else:
    with open(path + ".new", "w") as f:
        json.dump(interface_map, f, indent=4)
    os.replace(path + ".new", path)
    print("changed")
EOF
)
    if [ "$changed" = changed ]; then
        # OneWifi again (its pre-start makes wifi1.4), and the gateway's agent after it: the
        # controller's settings again
        cx "$CTL" systemctl restart onewifi
        for i in $(seq 1 24); do
            cx "$CTL" sh -c 'rbuscli get Device.WiFi.SSIDNumberOfEntries 2>/dev/null | grep -q Value' && break
            sleep 5
        done
        cx "$CTL" systemctl restart em_agent
        sleep 45    # the agent applies the controller's settings once onboarded: after that
        log "gateway: OneWifi with lnf_radius_5g on wifi1.4 in $PODBH_BRIDGE, after ${i}x5 s"
    fi
    # its SSID and key, the pod image's bootstrap credentials, for the image's emosa-podbh,
    # which gives them to OneWifi at each of its starts (OneWifi keeps no VAP settings across
    # a restart); then the gateway's agent again when they changed: it applies the
    # controller's settings with its copy of the radio's VAPs, taken when it started.
    # Checked on the interface, and applied again while they do not show there.
    cx "$CTL" sh -c "umask 077; cat > '$PODBH_CONF.new' && mv '$PODBH_CONF.new' '$PODBH_CONF'" <<EOF
PODBH_VAP=lnf_radius_5g
PODBH_SSID=$BHAUL_SSID
PODBH_KEY=$BHAUL_KEY
EOF
    cx "$CTL" systemctl enable -q emosa-podbh
    for i in 1 2 3 4; do
        cx "$CTL" systemctl restart emosa-podbh
        if cx "$CTL" journalctl -u emosa-podbh -n 1 -o cat --no-pager | grep -q ' set to '; then
            sleep 20
            cx "$CTL" systemctl restart em_agent
            sleep 45
        fi
        ssid=$(cx "$CTL" sh -c "for v in \$(ls /sys/class/net/$PODBH_BRIDGE/brif 2>/dev/null); do iw dev \$v info; done" |
            awk '$1 == "ssid" {print $2; exit}')
        [ "$ssid" = "$BHAUL_SSID" ] && break
    done
    [ "$ssid" = "$BHAUL_SSID" ] || die "$CTL: the pods' SSID did not come up on $PODBH_BRIDGE (${ssid:-none})"
    log "gateway: '$BHAUL_SSID' on $(cx "$CTL" ls "/sys/class/net/$PODBH_BRIDGE/brif" | tr '\n' ' ')in $PODBH_BRIDGE"
}

gateway_gtp() {    # the pods' GRE termination in the gateway: the image's emosa-gtp on the SSID's bridge
    podbh_vap
    # the underlay is the gateway's own (DHCP, GRE, ping): the firewall's syscfg rules, which
    # it applies again at each of its restarts
    # shellcheck disable=SC2016 # expanded in the gateway
    cx "$CTL" sh -s "$PODBH_BRIDGE" <<'EOF'
rule="-A INPUT -i $1 -j ACCEPT"
n=$(syscfg get GeneralPurposeFirewallRuleCount 2>/dev/null)
n=${n:-0}
i=1
while [ "$i" -le "$n" ]; do
    [ "$(syscfg get "GeneralPurposeFirewallRule_$i")" = "$rule" ] && exit 0
    i=$((i + 1))
done
n=$((n + 1))
syscfg set "GeneralPurposeFirewallRule_$n" "$rule" && syscfg set GeneralPurposeFirewallRuleCount "$n" && syscfg commit
sysevent set firewall-restart
EOF
    cx "$CTL" sh -c "cat > '$GTP_CONFIG.new' && mv '$GTP_CONFIG.new' '$GTP_CONFIG'" <<EOF
{
  "underlay": {
    "interface": "$PODBH_BRIDGE",
    "address": "169.254.2.1/25",
    "mtu": 1600,
    "dhcp_range": ["169.254.2.10", "169.254.2.126"],
    "lease_time": "1h"
  },
  "lan": {"bridge": "brlan0", "ports": []},
  "tunnel_mtu": 1562,
  "state_dir": "/var/lib/emosa-gtp"
}
EOF
    cx "$CTL" systemctl enable -q emosa-gtp
    cx "$CTL" systemctl restart emosa-gtp
    # em-gtp's SSID and GTP off: the pods' bootstrap is the gateway's now
    if exists em-gtp && running em-gtp; then
        cx em-gtp sh -c 'systemctl disable -q --now hostapd emosa-gtp 2>/dev/null' || true
    fi
    log "gateway: the pods' GRE ends in $CTL ($PODBH_BRIDGE 169.254.2.1/25, tunnels into brlan0)"
}

plumbing() {    # the front and agent ports on the gateway's LAN, the pods' broker and GRE end in the gateway
    gateway_ports
    [ ! -f "$STATE/telemetry" ] || gateway_broker "$(lan_address)"
    gateway_gtp
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
        cx "$CTL" systemctl enable -q emosa-fleet
        cx "$CTL" systemctl restart emosa-fleet    # with the advertise address plumbing wrote
        # and the agents with the broker it wrote (the fleet starts the registry's stopped ones)
        cx "$CTL" sh -c 'for u in $(systemctl list-units --plain --no-legend "emosa-agent@*" | cut -d" " -f1); do
            systemctl restart "$u"; done'
        log "gateway: EMOSA in $CTL again ($(emosa_layout "$CTL"))"
        pods_to_broker
        wait_provisioned "$CTL" "$(pods_running)"
        log "gateway: $(provisioned "$CTL") agents provisioning in $CTL"
        return
    fi
    if ! { exists emosa && cx emosa test -f "$(emosa_layout emosa | cut -d" " -f1)"; }; then
        # EMOSA anew in the gateway, no adapter container before it (vm/lab.sh up c gateway):
        # the lab's fleet configuration in the gateway's places, then all of the above
        install_package "${1:-}"
        touch "$STATE/gateway"
        bash "$HERE/lab.sh" fleet || { rm -f "$STATE/gateway"; die "the gateway's fleet configuration failed"; }
        plumbing
        cx "$CTL" systemctl restart emosa-fleet    # with the advertise address plumbing wrote
        log "gateway: EMOSA anew in $CTL ($(cx "$CTL" /usr/bin/emosa-agent-c --version); places $(emosa_layout "$CTL"))"
        return
    fi
    install_package "${1:-}"
    log "gateway: $(cx "$CTL" /usr/bin/emosa-agent-c --version); $(cx "$CTL" sh -c '. /etc/default/emosa; echo "trunk $EMOSA_TRUNK into ${EMOSA_BRIDGE:-(none)}, agents in namespace ${EMOSA_NETNS:-(the gateway'"'"'s)}"'); places $(emosa_layout "$CTL")"
    stop_emosa emosa
    move_state emosa "$CTL"
    plumbing
    touch "$STATE/gateway"
    cx "$CTL" systemctl enable -q emosa-fleet
    cx "$CTL" systemctl restart emosa-fleet
    log "gateway: fleet started in $CTL, with its registry's agents"
    pods_to_broker
    wait_provisioned "$CTL" "$(pods_running)"
    log "gateway: $(provisioned "$CTL") agents provisioning in $CTL"
}

off() {
    in_gateway || die "EMOSA does not run in $CTL"
    # back into the containers the gateway took EMOSA and its GTP from: none in the target
    # configuration (vm/lab.sh retire), which is built with them (EASYMESH_EMOSA_IN=container)
    if ! exists emosa || ! exists em-gtp; then
        die "no adapter or GTP container to take EMOSA back to (vm/lab.sh retire deleted them)"
    fi
    stop_emosa "$CTL"
    cx "$CTL" systemctl disable -q --now emosa-forward 2>/dev/null || true
    cx "$CTL" sh -c 'systemctl disable -q --now mosquitto 2>/dev/null; rm -f /etc/mosquitto/mosquitto.conf' || true
    # the pods' GRE back in em-gtp: the gateway's GTP off, its own VAP map again (no wifi1.4)
    cx "$CTL" sh -c "systemctl disable -q --now emosa-gtp emosa-podbh 2>/dev/null; rm -f '$GTP_CONFIG' '$PODBH_CONF'" || true
    if cx "$CTL" test -f /nvram/InterfaceMap.json.before-podbh; then
        cx "$CTL" mv /nvram/InterfaceMap.json.before-podbh /nvram/InterfaceMap.json
        cx "$CTL" systemctl restart onewifi
        sleep 30
        cx "$CTL" systemctl restart em_agent
    fi
    if exists em-gtp && running em-gtp; then
        cx em-gtp sh -c 'systemctl enable -q --now hostapd emosa-gtp 2>/dev/null' || true
    fi
    operator_redirect off
    move_state "$CTL" emosa
    # emosa's fleet and broker are reached through the lab's proxies on the WAN address again
    local emosa_config
    emosa_config=$(emosa_layout emosa | cut -d" " -f1)
    cx emosa cat "$emosa_config" |
        jq --arg wan "$WAN_HOST" '.advertise = $wan | del(.forward)
            | if .telemetry.mode? == "mqtt" then .telemetry.broker = $wan else . end' |
        lxc exec emosa -- sh -c "cat > '$emosa_config.new' && mv '$emosa_config.new' '$emosa_config'"
    [ ! -f "$STATE/telemetry" ] || proxy emosa mqtt 8883
    # the gateway as it was: no configuration (the package inert), no trunk, no agents'
    # namespace (the trunk's end and the agents' interfaces go with it)
    local config agents root run
    read -r config agents root run <<<"$(emosa_layout "$CTL")"
    [ "$run" != - ] || run=
    cx "$CTL" sh -c "rm -rf '$config' '$agents' '$root' ${run:+'$run'}
        . /etc/default/emosa 2>/dev/null; [ -z \"\${EMOSA_BRIDGE:-}\" ] || ip link del \"\${EMOSA_TRUNK:-emlan}\" 2>/dev/null || true
        [ -z \"\${EMOSA_NETNS:-}\" ] || ip netns del \"\$EMOSA_NETNS\" 2>/dev/null || true"
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
