"""Measure synthetic math-pattern matching only; no credentials or services."""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from time import perf_counter


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.brain.profiler import _contains_ascii_math_expression


LEGACY_PATTERN = re.compile(r"[0-9].*[\+\-\*/=^].*[0-9]")


def measure(function, text, runs):
    samples = []
    for _ in range(runs):
        started = perf_counter()
        matched = function(text)
        samples.append((perf_counter() - started) * 1_000)
        if matched:
            raise AssertionError("The adversarial digits-only input must not match")
    return {
        "median_ms": round(statistics.median(samples), 6),
        "samples_ms": [round(sample, 6) for sample in samples],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error("--runs must be between 1 and 10")

    results = []
    # Keep legacy-regex inputs bounded: its adversarial runtime is quadratic.
    for size in (2_000, 4_000, 8_000, 16_000):
        text = "1" * size
        results.append({
            "characters": size,
            "legacy_regex": measure(LEGACY_PATTERN.search, text, args.runs),
            "linear_scan": measure(_contains_ascii_math_expression, text, args.runs),
        })
    results.append({
        "characters": 1_000_000,
        "linear_scan": measure(_contains_ascii_math_expression, "1" * 1_000_000, args.runs),
    })
    print(json.dumps({
        "benchmark": "digits-only math-pattern nonmatch",
        "results": results,
        "note": "Local CPU measurement only; not application or hosting latency.",
    }, indent=2))


if __name__ == "__main__":
    main()
