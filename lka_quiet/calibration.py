"""Calibrating the quiet box: the decisions, from measurements quietbox/calibrate.py takes.

- The pinned clock: the highest frequency at which an all-core soak saw no throttle event, stayed
  under the temperature limit, and held its clock (every sample within 0.5% of the target).
- The sentinel's control limits: mean and sample standard deviation of its wall time over at least
  20 quiet readings; a session stops when a reading leaves mean +- k sigma.
- The box factor: the median, over the reference tests, of the official checker's wall time here
  over its published wall time on the arena's runners, rounded up to a tenth and never below one.
  The arena's time limits are multiplied by it.
"""

import math
import re

from .stats import median

MIN_SENTINEL_READINGS = 20
CLOCK_TOLERANCE = 0.005


def choose_frequency(soaks, max_temperature_c: float) -> int:
    clean = []
    for s in soaks:
        held = all(abs(e / s["freq_khz"] - 1) <= CLOCK_TOLERANCE for e in s["effective_khz"])
        if s["throttle_events"] == 0 and s["max_temperature_c"] <= max_temperature_c and held:
            clean.append(s["freq_khz"])
    if not clean:
        raise ValueError("no frequency was held without throttling: improve cooling or lower the power limit")
    return max(clean)


def _sample_sd(values) -> float:
    n = len(values)
    mean = math.fsum(values) / n
    return math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (n - 1))


def sentinel_limits(walls, k: float = 4) -> dict:
    if len(walls) < MIN_SENTINEL_READINGS:
        raise ValueError(f"{len(walls)} sentinel readings; at least {MIN_SENTINEL_READINGS} are needed")
    return {"mean_s": math.fsum(walls) / len(walls), "sigma_s": _sample_sd(walls), "k": k}


def box_factor(here: dict, published: dict) -> float:
    ratios = [here[t] / published[t] for t in here if t in published and published[t] > 0]
    if not ratios:
        raise ValueError("no reference test was measured both here and on the arena")
    return max(1.0, math.ceil(median(ratios) * 10 - 1e-9) / 10)


def noise_summary(walls) -> dict:
    mean = math.fsum(walls) / len(walls)
    return {"n": len(walls), "mean": mean, "median": median(walls),
            "cv": _sample_sd(walls) / mean if len(walls) > 1 else 0.0,
            "spread": (max(walls) - min(walls)) / median(walls)}


def _replace_null(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^(?P<prefix>\s*{key}: )null(?P<gap>\s*)(?P<comment>#.*)?$", re.M)
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"the profile has no `{key}: null` to calibrate")
    head = match.group("prefix") + value
    comment = match.group("comment")
    if comment:
        column = len(match.group("prefix")) + len("null") + len(match.group("gap"))
        line = head + " " * max(1, column - len(head)) + comment
    else:
        line = head
    return text[:match.start()] + line + text[match.end():]


def write_calibration(profile_text: str, *, pinned_hz: int, box_factor: float, limits: dict) -> str:
    """The machine profile with its calibrated values in place of the nulls, comments kept."""
    text = _replace_null(profile_text, "pinned_hz", str(int(pinned_hz)))
    text = _replace_null(text, "box_factor", f"{box_factor:g}")
    text = _replace_null(text, "limits",
                         f"{{mean_s: {limits['mean_s']:.6g}, sigma_s: {limits['sigma_s']:.6g}, k: {limits['k']:g}}}")
    return text
