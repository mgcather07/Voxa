"""Authentication — password hashing and the LDAP+local break-glass chain.
Directory access is stubbed; these lock the invariants that keep an AD outage
from locking admins out and a directory user from being bypassed locally.
"""

from app import auth


class TestPasswordHash:
    def test_roundtrip(self):
        h = auth.hash_password("correct horse battery")
        assert auth.verify_password("correct horse battery", h) is True

    def test_wrong_password_fails(self):
        h = auth.hash_password("secret1")
        assert auth.verify_password("secret2", h) is False

    def test_empty_or_none_stored_fails(self):
        assert auth.verify_password("anything", None) is False
        assert auth.verify_password("anything", "") is False

    def test_scheme_is_scrypt_with_salt(self):
        a = auth.hash_password("same")
        b = auth.hash_password("same")
        # Random salt -> two hashes of the same password differ.
        assert a != b
        assert a.startswith("scrypt$")


class _StubLocal:
    """Local backend returning a preset user for one username/password."""

    def __init__(self, user=None, want=("admin", "pw")):
        self.user, self.want = user, want

    def authenticate(self, session, username, password):
        return self.user if (username, password) == self.want else None


class _StubLdap:
    def __init__(self, user=None, raises=False):
        self.user, self.raises = user, raises
        self.called = False

    def authenticate(self, session, username, password):
        self.called = True
        if self.raises:
            raise NotImplementedError("directory unreachable — break-glass")
        return self.user


class TestChainedBackend:
    def test_local_checked_first(self):
        marker = object()
        chain = auth.ChainedAuthBackend(_StubLocal(user=marker), _StubLdap())
        assert chain.authenticate(None, "admin", "pw") is marker

    def test_falls_through_to_ldap(self):
        marker = object()
        ldap = _StubLdap(user=marker)
        chain = auth.ChainedAuthBackend(_StubLocal(user=None), ldap)
        assert chain.authenticate(None, "jsmith", "adpw") is marker
        assert ldap.called is True

    def test_break_glass_local_wins_even_if_ldap_would_error(self):
        marker = object()
        ldap = _StubLdap(raises=True)  # DC down
        chain = auth.ChainedAuthBackend(
            _StubLocal(user=marker, want=("breakglass", "bg")), ldap
        )
        # Local match returns before LDAP is ever consulted.
        assert chain.authenticate(None, "breakglass", "bg") is marker
        assert ldap.called is False

    def test_ldap_error_propagates_when_no_local_match(self):
        ldap = _StubLdap(raises=True)
        chain = auth.ChainedAuthBackend(_StubLocal(user=None), ldap)
        try:
            chain.authenticate(None, "jsmith", "adpw")
            assert False, "should have raised"
        except NotImplementedError as e:
            assert "break-glass" in str(e)
