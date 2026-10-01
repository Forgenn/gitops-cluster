"""Hermetic tests for mint_token.py: no network, no real key. Run: python -m pytest -q"""
import os
import stat

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import mint_token as m


@pytest.fixture
def key_pem(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    p = tmp_path / "key.pem"
    p.write_bytes(pem)
    return p, key.public_key()


def test_app_jwt_is_rs256_with_short_expiry(key_pem):
    path, pub = key_pem
    token = m.app_jwt("12345", path.read_bytes(), now=1_000_000)
    claims = jwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] == 1_000_000 - 60
    assert claims["exp"] - claims["iat"] <= 600


@pytest.mark.skipif(not hasattr(os, "getgid"), reason="POSIX only")
def test_write_files_is_atomic_and_group_readable(tmp_path):
    m.write_files(tmp_path, {"token": "ghs_example", "expires_at": "2026-09-29T12:00:00Z"}, gid=os.getgid())
    tok = tmp_path / "token"
    assert tok.read_text() == "ghs_example"
    assert (tmp_path / "expires_at").read_text() == "2026-09-29T12:00:00Z"
    assert stat.S_IMODE(tok.stat().st_mode) == 0o440
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(not hasattr(os, "getgid"), reason="POSIX only")
def test_write_files_survives_leftover_tmp(tmp_path):
    # A crash between write and rename leaves a read-only token.tmp behind.
    left = tmp_path / "token.tmp"
    left.write_text("old")
    left.chmod(0o440)
    m.write_files(tmp_path, {"token": "ghs_new"}, gid=os.getgid())
    assert (tmp_path / "token").read_text() == "ghs_new"


def test_mint_calls_installation_endpoint(key_pem, monkeypatch):
    path, _ = key_pem
    seen = {}

    def fake_post(url, headers):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return {"token": "ghs_new", "expires_at": "2026-09-29T13:00:00Z"}

    monkeypatch.setattr(m, "_post_json", fake_post)
    tok, exp = m.mint("12345", "678", path.read_bytes())
    assert seen["url"] == "https://api.github.com/app/installations/678/access_tokens"
    assert seen["auth"].startswith("Bearer ")
    assert (tok, exp) == ("ghs_new", "2026-09-29T13:00:00Z")


def test_installed_repos_follows_pagination(monkeypatch):
    pages = {
        1: {"total_count": 3, "repositories": [{"full_name": "Forgenn/plder"}, {"full_name": "Forgenn/Loomie"}]},
        2: {"total_count": 3, "repositories": [{"full_name": "Forgenn/gitops-cluster"}]},
    }
    seen = []

    def fake_get(url, headers):
        seen.append(headers["Authorization"])
        page = int(url.rsplit("page=", 1)[1])
        return pages.get(page, {"total_count": 3, "repositories": []})

    monkeypatch.setattr(m, "_get_json", fake_get)
    monkeypatch.setattr(m, "PER_PAGE", 2)
    assert m.installed_repos("ghs_t") == ["forgenn/gitops-cluster", "forgenn/loomie", "forgenn/plder"]
    assert seen[0] == "token ghs_t"
