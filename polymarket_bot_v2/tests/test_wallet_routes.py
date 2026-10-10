import scripts.web_dashboard as wd
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient


def _auth_client(monkeypatch):
    monkeypatch.setattr(wd, "DASHBOARD_KEY", "k")
    monkeypatch.setattr(wd, "TOTP_SECRET", "")
    return TestClient(wd.app)


def test_wallet_bind_challenge_verify_status(monkeypatch):
    c = _auth_client(monkeypatch)
    acct = Account.create()
    addr = acct.address.lower()

    r1 = c.post(f"/api/wallet/challenge?key=k", json={"address": addr})
    assert r1.status_code == 200
    payload = r1.json()
    assert payload["status"] == "ok"
    assert payload["address"] == addr

    msg = payload["message"]
    sig = Account.sign_message(encode_defunct(text=msg), acct.key).signature.hex()

    r2 = c.post(
        f"/api/wallet/verify?key=k",
        json={"address": addr, "signature": sig},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "ok"

    r3 = c.get(f"/api/wallet/status?key=k")
    assert r3.status_code == 200
    data = r3.json()
    assert data["status"] == "ok"
    assert data["count"] >= 1
    assert any(w["wallet_address"] == addr for w in data["wallets"])


def test_wallet_verify_rejects_mismatch_signature(monkeypatch):
    c = _auth_client(monkeypatch)
    acct1 = Account.create()
    acct2 = Account.create()

    addr = acct1.address.lower()
    r1 = c.post(f"/api/wallet/challenge?key=k", json={"address": addr})
    assert r1.status_code == 200
    msg = r1.json()["message"]

    wrong_sig = Account.sign_message(
        encode_defunct(text=msg), acct2.key
    ).signature.hex()
    r2 = c.post(
        f"/api/wallet/verify?key=k",
        json={"address": addr, "signature": wrong_sig},
    )
    assert r2.status_code == 401
