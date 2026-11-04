"""Offline equivalence and bounded-work coverage for math routing signals."""

import itertools
import random
import re
import unittest

from app.services.brain.profiler import (
    KeywordSignals,
    StructuralSignals,
    _contains_ascii_math_expression,
    profile_request,
)
from app.services.brain.context import RoutingContext


LEGACY_MATH_PATTERN = re.compile(r"[0-9].*[\+\-\*/=^].*[0-9]")
MATH_KEYWORDS = (
    "solve", "equation", "integral", "derivative", "calculate", "proof",
    "theorem", "probability", "matrix", "algebra", "geometry", "statistics",
)


def legacy_math_score(text):
    score = 0.0
    if LEGACY_MATH_PATTERN.search(text) or "√" in text:
        score += 0.4
    if any(word in text for word in MATH_KEYWORDS):
        score += 0.5
    return min(score, 1.0)


class CountingText(str):
    """Count input visits without depending on machine timing."""

    def __iter__(self):
        self.visits = 0
        for character in super().__iter__():
            self.visits += 1
            yield character


class LegacyKeywordSignals(KeywordSignals):
    def _math(self, lower):
        return legacy_math_score(lower)


class LinearMathSignalTests(unittest.TestCase):
    def assert_equivalent(self, text):
        self.assertEqual(
            _contains_ascii_math_expression(text),
            bool(LEGACY_MATH_PATTERN.search(text)),
            repr(text),
        )

    def test_exhaustive_short_strings_preserve_matching(self):
        # Covers competing starts, multiple operators, and newline resets.
        alphabet = "01+x\n"
        for size in range(8):
            for characters in itertools.product(alphabet, repeat=size):
                self.assert_equivalent("".join(characters))

    def test_ascii_digits_newlines_and_operator_edges(self):
        for operator in "+-*/=^":
            for text in (
                f"1{operator}2", f"1xx{operator}xx2", f"1{operator}{operator}2",
                f"{operator}12", f"12{operator}", f"1\n{operator}2",
                f"1{operator}\n2", f"1\r{operator}\r2", f"1\u2028{operator}\u20292",
                f"١{operator}٢", f"１{operator}２", f"²{operator}3",
                f"1{operator}٢", f"1{operator}٢3", f"\n1{operator}2\n",
                f"1{operator}\n2{operator}3", f"1\x00{operator}\x002",
            ):
                with self.subTest(text=text):
                    self.assert_equivalent(text)

        for text in ("", "0", "00", "123", "1\\2", "1%2", "1×2", "1−2"):
            with self.subTest(text=text):
                self.assert_equivalent(text)

    def test_seeded_randomized_equivalence_and_scores(self):
        randomizer = random.Random(970_2026)
        alphabet = "01289+-*/=^xyz \n\r\t\x00١٢３²√\u2028\u2029"
        signals = KeywordSignals()
        for _ in range(6_000):
            text = "".join(
                randomizer.choices(alphabet, k=randomizer.randrange(257))
            )
            if randomizer.randrange(4) == 0:
                text += randomizer.choice(MATH_KEYWORDS)
            lower = text.lower()
            self.assert_equivalent(lower)
            self.assertEqual(signals.score(text)["math"], legacy_math_score(lower))

    def test_square_root_and_keyword_scores_are_unchanged(self):
        signals = KeywordSignals()
        for text, expected in (
            ("plain prose", 0.0), ("√", 0.4), ("SOLVE", 0.5),
            ("√ solve", 0.9), ("1+2 solve", 0.9), ("1+2 √", 0.4),
            ("1+\n2", 0.0), ("1+\n2 √", 0.4), ("١+٢", 0.0),
        ):
            with self.subTest(text=text):
                self.assertEqual(signals.score(text)["math"], expected)

    def test_request_profiles_preserve_scores_and_dominant_category(self):
        randomizer = random.Random(419)
        messages = [
            "SOLVE 1+2", "1+\n2", "1\r+\r2", "١+٢", "√ PROOF",
            "class Example:\n    return 1+2", "summarize this file 1+\n2",
            "research statistics: 1\u2028+\u20292", "1+2 " * 700,
        ]
        messages.extend(
            " ".join(randomizer.choices(
                ["1+2", "1+\n2", "√", "١+٢", "SOLVE", "def", "why", "plain"],
                k=randomizer.randrange(30),
            ))
            for _ in range(250)
        )
        for message in messages:
            context = RoutingContext(message=message)
            with self.subTest(message=message[:60]):
                self.assertEqual(
                    profile_request(context, sources=[KeywordSignals(), StructuralSignals()]),
                    profile_request(context, sources=[LegacyKeywordSignals(), StructuralSignals()]),
                )

    def test_adversarial_nonmatches_visit_each_character_once(self):
        # Avoid running the quadratic reference regex on large adversarial input.
        for size in (1_000, 10_000, 250_000):
            for raw in ("1" * size, "1" + "+" * size, "1\n+2" * size):
                text = CountingText(raw)
                with self.subTest(size=size, prefix=raw[:8]):
                    self.assertFalse(_contains_ascii_math_expression(text))
                    self.assertEqual(text.visits, len(text))

    def test_matching_input_stops_at_first_complete_expression(self):
        text = CountingText("1+2" + "1" * 250_000)
        self.assertTrue(_contains_ascii_math_expression(text))
        self.assertEqual(text.visits, 3)


if __name__ == "__main__":
    unittest.main()
