# 03 · 核心功能说明

统一 Landing 页面作为入口，四大核心页面承载不同用户价值。

## 1. WeatherEventMap — 天气事件地图

面向天气关联市场交易者的可视化入口。

- **地图总览**：展示城市点位、事件热度与状态。
- **城市详情**：点击进入详情层，查看城市级天气与相关市场。
- **历史与走势**：价格历史、市场交易历史、城市历史数据。
- **实时价格联动**：拉取 live prices，提升盘中判断效率。
- **仓位联动**：结合用户 bets / open positions 做城市级关联展示。
- **图表分析**：基于 Leaflet + Chart 可视化。

## 2. SportsLive — 赛事赔率

面向体育市场交易者的赔率与机会页面。

- **赛事卡片流**：Live / Upcoming 状态、对阵、概率、赔率。
- **多体育筛选**：全量与分运动筛选。
- **状态过滤**：All / Live / Upcoming 视图切换。
- **SSR 首屏注入**：保证首屏打开速度。
- **运营位**：AI Banner + 页面 Banner（后台可配）。
- **详情引流**：卡片点击进入 `/event/{slug}`。

## 3. ValueLab — 价值实验室

面向价值投资者的量化机会页面。

- **多分类标签**：Crypto / Earnings / Economy / Finance。
- **搜索与排序**：按 score、EV、更新时间筛选。
- **SSR + 渐进加载**：首屏骨架与分页滚动结合。
- **数据健康展示**：freshness grade 与 lag 指标可视化。
- **价值卡片**：implied prob、value score、EV、recommendation。
- **详情跳转**：进入 `/valuelab/{slug}`。

## 4. Copytrader — 智能跟投

面向不想手动盯盘的用户的跟投工具。

- **钱包筛选与搜索**：按分数、PNL、胜率、交易数、分类过滤。
- **钱包榜单卡片**：score、PNL、胜率、风险标签（含 bot 风险）。
- **钱包分析页签**：analytics / positions / activity / closed positions。
- **任务管理**：创建 / 编辑 / 暂停 / 恢复 / 删除 copy tasks。
- **全局设置**：copytrader 开关与参数配置。
- **健康检查**：状态接口用于前端可观测与安全提醒。

## 5. Landing 页面

统一 Landing 聚合四大入口，含：

- 轮播式卡片（TABS：Veyra / Weather / Sports / ValueLab / Copytrader）。
- 拖拽 / 滚轮 / 键盘切换卡片。
- 多语言（i18n）文案。
- 品牌视觉：VeYra logo 与角色视觉语言一致。
