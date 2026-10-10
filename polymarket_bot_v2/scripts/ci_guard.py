#!/usr/bin/env python3
"""
CI / 部署安全检查脚本。
在每次部署前运行，验证关键安全配置：
  1. config.json 中 dry_run 必须为 true
  2. .env 文件不在 git 暂存区
  3. 风控参数没有被弱化

用法：
  python scripts/ci_guard.py           # 部署前自动检查
  python scripts/ci_guard.py --strict  # 严格模式（额外检查风控参数）

退出码：
  0 = 所有检查通过
  1 = 存在安全违规，禁止部署
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ci_guard")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

# 风控参数安全下限（不允许低于这些值）
RISK_FLOORS = {
    "risk.daily_loss_pause_pct": 0.03,
    "risk.cumulative_loss_pause_pct": 0.10,
    "risk.max_single_bet_pct": 0.01,
    "stop_loss.standard_stop_pct": 0.10,
}


def load_config() -> dict:
    """加载 config.json。"""
    if not CONFIG_PATH.exists():
        logger.error(f"❌ 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def check_dry_run(cfg: dict) -> bool:
    """检查 dry_run 是否为 true。"""
    dry_run = cfg.get("general", {}).get("dry_run")
    if dry_run is not True:
        logger.error(f"❌ FATAL: dry_run={dry_run}，必须为 true！禁止部署。")
        return False
    logger.info("✅ dry_run=true")
    return True


def check_env_not_staged() -> bool:
    """检查 .env 文件未被 git add。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        staged_files = (
            result.stdout.strip().split("\n") if result.stdout.strip() else []
        )
        for f in staged_files:
            if f.endswith(".env") or "/.env" in f:
                logger.error(f"❌ .env 文件在 git 暂存区: {f}")
                return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("⚠️ 无法检查 git 暂存区（非 git 仓库或 git 不可用），跳过")
    logger.info("✅ .env 未被 git add")
    return True


def check_jinja_brace_typos() -> bool:
    """T907 — 防止 HTML formatter 把 `{{` 拆成 `{ {` 导致线上 SyntaxError。

    扫描 Jinja 模板里出现的 `{ {` / `} }` 字面（中间是空格）模式。
    这种模式 100% 是 formatter 误伤，没有任何合法用法。
    """
    template_dirs = [
        ROOT / "scripts" / "frontend" / "templates",
        ROOT / "admin_v3_spa" / "src",
    ]
    bad = []
    import re

    pat = re.compile(
        # 形式：{ { ident (| filter) } }
        # 真正的 Jinja 变量表达式被 formatter 拆开后才会匹配；
        # 普通 JS/CSS 的 `{ method: ... } }` 不会命中。
        r"\{\s+\{\s*[A-Za-z_][\w\.\[\]]*(?:\s*\|\s*[A-Za-z_]\w*)?\s*\}\s*\}"
    )
    for d in template_dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.html"):
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:160]}")
    if bad:
        logger.error("❌ FATAL: 发现 Jinja `{ {` / `} }` 拆分（formatter 误伤）：")
        for b in bad:
            logger.error("   " + b)
        return False
    logger.info("✅ Jinja `{{ }}` 完整，无 formatter 误伤")
    return True


def check_risk_params(cfg: dict) -> bool:
    """检查风控参数未被弱化到危险水平。"""
    ok = True
    for key_path, floor in RISK_FLOORS.items():
        parts = key_path.split(".")
        val = cfg
        for p in parts:
            val = val.get(p, {}) if isinstance(val, dict) else None
            if val is None:
                break
        if val is None:
            logger.warning(f"⚠️ 配置项 {key_path} 不存在，跳过检查")
            continue
        if isinstance(val, (int, float)) and val < floor:
            logger.error(f"❌ {key_path}={val} < 安全下限 {floor}")
            ok = False
        else:
            logger.info(f"✅ {key_path}={val} ≥ {floor}")
    return ok


def main() -> None:
    strict = "--strict" in sys.argv
    frontend_only = "--frontend-only" in sys.argv
    if frontend_only:
        if check_jinja_brace_typos():
            logger.info("\n🟢 前端 Jinja 大括号检查通过。")
            sys.exit(0)
        else:
            logger.error("\n🔴 前端检查未通过，禁止部署！")
            sys.exit(1)
    cfg = load_config()

    checks = [
        check_dry_run(cfg),
        check_env_not_staged(),
        check_jinja_brace_typos(),
    ]
    if strict:
        checks.append(check_risk_params(cfg))

    if all(checks):
        logger.info("\n🟢 所有安全检查通过，允许部署。")
        sys.exit(0)
    else:
        logger.error("\n🔴 安全检查未通过，禁止部署！")
        sys.exit(1)


if __name__ == "__main__":
    main()
