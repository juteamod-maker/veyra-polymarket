#!/usr/bin/env python3
"""
build_static_landing.py — render the marketing landing page as a fully static
site suitable for Cloudflare Pages (or any static host).

It takes the Jinja template `scripts/frontend/templates/landing.html`, resolves
its server-side expressions, and copies the exact asset tree the page references
(/v2/static/..., /static/..., /favicon.ico) so no URL rewriting is required.

Usage:
    python3 scripts/build_static_landing.py [output_dir]
    # default output_dir: ./landing-dist
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # polymarket_bot_v2/
TEMPLATE = ROOT / "scripts/frontend/templates/landing.html"
STATIC_FRONTEND = ROOT / "scripts/frontend/static"
STATIC_ROOT = ROOT / "scripts/static"

LANGS = ["zh-CN", "en", "fr", "ja", "ko", "th", "vi", "es", "pt"]
ASSET_VERSION = "cfpages-1"
DEFAULT_LANG = "en"


def landing_keys(lang: str) -> dict:
    data = json.loads((STATIC_FRONTEND / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k.startswith("landing.")}


def escape_script_json(obj: object) -> str:
    """Mirror the Jinja `escape_script_json` filter used by the template."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def render(template_text: str) -> str:
    # All 9 languages are inlined so the language switcher works with no backend.
    preload = {lang: landing_keys(lang) for lang in LANGS}
    preload_json = escape_script_json(preload)

    out = template_text
    # Order matters: replace preload before the generic asset-version / lang tokens.
    out = re.sub(
        r"\{\{\s*landing_i18n_preload_json\s*\|\s*escape_script_json\s*\|\s*safe\s*\}\}",
        preload_json,
        out,
    )
    out = out.replace("{{ asset_version | default('0') }}", ASSET_VERSION)
    out = out.replace("{{ lang or 'zh-CN' }}", DEFAULT_LANG)
    out = re.sub(
        r"\{\{\s*\(lang or 'zh-CN'\)\s*\|\s*tojson\s*\}\}",
        json.dumps(DEFAULT_LANG),
        out,
    )
    return out


def build(output_dir: Path) -> None:
    template_text = TEMPLATE.read_text(encoding="utf-8")

    if output_dir.exists():
        shutil.rmtree(output_dir)

    (output_dir / "v2/static/css").mkdir(parents=True, exist_ok=True)
    (output_dir / "v2/static/js/landing").mkdir(parents=True, exist_ok=True)
    (output_dir / "v2/static/i18n/landing").mkdir(parents=True, exist_ok=True)
    (output_dir / "static").mkdir(parents=True, exist_ok=True)

    (output_dir / "index.html").write_text(render(template_text), encoding="utf-8")

    shutil.copy2(STATIC_FRONTEND / "css/landing.css", output_dir / "v2/static/css/landing.css")
    shutil.copy2(STATIC_FRONTEND / "js/i18n.js", output_dir / "v2/static/js/i18n.js")
    shutil.copy2(STATIC_FRONTEND / "js/landing/landing.js", output_dir / "v2/static/js/landing/landing.js")
    for lang in LANGS:
        shutil.copy2(STATIC_FRONTEND / "i18n" / f"{lang}.json", output_dir / "v2/static/i18n" / f"{lang}.json")
        shutil.copy2(STATIC_FRONTEND / "i18n/landing" / f"{lang}.json", output_dir / "v2/static/i18n/landing" / f"{lang}.json")
    shutil.copy2(STATIC_ROOT / "Vgril_left.png", output_dir / "static/Vgril_left.png")
    shutil.copy2(STATIC_ROOT / "favicon.ico", output_dir / "favicon.ico")

    (output_dir / "_headers").write_text(
        "/*\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        "\n"
        "/v2/static/*\n"
        "  Cache-Control: public, max-age=86400\n"
        "\n"
        "/static/*\n"
        "  Cache-Control: public, max-age=86400\n",
        encoding="utf-8",
    )


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (ROOT / "landing-dist")
    build(output_dir)
    files = sum(1 for p in output_dir.rglob("*") if p.is_file())
    print(f"[landing] rendered {files} files -> {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
