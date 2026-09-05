"""MOS classification — the single source of truth every quality view uses.

If a boundary here shifts, dashboards and filters silently mis-label calls, so
these lock the band edges and the deterministic 'likely contributor' logic.
"""

from app import mos


class TestBands:
    def test_boundaries_are_inclusive_lower(self):
        # (value, expected band) at and just below each documented edge.
        cases = [
            (5.00, "excellent"), (4.30, "excellent"), (4.29, "good"),
            (4.00, "good"), (3.99, "fair"), (3.60, "fair"), (3.59, "poor"),
            (3.10, "poor"), (3.09, "bad"), (1.00, "bad"),
        ]
        for value, expected in cases:
            assert mos.rating(value).band == expected, f"{value} -> {expected}"

    def test_labels_match_bands(self):
        assert mos.rating(4.5).label == "Excellent"
        assert mos.rating(4.1).label == "Good"
        assert mos.rating(3.7).label == "Fair"
        assert mos.rating(3.3).label == "Poor"
        assert mos.rating(2.0).label == "Bad"

    def test_none_is_no_data_not_zero(self):
        r = mos.rating(None)
        assert r.score is None and r.band == "none"
        assert not r.has_score

    def test_problem_threshold(self):
        assert mos.rating(3.59).is_problem is True
        assert mos.rating(3.60).is_problem is False
        assert mos.rating(None).is_problem is False

    def test_scale_position_clamped(self):
        assert mos.rating(1.0).pct == 0.0
        assert mos.rating(5.0).pct == 100.0
        assert mos.rating(3.0).pct == 50.0
        # out-of-range values clamp rather than overflow the bar
        assert mos.rating(6.0).pct == 100.0
        assert mos.rating(0.0).pct == 0.0

    def test_band_widths_sum_to_full_scale(self):
        assert round(sum(b.width_pct for b in mos.BANDS), 1) == 100.0

    def test_color_comes_from_helper_not_hardcoded(self):
        # Every band exposes a CSS token so components never hardcode a colour.
        for b in mos.BANDS:
            assert b.color.startswith("var(--")


class TestLikelyIssue:
    def test_clean_call_no_contributor(self):
        assert mos.likely_issue(loss_pct=0.5, jitter_ms=10, latency_ms=100) is None

    def test_single_impairment_named(self):
        assert mos.likely_issue(loss_pct=6.0) == "High packet loss"
        assert mos.likely_issue(jitter_ms=80) == "High jitter"
        assert mos.likely_issue(latency_ms=300) == "High latency"

    def test_multiple_impairments_collapse(self):
        assert mos.likely_issue(loss_pct=6.0, jitter_ms=80) == "Multiple quality impairments"

    def test_warn_level_does_not_overclaim(self):
        # values in the "warn" band but below "bad" must not assert a cause
        assert mos.likely_issue(loss_pct=2.0, jitter_ms=40, latency_ms=170) is None

    def test_metric_health_thresholds(self):
        assert mos.metric_health("loss", 0.5) == "ok"
        assert mos.metric_health("loss", 2.0) == "warn"
        assert mos.metric_health("loss", 4.0) == "bad"
        assert mos.metric_health("loss", None) == "none"
