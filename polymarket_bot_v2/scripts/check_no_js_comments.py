from __future__ import annotations
import argparse
import os
import sys
from typing import List, Tuple


def find_comments(src: str) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    n = len(src)
    i = 0
    line = 1
    last_meaningful = ''
    in_str = None
    str_start = 0
    template_brace_depth: List[int] = []
    in_template = False

    def at(k: int) -> str:
        return src[k] if 0 <= k < n else ''

    while i < n:
        ch = src[i]
        nxt = at(i + 1)

        if in_str is not None:
            if ch == '\\':
                if at(i + 1) == '\n':
                    line += 1
                i += 2
                continue
            if in_str == '`':
                if ch == '$' and nxt == '{':
                    template_brace_depth.append(1)
                    in_str = None
                    in_template = True
                    i += 2
                    continue
                if ch == '`':
                    in_str = None
                    last_meaningful = '`'
                    i += 1
                    continue
                if ch == '\n':
                    line += 1
                i += 1
                continue
            if ch == in_str:
                in_str = None
                last_meaningful = ch
                i += 1
                continue
            if ch == '\n':
                line += 1
            i += 1
            continue

        if in_template and template_brace_depth:
            if ch == '{':
                template_brace_depth[-1] += 1
            elif ch == '}':
                template_brace_depth[-1] -= 1
                if template_brace_depth[-1] == 0:
                    template_brace_depth.pop()
                    in_str = '`'
                    in_template = False
                    i += 1
                    continue

        if ch == '/' and nxt == '/':
            start = i
            while i < n and src[i] != '\n':
                i += 1
            out.append((line, start, src[start:i]))
            continue

        if ch == '/' and nxt == '*':
            start = i
            start_line = line
            i += 2
            while i < n and not (src[i] == '*' and at(i + 1) == '/'):
                if src[i] == '\n':
                    line += 1
                i += 1
            i += 2
            out.append((start_line, start, src[start:i]))
            continue

        if ch == '/':
            if _regex_context(last_meaningful):
                j = i + 1
                in_class = False
                while j < n:
                    cj = src[j]
                    if cj == '\\':
                        j += 2
                        continue
                    if cj == '[':
                        in_class = True
                    elif cj == ']':
                        in_class = False
                    elif cj == '/' and not in_class:
                        break
                    elif cj == '\n':
                        break
                    j += 1
                if j < n and src[j] == '/':
                    j += 1
                    while j < n and src[j].isalpha():
                        j += 1
                    last_meaningful = '0'
                    i = j
                    continue
            last_meaningful = '/'
            i += 1
            continue

        if ch in ('"', "'", '`'):
            in_str = ch
            str_start = i
            i += 1
            continue

        if ch == '\n':
            line += 1
            i += 1
            continue

        if ch.isspace():
            i += 1
            continue

        last_meaningful = ch
        i += 1

    return out


_REGEX_PRECEDENT = set('(){}[],;:!?=&|^*%+-~<>')


def _regex_context(last: str) -> bool:
    if not last:
        return True
    if last in _REGEX_PRECEDENT:
        return True
    if last.isalnum() or last in ('_', '$', ')', ']'):
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('paths', nargs='+')
    p.add_argument('--allow', action='append', default=[])
    p.add_argument('--max-display', type=int, default=20)
    args = p.parse_args()

    files: List[str] = []
    for pth in args.paths:
        if os.path.isdir(pth):
            for root, _, fnames in os.walk(pth):
                for fn in fnames:
                    if fn.endswith('.js'):
                        files.append(os.path.join(root, fn))
        else:
            files.append(pth)

    allow = set(os.path.normpath(a) for a in args.allow)
    bad: List[Tuple[str, int, str]] = []
    for fp in files:
        if os.path.normpath(fp) in allow:
            continue
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                src = f.read()
        except Exception:
            continue
        comments = find_comments(src)
        if comments:
            for ln, _, txt in comments:
                bad.append((fp, ln, txt[:80].replace('\n', ' ')))

    if bad:
        sys.stderr.write('[no-js-comments] FAIL: production JS contains comments\n')
        for fp, ln, txt in bad[: args.max_display]:
            sys.stderr.write('  %s:%d  %s\n' % (fp, ln, txt))
        if len(bad) > args.max_display:
            sys.stderr.write('  ... and %d more\n' % (len(bad) - args.max_display))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
