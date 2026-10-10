from scripts.dashboard_routes import user_routes


def test_verify_privy_tokens_prefers_access_token_over_invalid_id_token(monkeypatch):
    observed = []

    monkeypatch.setattr(user_routes, "_load_privy_secrets", lambda: ["secret"])
    monkeypatch.setattr(user_routes.os, "environ", {**user_routes.os.environ, "PRIVY_APP_ID": "test-app"})

    def _fake_verify(token: str, _app_id: str):
        observed.append(token)
        if token == "valid.access.token":
            return {"sub": "did:privy:test-user"}
        raise ValueError("invalid token")

    monkeypatch.setattr(user_routes, "_verify_privy_jwt", _fake_verify)
    monkeypatch.setattr(user_routes, "_looks_like_jwt", lambda token: True)
    monkeypatch.setattr(
        user_routes,
        "_fetch_privy_user",
        lambda uid, _app_id, _secret: {
            "id": uid,
            "linkedAccounts": [
                {"type": "wallet", "address": "0x2222222222222222222222222222222222222222"}
            ],
        },
    )

    uid, payload = user_routes._verify_privy_tokens(
        "bad.id.token",
        "valid.access.token",
        {"linkedAccounts": [{"address": "0x1111111111111111111111111111111111111111"}]},
    )

    assert uid == "did:privy:test-user"
    assert observed == ["valid.access.token"]
    # T901: 身份只能来自 Privy 服务端；客户端提交的 user 对象必须被忽略。
    assert (
        payload["linkedAccounts"][0]["address"]
        == "0x2222222222222222222222222222222222222222"
    )


def test_verify_privy_tokens_ignores_client_supplied_user(monkeypatch):
    """T901 回归测试：不得用客户端 linkedAccounts 推导身份（账户接管漏洞）。"""
    monkeypatch.setattr(user_routes, "_load_privy_secrets", lambda: ["secret"])
    monkeypatch.setattr(user_routes.os, "environ", {**user_routes.os.environ, "PRIVY_APP_ID": "test-app"})
    monkeypatch.setattr(
        user_routes,
        "_verify_privy_jwt",
        lambda _token, _app_id: {"sub": "did:privy:attacker"},
    )
    monkeypatch.setattr(user_routes, "_looks_like_jwt", lambda _token: True)

    fetched = []

    def _fake_fetch(uid, _app_id, _secret):
        fetched.append(uid)
        return {
            "id": uid,
            "linkedAccounts": [
                {"type": "wallet", "address": "0x3333333333333333333333333333333333333333"}
            ],
        }

    monkeypatch.setattr(user_routes, "_fetch_privy_user", _fake_fetch)

    uid, payload = user_routes._verify_privy_tokens(
        "",
        "attacker.access.token",
        {"linkedAccounts": [{"address": "0x4444444444444444444444444444444444444444"}]},
    )

    # 必须回源 Privy，且绝不返回攻击者声明的钱包地址。
    assert fetched == ["did:privy:attacker"]
    assert (
        payload["linkedAccounts"][0]["address"]
        == "0x3333333333333333333333333333333333333333"
    )
    assert "0x4444444444444444444444444444444444444444" not in str(payload)