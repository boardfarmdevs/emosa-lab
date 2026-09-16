"""Independent interface-bound client probes; no OVSDB or adapter imports."""

import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path("/opt/emosa-radio-manager")
HOSTAP = Path("/opt/emosa-baseline/hostap")
CLIENTS = {"em-baseline-wired": ("eth1", "192.0.2.20"), "em-baseline-wifi": ("wlan0", "192.0.2.21")}


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("client command failed")
    return result.stdout


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/probe":
            self.send_error(404)
            return
        result = json.loads((ROOT / "response.json").read_text())
        result["peer"] = self.client_address[0]
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def connect():
    desired = json.load(sys.stdin)
    if socket.gethostname() != "em-baseline-wifi":
        raise RuntimeError("Expected the wireless client")
    # wpa_supplicant's quoted PSK parser does not unescape JSON strings.
    # Derive its supported raw PSK with the standard-library PBKDF2 primitive.
    key = desired["passphrase"]
    if not 8 <= len(key) <= 63 or any(not 32 <= ord(c) <= 126 for c in key):
        raise ValueError("unsupported lab passphrase")
    unit = "emosa-radio-manager-client.service"
    if run("systemctl", "show", unit, "-p", "LoadState", "--value").strip() != "not-found":
        run("systemctl", "stop", unit)
    subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    path = ROOT / "client.conf"
    psk = hashlib.pbkdf2_hmac("sha1", key.encode("ascii"), desired["ssid"].encode(), 4096, 32).hex()
    path.write_text(f"""ctrl_interface={ROOT}/ctrl
update_config=0
network={{
    ssid={desired["ssid"].encode().hex()}
    psk={psk}
    bssid=02:00:00:ec:02:00
    key_mgmt=WPA-PSK
    proto=RSN
    pairwise=CCMP
    group=CCMP
    scan_freq=2437
}}
""")
    path.chmod(0o600)
    run(
        "systemd-run",
        "--quiet",
        "--property=Type=exec",
        "--unit",
        unit,
        str(HOSTAP / "sbin/wpa_supplicant"),
        "-Dnl80211",
        "-i",
        "wlan0",
        "-c",
        str(path),
        "-f",
        str(ROOT / "client.log"),
    )


def probe(nonce, ssid):
    iface, address = CLIENTS[socket.gethostname()]
    value = {
        "client": socket.gethostname(),
        "interface": iface,
        "routes": json.loads(run("ip", "-j", "route", "show")),
    }
    if iface == "wlan0":
        raw = run(str(HOSTAP / "bin/wpa_cli"), "-p", str(ROOT / "ctrl"), "-i", iface, "status")
        status = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        expected = {
            "wpa_state": "COMPLETED",
            "ssid": ssid,
            "bssid": "02:00:00:ec:02:00",
            "key_mgmt": "WPA2-PSK",
            "pairwise_cipher": "CCMP",
        }
        if any(status.get(k) != v for k, v in expected.items()):
            raise RuntimeError("wireless authentication incomplete")
        value["supplicant"] = status
        value["link"] = run("iw", "dev", iface, "link")
    value["ping"] = run("ping", "-n", "-I", iface, "-c", "2", "-W", "1", "192.0.2.1")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b"\0")
        connection.bind((address, 0))
        connection.connect(("192.0.2.1", 8080))
        connection.sendall(b"GET /probe HTTP/1.1\r\nHost: lab\r\nConnection: close\r\n\r\n")
        response = http.client.HTTPResponse(connection)
        response.begin()
        if response.status != 200:
            raise RuntimeError("application request failed")
        result = json.loads(response.read(8192))
    if result.get("nonce") != nonce or result.get("peer") != address:
        raise RuntimeError("application observation does not match this attempt/client")
    value["application"] = result
    print(json.dumps(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "connect", "probe"))
    parser.add_argument("--nonce")
    parser.add_argument("--ssid")
    args = parser.parse_args()
    try:
        name = socket.gethostname()
        if os.geteuid() != 0 or name not in {*CLIENTS, "em-baseline-controller"}:
            raise RuntimeError("Expected an owned observer container")
        if Path("/sys/class/net/eth0").exists():
            raise RuntimeError("Setup Ethernet still present")
        if args.action == "serve":
            if name != "em-baseline-controller":
                raise RuntimeError("Endpoint belongs behind wired backhaul")
            HTTPServer(("192.0.2.1", 8080), Handler).serve_forever()
        elif args.action == "connect":
            connect()
        else:
            probe(args.nonce, args.ssid)
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
