#!/usr/bin/env python3
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N_DIR = ROOT / 'scripts' / 'frontend' / 'static' / 'i18n'
LANGS = ['en', 'fr', 'ko', 'th', 'zh-CN', 'es', 'ja', 'pt', 'vi']

PATTERNS = [
    re.compile(r"i18n_dict\.get\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\$t\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bt\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"data-i18n(?:-key)?=\"([^\"]+)\""),
    re.compile(r"['\"](homeevent_[a-zA-Z0-9_\-\.]+)['\"]"),
]

SCAN_GLOBS = [
    'scripts/frontend/templates/**/*.html',
    'scripts/frontend/static/js/**/*.js',
    'scripts/static/**/*.html',
    'admin_v3_spa/src/**/*.{ts,tsx,vue,js,jsx}',
]

KEEP_PREFIXES = (
    'homeevent_',
    'banner.',
    'menu.',
    'nav.',
    'copytrader.',
    'discover.',
    'leaderboard.',
    'sportslive.',
    'homepage.',
    'ecosystem.',
    'admin.',
    'go.',
    'help.',
    'pm.',
)


MANUAL_EN = {
    'admin.common.cancel': 'Cancel',
    'admin.ecosystem.add': 'Add Ecosystem Item',
    'admin.ecosystem.openPublic': 'Open Public Page',
    'admin.ecosystem.refresh': 'Refresh',
    'admin.ecosystem.title': 'Ecosystem Management',
    'admin.frontendBanner.heroEn': 'Hero Text (EN)',
    'admin.frontendBanner.imageUrl': 'Image URL',
    'admin.frontendBanner.linkUrl': 'Link URL',
    'admin.frontendBanner.pages': 'Target Pages',
    'admin.frontendBanner.placement': 'Placement',
    'admin.frontendBanner.placement.hero': 'Hero',
    'admin.frontendBanner.placement.sidebar': 'Sidebar',
    'admin.frontendBanner.titleI18nKey': 'Title i18n Key',
    'admin.homepage.add': 'Add Homepage Card',
    'admin.homepage.refresh': 'Refresh',
    'admin.homepage.title': 'Homepage Cards',
    'admin.i18nFiles.btn.delete': 'Delete',
    'admin.i18nFiles.btn.reload': 'Reload',
    'admin.i18nFiles.btn.save': 'Save',
    'admin.i18nFiles.col.actions': 'Actions',
    'admin.i18nFiles.col.key': 'Key',
    'admin.i18nFiles.col.value': 'Value',
    'admin.i18nFiles.hint': 'Edit i18n key-value pairs safely.',
    'admin.i18nFiles.lang': 'Language',
    'admin.i18nFiles.newKey': 'New Key',
    'admin.i18nFiles.newValue': 'New Value',
    'admin.i18nFiles.search': 'Search',
    'admin.i18nFiles.search.ph': 'Search key/value...',
    'admin.i18nFiles.title': 'i18n Files',
    'admin.i18nFiles.totalKeys': 'Total Keys',
    'admin.menu.field.labelZhLegacy': 'Legacy Chinese Label',
    'admin.system.copytraderService': 'Copytrader Service',
    'admin.tab.ecosystem': 'Ecosystem',
    'admin.tab.frontend': 'Frontend',
    'admin.tab.homepage': 'Homepage',
    'admin.tab.i18nFiles': 'i18n Files',
    'admin.userModal.tpl.btn.applyPoints': 'Apply Points',
    'admin.userModal.tpl.pointsDelta': 'Points Delta',
    'admin.userModal.tpl.pointsReason': 'Adjustment Reason',
    'admin.userModal.tpl.pointsSection': 'Points Adjustment',
}


def smart_en_from_key(key: str) -> str:
    if key in MANUAL_EN:
        return MANUAL_EN[key]
    parts = re.split(r'[._\-]+', key)
    parts = [p for p in parts if p and not p.isdigit()]
    if not parts:
        return key
    words = []
    for p in parts[-4:]:
        words.append(p[:1].upper() + p[1:])
    return ' '.join(words)


def scan_used_keys() -> set[str]:
    used: set[str] = set()
    for g in SCAN_GLOBS:
        for p in ROOT.glob(g):
            try:
                txt = p.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
            for pat in PATTERNS:
                for m in pat.finditer(txt):
                    k = (m.group(1) or '').strip()
                    if not k:
                        continue
                    if k.startswith('/') or ' ' in k or len(k) > 160:
                        continue
                    used.add(k)
    return used


def load_lang(lang: str) -> dict:
    p = I18N_DIR / f'{lang}.json'
    return json.loads(p.read_text(encoding='utf-8'))


def write_lang(lang: str, data: dict):
    p = I18N_DIR / f'{lang}.json'
    ordered = {k: data[k] for k in sorted(data.keys())}
    p.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    en = load_lang('en')
    all_keys = set(en.keys())
    used = scan_used_keys()
    safe_keep = {k for k in all_keys if k.startswith(KEEP_PREFIXES)}

    retain = used | safe_keep
    missing_in_en = sorted(used - all_keys)

    for k in missing_in_en:
        en[k] = smart_en_from_key(k)

    retain = retain | set(missing_in_en)

    en_new = {k: en[k] for k in retain if k in en}
    write_lang('en', en_new)

    # align other languages to en keyset to guarantee full coverage
    en_keys = sorted(en_new.keys())
    for lang in LANGS:
        if lang == 'en':
            continue
        src = load_lang(lang)
        out = {}
        for k in en_keys:
            v = src.get(k)
            if isinstance(v, str) and v.strip() != '':
                out[k] = v
            else:
                # fallback to English baseline for missing entries
                out[k] = en_new[k]
        write_lang(lang, out)

    report = {
        'en_before': len(all_keys),
        'used_detected': len(used),
        'safe_keep': len(safe_keep),
        'en_missing_filled': len(missing_in_en),
        'en_after': len(en_new),
        'removed_from_en': len(all_keys) - len(set(en_new.keys()) & all_keys),
        'missing_keys_sample': missing_in_en[:80],
    }
    out = ROOT / 'logs' / 'i18n_audit_report_v2.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('I18N_AUDIT_DONE', report)


if __name__ == '__main__':
    main()
