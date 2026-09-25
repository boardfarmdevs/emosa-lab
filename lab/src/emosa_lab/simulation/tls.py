"""Disposable synthetic trust for TLS experiments; never a physical-pod bootstrap."""

import hashlib
import ipaddress
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def unused_port():
    # Selection is not reservation. Listener startup must still fail on a bind race.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def create_pki(directory, count):
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    now = datetime.now(UTC)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EMOSA synthetic lab CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, None, None),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )

    def write(name, value):
        path = directory / name
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(value)
        return str(path)

    ca = write("ca.pem", authority.public_bytes(serialization.Encoding.PEM))

    def leaf(name):
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(authority_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(True, False, False, False, False, False, False, None, None),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
                ),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
                critical=False,
            )
            .sign(authority_key, hashes.SHA256())
        )
        files = {
            "certificate": write(
                name + "-cert.pem", certificate.public_bytes(serialization.Encoding.PEM)
            ),
            "private_key": write(
                name + "-key.pem",
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            ),
            "ca": ca,
        }
        pin = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
        return files, pin

    server, server_pin = leaf("adapter")
    pods = [leaf(f"pod-{index}") for index in range(1, count + 1)]
    return {
        "server": server,
        "server_pin": server_pin,
        "pods": [p[0] for p in pods],
        "pins": [p[1] for p in pods],
    }


def trust_config(pki, index):
    return {
        "certificate_ref": Path(pki["server"]["certificate"]).name,
        "private_key_ref": Path(pki["server"]["private_key"]).name,
        "ca_ref": Path(pki["server"]["ca"]).name,
        "peer_certificate_sha256": pki["pins"][index],
    }
