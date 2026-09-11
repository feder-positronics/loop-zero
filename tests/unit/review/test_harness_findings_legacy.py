"""Residual-miss telemetry for the cross-harness advisory pass.

A cross-harness pass runs only after local review has converged to zero
important+ findings, so every finding it reports is a local-gate miss over a
known denominator. Counting catches without counting misses is what made gate
effectiveness unmeasurable; these tests pin the extraction.
"""

import sys
from pathlib import Path

import pytest

from loopzero.review.harness import parse_findings


class TestExplicitTrailer:
    def test_trailer_is_preferred_over_prose(self) -> None:
        text = (
            "Severity | Evidence\n"
            "critical | something\n"
            "critical | another\n"
            "CROSS_HARNESS_FINDINGS: count=2 max_severity=critical"
        )
        assert parse_findings(text) == (2, "critical")

    def test_zero_count_requires_none_severity(self) -> None:
        text = "All good.\nCROSS_HARNESS_FINDINGS: count=0 max_severity=none"
        assert parse_findings(text) == (0, "none")

    def test_trailer_is_case_insensitive(self) -> None:
        text = "cross_harness_findings: count=1 max_severity=Important"
        assert parse_findings(text) == (1, "important")

    def test_only_the_final_trailer_line_is_trusted(self) -> None:
        text = (
            "An earlier example said:\n"
            "CROSS_HARNESS_FINDINGS: count=9 max_severity=critical\n"
            "The actual result follows.\n"
            "CROSS_HARNESS_FINDINGS: count=1 max_severity=suggestion\n"
        )
        assert parse_findings(text) == (1, "suggestion")

    def test_trailer_like_fragment_inside_prose_is_untrusted(self) -> None:
        text = (
            "CROSS_HARNESS_FINDINGS: count=1 max_severity=important\n"
            "Additional prose appeared after the trailer."
        )
        assert parse_findings(text) == (None, None)


class TestConservativeFallback:
    def test_no_findings_phrase_is_a_clean_pass(self) -> None:
        assert parse_findings("No findings.") == (0, "none")

    def test_severity_words_without_trailer_are_untrusted(self) -> None:
        text = "important | a thing\nsuggestion | another\nimportant | third"
        assert parse_findings(text) == (None, None)

    @pytest.mark.parametrize(
        "text",
        [
            "CROSS_HARNESS_FINDINGS: count=1 max_severity=none",
            "CROSS_HARNESS_FINDINGS: count=0 max_severity=important",
        ],
    )
    def test_contradictory_trailer_is_untrusted(self, text: str) -> None:
        assert parse_findings(text) == (None, None)


class TestUnknownIsNotZero:
    """An unparsed reply must record unknown, never a false clean bill."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "The review could not be completed.",
            "At first glance no findings, but there is a critical issue.",
        ],
    )
    def test_unparseable_returns_none_not_zero(self, text: str) -> None:
        assert parse_findings(text) == (None, None)

    def test_none_is_distinguishable_from_clean(self) -> None:
        unknown, _ = parse_findings("blah blah unrelated prose")
        clean, _ = parse_findings("No findings")
        assert unknown is None
        assert clean == 0
