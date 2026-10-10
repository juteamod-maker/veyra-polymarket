# 01 · 产品与商业逻辑

## 1. 产品定位

Veyra.Trade 是面向多用户的 Polymarket 交易与运营平台，不是单机脚本 Bot。
系统把「市场发现 → 账户授权 → 交易执行 → 资产回流 → 运营治理」串成一条可审计的闭环。

## 2. 三大运行面

| 运行面 | 入口 | 面向对象 | 核心职责 |
|---|---|---|---|
| 用户站与 Dashboard API | `polymarket_bot_v2/scripts/web_dashboard.py` | 普通用户 | 页面渲染、交易前后 API、会话与权限、i18n 资源 |
| Copytrader V2 sidecar | `polymarket_bot_v2/src/services/copytrader_v2/daemon.py` | 系统运行面 | 跟投策略执行、独立生命周期、资源隔离 |
| Admin V3 管理后台（私有） | `src/services/admin_v3/`（不发布） | 运营 / 风控 / 管理员 | 配置治理、积分与费率、CMS、可观测工具 |

## 3. 核心商业链路

1. **拉新与注册** — 用户注册/登录进入系统，可带推荐码建立推荐关系，并按规则发放注册积分。
2. **市场发现与下单** — 在 discover / event 页面完成市场筛选与下单，系统完成参数校验、
   nonce/签名链路处理与 relayer 提交。
3. **资金与资产动作** — deposit / withdraw / redeem 属于高风险路径，保留可观测日志与失败诊断。
4. **推荐与积分** — 推荐关系支持多级（L1/L2/L3），奖励严格按规则发放，强调幂等、防环、防刷。
5. **运营治理** — 管理侧可配置积分、费率、页面文案与运行态，所有高权限动作可审计。

## 4. 设计与工程原则

1. **真实能力优先** — 禁止假按钮、假保存、假状态；所有控件连接真实 API。
2. **多用户隔离优先** — 用户敏感数据只能按会话上下文读取。
3. **可审计优先** — 部署、配置、链上失败诊断均可追踪。
4. **V2 边界优先** — 不回流历史 V1 单用户运行路径。

## 5. 站长如何赚钱（Polymarket Builder 计划）

本项目依托 [Polymarket Builder 计划](https://docs.polymarket.com/cn/programs/builders/overview)
构建商业闭环：你的站点把用户订单路由进 Polymarket 订单簿，每一笔经你站点成交的订单都
附带你的 Builder Code，从而把手续费、返佣与推荐奖励归因到你名下。

| 方式 | 机制 | 收益 |
|---|---|---|
| **Builder 手续费** | 成交订单附带你的 `bytes32` Builder Code，手续费计入你的 Builder 账户 | Taker ≤ **1.00%**、Maker ≤ **0.50%**（限额内自设，叠加平台费） |
| **推荐计划** | 你推荐用户产生的净交易手续费分成 | 直推 **10%**、间推 **5%**，每日 pUSD 结算 |
| **Taker 返利** | 用户 taker 交易量累积加权量，升档后每日返佣 | Bronze → Obsidian 共 7 档 |
| **跟投 / 高级功能** | Smart Money 跟投与自动化策略 | 订阅 / 增值服务定价由你决定 |
| **Admin V3 + 托管** | 私有运营台与代部署服务 | 商业授权 / 托管费（见 [07](./07-admin-console.md)） |

> **手续费直接归你**：在 `.env` 配置 `POLY_BUILDER_CODE` 即启用归因。费率上限与规则由
> Polymarket 设定，详见 [Builder Fees](https://docs.polymarket.com/cn/programs/builders/fees)，
> 以官方文档为准。

## 6. 商业 / 合作说明

本开源发行版覆盖用户站、API、Copytrader 与策略引擎（Apache-2.0）。
**Admin V3 运营控制台为私有模块**，通过赞助档或商业授权获取，见 [07](./07-admin-console.md)。
