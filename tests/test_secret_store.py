"""The secret store: its policy over any backend (spec §6), the files backend's privacy."""

import os

import pytest

from emosa.errors import EmosaError, Reason
from emosa.secrets import FileSecrets, SecretStore

pytestmark = pytest.mark.unit

RECEIVED = "wsc-0123456789abcdef0123456789abcdef-1"


class MemorySecrets:
    """Another backend, as a platform's secure storage would be."""

    name = "memory"

    def __init__(self, **values):
        self.values = {k: v.encode() for k, v in values.items()}
        self.calls = []

    def read(self, ref):
        self.calls.append(("read", ref))
        if ref not in self.values:
            raise EmosaError(Reason.MISSING_PREREQUISITE, "secret reference unavailable")
        return self.values[ref]

    def create(self, ref, value, *, durable=True):
        self.calls.append(("create", ref))
        if ref in self.values:
            raise FileExistsError(ref)
        self.values[ref] = value

    def remove(self, ref):
        self.calls.append(("remove", ref))
        self.values.pop(ref, None)

    def key(self):
        return bytes(range(32))


def test_the_policy_is_the_stores_over_another_backend():
    backend = MemorySecrets(backhaul="short")
    store = SecretStore(backend)
    assert store.directory is None
    with pytest.raises(EmosaError) as short:
        store.resolve("backhaul")
    assert short.value.code == Reason.INVALID_INPUT
    with pytest.raises(EmosaError):
        store.resolve("../backhaul")
    assert ("read", "../backhaul") not in backend.calls  # a path never reaches the backend
    store.persist_received(RECEIVED, "correct horse")
    with pytest.raises(FileExistsError):
        store.persist_received(RECEIVED, "correct horse")  # never overwritten
    with pytest.raises(EmosaError):
        store.persist_received(RECEIVED.replace("-1", "-9"), "correct horse")
    assert store.resolve(RECEIVED) == "correct horse"
    store.forget("../x")
    assert ("remove", "../x") not in backend.calls
    store.forget(RECEIVED)
    with pytest.raises(EmosaError) as gone:
        store.resolve(RECEIVED)
    assert gone.value.code == Reason.MISSING_PREREQUISITE


def test_the_fingerprint_is_keyed_by_the_backends_key(tmp_path):
    memory = SecretStore(MemorySecrets())
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    (directory / ".fingerprint-key").write_bytes(bytes(range(32)))
    (directory / ".fingerprint-key").chmod(0o600)
    files = SecretStore(directory)
    # the same key, the same fingerprint (the C's unit check has this value too)
    expected = "84ed201cb170309858f8577bb1c56d69f0831433f99150154798726621060576"
    assert memory.fingerprint("secret-pass") == files.fingerprint("secret-pass") == expected


def test_the_files_backend_keeps_private_files_only(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    assert isinstance(store.backend, FileSecrets) and store.directory == tmp_path / "secrets"
    store.persist_received(RECEIVED, "correct horse")
    assert (store.directory / RECEIVED).stat().st_mode & 0o777 == 0o600
    store.write_simulated("lab", "lab-passphrase")
    os.chmod(store.directory / "lab", 0o644)
    with pytest.raises(EmosaError) as shared:
        store.resolve("lab")  # readable by others: not a secret any more
    assert shared.value.code == Reason.INVALID_INPUT
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)  # explicitly: mkdir's mode is masked by the umask
    with pytest.raises(EmosaError):
        SecretStore(open_dir)
