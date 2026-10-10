from __future__ import annotations

import asyncio
import json

from eth_abi import decode

from scripts.dashboard_routes import redeem_withdraw_routes as routes


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def test_build_neg_risk_redeem_calldata_encodes_selected_outcome() -> None:
    condition_id = "0x" + "11" * 32
    calldata = routes._build_neg_risk_redeem_calldata(condition_id, 1, 12.5)

    assert calldata.startswith("0x")
    assert calldata[2:10] == routes._keccak4("redeemPositions(bytes32,uint256[])").hex()

    decoded_condition_id, decoded_amounts = decode(
        ["bytes32", "uint256[]"],
        bytes.fromhex(calldata[10:]),
    )
    assert decoded_condition_id == bytes.fromhex("11" * 32)
    assert list(decoded_amounts) == [0, 12_500_000]


def test_safe_withdraw_official_mode_builds_direct_transfer(monkeypatch) -> None:
    request = _FakeRequest(
        {
            "to_address": "0x1111111111111111111111111111111111111111",
            "amount_usdc": 12.5,
            "token": "usdc",
        }
    )

    monkeypatch.setattr(routes, "get_request_identity", lambda _request: {
        "authenticated": True,
        "wallet_address": "0x2222222222222222222222222222222222222222",
    })
    monkeypatch.setattr(routes, "_official_tx_mode", lambda: True)
    monkeypatch.setattr(routes, "_extract_relayer_payload", lambda _body: None)
    monkeypatch.setattr(routes, "_get_user_signer_binding", lambda _wallet: {"privy_embedded_wallet_id": "wid-1", "main_wallet": "0x2222222222222222222222222222222222222222", "privy_embedded_wallet_address": "0x2222222222222222222222222222222222222222", "privy_signer_status": "enrolled", "proxy_wallet": "0x3333333333333333333333333333333333333333"})
    monkeypatch.setattr(routes, "_log_signer_debug", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_extract_request_user_jwts", lambda _request: ["jwt-token"])
    monkeypatch.setattr(routes, "_insert_vault_tx", lambda **_kwargs: None)

    captured: dict[str, str] = {}

    def _fake_build_server_side_relayer_payload(caller_wallet: str, to: str, data_hex: str, user_jwt):
        captured["caller_wallet"] = caller_wallet
        captured["to"] = to
        captured["data_hex"] = data_hex
        captured["user_jwt"] = json.dumps(user_jwt)
        return {"from": caller_wallet, "to": to, "data": data_hex}

    monkeypatch.setattr(routes, "_build_server_side_relayer_payload", _fake_build_server_side_relayer_payload)
    monkeypatch.setattr(
        routes,
        "_relayer_submit",
        lambda _payload: {"transactionHash": "0xtx", "transactionID": "tx-1", "state": "submitted"},
    )

    response = asyncio.run(routes.api_pm_safe_withdraw(request))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["success"] is True
    assert captured["caller_wallet"] == "0x2222222222222222222222222222222222222222"
    # T471: token=usdc now silently routes to the active trading collateral
    # (pUSD post-2026-04-28 cutover). USDC.e is only used pre-cutover when
    # PM_USE_V2=0; legacy alias 'usdc_e' still works.
    assert captured["to"].lower() == routes._PUSD_TOKEN.lower()
    recipient, amount = decode(["address", "uint256"], bytes.fromhex(captured["data_hex"][10:]))
    assert recipient == "0x1111111111111111111111111111111111111111"
    assert amount == 12_500_000


def test_privy_create_embedded_wallet_includes_additional_signers(monkeypatch) -> None:
    monkeypatch.setattr(routes, "_load_privy_app_credentials", lambda: ("app-id", ["app-secret"]))

    captured: dict[str, object] = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {
                "id": "wallet-123",
                "address": "0xABCDEF",
                "additional_signers": [{"signer_id": "quorum-1"}],
            }

    def _fake_post(url: str, *, json: dict[str, object], auth, headers, timeout: int):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = auth
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(routes.requests, "post", _fake_post)

    wallet_id, wallet_address, signer_ids = routes._privy_create_embedded_wallet(
        "user-123",
        additional_signers=[{"signer_id": "quorum-1", "override_policy_ids": ["policy-1"]}],
    )

    assert wallet_id == "wallet-123"
    assert wallet_address == "0xabcdef"
    assert signer_ids == ["quorum-1"]
    assert captured["url"] == "https://api.privy.io/v1/wallets"
    assert captured["auth"] == ("app-id", "app-secret")
    assert captured["timeout"] == 20
    assert captured["headers"] == {
        "privy-app-id": "app-id",
        "Content-Type": "application/json",
    }
    assert captured["json"] == {
        "chain_type": "ethereum",
        "owner": {"user_id": "did:privy:user-123"},
        "additional_signers": [{"signer_id": "quorum-1", "override_policy_ids": ["policy-1"]}],
    }

def test_safe_withdraw_official_mode_supports_native_usdc(monkeypatch) -> None:
    request = _FakeRequest(
        {
            "to_address": "0x1111111111111111111111111111111111111111",
            "amount_usdc": 0.1,
            "token": "usdc",
        }
    )

    monkeypatch.setattr(routes, "get_request_identity", lambda _request: {
        "authenticated": True,
        "wallet_address": "0x2222222222222222222222222222222222222222",
    })
    monkeypatch.setattr(routes, "_official_tx_mode", lambda: True)
    monkeypatch.setattr(routes, "_extract_relayer_payload", lambda _body: None)
    monkeypatch.setattr(routes, "_get_user_signer_binding", lambda _wallet: {"privy_embedded_wallet_id": "wid-1", "main_wallet": "0x2222222222222222222222222222222222222222", "privy_embedded_wallet_address": "0x2222222222222222222222222222222222222222", "privy_signer_status": "enrolled", "proxy_wallet": "0x3333333333333333333333333333333333333333"})
    monkeypatch.setattr(routes, "_log_signer_debug", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_extract_request_user_jwts", lambda _request: ["jwt-token"])
    monkeypatch.setattr(routes, "_insert_vault_tx", lambda **_kwargs: None)

    captured: dict[str, str] = {}

    def _fake_build_server_side_relayer_payload(caller_wallet: str, to: str, data_hex: str, user_jwt):
        captured["caller_wallet"] = caller_wallet
        captured["to"] = to
        captured["data_hex"] = data_hex
        captured["user_jwt"] = json.dumps(user_jwt)
        return {"from": caller_wallet, "to": to, "data": data_hex}

    monkeypatch.setattr(routes, "_build_server_side_relayer_payload", _fake_build_server_side_relayer_payload)
    monkeypatch.setattr(
        routes,
        "_relayer_submit",
        lambda _payload: {"transactionHash": "0xtx", "transactionID": "tx-1", "state": "submitted"},
    )

    response = asyncio.run(routes.api_pm_safe_withdraw(request))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload["success"] is True
    assert captured["caller_wallet"] == "0x2222222222222222222222222222222222222222"
    assert captured["to"].lower() == routes._PUSD_TOKEN.lower()
    recipient, amount = decode(["address", "uint256"], bytes.fromhex(captured["data_hex"][10:]))
    assert recipient == "0x1111111111111111111111111111111111111111"
    assert amount == 100_000


def test_build_privy_authorization_signature_uses_sdk_request_envelope(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_canonicalize(value):
        captured_payload["value"] = value
        return "{}"

    class _FakePrivateKey:
        def sign(self, _payload_bytes, _algo):
            return b"abc"

    monkeypatch.setattr(routes, "_canonicalize_json", _fake_canonicalize)
    monkeypatch.setattr(
        routes.serialization,
        "load_der_private_key",
        lambda _decoded, password=None: _FakePrivateKey(),
    )

    signature = routes._build_privy_authorization_signature(
        "https://api.privy.io/v1/wallets/wallet-1/rpc",
        {"chain_type": "ethereum", "method": "eth_signTypedData_v4"},
        {
            "privy-app-id": "app-id-1",
            "privy-request-expiry": "1234567890",
        },
        key_pem="wallet-auth:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        method="post",
    )

    assert signature == "YWJj"
    assert captured_payload["value"] == {
        "version": 1,
        "method": "POST",
        "url": "https://api.privy.io/v1/wallets/wallet-1/rpc",
        "body": {"chain_type": "ethereum", "method": "eth_signTypedData_v4"},
        "headers": {
            "privy-app-id": "app-id-1",
            "privy-request-expiry": "1234567890",
        },
    }