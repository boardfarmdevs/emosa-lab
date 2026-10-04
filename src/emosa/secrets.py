# SPDX-License-Identifier: Apache-2.0
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path

from emosa.errors import EmosaError, Reason

SENSITIVE = {"psk", "password", "wpa_psks", "security", "private_key", "credential_fingerprint"}


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k in SENSITIVE else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class FileSecrets:
    """The files backend: one private file per reference in a 0700 directory, and the
    fingerprint key beside them. A platform's secure storage implements the same four
    methods instead (spec §6); the policy stays SecretStore's."""

    name = "files"

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_mode & 0o077:
            raise EmosaError(Reason.INVALID_INPUT, "secret directory must have mode 0700")

    def _read(self, path: Path) -> bytes:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                s = os.fstat(f.fileno())
                if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
                    raise EmosaError(Reason.INVALID_INPUT, "secret must be an owned private file")
                data = f.read(4097)
                if len(data) > 4096:
                    raise EmosaError(Reason.INVALID_INPUT, "secret file exceeds limit")
                return data
        except OSError as exc:
            raise EmosaError(Reason.MISSING_PREREQUISITE, "secret reference unavailable") from exc

    def read(self, ref: str) -> bytes:
        """The stored value; EmosaError when there is none or it is not kept privately."""
        return self._read(self.directory / ref)

    def create(self, ref: str, value: bytes, *, durable=True):
        """A new reference, never replacing one; flushed with its directory when durable."""
        path = self.directory / ref
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
            if durable:
                parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def remove(self, ref: str):
        (self.directory / ref).unlink(missing_ok=True)

    def key(self) -> bytes:
        """The 32-byte fingerprint key, created on first use."""
        key_path = self.directory / ".fingerprint-key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
                f.flush()
                os.fsync(f.fileno())
        return self._read(key_path)


REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
RECEIVED = re.compile(r"wsc-[a-f0-9]{32}(-[1-7])?")


class SecretStore:
    """Secrets by reference, kept by a backend (FileSecrets for a directory). The policy
    is the store's: references never contain paths or credential values, a usable
    passphrase, a received credential never overwritten."""

    def __init__(self, backend):
        self.backend = backend if hasattr(backend, "read") else FileSecrets(Path(backend))
        self.key = self.backend.key()
        if len(self.key) != 32:
            raise EmosaError(Reason.INVALID_INPUT, "invalid fingerprint key")

    @property
    def directory(self):
        """The files backend's directory (None for another backend)."""
        return getattr(self.backend, "directory", None)

    def resolve(self, ref: str) -> str:
        if not REFERENCE.fullmatch(ref):
            raise EmosaError(Reason.INVALID_INPUT, "invalid secret reference")
        try:
            value = self.backend.read(ref).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EmosaError(Reason.INVALID_INPUT, "secret must be UTF-8") from exc
        if not 8 <= len(value.encode()) <= 63 or not value.isascii() or not value.isprintable():
            raise EmosaError(Reason.INVALID_INPUT, "qualified PSK is 8–63 printable ASCII bytes")
        return value

    def fingerprint(self, value) -> str:
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.key, data, hashlib.sha256).hexdigest()

    def write_simulated(self, ref: str, value: str):
        """Test-resource setup only; callers must not use this for hardware provisioning."""
        if not REFERENCE.fullmatch(ref):
            raise ValueError("invalid simulated secret reference")
        self.backend.create(ref, value.encode(), durable=False)

    def persist_received(self, ref: str, value: str):
        """Persist a received component credential before its journal reference.

        No overwrite, durable before it returns. This is not an authorization API; only
        the isolated WSC lab currently calls it.
        """
        # One reference per BSS of a received M2 set: wsc-<exchange>[-1..7]
        if not RECEIVED.fullmatch(ref) or (
            not isinstance(value, str)
            or not 8 <= len(value) <= 63
            or not value.isascii()
            or not value.isprintable()
        ):
            raise EmosaError(Reason.INVALID_INPUT, "invalid received credential reference/value")
        self.backend.create(ref, value.encode())

    def forget(self, ref: str):
        """Removes a reference (a credential whose operation was never journaled)."""
        if REFERENCE.fullmatch(ref):
            self.backend.remove(ref)
