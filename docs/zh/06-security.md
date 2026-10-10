# 06 · 安全模型

完整的安全政策见仓库根目录 [SECURITY.md](../SECURITY.md)。本分册给出概要。

## 1. 身份模型

- `user_session` 是唯一用户身份通道，HMAC-SHA256 签名，源自 `USER_SESSION_SECRET`。
- 历史 `DASHBOARD_KEY` / TOTP / `?key=` 旁路认证已全部移除并显式拒绝。
- Privy 登录：access token 服务端验签，用户钱包 / 内嵌钱包 / 邮箱**只取自 Privy API 返回**，
  客户端提交的 `user.linkedAccounts` 一律忽略。

## 2. MFA（两步验证）

- 登录 MFA 由**服务端 TOTP** 强制：`totp_enabled` 用户必须提供有效 6 位 TOTP 码才能登录。
- `mfa_verified` 会话标记为 HMAC 签名，客户端无法伪造。
- 不再信任请求体中的 `mfa_verified` 布尔值。

## 3. 权限边界

- 全局状态变更（`/api/emergency-freeze`、`/api/weather-cities*`、`/api/evolution/trigger`、
  `/api/risk/resume`、`/api/map/markers*`、`/api/copy-tasks*`）要求 admin / super_admin。
- Copytrader V2 语句按 `owner_wallet_address` 隔离。
- 资金动作从会话解析钱包，不从请求体取值。

## 4. 注入与安全编码

- SQL 全参数化；动态标识符仅来自固定白名单或整数校验。
- 无 `shell=True` / `eval` / `exec` / `pickle.load` / `yaml.load`。
- 模板内嵌 `<script>` 的 JSON 统一经 `escape_script_json` 转义 `</`。
- 出站 URL 均使用硬编码主机，用户输入只进入 query / path 段。
- Cookie（`user_session` / `privy-*-token` / `mfa_verified`）均 `Secure + HttpOnly + SameSite`。
- 会话密钥不再有硬编码兜底；未配置时使用进程内随机密钥并输出 CRITICAL 日志。

## 5. 链上约束

- 链上常量只来自 `src/utils/pm_v2_constants.py`。
- SAFE nonce 查询使用 owner EOA。
- Safe 交易构建复用统一函数。
- 保留 `[Relayer 4xx]` 诊断日志。

## 6. 漏洞上报

请勿公开披露。通过 GitHub Security Advisories 或邮箱私下报告，详见 [SECURITY.md](../SECURITY.md)。
