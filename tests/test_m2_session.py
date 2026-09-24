"""Set-level registrar-session rule for one M2 set (per-M2 authentication stubbed)."""

import pytest

from emosa.errors import EmosaError
from emosa.wsc import Attribute
from emosa.wsc_messages import PUBLIC_KEY, AuthenticatedM2Envelope, M1Transcript

pytestmark = pytest.mark.unit


def envelopes(monkeypatch, sessions):
    """Each message authenticates to (registrar nonce, registrar public key)."""
    table = dict(sessions)

    def authenticate(self, message):
        nonce, public = table[message]
        return AuthenticatedM2Envelope(nonce, b"u" * 16, None, (), (Attribute(PUBLIC_KEY, public),))

    monkeypatch.setattr(M1Transcript, "authenticate_m2_envelope", authenticate)
    return tuple(table)


def check(messages, **kwargs):
    transcript = M1Transcript.__new__(M1Transcript)  # per-M2 checks are stubbed
    return transcript.authenticate_m2_envelopes(messages, max_bss=5, **kwargs)


def test_distinct_is_the_default_and_refuses_a_shared_nonce(monkeypatch):
    messages = envelopes(monkeypatch, [(b"m1", (b"n", b"k")), (b"m2", (b"n", b"k"))])
    with pytest.raises(EmosaError):
        check(messages)
    assert len(check(messages, shared_session=True)) == 2


def test_shared_session_needs_one_nonce_one_key_and_distinct_payloads(monkeypatch):
    two_nonces = envelopes(monkeypatch, [(b"a", (b"n1", b"k")), (b"b", (b"n2", b"k"))])
    with pytest.raises(EmosaError):
        check(two_nonces, shared_session=True)
    two_keys = envelopes(monkeypatch, [(b"a", (b"n", b"k1")), (b"b", (b"n", b"k2"))])
    with pytest.raises(EmosaError):
        check(two_keys, shared_session=True)
    envelopes(monkeypatch, [(b"a", (b"n", b"k"))])
    with pytest.raises(EmosaError):
        check((b"a", b"a"), shared_session=True)


def test_distinct_nonces_still_pass_by_default(monkeypatch):
    messages = envelopes(monkeypatch, [(b"a", (b"n1", b"k")), (b"b", (b"n2", b"k"))])
    assert len(check(messages)) == 2


def test_each_bss_of_a_set_gets_its_own_received_credential_reference(tmp_path):
    from emosa.secrets import SecretStore

    vault = SecretStore(tmp_path / "secrets")
    base = "wsc-" + "a" * 32
    for ref in (base, base + "-1", base + "-7"):
        vault.persist_received(ref, "ExtraKey-2026")
        assert vault.resolve(ref) == "ExtraKey-2026"
    for ref in (base + "-8", base + "-10", base + "x"):
        with pytest.raises(EmosaError):
            vault.persist_received(ref, "ExtraKey-2026")
