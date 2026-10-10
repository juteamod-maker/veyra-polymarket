-- T601 cleanup — 清空 app_menus.label_zh
-- 前提：所有有 i18n_key 的菜单条目，其中文翻译已经迁入
-- frontend/static/i18n/zh-CN.json（T600 W5 dry-run 已确认 6 条全部命中）。
-- 之后中文文案的唯一来源是 zh-CN.json + /api/i18n/dynamic 派生。
--
-- 安全护栏：
--   1. 只清空 i18n_key 非空的行（保留旧实体的 fallback 文案）。
--   2. 写入前先打印将受影响的行数，方便回滚校验。
-- 回滚：从最近一次 pg_dump 还原 app_menus 即可（label_zh 是纯 UI 字段）。

BEGIN;

SELECT COUNT(*) AS rows_to_clear
FROM app_menus
WHERE COALESCE(i18n_key, '') <> ''
  AND COALESCE(label_zh, '') <> '';

UPDATE app_menus
SET label_zh = NULL
WHERE COALESCE(i18n_key, '') <> ''
  AND COALESCE(label_zh, '') <> '';

COMMIT;
