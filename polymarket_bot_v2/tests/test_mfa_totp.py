"""T902: 服务端 TOTP MFA 原语与签名 cookie 的单元测试（无 DB / 无网络）。"""

from scripts.dashboard_routes import _shared


def test_generate_totp_secret_is_base32():
    secret = _shared.generate_totp_secret()
    assert isinstance(secret, str)
    assert len(secret) >= 16
    assert secret.replace("=", "").isalnum()


def test_provisioning_uri_and_qr():
    secret = _shared.generate_totp_secret()
    uri = _shared.totp_provisioning_uri(secret, "0x123")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=VeyraTrade" in uri
    qr = _shared.totp_qr_svg(uri)
    assert qr.startswith("<svg")
    assert "</svg>" in qr


def test_verify_totp_accepts_valid_code():
    import pyotp

    secret = _shared.generate_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert _shared.verify_totp(secret, code) is True


def test_verify_totp_rejects_garbage():
    secret = _shared.generate_totp_secret()
    assert _shared.verify_totp(secret, "000000") is False
    assert _shared.verify_totp(secret, "") is False
    assert _shared.verify_totp("", "123456") is False
    assert _shared.verify_totp(secret, "12345") is False
    assert _shared.verify_totp(secret, "abcdef") is False


def test_mfa_cookie_roundtrip_and_tamper():
    wallet = "0x1538cccf03bd2d95c210e22eab15e7a606faab35"
    token = _shared.mfa_cookie_sign(wallet)
    assert _shared.mfa_cookie_verify(token, wallet) is True
    # 篡改钱包
    assert _shared.mfa_cookie_verify(token, "0x" + "1" * 40) is False
    # 篡改 MAC
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert _shared.mfa_cookie_verify(tampered, wallet) is False
    # 空 / 畸形
    assert _shared.mfa_cookie_verify("", wallet) is False
    assert _shared.mfa_cookie_verify("not-a-token", wallet) is False


def test_mfa_cookie_is_not_plain_wallet():
    """T902: cookie 值不得等于裸钱包地址（旧实现可被伪造）。"""
    wallet = "0x1538cccf03bd2d95c210e22eab15e7a606faab35"
    token = _shared.mfa_cookie_sign(wallet)
    assert token != wallet
    assert token.startswith(wallet + ".")
    assert len(token) > len(wallet) + 40
