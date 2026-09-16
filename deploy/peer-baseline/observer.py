"""Independent client and application observations for the isolated baseline."""

import argparse
import http.client
import json
import os
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path("/opt/emosa-baseline")
CLIENTS = {"em-baseline-wired": ("eth1", "192.0.2.20"), "em-baseline-wifi": ("wlan0", "192.0.2.21")}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/probe":
            self.send_error(404)
            return
        value = json.loads((ROOT / "response.json").read_text())
        value["peer"] = self.client_address[0]
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def prepare(name):
    iface, address = CLIENTS[name]
    if Path("/sys/class/net/eth0").exists():
        raise SystemExit("Setup Ethernet is still present")
    run("ip", "link", "set", iface, "up")
    run("ip", "address", "replace", address + "/24", "dev", iface)
    if name == "em-baseline-wifi":
        unit = "emosa-baseline-client.service"
        if run("systemctl", "show", unit, "-p", "LoadState", "--value").strip() != "not-found":
            raise SystemExit("Collect/stop the previous client before preparing")
        (ROOT / "client.conf").write_text("""ctrl_interface=/var/run/wpa_supplicant
update_config=0
network={
    ssid="emosa-baseline-provisioned"
    psk="EmosaBaseline2026!"
    bssid=02:00:00:ec:02:00
    key_mgmt=WPA-PSK
    proto=RSN
    pairwise=CCMP
    group=CCMP
    scan_freq=2437
}
""")
        run(
            "systemd-run",
            "--quiet",
            "--property=Type=exec",
            "--unit",
            "emosa-baseline-client",
            str(ROOT / "hostap/sbin/wpa_supplicant"),
            "-Dnl80211",
            "-i",
            iface,
            "-c",
            str(ROOT / "client.conf"),
            "-f",
            str(ROOT / "client.log"),
        )
    print(json.dumps({"prepared": name, "interface": iface}))


def observe(name, nonce):
    iface, address = CLIENTS[name]
    result = {
        "client": name,
        "interface": iface,
        "addresses": json.loads(run("ip", "-j", "address", "show", "dev", iface)),
        "routes": json.loads(run("ip", "-j", "route", "show")),
    }
    if name == "em-baseline-wifi":
        status = run(str(ROOT / "hostap/bin/wpa_cli"), "-i", iface, "status")
        values = dict(line.split("=", 1) for line in status.splitlines() if "=" in line)
        result["supplicant"] = values
        result["link"] = run("iw", "dev", iface, "link")
        expected = {
            "wpa_state": "COMPLETED",
            "ssid": "emosa-baseline-provisioned",
            "bssid": "02:00:00:ec:02:00",
            "key_mgmt": "WPA2-PSK",
        }
        if any(values.get(key) != value for key, value in expected.items()):
            raise SystemExit(
                json.dumps({"failed": "station authentication", "observation": result})
            )
    result["ping"] = run("ping", "-n", "-I", iface, "-c", "3", "-W", "2", "192.0.2.1")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode() + b"\0")
        connection.bind((address, 0))
        connection.connect(("192.0.2.1", 8080))
        connection.sendall(b"GET /probe HTTP/1.1\r\nHost: baseline\r\nConnection: close\r\n\r\n")
        response = http.client.HTTPResponse(connection)
        response.begin()
        if response.status != 200:
            raise SystemExit("Application endpoint returned a failure")
        payload = json.loads(response.read(8192))
    if payload.get("nonce") != nonce or payload.get("peer") != address:
        raise SystemExit("Application response does not match this client/attempt")
    result["application"] = payload
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "prepare", "observe"))
    parser.add_argument("--nonce")
    args = parser.parse_args()
    name = socket.gethostname()
    if os.geteuid() != 0 or run("systemd-detect-virt", "--container").strip() != "lxc":
        raise SystemExit("Run only in the owned baseline containers")
    if args.action == "serve":
        if name != "em-baseline-controller":
            raise SystemExit("Application endpoint belongs on the controller side")
        HTTPServer(("192.0.2.1", 8080), Handler).serve_forever()
    elif name not in CLIENTS:
        raise SystemExit("Expected an owned baseline client")
    elif args.action == "prepare":
        prepare(name)
    elif not args.nonce:
        raise SystemExit("A fresh expected application nonce is required")
    else:
        observe(name, args.nonce)


if __name__ == "__main__":
    main()
