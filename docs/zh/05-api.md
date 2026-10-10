# 05 · API 与数据模型

## 1. API 入口分区

| 分区 | 入口 | 前缀 |
|---|---|---|
| 用户站 API | `scripts/web_dashboard.py` 统一挂载 | `/api/user/*`, `/api/pm/*`, `/api/leaderboard/*`, `/api/public/*` |
| Admin API（私有） | `src/services/admin_v3/routes_admin.py` | `/admin-v3/api`（不包含在本发行版） |

## 2. 身份与认证

- 唯一用户身份通道：`user_session` cookie（HMAC 签名，源自 `USER_SESSION_SECRET`）。
- 登录：`POST /api/user/privy/verify` — Privy access token 服务端验签，身份取自 Privy API
  返回，绝不采信客户端提交的 `user` 对象。
- MFA：服务端 TOTP（见 [04](./04-getting-started.md) §6）。

## 3. 主要接口索引（用户侧）

| 接口 | 用途 |
|---|---|
| `POST /api/user/privy/verify` | 登录 / 会话建立（含 TOTP 二次验证） |
| `GET /api/user/mfa/status` | 查询当前 MFA 状态 |
| `POST /api/user/mfa/enroll/start` | 开始 TOTP 注册（返回二维码与 secret） |
| `POST /api/user/mfa/enroll/confirm` | 校验确认码，完成注册 |
| `POST /api/user/mfa/verify-mark` | 校验 TOTP 码，签发 30 分钟签名会话 |
| `POST /api/user/mfa/disable` | 校验当前码后关闭 MFA |
| `GET /api/user/referral` | 推荐链路、奖励与分页数据 |
| `GET /api/pm/*` | Polymarket 数据与资金动作（deposit / withdraw / redeem / vault） |
| `GET /api/leaderboard/*` | 排行榜 |
| `GET /api/public/*` | 公开数据 |
| `GET /api/copy-tasks` 等 | 跟投任务（管理员操作） |

> 完整路由以 `scripts/dashboard_routes/` 源码为准。资金相关动作（withdraw / redeem）
> 全部从会话中解析调用方钱包，不接受请求体中的钱包地址。

## 4. 数据模型概览

| 表 | 说明 |
|---|---|
| `users` | 用户主表（wallet_address 主键，角色、Privy 绑定、TOTP、MFA 状态） |
| `user_sessions` | 会话表（HMAC token，过期时间，来源 IP） |
| `copy_tasks` | 跟投任务（全局，管理员创建） |
| `point_transactions` / `points_tiers` | 积分流水与等级 |
| `fee_config` / `fee_discount_tiers` | 费率与折扣 |
| `user_pm_profiles` | 用户 Polymarket 绑定档案 |

Schema 定义见 `scripts/init_pg_schema.sql` 与 `src/utils/db.py` 迁移。
