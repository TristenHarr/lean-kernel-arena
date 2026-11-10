"""The board's charts: well-formed, deterministic, accessible SVG that the page's CSS themes."""

import math
import unittest
import xml.etree.ElementTree as ET

from lka_quiet.charts import (
    byte_ticks,
    factor_text,
    forest,
    frontier_path,
    heatmap,
    log_ticks,
    parallel,
    pareto,
    range_bars,
    scaled_bars,
    seq_step,
    strip,
    time_ticks,
    value_bars,
)

NS = {"svg": "http://www.w3.org/2000/svg"}


def parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def texts(root: ET.Element) -> list[str]:
    return ["".join(t.itertext()) for t in root.iter("{http://www.w3.org/2000/svg}text")]


FOREST_ROWS = [
    {"name": "nano-lean", "rank": "1", "factor": 3.2, "lo": 2.9, "hi": 3.5, "href": "checker/nano-lean/", "tied": False},
    {"name": "sokonanoda", "rank": "2=", "factor": 2.1, "lo": 1.8, "hi": 2.6, "href": "checker/sokonanoda/", "tied": True},
    {"name": "logos-kernel", "rank": "2=", "factor": 1.9, "lo": 1.5, "hi": 2.4, "href": "checker/logos-kernel/", "tied": True},
    {"name": "<script>", "rank": "4", "factor": 0.4, "lo": 0.3, "hi": 0.5, "href": None, "tied": False},
]


class Formatting(unittest.TestCase):
    def test_factor_text(self):
        self.assertEqual(factor_text(3.2149), "3.21×")
        self.assertEqual(factor_text(12.3456), "12.3×")
        self.assertEqual(factor_text(0.0123), "0.0123×")
        self.assertEqual(factor_text(1.0), "1.00×")

    def test_log_ticks_narrow_range_uses_one_two_five(self):
        self.assertEqual(log_ticks(0.3, 4.0), [0.5, 1, 2])

    def test_log_ticks_middle_range_uses_one_three(self):
        self.assertEqual(log_ticks(0.05, 40), [0.1, 0.3, 1, 3, 10, 30])

    def test_log_ticks_wide_range_thins_to_decades(self):
        ticks = log_ticks(0.001, 1000)
        self.assertEqual(ticks, [0.001, 0.01, 0.1, 1, 10, 100, 1000])

    def test_sequential_steps(self):
        self.assertEqual(seq_step(0.0), 0)
        self.assertEqual(seq_step(1.0), 6)
        self.assertEqual(seq_step(0.5), 3)
        self.assertEqual(seq_step(math.nan), None)


class Forest(unittest.TestCase):
    def test_well_formed_and_titled(self):
        root = parse(forest(FOREST_ROWS, title="Overall", subtitle="× better than official"))
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertEqual(root.get("role"), "img")
        self.assertIn("Overall", root.find("svg:title", NS).text)

    def test_one_row_per_checker_with_its_interval(self):
        svg = forest(FOREST_ROWS, title="Overall")
        root = parse(svg)
        marks = [g for g in root.iter("{http://www.w3.org/2000/svg}g") if g.get("class", "").startswith("row")]
        self.assertEqual(len(marks), 4)
        self.assertIn("3.20× [2.90, 3.50]", texts(root))

    def test_labels_are_escaped(self):
        svg = forest(FOREST_ROWS, title="Overall")
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_marks_are_focusable_with_tooltips(self):
        root = parse(forest(FOREST_ROWS, title="Overall"))
        rows = [g for g in root.iter("{http://www.w3.org/2000/svg}g") if g.get("class", "").startswith("row")]
        for g in rows:
            self.assertEqual(g.get("tabindex"), "0")
            self.assertIsNotNone(g.find("svg:title", NS))

    def test_reference_line_at_one(self):
        root = parse(forest(FOREST_ROWS, title="Overall"))
        refs = [l for l in root.iter("{http://www.w3.org/2000/svg}line") if "reference" in l.get("class", "")]
        self.assertEqual(len(refs), 1)

    def test_deterministic(self):
        self.assertEqual(forest(FOREST_ROWS, title="Overall"), forest(FOREST_ROWS, title="Overall"))

    def test_colors_come_from_classes(self):
        svg = forest(FOREST_ROWS, title="Overall")
        self.assertNotIn("#", svg.replace("&#", ""))

    def test_empty(self):
        root = parse(forest([], title="Overall"))
        self.assertIn("No ranked checker yet", texts(root))


class Heatmap(unittest.TestCase):
    def test_cells_and_steps(self):
        names = ["a", "b", "c"]
        matrix = {"a": {"b": 1.0, "c": 0.5}, "b": {"a": 0.0, "c": 0.9}, "c": {"a": 0.5, "b": 0.1}}
        root = parse(heatmap(names, matrix, title="Head to head"))
        cells = [r for r in root.iter("{http://www.w3.org/2000/svg}rect") if "cell" in r.get("class", "")]
        self.assertEqual(len(cells), 6)
        classes = {r.get("data-pair"): r.get("class") for r in cells}
        self.assertIn("q6", classes["a/b"])
        self.assertIn("q0", classes["b/a"])
        self.assertIn("q3", classes["a/c"])


class Pareto(unittest.TestCase):
    def test_front_is_emphasised_and_labelled(self):
        points = [
            {"name": "fast", "x": 4.0, "y": 0.5, "pareto": True},
            {"name": "lean", "x": 1.2, "y": 3.0, "pareto": True},
            {"name": "worse", "x": 1.0, "y": 0.4, "pareto": False},
        ]
        root = parse(pareto(points, title="Speed and memory", x_label="speed", y_label="memory"))
        label_texts = texts(root)
        self.assertIn("fast", label_texts)
        self.assertIn("lean", label_texts)
        self.assertNotIn("worse", label_texts)
        front = [p for p in root.iter("{http://www.w3.org/2000/svg}polyline") if "front" in p.get("class", "")]
        self.assertEqual(len(front), 1)


class ParetoLabels(unittest.TestCase):
    def test_labels_of_neighbouring_points_do_not_collide(self):
        from lka_quiet.charts import label_width
        points = [
            {"name": "sokonanoda", "x": 8.4, "y": 1.6, "pareto": True},
            {"name": "nano-lean", "x": 9.9, "y": 1.6, "pareto": True},
            {"name": "kefir", "x": 3.5, "y": 3.2, "pareto": True},
            {"name": "other", "x": 1.0, "y": 1.0, "pareto": False},
        ]
        root = parse(pareto(points, title="t", x_label="x", y_label="y"))
        boxes = []
        for t in root.iter("{http://www.w3.org/2000/svg}text"):
            if "direct-label" not in t.get("class", ""):
                continue
            x, y, text = float(t.get("x")), float(t.get("y")), t.text
            w = label_width(text)
            x0 = x - w if t.get("text-anchor") == "end" else x
            boxes.append((x0, y - 11, x0 + w, y + 3, text))
        self.assertEqual(len(boxes), 3)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                self.assertFalse(overlap, (a, b))

    def test_a_moved_label_gets_a_leader(self):
        # three points a few pixels apart at one height: the middle label fits neither left nor right
        points = [{"name": "first", "x": 8.0, "y": 1.6, "pareto": True},
                  {"name": "middle", "x": 8.4, "y": 1.6, "pareto": True},
                  {"name": "last", "x": 8.8, "y": 1.6, "pareto": True}]
        root = parse(pareto(points, title="t", x_label="x", y_label="y"))
        leaders = [l for l in root.iter("{http://www.w3.org/2000/svg}line") if "leader" in l.get("class", "")]
        self.assertGreaterEqual(len(leaders), 1)


class Strip(unittest.TestCase):
    def test_families_are_rows_and_failures_are_marked(self):
        groups = [
            ("Libraries", [("mathlib", 2.0, False), ("std", 1.5, False)]),
            ("Stress", [("perf/a", 0.2, False), ("perf/b", None, True)]),
            ("Interactive", [("tutorial/x", 1.0, False)]),
        ]
        root = parse(strip(groups, title="Per test"))
        self.assertIn("Libraries", texts(root))
        dots = [c for c in root.iter("{http://www.w3.org/2000/svg}circle") if "dot" in c.get("class", "")]
        self.assertEqual(len(dots), 4)
        failed = [g for g in root.iter("{http://www.w3.org/2000/svg}g") if "failed" in g.get("class", "")]
        self.assertEqual(len(failed), 1)
        self.assertIn("s1", dots[0].get("class"))


class Bars(unittest.TestCase):
    def test_scaled_bars_with_ideal(self):
        root = parse(scaled_bars([("a", 3.5), ("b", 1.0)], title="Speed-up", ideal=4.0, unit="×"))
        bars = [r for r in root.iter("{http://www.w3.org/2000/svg}path") if "bar" in r.get("class", "")]
        self.assertEqual(len(bars), 2)
        self.assertIn("ideal 4×", texts(root))

    def test_range_bars(self):
        root = parse(range_bars([("a", 1, 1, 1), ("b", 2, 1, 4)], title="Sensitivity"))
        self.assertIn("1–4", texts(root))


SVG = "{http://www.w3.org/2000/svg}"


def staircase(root):
    lines = [p for p in root.iter(SVG + "polyline") if "front" in p.get("class", "")]
    return [tuple(map(float, xy.split(","))) for xy in lines[0].get("points").split()]


class Frontier(unittest.TestCase):
    """frontier_path: the boundary of the region the front dominates, in "goodness" order, out to
    the axes on the worse side of each coordinate."""

    def test_bigger_is_better_steps_down_then_right(self):
        path = frontier_path([(1, 4), (3, 2), (5, 1)], better=("high", "high"), worse_edges=(0, 0))
        self.assertEqual(path, [(0, 4), (1, 4), (1, 2), (3, 2), (3, 1), (5, 1), (5, 0)])

    def test_smaller_is_better_steps_right_then_down(self):
        path = frontier_path([(1, 5), (2, 3), (4, 1)], better=("low", "low"), worse_edges=(9, 9))
        self.assertEqual(path, [(1, 9), (1, 5), (2, 5), (2, 3), (4, 3), (4, 1), (9, 1)])

    def test_order_of_the_points_given_does_not_matter(self):
        a = frontier_path([(5, 1), (1, 4), (3, 2)], better=("high", "high"), worse_edges=(0, 0))
        b = frontier_path([(3, 2), (5, 1), (1, 4)], better=("high", "high"), worse_edges=(0, 0))
        self.assertEqual(a, b)

    def test_one_point_is_a_corner(self):
        self.assertEqual(frontier_path([(2, 3)], better=("low", "low"), worse_edges=(9, 9)), [(2, 9), (2, 3), (9, 3)])


class ParetoOptions(unittest.TestCase):
    POINTS = [
        {"name": "fast", "x": 40.0, "y": 6 * 2**30, "pareto": True},
        {"name": "lean", "x": 300.0, "y": 1 * 2**30, "pareto": True},
        {"name": "slow", "x": 900.0, "y": 8 * 2**30, "pareto": False},
    ]

    def chart(self, **options):
        return parse(pareto(self.POINTS, title="Mathlib", x_label="wall time", y_label="peak memory",
                            better=("low", "low"), x_ticks=log_ticks, y_ticks=byte_ticks,
                            x_format=lambda v: f"{v:g} s", y_format=lambda v: f"{v / 2**30:g} GiB",
                            reference=(2600.0, 9.9 * 2**30), **options))

    def test_smaller_is_better_shades_the_upper_right(self):
        root = self.chart()
        shade = [p for p in root.iter(SVG + "polygon") if "dominated" in p.get("class", "")]
        self.assertEqual(len(shade), 1)
        points = staircase(root)
        self.assertEqual(len(points), 2 * 2 + 1)
        xs = [x for x, _ in points]
        self.assertEqual(xs, sorted(xs))

    def test_absolute_ticks_are_formatted(self):
        labels = texts(self.chart())
        self.assertIn("100 s", labels)
        self.assertIn("1 GiB", labels)
        self.assertIn("4 GiB", labels)

    def test_every_point_can_be_labelled(self):
        labels = texts(self.chart(labels="all"))
        for name in ("fast", "lean", "slow"):
            self.assertIn(name, labels)
        self.assertNotIn("slow", texts(self.chart()))

    def test_the_reference_is_marked(self):
        root = self.chart()
        refs = [l for l in root.iter(SVG + "line") if "reference" in l.get("class", "")]
        self.assertEqual(len(refs), 2)
        self.assertIn("official", texts(root))

    def test_tooltips_read_the_value_not_the_tick(self):
        root = parse(pareto(self.POINTS, title="Mathlib", x_label="wall time", y_label="peak memory",
                            better=("low", "low"), x_ticks=log_ticks, y_ticks=byte_ticks,
                            x_format=lambda v: f"{v:g} s", y_format=lambda v: f"{v / 2**30:g} GiB",
                            x_value=lambda v: f"{v:.1f} s exactly", y_value=lambda v: f"{v / 2**30:.2f} GiB exactly"))
        tips = [g.get("data-tip") for g in root.iter(SVG + "g") if g.get("data-tip")]
        self.assertIn("40.0 s exactly wall time, 6.00 GiB exactly peak memory", tips)

    def test_small_multiples_take_their_size(self):
        root = self.chart(width=360, height=280)
        self.assertEqual((root.get("width"), root.get("height")), ("360", "280"))

    def test_deterministic(self):
        self.assertEqual(ET.tostring(self.chart()), ET.tostring(self.chart()))


class ByteTicks(unittest.TestCase):
    def test_powers_of_two(self):
        self.assertEqual(byte_ticks(0.7 * 2**30, 10 * 2**30), [2**30, 2 * 2**30, 4 * 2**30, 8 * 2**30])

    def test_a_wide_range_is_thinned(self):
        ticks = byte_ticks(2**20, 2**40)
        self.assertLessEqual(len(ticks), 9)
        self.assertTrue(all(t2 / t1 == ticks[1] / ticks[0] for t1, t2 in zip(ticks, ticks[1:])))


class Parallel(unittest.TestCase):
    ROWS = [
        {"name": "nano-lean", "values": {"multi-core": 8.9, "single-core": 4.5, "memory": 1.1}, "front": True},
        {"name": "kefir", "values": {"multi-core": 3.4, "single-core": 2.9, "memory": 3.2}, "front": True},
        {"name": "slow", "values": {"multi-core": 0.8, "single-core": 0.9, "memory": 0.9}, "front": False},
    ]
    AXES = [("multi-core", "Multi-core"), ("single-core", "Single-core"), ("memory", "Memory")]

    def test_one_line_per_checker_and_one_axis_per_metric(self):
        root = parse(parallel(self.ROWS, self.AXES, title="Four ways"))
        lines = [p for p in root.iter(SVG + "polyline") if "line" in p.get("class", "").split()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(len(line.get("points").split()), 3)
        for _, title in self.AXES:
            self.assertIn(title, texts(root))

    def test_the_front_is_emphasised_and_labelled(self):
        root = parse(parallel(self.ROWS, self.AXES, title="Four ways"))
        lines = {p.get("data-name"): p.get("class") for p in root.iter(SVG + "polyline") if p.get("data-name")}
        self.assertIn("s1", lines["nano-lean"].split())
        self.assertIn("muted", lines["slow"].split())
        labels = texts(root)
        self.assertIn("nano-lean", labels)
        self.assertIn("kefir", labels)

    def test_better_is_up_on_every_axis(self):
        root = parse(parallel(self.ROWS, self.AXES, title="Four ways"))
        lines = {p.get("data-name"): p.get("points").split() for p in root.iter(SVG + "polyline") if p.get("data-name")}
        nano_y = float(lines["nano-lean"][0].split(",")[1])
        slow_y = float(lines["slow"][0].split(",")[1])
        self.assertLess(nano_y, slow_y)

    def test_deterministic_and_escaped(self):
        rows = self.ROWS + [{"name": "<b>", "values": {"multi-core": 1, "single-core": 1, "memory": 1}, "front": True}]
        a = parallel(rows, self.AXES, title="t")
        self.assertEqual(a, parallel(rows, self.AXES, title="t"))
        parse(a)
        self.assertNotIn("<b>", a.replace("&lt;b&gt;", ""))


class ValueBars(unittest.TestCase):
    ROWS = [
        {"name": "slow", "value": 300.0, "lo": 290.0, "hi": 320.0, "failed": False, "href": "checker/slow/"},
        {"name": "fast", "value": 45.0, "lo": 44.0, "hi": 47.0, "failed": False, "href": "checker/fast/"},
        {"name": "gave-up", "value": None, "lo": None, "hi": None, "failed": True, "href": None},
    ]

    def test_ticks_take_their_own_set_and_format(self):
        root = parse(value_bars(self.ROWS, title="t", fmt=lambda v: f"{v:.1f} seconds", ticks=time_ticks,
                                tick_format=lambda v: f"{v / 60:g} min" if v >= 60 else f"{v:g} s"))
        labels = texts(root)
        self.assertIn("1 min", labels)
        self.assertIn("5 min", labels)
        self.assertIn("45.0 seconds", labels)

    def test_a_bar_is_coloured_by_what_its_checker_is(self):
        rows = [{**self.ROWS[0], "kind": "reference"}, {**self.ROWS[1], "kind": "ranked"},
                {"name": "cheat", "value": 10.0, "lo": 10.0, "hi": 10.0, "failed": False, "href": None, "kind": "unranked"}]
        root = parse(value_bars(rows, title="t", fmt=lambda v: f"{v:g} s"))
        bars = [p.get("class") for p in root.iter(SVG + "path") if "bar" in p.get("class", "")]
        self.assertEqual(bars, ["bar muted", "bar s1", "bar s3"])

    def chart(self):
        return parse(value_bars(self.ROWS, title="Mathlib, 4 cores", fmt=lambda v: f"{v:g} s", reference=2600.0))

    def test_fastest_first_with_whiskers_and_failures_last(self):
        root = self.chart()
        labels = [t for t in texts(root) if t in ("slow", "fast", "gave-up")]
        self.assertEqual(labels, ["fast", "slow", "gave-up"])
        whiskers = [l for l in root.iter(SVG + "line") if "whisker" in l.get("class", "")]
        self.assertEqual(len(whiskers), 2)
        self.assertIn("did not finish", texts(root))

    def test_values_are_formatted_and_the_reference_marked(self):
        labels = texts(self.chart())
        self.assertIn("45 s", labels)
        self.assertIn("300 s", labels)
        self.assertIn("official", labels)

    def test_deterministic(self):
        self.assertEqual(ET.tostring(self.chart()), ET.tostring(self.chart()))


class TimeTicks(unittest.TestCase):
    def test_round_times(self):
        self.assertEqual(time_ticks(40, 3000), [60, 120, 300, 600, 1200, 1800])
        self.assertEqual(time_ticks(0.6, 25), [1, 2, 5, 10, 20])

    def test_a_wide_range_is_thinned(self):
        self.assertLessEqual(len(time_ticks(0.01, 100000)), 8)


class OffScale(unittest.TestCase):
    POINTS = [{"name": f"c{i}", "x": x, "y": y, "pareto": False}
              for i, (x, y) in enumerate([(0.5, 0.8), (1.0, 1.2), (2.0, 2.5), (4.0, 1.5), (8.0, 1.6), (6.0, 3.0)])]
    POINTS[-1]["pareto"] = True
    POINTS[-2]["pareto"] = True
    OUTLIERS = [{"name": "far", "x": 0.02, "y": 0.03, "pareto": False},
                {"name": "farther", "x": 0.025, "y": 0.05, "pareto": False}]

    def test_points_past_a_tenfold_gap_go_to_the_gutter(self):
        root = parse(pareto(self.POINTS + self.OUTLIERS, title="t", x_label="x", y_label="y"))
        off = [g for g in root.iter(SVG + "g") if "off-scale" in g.get("class", "")]
        self.assertEqual(len(off), 2)
        titles = ["".join(t.itertext()) for g in off for t in g.iter(SVG + "title")]
        self.assertTrue(any(t.startswith("far:") for t in titles))
        self.assertIn("off scale", " ".join(texts(root)))

    def test_without_a_gap_nothing_is_off_scale(self):
        root = parse(pareto(self.POINTS, title="t", x_label="x", y_label="y"))
        self.assertEqual([g for g in root.iter(SVG + "g") if "off-scale" in g.get("class", "")], [])

    def test_a_fivefold_gap_is_enough(self):
        outliers = [{"name": "far", "x": 0.03, "y": 1.0, "pareto": False}]
        nearer = [{"name": "near", "x": 0.16, "y": 1.0, "pareto": False}]
        root = parse(pareto(self.POINTS + outliers + nearer, title="t", x_label="x", y_label="y"))
        off = [g.get("data-tip-label") for g in root.iter(SVG + "g") if "off-scale" in g.get("class", "")]
        self.assertEqual(off, ["far"])

    def test_tick_labels_never_overlap(self):
        root = parse(pareto(ParetoOptions.POINTS, title="t", x_label="x", y_label="y", better=("low", "low"),
                            x_ticks=time_ticks, y_ticks=byte_ticks, x_format=lambda v: f"{v:g} seconds",
                            y_format=lambda v: f"{v:g}", reference=None, width=360, height=280))
        from lka_quiet.charts import label_width
        ticks = sorted((float(t.get("x")), "".join(t.itertext())) for t in root.iter(SVG + "text")
                       if t.get("class") == "tick" and t.get("text-anchor") == "middle")
        self.assertGreaterEqual(len(ticks), 2)
        for (x1, a), (x2, b) in zip(ticks, ticks[1:]):
            self.assertGreaterEqual(x2 - x1, (label_width(a, 11) + label_width(b, 11)) / 2, (a, b))

    def test_the_reference_label_sits_at_the_worse_end(self):
        root = parse(pareto(self.POINTS, title="t", x_label="x", y_label="y"))
        label = next(t for t in root.iter(SVG + "text") if "reference-label" in t.get("class", ""))
        self.assertGreater(float(label.get("y")), 250)

    def test_a_crowd_is_never_off_scale(self):
        many = self.OUTLIERS + [{"name": f"o{i}", "x": 0.03, "y": 0.04, "pareto": False} for i in range(6)]
        root = parse(pareto(self.POINTS + many, title="t", x_label="x", y_label="y"))
        self.assertEqual([g for g in root.iter(SVG + "g") if "off-scale" in g.get("class", "")], [])


if __name__ == "__main__":
    unittest.main()
