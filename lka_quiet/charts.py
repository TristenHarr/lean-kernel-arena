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


def label_width(text: str, size: float = 12.0) -> float:
    """A generous estimate of a label's rendered width in the system sans at `size` px."""
    return len(text) * size * 0.6 + 2


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# Where a direct label may go relative to its point: (dx, dy, anchor), in order of preference.
_LABEL_SLOTS = ((9, 4, "start"), (-9, 4, "end"), (9, -12, "start"), (9, 20, "start"), (-9, -12, "end"),
                (-9, 20, "end"), (9, -28, "start"), (9, 36, "start"), (-9, -28, "end"), (-9, 36, "end"))


def _place_labels(items, width: float, height: float):
    """Greedy collision-free placement: items are (name, px, py); returns (name, px, py, x, y, anchor)."""
    obstacles = [(px - 7, py - 7, px + 7, py + 7) for _, px, py in items]
    placed = []
    out = []
    for name, px, py in items:
        w = label_width(name)
        choice = None
        for dx, dy, anchor in _LABEL_SLOTS:
            x0 = px + dx if anchor == "start" else px + dx - w
            box = (x0, py + dy - 11, x0 + w, py + dy + 3)
            inside = box[0] >= 0 and box[2] <= width and box[1] >= 0 and box[3] <= height
            own = (px - 7, py - 7, px + 7, py + 7)
            if inside and not any(_overlaps(box, b) for b in placed) and \
                    not any(_overlaps(box, b) for b in obstacles if b != own):
                choice = (dx, dy, anchor, box)
                break
        if choice is None:
            dx, dy, anchor = _LABEL_SLOTS[0]
            x0 = px + dx
            choice = (dx, dy, anchor, (x0, py + dy - 11, x0 + w, py + dy + 3))
        dx, dy, anchor, box = choice
        placed.append(box)
        out.append((name, px, py, px + dx, py + dy, anchor))
    return out


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
        f'<svg xmlns="{_SVG}" viewBox="0 0 {_n(width)} {_n(height)}" width="{_n(width)}" height="{_n(height)}" role="img" '
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


def forest(rows, *, title: str, subtitle: str = "", value_header: str = "× official [95% CI]",
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


def byte_ticks(lo: float, hi: float) -> list:
    """Powers of two inside [lo, hi], thinned to at most eight at a constant ratio."""
    ticks = [2.0**k for k in range(math.ceil(math.log2(lo)), math.floor(math.log2(hi)) + 1)]
    ticks = [int(t) if t >= 1 else t for t in ticks]
    if len(ticks) > 8:
        step = math.ceil(len(ticks) / 8)
        ticks = ticks[::step]
    return ticks


_TIME_STEPS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600,
               1200, 1800, 3600, 7200, 14400, 28800, 86400)


def time_ticks(lo: float, hi: float) -> list:
    """Round durations in seconds inside [lo, hi] (1-2-5 below a minute, then 1, 2, 5, 10, 20, 30
    minutes, then hours), thinned to at most eight."""
    ticks = [t for t in _TIME_STEPS if lo * (1 - 1e-9) <= t <= hi * (1 + 1e-9)]
    if len(ticks) > 8:
        step = math.ceil(len(ticks) / 8)
        ticks = ticks[::step]
    return ticks


def _off_scale_cut(values, better: str, gap: float = 5.0, share: float = 0.25):
    """Where to cut an axis so a few points far beyond the rest go to a gutter: the worse side of
    the widest gap of at least `gap` times between neighbouring values, when no more than `share`
    of the points lie beyond it; None when there is no such gap."""
    vs = sorted(v for v in values if v and v > 0)
    if better == "low":
        vs = [1 / v for v in vs]
    best = None
    for i in range(len(vs) - 1):
        ratio = vs[i + 1] / vs[i]
        if ratio >= gap and i + 1 <= share * len(vs) and (best is None or ratio > best[0]):
            best = (ratio, vs[i + 1])
    if best is None:
        return None
    return best[1] if better == "high" else 1 / best[1]


def _beyond(v: float, cut, better: str) -> bool:
    return cut is not None and (v < cut if better == "high" else v > cut)


def _spaced(ticks, position, label, size: float = 11.0) -> list:
    """The ticks whose labels fit beside the one before, in order of position."""
    kept = []
    for t in sorted(ticks, key=position):
        if kept:
            last = kept[-1]
            if position(t) - position(last) < (label_width(label(t), size) + label_width(label(last), size)) / 2 + 4:
                continue
        kept.append(t)
    return kept


def frontier_path(points, *, better: tuple[str, str], worse_edges: tuple[float, float]) -> list[tuple[float, float]]:
    """The boundary of the region a Pareto front dominates, in data units and in increasing x: from
    the worse edge of the plot along each front point, stepping through the corner each pair of
    neighbours leaves (the worse of their two x and the worse of their two y), out to the worse edge
    of the other axis. `better` is "high" or "low" per axis."""
    sign = tuple(1 if b == "high" else -1 for b in better)
    front = sorted(points, key=lambda p: (sign[0] * p[0], sign[1] * p[1]))
    path = [(worse_edges[0], front[0][1])]
    for k, (x, y) in enumerate(front):
        if k:
            path.append((front[k - 1][0], y))
        path.append((x, y))
    path.append((front[-1][0], worse_edges[1]))
    return path if better[0] == "high" else path[::-1]


def _span(values, extra=(), pad: float = 1.18) -> tuple[float, float]:
    finite = [v for v in list(values) + list(extra) if v is not None and math.isfinite(v) and v > 0]
    return min(finite) / pad, max(finite) * pad


def pareto(points, *, title: str, x_label: str, y_label: str, subtitle: str = "",
           better: tuple[str, str] = ("high", "high"), x_ticks=log_ticks, y_ticks=log_ticks,
           x_format=_tick_label, y_format=_tick_label, x_value=factor_text, y_value=factor_text,
           reference=(1.0, 1.0), reference_label: str = "official",
           labels: str = "front", width: float = 780, height: float = 500) -> str:
    """A scatter on two log axes with its Pareto front: the front's points emphasised and labelled,
    the boundary of what they dominate drawn as a staircase out to the axes and the dominated region
    shaded. `better` says, per axis, whether more ("high") or less ("low") is better; points carry
    name, x, y and pareto. Ticks are written by x_format and y_format, a point's own values in its
    tooltip by x_value and y_value. A few points beyond a fivefold gap on the worse side of an axis
    sit in a gutter marked off scale. With labels="all" every point is labelled where it fits."""
    compact = width < 600
    left, right, top, bottom = (64, 24, 18, 46) if compact else (76, 150, 24, 56)
    out = _open("pareto", title, subtitle, width, height)
    if not points:
        out.append("</svg>")
        return "".join(out)
    cut_x = _off_scale_cut([p["x"] for p in points], better[0])
    cut_y = _off_scale_cut([p["y"] for p in points], better[1])
    xlo, xhi = _span([p["x"] for p in points if not _beyond(p["x"], cut_x, better[0])], [reference[0]] if reference else [])
    ylo, yhi = _span([p["y"] for p in points if not _beyond(p["y"], cut_y, better[1])], [reference[1]] if reference else [])
    gutter = 22
    x0 = left + (gutter if cut_x is not None and better[0] == "high" else 0)
    x1 = width - right - (gutter if cut_x is not None and better[0] == "low" else 0)
    y_top = top + (gutter if cut_y is not None and better[1] == "low" else 0)
    y_bottom = height - bottom - (gutter if cut_y is not None and better[1] == "high" else 0)
    x = _Log(xlo, xhi, x0, x1)
    y_inv = _Log(ylo, yhi, 0, y_bottom - y_top)
    y = lambda v: y_bottom - y_inv(v)
    gutter_x = (left + 8) if better[0] == "high" else (width - right - 8)
    gutter_y = (height - bottom - 8) if better[1] == "high" else (top + 8)
    px_of = lambda v: gutter_x if _beyond(v, cut_x, better[0]) else x(v)
    py_of = lambda v: gutter_y if _beyond(v, cut_y, better[1]) else y(v)
    for t in _spaced(x_ticks(xlo, xhi), x, x_format):
        out.append(f'<line class="grid" x1="{_n(x(t))}" y1="{y_top}" x2="{_n(x(t))}" y2="{_n(y_bottom)}"/>')
        out.append(f'<text class="tick" x="{_n(x(t))}" y="{height - bottom + 18}" text-anchor="middle">{_esc(x_format(t))}</text>')
    for t in y_ticks(ylo, yhi):
        out.append(f'<line class="grid" x1="{_n(x0)}" y1="{_n(y(t))}" x2="{_n(x1)}" y2="{_n(y(t))}"/>')
        out.append(f'<text class="tick" x="{left - 8}" y="{_n(y(t) + 4)}" text-anchor="end">{_esc(y_format(t))}</text>')
    if cut_x is not None:
        edge = x0 if better[0] == "high" else x1
        out.append(f'<line class="gutter" x1="{_n(edge)}" y1="{y_top}" x2="{_n(edge)}" y2="{_n(y_bottom)}"/>')
        out.append(f'<text class="tick off-scale-label" transform="translate({_n(gutter_x + 4)} {_n(y_top + 4)}) rotate(90)">off scale</text>')
    if cut_y is not None:
        edge = y_bottom if better[1] == "high" else y_top
        out.append(f'<line class="gutter" x1="{_n(x0)}" y1="{_n(edge)}" x2="{_n(x1)}" y2="{_n(edge)}"/>')
        out.append(f'<text class="tick off-scale-label" x="{_n(x1 - 4)}" y="{_n(gutter_y + 4)}" text-anchor="end">off scale</text>')
    if reference:
        rx, ry = x(reference[0]), y(reference[1])
        out.append(f'<line class="reference" x1="{_n(rx)}" y1="{y_top}" x2="{_n(rx)}" y2="{_n(y_bottom)}"/>')
        out.append(f'<line class="reference" x1="{_n(x0)}" y1="{_n(ry)}" x2="{_n(x1)}" y2="{_n(ry)}"/>')
        ly = (y_bottom - 6) if better[1] == "high" else (y_top + 10)
        out.append(f'<text class="tick reference-label" x="{_n(rx + 4)}" y="{_n(ly)}">{_esc(reference_label)}</text>')
    x_text = f"{x_label} →" if better[0] == "high" else f"← {x_label}"
    y_text = f"{y_label} →" if better[1] == "high" else f"← {y_label}"
    out.append(f'<text class="axis-label" x="{_n((left + width - right) / 2)}" y="{height - 12}" text-anchor="middle">{_esc(x_text)}</text>')
    out.append(f'<text class="axis-label" transform="translate({14 if compact else 18} {_n((top + height - bottom) / 2)}) rotate(-90)" '
               f'text-anchor="middle">{_esc(y_text)}</text>')
    front = [p for p in points if p["pareto"] and not _beyond(p["x"], cut_x, better[0]) and not _beyond(p["y"], cut_y, better[1])]
    if front:
        edges = (xlo if better[0] == "high" else xhi, ylo if better[1] == "high" else yhi)
        path = frontier_path([(p["x"], p["y"]) for p in front], better=better, worse_edges=edges)
        pixels = [(x(px), y(py)) for px, py in path]
        corner = (x(edges[0]), y(edges[1]))
        out.append(f'<polygon class="dominated" points="{" ".join(f"{_n(a)},{_n(b)}" for a, b in pixels + [corner])}"/>')
        out.append(f'<polyline class="front s1" points="{" ".join(f"{_n(a)},{_n(b)}" for a, b in pixels)}"/>')
    labelled = [p for p in points if p["pareto"] or labels == "all"]
    labelled.sort(key=lambda p: (not p["pareto"], y(p["y"]), x(p["x"])))
    placement = {name: (lx, ly, anchor) for name, px, py, lx, ly, anchor in
                 _place_labels([(p["name"], px_of(p["x"]), py_of(p["y"])) for p in labelled], width, height)}
    for p in sorted(points, key=lambda p: (p["pareto"], p["name"])):
        cls = "s1" if p["pareto"] else "muted"
        px, py = px_of(p["x"]), py_of(p["y"])
        off = _beyond(p["x"], cut_x, better[0]) or _beyond(p["y"], cut_y, better[1])
        tip = f"{x_value(p['x'])} {x_label}, {y_value(p['y'])} {y_label}"
        out.append(f'<g class="point{" off-scale" if off else ""}" tabindex="0" data-tip="{_esc(tip)}" data-tip-label="{_esc(p["name"])}">'
                   f'<title>{_esc(p["name"])}: {_esc(tip)}</title>'
                   f'<circle class="hit" cx="{_n(px)}" cy="{_n(py)}" r="12"/>'
                   f'<circle class="pt {cls}" cx="{_n(px)}" cy="{_n(py)}" r="{3.5 if compact else 4.5}"/>')
        if p["name"] in placement:
            lx, ly, anchor = placement[p["name"]]
            if ly != py + 4:
                edge = lx - 2 if anchor == "start" else lx + 2
                out.append(f'<line class="leader" x1="{_n(px)}" y1="{_n(py)}" x2="{_n(edge)}" y2="{_n(ly - 4)}"/>')
            anchor_attr = ' text-anchor="end"' if anchor == "end" else ""
            minor = "" if p["pareto"] else " minor"
            out.append(f'<text class="direct-label{minor}" x="{_n(lx)}" y="{_n(ly)}"{anchor_attr}>{_esc(p["name"])}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Parallel coordinates: one line per checker across the Pareto metrics, better up on every axis


def parallel(rows, axes, *, title: str, subtitle: str = "") -> str:
    """rows: name, values (axis id -> factor, bigger better) and front; axes: (id, title) in order.
    Every axis shares one log scale, so a line's slope between two axes is a real trade-off."""
    width, height = 780, 440
    left, right, top, bottom = 64, 150, 40, 36
    out = _open("parallel", title, subtitle, width, height)
    if not rows or not axes:
        out.append("</svg>")
        return "".join(out)
    values = [r["values"][a] for r in rows for a, _ in axes]
    lo, hi = _span(values, [1.0])
    y_inv = _Log(lo, hi, 0, height - top - bottom)
    y = lambda v: height - bottom - y_inv(v)
    step = (width - left - right) / max(len(axes) - 1, 1)
    xs = [left + i * step for i in range(len(axes))]
    for t in log_ticks(lo, hi):
        out.append(f'<line class="grid" x1="{left}" y1="{_n(y(t))}" x2="{_n(xs[-1])}" y2="{_n(y(t))}"/>')
        out.append(f'<text class="tick" x="{left - 10}" y="{_n(y(t) + 4)}" text-anchor="end">{_tick_label(t)}</text>')
    out.append(f'<line class="reference" x1="{left}" y1="{_n(y(1))}" x2="{_n(xs[-1])}" y2="{_n(y(1))}"/>')
    for (axis_id, axis_title), ax in zip(axes, xs):
        out.append(f'<line class="axis" x1="{_n(ax)}" y1="{top}" x2="{_n(ax)}" y2="{height - bottom}"/>')
        out.append(f'<text class="axis-title" x="{_n(ax)}" y="{top - 14}" text-anchor="middle">{_esc(axis_title)}</text>')
    ordered = sorted(rows, key=lambda r: (r["front"], r["name"]))
    ends = []
    for r in ordered:
        points = " ".join(f"{_n(ax)},{_n(y(r['values'][a]))}" for (a, _), ax in zip(axes, xs))
        cls = "s1" if r["front"] else "muted"
        tip = ", ".join(f"{t} {factor_text(r['values'][a])}" for a, t in axes)
        out.append(f'<g class="series" tabindex="0" data-tip="{_esc(tip)}" data-tip-label="{_esc(r["name"])}">'
                   f'<title>{_esc(r["name"])}: {_esc(tip)}</title>'
                   f'<polyline class="hit-line" points="{points}"/>'
                   f'<polyline class="line {cls}" data-name="{_esc(r["name"])}" points="{points}"/>')
        for (a, _), ax in zip(axes, xs):
            out.append(f'<circle class="pt {cls}" cx="{_n(ax)}" cy="{_n(y(r["values"][a]))}" r="{3.5 if r["front"] else 2.5}"/>')
        out.append("</g>")
        if r["front"]:
            ends.append([y(r["values"][axes[-1][0]]), r["name"]])
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 14)
    for ly, name in ends:
        out.append(f'<text class="direct-label" x="{_n(xs[-1] + 10)}" y="{_n(ly + 4)}">{_esc(name)}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Value bars: one test's measurement per checker, best first, the spread of its runs as whiskers


_BAR_KIND = {"ranked": "s1", "reference": "s3", "unranked": "muted"}


def value_bars(rows, *, title: str, fmt, subtitle: str = "", reference: float | None = None,
               reference_label: str = "official", ticks=log_ticks, tick_format=None) -> str:
    """rows: name, value, lo, hi, failed, href and kind (ranked, reference or unranked, which colour
    the bar). Smaller is better: the best bar is first. A row that did not produce the value is
    listed after the others. Values are written by fmt, the axis's ticks by tick_format."""
    tick_format = tick_format or fmt
    measured = sorted((r for r in rows if not r["failed"] and r["value"]), key=lambda r: (r["value"], r["name"]))
    failed = sorted((r for r in rows if r["failed"] or not r["value"]), key=lambda r: r["name"])
    width, row_h, top, bottom = 780, 22, 18, 34
    name_w, value_w = 170, 90
    height = top + bottom + row_h * (len(measured) + len(failed))
    out = _open("bars", title, subtitle, width, height)
    if not measured and not failed:
        out.append("</svg>")
        return "".join(out)
    values = [r["value"] for r in measured] + [r["hi"] for r in measured if r.get("hi")] + \
             [r["lo"] for r in measured if r.get("lo")] + ([reference] if reference else [])
    lo, hi = (min(values) / 1.4, max(values) * 1.15) if values else (1.0, 10.0)
    x = _Log(lo, hi, name_w, width - value_w)
    axis_bottom = top + row_h * len(measured)
    for t in _spaced(ticks(lo, hi), x, tick_format):
        out.append(f'<line class="grid" x1="{_n(x(t))}" y1="{top}" x2="{_n(x(t))}" y2="{axis_bottom}"/>')
        out.append(f'<text class="tick" x="{_n(x(t))}" y="{axis_bottom + 16}" text-anchor="middle">{_esc(tick_format(t))}</text>')
    if reference:
        out.append(f'<line class="reference" x1="{_n(x(reference))}" y1="{top - 6}" x2="{_n(x(reference))}" y2="{axis_bottom}"/>')
        out.append(f'<text class="tick reference-label" x="{_n(x(reference) + 4)}" y="{top + 2}">{_esc(reference_label)}</text>')
    for i, r in enumerate(measured):
        cy = top + i * row_h
        tip = fmt(r["value"]) + (f" (runs {fmt(r['lo'])} to {fmt(r['hi'])})" if r.get("lo") and r.get("hi") and r["lo"] != r["hi"] else "")
        name = f'<a href="{_esc(r["href"])}">{_esc(r["name"])}</a>' if r.get("href") else _esc(r["name"])
        out.append(f'<g class="row" tabindex="0" data-tip="{_esc(tip)}" data-tip-label="{_esc(r["name"])}">'
                   f'<title>{_esc(r["name"])}: {_esc(tip)}</title>'
                   f'<rect class="hit" x="0" y="{_n(cy)}" width="{width}" height="{row_h}"/>'
                   f'<text class="name" x="{name_w - 10}" y="{_n(cy + 15)}" text-anchor="end">{name}</text>'
                   f'<path class="bar {_BAR_KIND[r.get("kind", "ranked")]}" d="{_rounded_bar(name_w, x(r["value"]), cy + 5, row_h - 10)}"/>')
        if r.get("lo") and r.get("hi") and r["lo"] != r["hi"]:
            out.append(f'<line class="whisker" x1="{_n(x(r["lo"]))}" y1="{_n(cy + row_h / 2)}" x2="{_n(x(r["hi"]))}" y2="{_n(cy + row_h / 2)}"/>')
        out.append(f'<text class="value" x="{width - value_w + 8}" y="{_n(cy + 15)}">{_esc(fmt(r["value"]))}</text></g>')
    for j, r in enumerate(failed):
        cy = axis_bottom + bottom - 8 + j * row_h
        name = f'<a href="{_esc(r["href"])}">{_esc(r["name"])}</a>' if r.get("href") else _esc(r["name"])
        out.append(f'<g class="row failed"><text class="name" x="{name_w - 10}" y="{_n(cy + 15)}" text-anchor="end">{name}</text>'
                   f'<text class="value muted-text" x="{name_w + 4}" y="{_n(cy + 15)}">did not finish</text></g>')
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
