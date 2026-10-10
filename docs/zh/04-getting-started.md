# 04 · 快速开始

## 1. 环境要求

- Python 3.12+
- PostgreSQL 14+
- [Privy](https://privy.io) App（App ID / App Secret / authorization key）
- Polymarket CLOB v2 API 凭据（`py-clob-client-v2`）
- 可选：Telegram 通知、RapidAPI/OddsAPI 体育赔率 key

## 2. 配置

```bash
cd polymarket_bot_v2
cp .env.example .env        # 填入真实值；.env 永不入库
```

关键变量见根目录 [README.md](../README.md) §4。

## 3. 本地运行

```bash
cd polymarket_bot_v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 初始化数据库 schema
bash scripts/setup_postgres_local.sh
psql -d polymarket_v2 -f scripts/init_pg_schema.sql

# 启动用户站 / API（默认 8080）
uvicorn scripts.web_dashboard:app --host 127.0.0.1 --port 8080 --proxy-headers
```

> 本发行版不包含 Admin V3 控制台，因此无需启动 8081 端口服务。

## 4. 测试

```bash
cd polymarket_bot_v2
pytest -q                                   # 详见 pytest.ini 的路径与 marker
pytest -q tests/test_mfa_totp.py            # 服务端 TOTP MFA 单元测试
```

部分测试需要本地 PostgreSQL 与已填充的 `config/config.json`；裸检出一份代码时
因数据库不可用而失败是预期行为。

## 5. 部署

部署入口为仓库自带脚本：

```bash
bash polymarket_bot_v2/scripts/deploy_vps.sh update
```

`scripts/deploy_vps_v2.sh` 通过 `VPS_HOST` 读取目标主机别名，请替换为你自己的
SSH 主机。私有 Admin V3 模块缺失时，脚本会自动跳过所有 admin 相关步骤。

## 6. MFA（两步验证）

用户登录支持**服务端 TOTP** 两步验证（Google Authenticator 兼容）：

- 注册：`POST /api/user/mfa/enroll/start` → 获取二维码与 secret；
  `POST /api/user/mfa/enroll/confirm` 校验确认码。
- 验证：`POST /api/user/mfa/verify-mark` 校验 6 位 TOTP 码，签发 30 分钟签名会话标记。
- 登录：`POST /api/user/privy/verify` 在 `mfa_code` 中携带 TOTP 码完成二次验证。
- 关闭：`POST /api/user/mfa/disable` 校验当前码后关闭。

前端登录流程会弹出 TOTP 输入框；用户资料页提供注册 / 验证 / 关闭入口。
