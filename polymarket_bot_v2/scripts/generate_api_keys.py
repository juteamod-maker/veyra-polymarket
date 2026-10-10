#!/usr/bin/env python3
"""
generate_api_keys.py — 从 Polygon 钱包私钥生成 Polymarket CLOB L2 API 凭据

用法：
    cd polymarket_bot
    python3 scripts/generate_api_keys.py

前提条件：
    - .env 中已填写 POLY_PRIVATE_KEY（Ethereum/Polygon 钱包私钥，0x 开头的 64 位十六进制字符串）
    - 已安装 py-clob-client：pip install py-clob-client

生成的三个值需填入 .env：
    POLY_API_KEY    → 对应输出的 apiKey
    POLY_SECRET     → 对应输出的 secret
    POLY_PASSPHRASE → 对应输出的 passphrase
"""
import os
import sys
import inspect

# 确保在 polymarket_bot 目录下运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main():
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    if not private_key or private_key == "your_private_key_here":
        print("❌ 错误：请先在 .env 中设置 POLY_PRIVATE_KEY")
        print("   格式：POLY_PRIVATE_KEY=0x你的64位十六进制私钥")
        sys.exit(1)

    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError:
        print("❌ 错误：py-clob-client 未安装，请运行：pip install py-clob-client")
        sys.exit(1)

    host = "https://clob.polymarket.com"
    chain_id = 137  # Polygon 主网

    print("正在连接 Polymarket CLOB API 并生成 L2 API 凭据...")
    print(f"  Host: {host}")
    print(f"  Chain ID: {chain_id}\n")

    try:
        client = ClobClient(host, key=private_key, chain_id=chain_id)

        # 优先使用官方推荐接口：已存在则派生，不存在则创建。
        # 这样可避免反复运行脚本时意外创建多把 key。
        if hasattr(client, "create_or_derive_api_key"):
            creds = client.create_or_derive_api_key()
        elif hasattr(client, "derive_api_key"):
            derive = client.derive_api_key
            try:
                sig = inspect.signature(derive)
                # 某些 SDK 版本支持 nonce 参数；固定为 0，避免非确定性派生。
                if len(sig.parameters) >= 1:
                    creds = derive(0)
                else:
                    creds = derive()
            except (TypeError, ValueError):
                creds = derive()
        elif hasattr(client, "create_api_key"):
            creds = client.create_api_key()
        else:
            print(
                "❌ 错误：当前版本 py-clob-client 不支持 create_or_derive_api_key/derive_api_key/create_api_key"
            )
            print("   请检查版本：pip show py-clob-client-v2")
            sys.exit(1)

        print("✅ L2 API 凭据生成成功！请将以下内容填入 .env 文件：\n")
        print("=" * 60)

        if isinstance(creds, dict):
            api_key = (
                creds.get("apiKey") or creds.get("api_key") or creds.get("key", "")
            )
            secret = creds.get("secret", "")
            passphrase = creds.get("passphrase", "")
        else:
            # 有些版本返回 namedtuple 或 dataclass
            api_key = getattr(creds, "api_key", getattr(creds, "apiKey", ""))
            secret = getattr(creds, "secret", "")
            passphrase = getattr(creds, "passphrase", "")

        print(f"POLY_API_KEY={api_key}")
        print(f"POLY_SECRET={secret}")
        print(f"POLY_PASSPHRASE={passphrase}")
        print("=" * 60)
        print("\n⚠️  注意：将以上内容填入 polymarket_bot_v2/.env，不要提交到 Git！")
        print("      如果已存在旧的 API 密钥，以新生成的为准。")
        print("\n完成后，再次运行 DRY RUN 验证，确认无误后再将 dry_run 改为 false。")

    except Exception as e:
        print(f"❌ 生成失败: {e}")
        print("\n可能原因：")
        print("  1. POLY_PRIVATE_KEY 格式错误（应为 0x + 64 位十六进制）")
        print("  2. 网络连接问题（需要能访问 clob.polymarket.com）")
        print("  3. py-clob-client 版本不兼容：pip install --upgrade py-clob-client")
        sys.exit(1)


if __name__ == "__main__":
    main()
