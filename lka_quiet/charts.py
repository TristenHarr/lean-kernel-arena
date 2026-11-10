"""The board's charts as inline SVG, generated here so a published round renders forever without
any script or font from elsewhere.

Every chart is deterministic text: the same board gives the same bytes. Colours are never written
into the SVG; marks carry classes (`s1`..`s3` for the three test families, `q0`..`q6` for the
sequential ramp, `muted`, `reference`, `grid`) that the page's stylesheet maps to its light and
dark tokens. Every mark that carries a value is focusable and has a <title>, which is its tooltip
without JavaScript; every chart on the page also has a table twin, so no value hides behind hover.
"""

import hashlib
import html
import math

_SVG = "http://www.w3.org/2000/svg"


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _n(x: float) -> str:
    """A coordinate, rounded so the output is stable and compact."""
    value = round(x, 2)
    return str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0")


def _uid(*parts) -> str:
    return "c" + hashlib.sha1("\x1f".join(map(str, parts)).encode()).hexdigest()[:10]


def factor_text(x: float) -> str:
    """Three significant figures, never fewer than two decimals below 10: 3.21×, 12.3×, 1.00×."""
    if x is None or not math.isfinite(x) or x <= 0:
        return "–"
    decimals = max(0, 2 - math.floor(math.log10(x)))
    return f"{x:.{decimals}f}×"


def _plain(x: float) -> str:
    decimals = max(0, 2 - math.floor(math.log10(x))) if x > 0 else 2
    return f"{x:.{decimals}f}"


def log_ticks(lo: float, hi: float) -> list:
    """Round values inside [lo, hi]: 1-2-5 for a narrow span, 1-3 for a middle one, decades beyond."""
    span = math.log10(hi / lo)
    if span > 3:
        mantissas = (1,)
    elif span > 1.2:
        mantissas = (1, 3)
    else:
        mantissas = (1, 2, 5)
    ticks = []
    for exponent in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for m in mantissas:
            value = m * 10.0**exponent
            value = round(value, max(0, -exponent + 1))
            if lo * (1 - 1e-9) <= value <= hi * (1 + 1e-9):
                ticks.append(int(value) if value >= 1 and value == int(value) else value)
    if len(ticks) > 9:
        step = math.ceil(len(ticks) / 8)
        ticks = [t for i, t in enumerate(ticks) if i % step == 0]
    return ticks


def _tick_label(t) -> str:
    return f"{t:g}×"


def seq_step(p: float):
    """0..6 on the seven-step sequential ramp, for a value in [0, 1]."""
    if p is None or not math.isfinite(p):
        return None
    return min(6, max(0, int(p * 7)))


class _Log:
    def __init__(self, lo: float, hi: float, x0: float, x1: float):
        self.lo, self.hi = math.log(lo), math.log(hi)
        self.x0, self.x1 = x0, x1

    def __call__(self, v: float) -> float:
        return self.x0 + (math.log(v) - self.lo) / (self.hi - self.lo) * (self.x1 - self.x0)


def _open(kind: str, title: str, subtitle: str, width: float, height: float) -> list[str]:
    uid = _uid(kind, title, subtitle)
    return [
        f'<svg xmlns="{_SVG}" viewBox="0 0 {_n(width)} {_n(height)}" width="100%" role="img" '
        f'aria-labelledby="{uid}-t {uid}-d" class="chart chart-{kind}">',
        f'<title id="{uid}-t">{_esc(title)}</title>',
        f'<desc id="{uid}-d">{_esc(subtitle)}</desc>',
    ]


def _domain(values, pad: float = 1.18) -> tuple[float, float]:
    finite = [v for v in values if v is not None and math.isfinite(v) and v > 0]
    lo, hi = min(finite), max(finite)
    lo, hi = min(lo, 1.0) / pad, max(hi, 1.0) * pad
    return lo, hi


def _rounded_bar(x0: float, x1: float, y: float, h: float, r: float = 4.0) -> str:
    """A horizontal bar square at the baseline x0, rounded at its data end x1."""
    r = min(r, (x1 - x0) / 2, h / 2)
    if r <= 0:
        return f"M{_n(x0)} {_n(y)}H{_n(x1)}V{_n(y + h)}H{_n(x0)}Z"
    return (f"M{_n(x0)} {_n(y)}H{_n(x1 - r)}Q{_n(x1)} {_n(y)} {_n(x1)} {_n(y + r)}"
            f"V{_n(y + h - r)}Q{_n(x1)} {_n(y + h)} {_n(x1 - r)} {_n(y + h)}H{_n(x0)}Z")


# ---------------------------------------------------------------------------------------------
# Forest plot: one row per checker, its estimate and 95% interval on a log axis around the reference


def forest(rows, *, title: str, subtitle: str = "", value_header: str = "× better than official [95% CI]",
           reference_label: str = "official") -> str:
    width, row_h, top, bottom = 780, 26, 34, 40
    rank_w, name_w, num_w = 40, 190, 200
    x0, x1 = rank_w + name_w + 12, width - num_w - 8
    height = top + max(len(rows), 1) * row_h + bottom
    out = _open("forest", title, subtitle, width, height)
    if not rows:
        out.append(f'<text class="empty" x="{_n(width / 2)}" y="{_n(height / 2)}" text-anchor="middle">'
                   f'No ranked checker yet</text></svg>')
        return "".join(out)
    lo, hi = _domain([r["lo"] for r in rows] + [r["hi"] for r in rows])
    x = _Log(lo, hi, x0, x1)
    plot_bottom = top + len(rows) * row_h
    out.append(f'<text class="head" x="{_n(rank_w + 4)}" y="18">Checker</text>')
    out.append(f'<text class="head" x="{_n(width - num_w + 4)}" y="18">{_esc(value_header)}</text>')
    for t in log_ticks(lo, hi):
        tx = x(t)
        out.append(f'<line class="grid" x1="{_n(tx)}" y1="{top - 6}" x2="{_n(tx)}" y2="{_n(plot_bottom)}"/>')
        out.append(f'<text class="tick" x="{_n(tx)}" y="{_n(plot_bottom + 18)}" text-anchor="middle">{_tick_label(t)}</text>')
    rx = x(1.0)
    out.append(f'<line class="reference" x1="{_n(rx)}" y1="{top - 10}" x2="{_n(rx)}" y2="{_n(plot_bottom)}"/>')
    out.append(f'<text class="reference-label" x="{_n(rx)}" y="{top - 14}" text-anchor="middle">{_esc(reference_label)}</text>')
    for i, row in enumerate(rows):
        cy = top + i * row_h + row_h / 2
        estimate = factor_text(row["factor"])
        interval = f"{estimate} [{_plain(row['lo'])}, {_plain(row['hi'])}]"
        tied = " tied" if row.get("tied") else ""
        out.append(f'<g class="row{tied}" tabindex="0" data-tip="{_esc(interval)}" data-tip-label="{_esc(row["name"])}">')
        out.append(f'<title>{_esc(row["name"])}: {_esc(interval)}, rank {_esc(row["rank"])}</title>')
        out.append(f'<rect class="hit" x="0" y="{_n(cy - row_h / 2)}" width="{width}" height="{row_h}"/>')
        out.append(f'<text class="rank" x="{_n(rank_w - 6)}" y="{_n(cy + 4)}" text-anchor="end">{_esc(row["rank"])}</text>')
        name = f'<text class="name" x="{_n(rank_w + 4)}" y="{_n(cy + 4)}">{_esc(row["name"])}</text>'
        out.append(f'<a href="{_esc(row["href"])}">{name}</a>' if row.get("href") else name)
        out.append(f'<line class="ci s1" x1="{_n(x(row["lo"]))}" y1="{_n(cy)}" x2="{_n(x(row["hi"]))}" y2="{_n(cy)}"/>')
        out.append(f'<circle class="pt s1" cx="{_n(x(row["factor"]))}" cy="{_n(cy)}" r="4.5"/>')
        out.append(f'<text class="num" x="{_n(width - num_w + 4)}" y="{_n(cy + 4)}">{_esc(interval)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Head-to-head matrix: P(row beats column) on the sequential ramp


def heatmap(names, matrix, *, title: str, subtitle: str = "", hrefs=None) -> str:
    n = len(names)
    cell, left, top = 16, 160, 132
    width, height = left + n * cell + 24, top + n * cell + 64
    out = _open("heatmap", title, subtitle, width, height)
    for j, col in enumerate(names):
        cx = left + j * cell + cell / 2
        out.append(f'<text class="col-label" transform="translate({_n(cx + 4)} {top - 6}) rotate(-55)">{_esc(col)}</text>')
    for i, row in enumerate(names):
        cy = top + i * cell
        out.append(f'<text class="row-label" x="{left - 6}" y="{_n(cy + cell * 0.72)}" text-anchor="end">{_esc(row)}</text>')
        for j, col in enumerate(names):
            cx = left + j * cell
            if row == col:
                out.append(f'<rect class="diag" x="{_n(cx + 1)}" y="{_n(cy + 1)}" width="{cell - 2}" height="{cell - 2}"/>')
                continue
            p = matrix.get(row, {}).get(col)
            step = seq_step(p)
            if step is None:
                continue
            tip = f"{row} beats {col} in {p * 100:.1f}% of resamples"
            out.append(f'<rect class="cell q{step}" tabindex="0" data-pair="{_esc(row)}/{_esc(col)}" '
                       f'data-tip="{p * 100:.1f}%" data-tip-label="{_esc(row)} beats {_esc(col)}" '
                       f'x="{_n(cx + 1)}" y="{_n(cy + 1)}" width="{cell - 2}" height="{cell - 2}"><title>{_esc(tip)}</title></rect>')
    ly = top + n * cell + 22
    out.append(f'<text class="legend-caption" x="{left}" y="{ly}">P(row beats column)</text>')
    for k in range(7):
        out.append(f'<rect class="swatch q{k}" x="{left + 150 + k * 22}" y="{ly - 11}" width="20" height="12"/>')
    for k, label in ((0, "0"), (3, "½"), (6, "1")):
        out.append(f'<text class="tick" x="{left + 160 + k * 22}" y="{ly + 16}" text-anchor="middle">{label}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Pareto scatter: two category factors, log-log, the non-dominated front drawn and labelled


def pareto(points, *, title: str, x_label: str, y_label: str, subtitle: str = "") -> str:
    width, height = 780, 500
    left, right, top, bottom = 70, 150, 24, 56
    out = _open("pareto", title, subtitle, width, height)
    if not points:
        out.append("</svg>")
        return "".join(out)
    xlo, xhi = _domain([p["x"] for p in points])
    ylo, yhi = _domain([p["y"] for p in points])
    x = _Log(xlo, xhi, left, width - right)
    y_inv = _Log(ylo, yhi, 0, height - top - bottom)
    y = lambda v: height - bottom - y_inv(v)
    for t in log_ticks(xlo, xhi):
        out.append(f'<line class="grid" x1="{_n(x(t))}" y1="{top}" x2="{_n(x(t))}" y2="{height - bottom}"/>')
        out.append(f'<text class="tick" x="{_n(x(t))}" y="{height - bottom + 18}" text-anchor="middle">{_tick_label(t)}</text>')
    for t in log_ticks(ylo, yhi):
        out.append(f'<line class="grid" x1="{left}" y1="{_n(y(t))}" x2="{width - right}" y2="{_n(y(t))}"/>')
        out.append(f'<text class="tick" x="{left - 8}" y="{_n(y(t) + 4)}" text-anchor="end">{_tick_label(t)}</text>')
    out.append(f'<line class="reference" x1="{_n(x(1))}" y1="{top}" x2="{_n(x(1))}" y2="{height - bottom}"/>')
    out.append(f'<line class="reference" x1="{left}" y1="{_n(y(1))}" x2="{width - right}" y2="{_n(y(1))}"/>')
    out.append(f'<text class="axis-label" x="{_n((left + width - right) / 2)}" y="{height - 14}" text-anchor="middle">{_esc(x_label)} →</text>')
    out.append(f'<text class="axis-label" transform="translate(18 {_n((top + height - bottom) / 2)}) rotate(-90)" '
               f'text-anchor="middle">{_esc(y_label)} →</text>')
    front = sorted((p for p in points if p["pareto"]), key=lambda p: (p["x"], -p["y"]))
    if len(front) >= 1:
        coords = []
        for k, p in enumerate(front):
            if k:
                coords.append(f"{_n(x(front[k - 1]['x']))},{_n(y(p['y']))}")
            coords.append(f"{_n(x(p['x']))},{_n(y(p['y']))}")
        out.append(f'<polyline class="front s1" points="{" ".join(coords)}"/>')
    for p in sorted(points, key=lambda p: p["pareto"]):
        cls = "s1" if p["pareto"] else "muted"
        px, py = x(p["x"]), y(p["y"])
        tip = f"{factor_text(p['x'])} {x_label}, {factor_text(p['y'])} {y_label}"
        out.append(f'<g class="point" tabindex="0" data-tip="{_esc(tip)}" data-tip-label="{_esc(p["name"])}">'
                   f'<title>{_esc(p["name"])}: {_esc(tip)}</title>'
                   f'<circle class="hit" cx="{_n(px)}" cy="{_n(py)}" r="12"/>'
                   f'<circle class="pt {cls}" cx="{_n(px)}" cy="{_n(py)}" r="4.5"/>')
        if p["pareto"]:
            out.append(f'<text class="direct-label" x="{_n(px + 9)}" y="{_n(py + 4)}">{_esc(p["name"])}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Strip plot: every test as a dot, by family, at its ratio to the reference


def strip(groups, *, title: str, subtitle: str = "") -> str:
    width, row_h, top, bottom, left, gutter = 780, 54, 16, 40, 130, 46
    height = top + len(groups) * row_h + bottom
    out = _open("strip", title, subtitle, width, height)
    ratios = [r for _, tests in groups for _, r, failed in tests if not failed and r]
    lo, hi = _domain(ratios or [1.0])
    x = _Log(lo, hi, left + gutter, width - 16)
    plot_bottom = top + len(groups) * row_h
    for t in log_ticks(lo, hi):
        out.append(f'<line class="grid" x1="{_n(x(t))}" y1="{top}" x2="{_n(x(t))}" y2="{_n(plot_bottom)}"/>')
        out.append(f'<text class="tick" x="{_n(x(t))}" y="{_n(plot_bottom + 18)}" text-anchor="middle">{_tick_label(t)}</text>')
    out.append(f'<line class="reference" x1="{_n(x(1))}" y1="{top}" x2="{_n(x(1))}" y2="{_n(plot_bottom)}"/>')
    out.append(f'<text class="tick" x="{left + gutter / 2}" y="{_n(plot_bottom + 18)}" text-anchor="middle">failed</text>')
    for i, (family, tests) in enumerate(groups):
        series = f"s{min(i, 2) + 1}"
        cy = top + i * row_h + row_h / 2
        out.append(f'<line class="grid" x1="{left}" y1="{_n(top + (i + 1) * row_h)}" x2="{width - 16}" y2="{_n(top + (i + 1) * row_h)}"/>')
        out.append(f'<text class="row-label" x="{left - 10}" y="{_n(cy + 4)}" text-anchor="end">{_esc(family)}</text>')
        for name, ratio, failed in tests:
            jitter = (int(hashlib.sha1(name.encode()).hexdigest()[:6], 16) / 0xFFFFFF - 0.5) * (row_h - 22)
            py = cy + jitter
            if failed:
                gx = left + gutter / 2
                out.append(f'<g class="failed" tabindex="0" data-tip="failed" data-tip-label="{_esc(name)}">'
                           f'<title>{_esc(name)}: failed (scored at twice its cap)</title>'
                           f'<path class="cross {series}" d="M{_n(gx - 4)} {_n(py - 4)}L{_n(gx + 4)} {_n(py + 4)}'
                           f'M{_n(gx + 4)} {_n(py - 4)}L{_n(gx - 4)} {_n(py + 4)}"/></g>')
                continue
            tip = factor_text(ratio)
            out.append(f'<g class="test" tabindex="0" data-tip="{_esc(tip)}" data-tip-label="{_esc(name)}">'
                       f'<title>{_esc(name)}: {tip} the reference</title>'
                       f'<circle class="dot {series}" cx="{_n(x(ratio))}" cy="{_n(py)}" r="4"/></g>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Bars: a value per checker against a stated ideal; rank ranges under weight perturbation


def scaled_bars(items, *, title: str, ideal: float | None = None, unit: str = "", subtitle: str = "") -> str:
    width, row_h, top, bottom, left, right = 780, 24, 22, 34, 200, 90
    height = top + max(len(items), 1) * row_h + bottom
    out = _open("bars", title, subtitle, width, height)
    top_value = max([v for _, v in items] + [ideal or 0]) * 1.06 or 1
    scale = lambda v: left + v / top_value * (width - left - right)
    plot_bottom = top + len(items) * row_h
    for i, (name, value) in enumerate(items):
        y0 = top + i * row_h + 6
        label = f"{value:.2f}{unit}"
        out.append(f'<g class="row" tabindex="0" data-tip="{_esc(label)}" data-tip-label="{_esc(name)}">'
                   f'<title>{_esc(name)}: {_esc(label)}</title>'
                   f'<text class="name" x="{left - 8}" y="{_n(y0 + 10)}" text-anchor="end">{_esc(name)}</text>'
                   f'<path class="bar s1" d="{_rounded_bar(left, scale(value), y0, 12)}"/>'
                   f'<text class="num" x="{_n(scale(value) + 6)}" y="{_n(y0 + 10)}">{_esc(label)}</text></g>')
    out.append(f'<line class="axis" x1="{left}" y1="{top - 4}" x2="{left}" y2="{_n(plot_bottom)}"/>')
    if ideal:
        ix = scale(ideal)
        out.append(f'<line class="reference" x1="{_n(ix)}" y1="{top - 4}" x2="{_n(ix)}" y2="{_n(plot_bottom)}"/>')
        out.append(f'<text class="reference-label" x="{_n(ix)}" y="{_n(plot_bottom + 18)}" text-anchor="middle">ideal {ideal:g}{unit}</text>')
    out.append("</svg>")
    return "".join(out)


def range_bars(items, *, title: str, subtitle: str = "") -> str:
    """items: (name, rank, best, worst) - the rank under the season's weights and its range."""
    width, row_h, top, bottom, left, right = 780, 24, 22, 34, 200, 70
    height = top + max(len(items), 1) * row_h + bottom
    out = _open("ranges", title, subtitle, width, height)
    worst = max([w for *_, w in items] + [2])
    scale = lambda r: left + (r - 1) / (worst - 1) * (width - left - right)
    plot_bottom = top + len(items) * row_h
    for r in range(1, worst + 1):
        if worst <= 12 or r == 1 or r % 5 == 0:
            out.append(f'<line class="grid" x1="{_n(scale(r))}" y1="{top - 4}" x2="{_n(scale(r))}" y2="{_n(plot_bottom)}"/>')
            out.append(f'<text class="tick" x="{_n(scale(r))}" y="{_n(plot_bottom + 18)}" text-anchor="middle">{r}</text>')
    for i, (name, rank, best, worst_rank) in enumerate(items):
        cy = top + i * row_h + row_h / 2
        span = f"{best}" if best == worst_rank else f"{best}–{worst_rank}"
        out.append(f'<g class="row" tabindex="0" data-tip="{span}" data-tip-label="{_esc(name)}">'
                   f'<title>{_esc(name)}: rank {rank}, between {best} and {worst_rank} under every weighting tried</title>'
                   f'<text class="name" x="{left - 8}" y="{_n(cy + 4)}" text-anchor="end">{_esc(name)}</text>'
                   f'<line class="ci s1" x1="{_n(scale(best))}" y1="{_n(cy)}" x2="{_n(scale(worst_rank))}" y2="{_n(cy)}"/>'
                   f'<circle class="pt s1" cx="{_n(scale(rank))}" cy="{_n(cy)}" r="4.5"/>'
                   f'<text class="num" x="{width - right + 10}" y="{_n(cy + 4)}">{span}</text></g>')
    out.append("</svg>")
    return "".join(out)
