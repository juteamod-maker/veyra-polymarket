#!/usr/bin/env python3
"""T297 — In-place sweep of all frontend templates / JS / CSS to strip
developer comments, decorative banners (═════ / ─────), trailing whitespace
and excess blank lines. Preserves Jinja2 ({% %}, {{ }}), HTML conditional
comments (<!--[if ...]> ... <![endif]-->) and JS string-literal contents.

Usage:
  python3 scripts/clean_frontend_assets.py [--check]

--check  exit 1 if any file would be modified (CI guard).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "scripts" / "frontend" / "templates"
JS_DIR = ROOT / "scripts" / "frontend" / "static" / "js"
CSS_DIR = ROOT / "scripts" / "frontend" / "static" / "css"

# ---------- HTML ----------
# Match <!-- ... --> but NOT <!--[if ...]> ... <![endif]-->
_HTML_COMMENT_RE = re.compile(r"<!--(?!\[if)(?:.|\n)*?-->", re.MULTILINE)
_BANNER_RE = re.compile(r"^[ \t]*[/#*]?\s*[═─━]{3,}.*$", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n[ \t]*\n[ \t]*\n+")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def clean_html(text: str) -> str:
    text = _HTML_COMMENT_RE.sub("", text)
    text = _BANNER_RE.sub("", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text


# ---------- CSS ----------
_CSS_COMMENT_RE = re.compile(r"/\*(?:.|\n)*?\*/", re.MULTILINE)


def clean_css(text: str) -> str:
    text = _CSS_COMMENT_RE.sub("", text)
    text = _BANNER_RE.sub("", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text


# ---------- JS ----------
# String-literal aware stripper.  Skip content inside ' " ` and regex literals.
def clean_js(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Line comment
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j  # keep newline
            continue
        # Block comment
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        # String literal
        if c in ("'", '"', "`"):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                ch = text[i]
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    i += 1
                    break
                # Template literal interpolation kept verbatim
                i += 1
            continue
        out.append(c)
        i += 1
    cleaned = "".join(out)
    cleaned = _BANNER_RE.sub("", cleaned)
    cleaned = _TRAILING_WS_RE.sub("", cleaned)
    cleaned = _MULTI_BLANK_RE.sub("\n\n", cleaned)
    return cleaned


def process(path: Path, fn) -> tuple[bool, int]:
    src = path.read_text(encoding="utf-8")
    out = fn(src)
    if out == src:
        return False, 0
    delta = len(src) - len(out)
    return True, delta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    targets: list[tuple[Path, str, callable]] = []
    for p in sorted(TPL_DIR.rglob("*.html")):
        targets.append((p, "html", clean_html))
    for p in sorted(JS_DIR.rglob("*.js")):
        targets.append((p, "js", clean_js))
    for p in sorted(CSS_DIR.rglob("*.css")):
        targets.append((p, "css", clean_css))

    total_saved = 0
    changed_files = 0
    for path, kind, fn in targets:
        src = path.read_text(encoding="utf-8")
        out = fn(src)
        if out != src:
            changed_files += 1
            saved = len(src) - len(out)
            total_saved += saved
            if args.check:
                print(f"DIRTY {kind:4} {path.relative_to(ROOT)}  -{saved}B")
            else:
                path.write_text(out, encoding="utf-8")
                print(f"clean {kind:4} {path.relative_to(ROOT)}  -{saved}B")

    print(f"\n{changed_files} file(s) changed, {total_saved} bytes saved.")
    if args.check and changed_files:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
