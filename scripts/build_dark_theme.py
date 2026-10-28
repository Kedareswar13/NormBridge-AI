"""Generate the NormBridge AI dark theme from the app's existing stylesheets.

For every CSS rule that paints a light background, a dark text colour or a light border, emit an override
with the same selector (scoped under `html.pp-dark`) using the NormBridge AI ink/navy palette. The source
stylesheets stay untouched; the output is `frontend/src/styles/normbridge-dark.generated.css`.

Re-run after changing any stylesheet:   python scripts/build_dark_theme.py
"""
import colorsys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'frontend' / 'src'
OUT = SRC / 'styles' / 'normbridge-dark.generated.css'
INPUTS = ['index.css', 'App.css', 'pages/ChatApp.css', 'pages/Admin.css', 'pages/SystemOverview.css',
          'components/DocumentCompareView.css', 'components/PolicyDiffCompare.css', 'components/PolicyPdfPopup.css',
          'components/SourcePopup.css', 'components/EvalsPanel.css', 'styles/normbridge-theme.css']
SCOPE = 'html.pp-dark'
NAV_HUE = 228 / 360  # NormBridge AI ink/navy hue

BG_PROPS = {'background', 'background-color', 'background-image'}
TEXT_PROPS = {'color', 'fill', '-webkit-text-fill-color', 'caret-color'}
BORDER_PROPS = {'border', 'border-color', 'border-top', 'border-bottom', 'border-left', 'border-right',
                'border-top-color', 'border-bottom-color', 'border-left-color', 'border-right-color',
                'outline', 'outline-color', 'border-block', 'border-inline'}

NAMED = {'white': (255, 255, 255), 'black': (0, 0, 0), 'whitesmoke': (245, 245, 245), 'snow': (255, 250, 250),
         'ghostwhite': (248, 248, 255), 'ivory': (255, 255, 240), 'gainsboro': (220, 220, 220), 'lightgray': (211, 211, 211),
         'lightgrey': (211, 211, 211), 'gray': (128, 128, 128), 'grey': (128, 128, 128), 'silver': (192, 192, 192)}
COLOR_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|\b(?:' + '|'.join(NAMED) + r')\b', re.I)


# ----------------------------------------------------------------------------- css parsing
def strip_comments(css):
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def parse_blocks(css):
    """Yield (prelude, body) for each top-level block, respecting nested braces."""
    i, n = 0, len(css)
    while i < n:
        start = css.find('{', i)
        if start == -1:
            return
        prelude = css[i:start].strip()
        depth, j = 1, start + 1
        while j < n and depth:
            if css[j] == '{':
                depth += 1
            elif css[j] == '}':
                depth -= 1
            j += 1
        yield prelude, css[start + 1:j - 1]
        i = j


def split_top(text, sep):
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append(''.join(cur))
    return [p.strip() for p in parts if p.strip()]


def declarations(body):
    for d in split_top(body, ';'):
        if ':' in d:
            prop, val = d.split(':', 1)
            yield prop.strip().lower(), val.strip()


# ----------------------------------------------------------------------------- colours
def parse_color(tok):
    t = tok.strip().lower()
    if t in NAMED:
        return (*NAMED[t], 1.0)
    if t.startswith('#'):
        h = t[1:]
        if len(h) in (3, 4):
            h = ''.join(c * 2 for c in h)
        if len(h) not in (6, 8):
            return None
        r, g, b = (int(h[k:k + 2], 16) for k in (0, 2, 4))
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return r, g, b, a
    m = re.match(r'(rgba?|hsla?)\((.*)\)', t)
    if not m:
        return None
    nums = [x for x in re.split(r'[\s,/]+', m.group(2)) if x]
    try:
        if m.group(1).startswith('rgb'):
            r, g, b = (float(x.rstrip('%')) * (2.55 if x.endswith('%') else 1) for x in nums[:3])
        else:
            hh, ss, ll = float(nums[0].rstrip('deg')) / 360, float(nums[1].rstrip('%')) / 100, float(nums[2].rstrip('%')) / 100
            r, g, b = (c * 255 for c in colorsys.hls_to_rgb(hh, ll, ss))
        a = nums[3] if len(nums) > 3 else '1'
        a = float(a.rstrip('%')) / (100 if a.endswith('%') else 1)
        return r, g, b, a
    except (ValueError, IndexError):
        return None


def hsl(r, g, b):
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return h, s, l


def fmt(h, s, l, a):
    r, g, b = (round(c * 255) for c in colorsys.hls_to_rgb(h, max(0, min(1, l)), max(0, min(1, s))))
    return f'rgba({r}, {g}, {b}, {round(a, 3)})' if a < 0.999 else f'#{r:02x}{g:02x}{b:02x}'


def to_dark(color, role):
    r, g, b, a = color
    h, s, l = hsl(r, g, b)
    chroma = (max(r, g, b) - min(r, g, b)) / 255
    neutral = chroma < 0.2 or l > 0.97  # slate greys, beige/pastel tints and near-white count as neutral
    if role == 'bg':
        if neutral and l >= 0.55:
            return fmt(NAV_HUE, 0.34, 0.115 + (1 - l) * 0.85, a)
        if not neutral and l >= 0.85:
            return fmt(h, min(s, 0.5), 0.17, a)
        return None
    if role == 'text':
        if neutral and l < 0.6:
            return fmt(220 / 360, 0.22, 0.93 - l * 0.4, a)
        if not neutral and l < 0.42:
            return fmt(h, min(1, s), 0.7, a)
        return None
    if role == 'border':
        if neutral and l >= 0.55:
            return fmt(NAV_HUE, 0.28, 0.24, a)
        if not neutral and l >= 0.8:
            return fmt(h, 0.4, 0.32, a)
        return None
    return None


# ----------------------------------------------------------------------------- variables
def collect_vars(texts):
    out = {}
    for css in texts:
        for prelude, body in parse_blocks(css):
            if prelude.startswith('@media') or prelude.startswith('@supports'):
                for p2, b2 in parse_blocks(body):
                    if p2.strip() in (':root', 'html', 'body'):
                        out.update({k: v for k, v in declarations(b2) if k.startswith('--')})
            elif prelude.strip() in (':root', 'html', 'body'):
                out.update({k: v for k, v in declarations(body) if k.startswith('--')})
    return out


VAR_RE = re.compile(r'var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)')


def resolve_vars(value, varmap, depth=0):
    if depth > 5 or 'var(' not in value:
        return value
    def sub(m):
        v = varmap.get(m.group(1))
        return v if v is not None else (m.group(2) or m.group(0))
    return resolve_vars(VAR_RE.sub(sub, value), varmap, depth + 1)


def transform_value(prop, value, varmap):
    role = 'bg' if prop in BG_PROPS else 'text' if prop in TEXT_PROPS else 'border' if prop in BORDER_PROPS else None
    if not role:
        return None
    important = '!important' in value
    raw = value.replace('!important', '').strip()
    resolved = resolve_vars(raw, varmap)
    if 'var(' in resolved:
        return None
    changed = False

    def repl(m):
        nonlocal changed
        c = parse_color(m.group(0))
        if not c:
            return m.group(0)
        new = to_dark(c, role)
        if new is None:
            return m.group(0)
        changed = True
        return new
    out = COLOR_RE.sub(repl, resolved)
    if not changed:
        return None
    return out + (' !important' if important else '')


def scope_selector(sel):
    sel = sel.strip()
    for root in (':root', 'html'):
        if sel == root or sel.startswith(root + ' ') or sel.startswith(root + ':') or sel.startswith(root + '['):
            return SCOPE + sel[len(root):]
    return f'{SCOPE} {sel}'


def process(css, varmap):
    rules = []
    for prelude, body in parse_blocks(css):
        if prelude.startswith('@keyframes') or prelude.startswith('@-webkit-keyframes') or prelude.startswith('@font-face') or prelude.startswith('@import'):
            continue
        if prelude.startswith('@media') or prelude.startswith('@supports'):
            inner = process(body, varmap)
            if inner:
                rules.append(prelude + ' {\n' + '\n'.join('  ' + r.replace('\n', '\n  ') for r in inner) + '\n}')
            continue
        if prelude.startswith('@'):
            continue
        decls = []
        for prop, val in declarations(body):
            if prop.startswith('--'):
                continue
            new = transform_value(prop, val, varmap)
            if new:
                decls.append(f'  {prop}: {new};')
        if decls:
            selectors = ', '.join(scope_selector(s) for s in split_top(prelude, ','))
            rules.append(selectors + ' {\n' + '\n'.join(decls) + '\n}')
    return rules


# Inline JSX styles that would stay light/dark-on-dark (matched by their serialized style attribute).
INLINE_FIXES = """
html.pp-dark [style*="color: rgb(15, 23, 42)"], html.pp-dark [style*="color: rgb(30, 41, 59)"],
html.pp-dark [style*="color: rgb(51, 65, 85)"], html.pp-dark [style*="color: rgb(71, 85, 105)"],
html.pp-dark [style*="color: rgb(102, 102, 102)"] { color: #cbd5e1 !important; }
html.pp-dark [style*="background: rgb(255, 255, 255)"], html.pp-dark [style*="background: white"],
html.pp-dark [style*="background-color: rgb(255, 255, 255)"], html.pp-dark [style*="background: rgb(248, 250, 252)"] {
  background: #141c38 !important; }
html.pp-dark [style*="background: rgb(255, 251, 232)"] { background: rgba(245, 158, 11, 0.12) !important; }
html.pp-dark [style*="background: rgb(224, 242, 254)"] { background: rgba(14, 165, 233, 0.14) !important; }
html.pp-dark [style*="color: rgb(133, 77, 14)"] { color: #fcd34d !important; }
html.pp-dark [style*="color: rgb(3, 105, 161)"] { color: #7dd3fc !important; }
"""


def main():
    texts = {f: strip_comments((SRC / f).read_text(encoding='utf-8')) for f in INPUTS if (SRC / f).exists()}
    varmap = collect_vars(texts.values())
    parts = ['/* AUTO-GENERATED by scripts/build_dark_theme.py - do not edit by hand. */']
    total = 0
    for f, css in texts.items():
        rules = process(css, varmap)
        total += len(rules)
        if rules:
            parts.append(f'\n/* ---- from {f} ({len(rules)} rules) ---- */\n' + '\n'.join(rules))
    parts.append('\n/* ---- inline style fixes ---- */' + INLINE_FIXES)
    OUT.write_text('\n'.join(parts), encoding='utf-8')
    print(f'wrote {OUT.relative_to(ROOT)} with {total} rule overrides from {len(texts)} stylesheets')


if __name__ == '__main__':
    main()
