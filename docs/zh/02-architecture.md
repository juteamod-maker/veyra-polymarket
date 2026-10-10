# 02 · 系统架构与运行边界

## 1. 服务与端口

| 服务单元 | 启动对象 | 端口 | 说明 |
|---|---|---|---|
| `polymarket-dashboard-v2` | `scripts.web_dashboard:app` | `8080`（对外，经网关转发） | 用户站 + Dashboard API |
| `polymarket-copytrader-v2` | `python3 -m src.services.copytrader_v2.daemon` | —（常驻进程） | 跟投 sidecar |
| `polymarket-admin-v3` | `src.services.admin_v3.app:app` | `127.0.0.1:8081`（仅本机） | 私有控制台，不包含在本发行版 |

## 2. 代码边界

| 目录 | 职责 |
|---|---|
| `scripts/web_dashboard.py` | 用户站入口，统一 `include_router` 挂载 |
| `scripts/dashboard_routes/` | 用户侧路由模块（auth / user / discover / copytrader / redeem_withdraw 等） |
| `scripts/frontend/templates/` + `scripts/frontend/static/js/` | 用户前端模板与运行时 JS |
| `src/services/copytrader_v2/` | 跟投 sidecar |
| `src/` | 策略引擎、数据同步、链上工具等 |

## 3. 配置边界

| 配置 | 路径 |
|---|---|
| 通用配置 | `config/config.json`（git-ignored，参考 `config/config_docs.json`） |
| Copytrader 专属配置 | `config/copytrader_v2.json` |
| 环境变量模板 | `.env.example` |
| i18n 静态字典 | `scripts/frontend/static/i18n/*.json` |

## 4. 历史禁区（禁止作为默认入口）

- `main.py`
- `polymarket-bot*.service`（V1 单用户服务）
- 旧 Logs Tab、旧 `/api/logs`
- 任何 V1 单用户自动交易回流路径

## 5. 安全与链上约束

1. 链上常量只来自 `src/utils/pm_v2_constants.py`。
2. SAFE nonce 查询使用 owner EOA。
3. Safe 交易构建复用统一函数，不允许临时分叉。
4. 保留 Relayer 4xx 诊断日志以便排障。

## 6. 变更策略

1. 先改运行面入口与核心路由，再扩散到 UI。
2. 涉及权限 / 资金 / 推荐奖励的修改，必须同步更新文档与安全说明。
3. 避免跨层大改，一次只修一层可验证增量。
