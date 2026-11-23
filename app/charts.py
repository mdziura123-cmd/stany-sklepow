"""Proste wykresy SVG generowane po stronie serwera (bez zewnętrznych bibliotek).

Kolory pochodzą ze zmiennych CSS (--series-1, --series-2 …), więc działają
w trybie jasnym i ciemnym. Podpowiedzi (tooltip) obsługuje static/app.js
na podstawie atrybutów data-tip.
"""
import math
from html import escape
from markupsafe import Markup


def nice_max(v: float) -> float:
    if v <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def ticks(v: float) -> tuple[float, list[float]]:
    """Okrągłe podziałki osi Y (3–5 linii) i górna granica."""
    if v <= 0:
        return 1, [0, 0.25, 0.5, 0.75, 1]
    raw = v / 4
    exp = 10 ** math.floor(math.log10(raw))
    step = next(m * exp for m in ((1, 2, 2.5, 5, 10) if exp >= 10 else (1, 2, 5, 10)) if m * exp >= raw)
    if step < 1 and v >= 4:
        step = 1
    n = math.ceil(v / step - 1e-9)
    return step * n, [step * k for k in range(n + 1)]


def num(v: float) -> str:
    if v is None:
        return "–"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} mln".replace(".", ",")
    if abs(v) >= 10_000:
        return f"{v / 1000:.0f} tys."
    if float(v).is_integer():
        return f"{int(v):,}".replace(",", " ")
    return f"{v:,.1f}".replace(",", " ").replace(".", ",")


def short_day(d: str) -> str:
    return f"{d[8:10]}.{d[5:7]}" if len(d) >= 10 else d


def line_chart(labels: list[str], series: list[dict], height=240, fmt=num, area=False, unit=""):
    """series: [{"name": "Sprzedane", "var": "--series-1", "values": [..]}]"""
    if not labels:
        return Markup('<div class="chart-empty">Brak danych</div>')
    W, H = 720, height
    L, R, T, B = 52, 12, 14, 28
    pw, ph = W - L - R, H - T - B
    mx, yt = ticks(max([max([x or 0 for x in s["values"]] or [0]) for s in series] + [0]))
    n = len(labels)
    step = pw / max(n - 1, 1)

    def x(i):
        return L + (i * step if n > 1 else pw / 2)

    def y(v):
        return T + ph - (v or 0) / mx * ph

    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" data-chart="line" '
             f'data-left="{L}" data-right="{W - R}" data-top="{T}" data-bottom="{T + ph}">']
    for v in yt:
        yy = y(v)
        parts.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        parts.append(f'<text class="axis" x="{L - 8}" y="{yy + 4:.1f}" text-anchor="end">{escape(num(v))}</text>')
    xticks = sorted({0, n - 1, *(round(i * (n - 1) / 5) for i in range(1, 5))}) if n > 6 else range(n)
    for i in xticks:
        parts.append(f'<text class="axis" x="{x(i):.1f}" y="{H - 8}" text-anchor="middle">'
                     f'{escape(short_day(labels[i]))}</text>')
    for s in series:
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s["values"]))
        if area and n > 1:
            parts.append(f'<polygon class="area" style="fill:var({s["var"]})" '
                         f'points="{x(0):.1f},{T + ph} {pts} {x(n - 1):.1f},{T + ph}"/>')
        parts.append(f'<polyline class="line" style="stroke:var({s["var"]})" points="{pts}"/>')
        if n <= 31:
            for i, v in enumerate(s["values"]):
                parts.append(f'<circle class="dot" style="fill:var({s["var"]})" cx="{x(i):.1f}" '
                             f'cy="{y(v):.1f}" r="3"/>')
    # strefy podpowiedzi
    for i, lab in enumerate(labels):
        tip = short_day(lab) + "|" + "|".join(
            f'{s["name"]}: {fmt(s["values"][i])}{unit}' for s in series)
        x0 = x(i) - step / 2 if n > 1 else L
        w = step if n > 1 else pw
        parts.append(f'<rect class="hit" x="{max(L, x0):.1f}" y="{T}" width="{w:.1f}" height="{ph}" '
                     f'data-x="{x(i):.1f}" data-tip="{escape(tip)}"/>')
    parts.append('<line class="crosshair" x1="0" x2="0" y1="0" y2="0"/></svg>')
    return Markup("".join(parts))


def bar_chart(items: list[dict], var="--series-1", height=200, fmt=num, unit="", width=720):
    """items: [{"label": "Pon", "value": 12, "tip": "..."}]"""
    if not items:
        return Markup('<div class="chart-empty">Brak danych</div>')
    W, H = width, height
    L, R, T, B = 44, 8, 14, 28
    pw, ph = W - L - R, H - T - B
    mx, yt = ticks(max(i["value"] or 0 for i in items))
    n = len(items)
    slot = pw / n
    bw = min(56, slot * 0.62)
    every = max(1, math.ceil(n / 12))  # przy wielu słupkach podpisujemy co któryś
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">']
    for v in yt:
        yy = T + ph - v / mx * ph
        parts.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        parts.append(f'<text class="axis" x="{L - 8}" y="{yy + 4:.1f}" text-anchor="end">{escape(num(v))}</text>')
    for i, it in enumerate(items):
        h = (it["value"] or 0) / mx * ph
        cx = L + slot * i + slot / 2
        tip = it.get("tip") or f'{it["label"]}|{fmt(it["value"])}{unit}'
        if h > 0:
            r = min(4, bw / 2, h)
            x0, y0 = cx - bw / 2, T + ph - h
            # słupek z zaokrąglonym tylko górnym końcem
            parts.append(
                f'<path class="bar" style="fill:var({it.get("var") or var})" d="M{x0:.1f},{T + ph:.1f} V{y0 + r:.1f} '
                f'Q{x0:.1f},{y0:.1f} {x0 + r:.1f},{y0:.1f} H{x0 + bw - r:.1f} '
                f'Q{x0 + bw:.1f},{y0:.1f} {x0 + bw:.1f},{y0 + r:.1f} V{T + ph:.1f} Z"/>')
        parts.append(f'<rect class="hit" x="{L + slot * i:.1f}" y="{T}" width="{slot:.1f}" height="{ph}" '
                     f'data-tip="{escape(tip)}"/>')
        if i % every == 0:
            parts.append(f'<text class="axis" x="{cx:.1f}" y="{H - 8}" text-anchor="middle">'
                         f'{escape(it["label"])}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def sparkline(values: list[float], var="--series-1", width=120, height=32, fluid=False):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return Markup("")
    mn, mx = min(vals), max(vals)
    rng = (mx - mn) or 1
    n = len(vals)
    pts = " ".join(f"{i * (width - 4) / (n - 1) + 2:.1f},{height - 3 - (v - mn) / rng * (height - 6):.1f}"
                   for i, v in enumerate(vals))
    size = (f'preserveAspectRatio="none" style="width:100%;height:{height}px"' if fluid
            else f'width="{width}" height="{height}"')
    return Markup(f'<svg class="spark" viewBox="0 0 {width} {height}" {size}>'
                  f'<polyline style="stroke:var({var})" vector-effect="non-scaling-stroke" points="{pts}"/></svg>')


def step_chart(days: list[str], values: list[float], var="--series-1", height=200, unit=""):
    """Historia stanu produktu: stan zmienia się skokowo."""
    if len(days) < 2:
        return Markup('<div class="chart-empty">Za mało odczytów do wykresu</div>')
    return line_chart(days, [{"name": "Stan", "var": var, "values": values}], height=height, area=True, unit=unit)
