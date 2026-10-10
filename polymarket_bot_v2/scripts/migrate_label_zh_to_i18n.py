#!/usr/bin/env python3
"""T600 W5 — label_zh / *_zh 迁移脚本

把 app_menus.label_zh 与 frontend_banners 的 *_zh 字段值（仅当对应
i18n_key 存在且 zh-CN.json 中尚未覆盖时）回填到 scripts/frontend/static/i18n/zh-CN.json，
让"目标架构"完全闭环：admin 后台只填 i18n_key + label_en，多语种全部走 JSON。

特性：
- 默认 dry-run，仅打印将写入的 key/value，不修改文件。
- 加 `--apply` 才真正落盘，会先备份原文件为 `<file>.bak.<ts>`。
- 不删除 DB 中已有 label_zh / *_zh，向下兼容；后续可手工或单独脚本清理。

用法：
    python3 -m scripts.migrate_label_zh_to_i18n            # dry-run
    python3 -m scripts.migrate_label_zh_to_i18n --apply    # 落盘
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

I18N_FILE = ROOT / "scripts" / "frontend" / "static" / "i18n" / "zh-CN.json"


def load_i18n() -> dict:
    if not I18N_FILE.exists():
        return {}
    try:
        return json.loads(I18N_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[ERR] read {I18N_FILE}: {exc}", file=sys.stderr)
        sys.exit(2)


def collect_menu_pairs() -> list[tuple[str, str, str]]:
    """返回 [(i18n_key, label_zh, source_label_for_log)]"""
    from src.utils.database import connect

    out: list[tuple[str, str, str]] = []
    try:
        with connect() as conn:
            cur = conn.execute(
                "SELECT i18n_key, label_zh, label "
                "FROM app_menus "
                "WHERE i18n_key IS NOT NULL AND TRIM(i18n_key) <> '' "
                "  AND label_zh IS NOT NULL AND TRIM(label_zh) <> ''"
            )
            for r in cur.fetchall():
                if isinstance(r, dict):
                    k = (r.get("i18n_key") or "").strip()
                    v = (r.get("label_zh") or "").strip()
                    src = (r.get("label") or "").strip()
                else:
                    k = (r[0] or "").strip()
                    v = (r[1] or "").strip()
                    src = (r[2] or "").strip()
                if k and v:
                    out.append((k, v, src))
    except Exception as exc:
        print(f"[WARN] menus DB query failed: {exc}", file=sys.stderr)
    return out


def collect_banner_pairs() -> list[tuple[str, str, str]]:
    """从 config/frontend_banners.json + _DEFAULT_BANNERS 提取
    (title|subtitle|label)_i18n_key + (title|subtitle|label)_zh 对。"""
    from scripts.dashboard_routes.frontend_banner_routes import _merged

    out: list[tuple[str, str, str]] = []
    try:
        merged = _merged() or {}
    except Exception as exc:
        print(f"[WARN] banners merge failed: {exc}", file=sys.stderr)
        return out
    for slot, b in merged.items():
        if not isinstance(b, dict):
            continue
        for prefix in ("title", "subtitle", "label"):
            k = (b.get(f"{prefix}_i18n_key") or "").strip()
            v = (b.get(f"{prefix}_zh") or "").strip()
            if k and v:
                out.append((k, v, f"{slot}.{prefix}"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate label_zh -> zh-CN.json")
    parser.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    args = parser.parse_args()

    static_dict = load_i18n()
    print(f"[INFO] static zh-CN.json 已有 {len(static_dict)} 条 key")

    pairs: list[tuple[str, str, str]] = []
    pairs.extend(collect_menu_pairs())
    pairs.extend(collect_banner_pairs())
    print(f"[INFO] DB / banner 收集到 {len(pairs)} 条 (key, zh) 候选")

    additions: dict[str, str] = {}
    skipped_existing = 0
    skipped_dup = 0

    for key, val, src in pairs:
        if key in static_dict:
            skipped_existing += 1
            continue
        if key in additions:
            if additions[key] != val:
                print(f"[WARN] key 冲突 {key} → 多个不同 zh 值（保留第一个）src={src}")
            skipped_dup += 1
            continue
        additions[key] = val

    print(
        f"[INFO] 跳过已存在: {skipped_existing}; 重复 key: {skipped_dup}; 待新增: {len(additions)}"
    )
    if not additions:
        print("[OK] 没有需要写入的新 key。退出。")
        return 0

    print("\n— 待写入条目 —")
    for k in sorted(additions.keys()):
        print(f"  {k:48} = {additions[k]!r}")

    if not args.apply:
        print("\n[DRY-RUN] 未实际写入。需要落盘请加 --apply。")
        return 0

    # 落盘 + 备份
    ts = int(time.time())
    backup = I18N_FILE.with_suffix(I18N_FILE.suffix + f".bak.{ts}")
    backup.write_bytes(I18N_FILE.read_bytes())
    print(f"\n[BACKUP] {backup}")

    static_dict.update(additions)
    # 排序保持 diff 友好
    ordered = dict(sorted(static_dict.items(), key=lambda kv: kv[0]))
    I18N_FILE.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[APPLIED] zh-CN.json 写入 {len(additions)} 条新 key (总 {len(ordered)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
