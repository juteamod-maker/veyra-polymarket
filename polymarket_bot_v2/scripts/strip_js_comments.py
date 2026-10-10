from __future__ import annotations
import argparse
import os
import sys
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_no_js_comments import find_comments


def strip_comments(src: str) -> str:
    spans = find_comments(src)
    if not spans:
        return src
    out: List[str] = []
    last = 0
    n = len(src)
    for _ln, start, txt in spans:
        end = start + len(txt)
        out.append(src[last:start])
        if start > 0 and src[start - 1] != '\n' and end < n and src[end] == '\n':
            j = start
            while j > 0 and src[j - 1] in (' ', '\t'):
                j -= 1
            if j == 0 or src[j - 1] == '\n':
                out[-1] = src[last:j]
                last = end + 1
                continue
        last = end
    out.append(src[last:])
    cleaned = ''.join(out)
    lines = cleaned.split('\n')
    pruned: List[str] = []
    blank_run = 0
    for ln in lines:
        if ln.strip() == '':
            blank_run += 1
            if blank_run <= 1:
                pruned.append('')
        else:
            blank_run = 0
            pruned.append(ln.rstrip())
    while pruned and pruned[-1] == '':
        pruned.pop()
    return '\n'.join(pruned) + '\n'


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('paths', nargs='+')
    p.add_argument('--write', action='store_true')
    args = p.parse_args()

    for fp in args.paths:
        with open(fp, 'r', encoding='utf-8') as f:
            src = f.read()
        cleaned = strip_comments(src)
        if args.write:
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(cleaned)
            sys.stdout.write('stripped: %s (%d -> %d bytes)\n' % (fp, len(src), len(cleaned)))
        else:
            sys.stdout.write(cleaned)
    return 0


if __name__ == '__main__':
    sys.exit(main())
