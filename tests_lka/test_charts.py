"""The board's charts: well-formed, deterministic, accessible SVG that the page's CSS themes."""

import math
import unittest
import xml.etree.ElementTree as ET

from lka_quiet.charts import (
    factor_text,
    forest,
    heatmap,
    log_ticks,
    pareto,
    range_bars,
    scaled_bars,
    seq_step,
    strip,
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


if __name__ == "__main__":
    unittest.main()
