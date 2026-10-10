# SPDX-License-Identifier: Apache-2.0
"""deploy/bench-ca/bench-ca.sh: the bench CA's key stays home, keys are made where they are used,
and what it signs is what a pod and a broker need (mutual TLS for the pods' statistics)."""

import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl"),
]

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "bench-ca" / "bench-ca.sh"
HOST = socket.gethostname().split(".")[0]


def run(tmp_path, *args, host=HOST, check=True):
    env = {**os.environ, "BENCH_CA_DIR": str(tmp_path / "ca"), "BENCH_CA_HOST": host}
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def request(tmp_path, name, subject, key="rsa:2048"):
    """A key and its request, as a pod or a router makes them (the key never leaves)."""
    key_file, csr = tmp_path / f"{name}.key", tmp_path / f"{name}.csr"
    if key.startswith("ec:"):
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                f"ec_paramgen_curve:{key[3:]}",
                "-out",
                str(key_file),
            ],
            check=True,
            capture_output=True,
        )
        new_key = ["-key", str(key_file)]
    else:
        new_key = ["-newkey", key, "-nodes", "-keyout", str(key_file)]
    subprocess.run(
        ["openssl", "req", "-new", *new_key, "-subj", subject, "-out", str(csr)],
        check=True,
        capture_output=True,
    )
    return csr


def text(cert):
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def verify(tmp_path, cert, purpose):
    return subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(tmp_path / "ca" / "ca.pem"),
            "-purpose",
            purpose,
            str(cert),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_ca_is_made_once_and_its_key_kept_private(tmp_path):
    run(tmp_path, "init")
    key = tmp_path / "ca" / "ca.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ca").stat().st_mode) == 0o700
    assert "CA:TRUE" in text(tmp_path / "ca" / "ca.pem")
    again = run(tmp_path, "init", check=False)
    assert again.returncode != 0 and "already holds a CA" in again.stderr
    assert "BEGIN CERTIFICATE" in run(tmp_path, "ca").stdout
    assert "PRIVATE KEY" not in run(tmp_path, "ca").stdout


def test_it_runs_on_the_ca_host_only(tmp_path):
    other = run(tmp_path, "init", host="not-" + HOST, check=False)
    assert other.returncode != 0 and "the CA key stays there" in other.stderr
    assert not (tmp_path / "ca").exists()
    unset = subprocess.run(
        ["bash", str(SCRIPT), "init"],
        env={k: v for k, v in os.environ.items() if k != "BENCH_CA_HOST"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert unset.returncode != 0 and "BENCH_CA_HOST" in unset.stderr


def test_a_pod_gets_a_client_certificate_named_by_its_serial(tmp_path):
    run(tmp_path, "init")
    csr = request(tmp_path, "pod", "/CN=MVXPOD02D7777EF0D9")
    run(tmp_path, "sign-pod", str(csr), str(tmp_path / "client.pem"))
    cert = text(tmp_path / "client.pem")
    assert "Subject: CN = MVXPOD02D7777EF0D9" in cert and "TLS Web Client Authentication" in cert
    assert "TLS Web Server Authentication" not in cert and "CA:FALSE" in cert
    assert verify(tmp_path, tmp_path / "client.pem", "sslclient").returncode == 0
    assert verify(tmp_path, tmp_path / "client.pem", "sslserver").returncode != 0
    assert (
        "pod" in run(tmp_path, "list").stdout
        and "MVXPOD02D7777EF0D9" in run(tmp_path, "list").stdout
    )


def test_the_broker_gets_a_server_certificate_for_its_addresses(tmp_path):
    run(tmp_path, "init")
    csr = request(tmp_path, "broker", "/CN=10.0.0.1", key="ec:P-256")
    run(tmp_path, "sign-broker", str(csr), str(tmp_path / "broker.pem"), "10.0.0.1", "router.lan")
    cert = text(tmp_path / "broker.pem")
    assert "IP Address:10.0.0.1" in cert and "DNS:router.lan" in cert
    assert "TLS Web Server Authentication" in cert
    assert verify(tmp_path, tmp_path / "broker.pem", "sslserver").returncode == 0


@pytest.mark.parametrize(
    "subject,key,why",
    [
        ("/CN=MVXPOD01/O=lab", "rsa:2048", "CN=<the pod's serial> alone"),
        ("/CN=.pod", "rsa:2048", "CN=<the pod's serial> alone"),  # not a serial: a file name's
        ("/CN=MVXPOD01", "rsa:1024", "at least 2048"),
        ("/CN=MVXPOD01", "ec:secp256k1", "P-256 or P-384"),
    ],
)
def test_a_pod_request_it_cannot_vouch_for_is_refused(tmp_path, subject, key, why):
    run(tmp_path, "init")
    csr = request(tmp_path, "pod", subject, key=key)
    refused = run(tmp_path, "sign-pod", str(csr), str(tmp_path / "client.pem"), check=False)
    assert refused.returncode != 0 and why in refused.stderr
    assert not (tmp_path / "client.pem").exists()


def test_nothing_is_overwritten_and_an_address_must_be_one(tmp_path):
    run(tmp_path, "init")
    csr = request(tmp_path, "pod", "/CN=MVXPOD01")
    run(tmp_path, "sign-pod", str(csr), str(tmp_path / "client.pem"))
    again = run(tmp_path, "sign-pod", str(csr), str(tmp_path / "client.pem"), check=False)
    assert again.returncode != 0 and "exists" in again.stderr
    broker = request(tmp_path, "broker", "/CN=broker")
    bad = run(
        tmp_path, "sign-broker", str(broker), str(tmp_path / "b.pem"), "10.0.0.1;x", check=False
    )
    assert bad.returncode != 0 and "neither an IPv4 address nor a DNS name" in bad.stderr
