#!/bin/bash
# Inside the em-ctl container: the native prplMesh controller with its colocated
# agent, as in the peer baseline (deploy/peer-baseline/node.py, ordinal 1), wired
# to the EasyMesh bridge on eth1. EMOSA's virtual agents sit on the same bridge.
#
#   em-ctl-node.sh prepare            platform db, br-lan (eth1), hostapd config
#   em-ctl-node.sh start              hostapd, ubusd, ieee1905_transport, controller, agent
#   em-ctl-node.sh policy SSID KEY AL...   fronthaul SSID/KEY for the gateway agent and each AL
#   em-ctl-node.sh stop
#   em-ctl-node.sh topology [DEPTH]   the controller's own Device.WiFi.DataElements.Network
set -euo pipefail
INSTALL=/opt/prpl-install-nl80211
ROOT=/opt/emosa-baseline
HOSTAP=$ROOT/hostap
AL=02:00:00:e0:00:01
RADIO=02:00:00:ec:01:00
UNITS=(hostap bus transport controller agent)

conf() {        # conf FILE KEY VALUE: replace the single KEY= line
    local n; n=$(grep -c "^$2=" "$1" || true)
    [ "$n" = 1 ] || { echo "expected one $2= in $1" >&2; exit 1; }
    sed -i "s|^$2=.*|$2=$3|" "$1"
}

prepare() {
    for u in "${UNITS[@]}"; do
        systemctl is-active -q "em-$u" && { echo "stop first: em-$u active" >&2; exit 1; }
        systemctl reset-failed "em-$u" 2>/dev/null || true
    done
    tar -C /opt -xzf /opt/prpl-install-nl80211-6.0.0.tar.gz \
        prpl-install-nl80211/config/beerocks_agent.conf prpl-install-nl80211/config/beerocks_controller.conf \
        prpl-install-nl80211/share/prplmesh_platform_db
    rm -rf /tmp/beerocks; mkdir -p /tmp/beerocks /var/run/hostapd /var/run/wpa_supplicant /var/run/ubus
    cp $INSTALL/share/prplmesh_platform_db /tmp/beerocks/prplmesh_platform_db
    conf /tmp/beerocks/prplmesh_platform_db management_mode Multi-AP-Controller-and-Agent
    conf /tmp/beerocks/prplmesh_platform_db certification_mode 0
    conf /tmp/beerocks/prplmesh_platform_db backhaul_wire_iface eth1
    conf $INSTALL/config/beerocks_agent.conf ucc_listener_port 0
    conf $INSTALL/config/beerocks_controller.conf ucc_listener_port 0
    # Create the bridge with its address: an address set afterwards races with
    # udev's MACAddressPolicy, which can overwrite it (and so the AL identity).
    ip link del br-lan 2>/dev/null || true
    ip link add br-lan address $AL type bridge
    ip address flush dev eth1
    ip link set eth1 master br-lan
    ip link set eth1 up
    ip link set br-lan up
    ip address replace 192.0.2.1/24 dev br-lan
    sleep 1
    [ "$(cat /sys/class/net/br-lan/address)" = $AL ] || { echo "br-lan is not $AL" >&2; exit 1; }
    readlink -f /sys/class/net/wlan0/phy80211/device | grep -q mac80211_hwsim || { echo "wlan0 is not hwsim" >&2; exit 1; }
    ip link set wlan0 down
    ip link set wlan0 address $RADIO
    cat > /var/run/hostapd-phy0.conf <<EOF
driver=nl80211
hw_mode=g
channel=6
ieee80211n=1
interface=wlan0
ctrl_interface=/var/run/hostapd
bridge=br-lan
bssid=$RADIO
ssid=emosa-unprovisioned-1
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_passphrase=UnprovisionedGateway2026!
rsn_pairwise=CCMP
multi_ap=2
wps_state=2
eap_server=1
config_methods=push_button
device_name=EMOSA-gateway
device_type=6-0050F204-1
manufacturer=EMOSA-Lab
model_name=prplMesh-hwsim-gateway
model_number=1
serial_number=em-ctl-1
os_version=01020300
ap_setup_locked=0
uuid=00000000-0000-4000-8000-000000000001
bss=wlan0.0
ctrl_interface=/var/run/hostapd
bridge=br-lan
bssid=02:00:00:ec:01:01
ssid=emosa-unprovisioned-backhaul-1
wpa=2
wpa_key_mgmt=WPA-PSK
wpa_passphrase=UnprovisionedGateway2026!
rsn_pairwise=CCMP
multi_ap=1
EOF
    # 1905 multicast is not forwarded between bridge ports (as in the baseline)
    ebtables -C FORWARD -d 01:80:c2:00:00:13 -j DROP 2>/dev/null || ebtables -A FORWARD -d 01:80:c2:00:00:13 -j DROP
    echo "prepared: controller+agent AL $AL, br-lan=eth1, radio $RADIO"
}

launch() {      # launch UNIT CMD...
    local u=$1; shift
    systemctl reset-failed "em-$u" 2>/dev/null || true
    systemd-run --quiet --property=Type=exec --property=TimeoutStopSec=20 --unit "em-$u" "$@"
}

start() {
    launch hostap $HOSTAP/sbin/hostapd -g /var/run/hostapd/global -f $ROOT/hostapd.log /var/run/hostapd-phy0.conf
    launch bus /usr/sbin/ubusd
    for _ in $(seq 100); do [ -S /var/run/ubus/ubus.sock ] && break; sleep 0.1; done
    [ -S /var/run/ubus/ubus.sock ] || { echo "ubus socket missing" >&2; exit 1; }
    launch transport $INSTALL/bin/ieee1905_transport
    launch controller $INSTALL/bin/beerocks_controller
    sleep 2
    launch agent $INSTALL/bin/beerocks_agent
    echo "started: ${UNITS[*]}"
}

stop() {
    local u
    for ((i=${#UNITS[@]}-1; i>=0; i--)); do
        u=${UNITS[$i]}
        systemctl stop "em-$u" 2>/dev/null || true
    done
    systemctl list-units --no-legend 'em-*' || true
}

bml() {
    local out; out=$($INSTALL/bin/beerocks_cli -c "$*")
    grep -q "return value is: BML_RET_OK, Success status" <<<"$out" || { echo "BML failed: $*: $out" >&2; exit 1; }
}

policy() {      # policy SSID KEY AL...
    local ssid=$1 key=$2 al; shift 2
    bml bml_clear_wifi_credentials $AL
    bml bml_set_wifi_credentials $AL "$ssid" "$key" 24g-5g fronthaul 0
    # The network's backhaul is "$ssid-bh". This node's own radio is on the 1905-only
    # LAN (no router, no DHCP), so its backhaul BSS would strand a pod's backhaul
    # station: em-gtp serves "$ssid-bh" into the gateway LAN instead (vm/lab.sh gtp).
    if [ "${EM_GATEWAY_BACKHAUL:-0}" = 1 ]; then
        bml bml_set_wifi_credentials $AL "$ssid-bh" "$key" 24g-5g backhaul 0
    fi
    for al in "$@"; do
        bml bml_clear_wifi_credentials "$al"
        bml bml_set_wifi_credentials "$al" "$ssid" "$key" 24g-5g fronthaul 0
    done
    bml bml_update_wifi_credentials
    echo "policy: fronthaul '$ssid' for gateway $AL${*:+ and $*}"
}

topology() {
    ubus call Device.WiFi.DataElements.Network _get "{\"rel_path\":\"\",\"depth\":${1:-6}}"
}

case ${1:-} in
    prepare) prepare ;;
    start) start ;;
    stop) stop ;;
    policy) shift; policy "$@" ;;
    topology) shift; topology "$@" ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
