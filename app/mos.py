"""MOS (Mean Opinion Score) classification — the single source of truth.

Every page that shows call quality (dashboard, call search, call trace,
analytics) classifies a MOS value through :func:`rating` here, so a score of
4.04 is labelled "Good", coloured the same green, and bucketed the same way
everywhere. Do not re-implement these thresholds anywhere else.

MOS runs 1.0–5.0. Bands and colours (colours are dashboard theme tokens, so a
component only ever references ``rating(x).color`` — it never hardcodes one):

    4.30–5.00  Excellent  green
    4.00–4.29  Good       mint  (lighter green)
    3.60–3.99  Fair       yellow / amber
    3.10–3.59  Poor       orange
    < 3.10     Bad        red

A "problem call" is anything below :data:`PROBLEM_MAX` (Fair and worse).

The telemetry helpers (:func:`likely_issue`, :func:`metric_health`) only use
fields Voxa actually collects from CMR — packet loss, jitter, one-way latency.
They never invent a cause; wording stays "likely contributor", not "root cause".
"""

from __future__ import annotations

from dataclasses import dataclass

# MOS scale endpoints, for positioning a score on the 1–5 quality bar.
MOS_MIN = 1.0
MOS_MAX = 5.0

# Below this a call is a "problem call" (Fair, Poor or Bad).
PROBLEM_MAX = 3.6

# Thresholds the summary cards call out. Kept here so the labels ("Below 3.5",
# "Below 4.0") and the maths can never drift apart.
BELOW_A = 3.5
BELOW_B = 4.0


@dataclass(frozen=True)
class Band:
    """One quality band. ``lo`` is inclusive; ``color`` is a CSS theme token."""

    key: str
    label: str
    color: str
    lo: float
    hi: float
    severity: str
    range_text: str

    @property
    def width_pct(self) -> float:
        """This band's share of the 1.0–5.0 bar, for the quality-scale zones."""
        span = max(min(self.hi, MOS_MAX) - max(self.lo, MOS_MIN), 0.0)
        return round(100 * span / (MOS_MAX - MOS_MIN), 3)


# Ordered worst → best. That is the visual order of the quality scale
# (Bad | Poor | Fair | Good | Excellent) and the fixed order for legends.
BANDS: list[Band] = [
    Band("bad", "Bad", "var(--red)", MOS_MIN, 3.10, "critical", "< 3.10"),
    Band("poor", "Poor", "var(--orange)", 3.10, 3.60, "warning", "3.10–3.59"),
    Band("fair", "Fair", "var(--yellow)", 3.60, 4.00, "watch", "3.60–3.99"),
    Band("good", "Good", "var(--mint)", 4.00, 4.30, "healthy", "4.00–4.29"),
    Band("excellent", "Excellent", "var(--green)", 4.30, MOS_MAX, "healthy",
         "4.30–5.00"),
]

# Best → worst, the order MOS filters and distributions read most naturally.
BANDS_DESC: list[Band] = list(reversed(BANDS))

_BY_KEY = {b.key: b for b in BANDS}

_NO_DATA = Band("none", "No data", "var(--ink-3)", 0.0, 0.0, "none", "—")


@dataclass(frozen=True)
class MosRating:
    """The classification of one MOS value — what every UI renders from."""

    score: float | None
    label: str
    band: str
    severity: str
    color: str
    range: str

    @property
    def has_score(self) -> bool:
        return self.score is not None

    @property
    def is_problem(self) -> bool:
        return self.score is not None and self.score < PROBLEM_MAX

    @property
    def pct(self) -> float:
        """Position of the score on the 1.0–5.0 bar, 0–100 (clamped)."""
        if self.score is None:
            return 0.0
        frac = (self.score - MOS_MIN) / (MOS_MAX - MOS_MIN)
        return round(100 * min(max(frac, 0.0), 1.0), 2)

    @property
    def css(self) -> str:
        """CSS-class suffix, e.g. ``mos-good``."""
        return f"mos-{self.band}"


def band_for(mos: float | None) -> Band:
    if mos is None:
        return _NO_DATA
    for b in BANDS_DESC:  # best first: first band whose floor we clear wins
        if mos >= b.lo:
            return b
    return BANDS[0]  # below every floor → Bad


def rating(mos: float | None) -> MosRating:
    """Classify a MOS value. Returns a no-data rating for ``None`` — a missing
    MOS is never treated as 0."""
    b = band_for(mos)
    return MosRating(
        score=round(mos, 2) if mos is not None else None,
        label=b.label,
        band=b.key,
        severity=b.severity,
        color=b.color,
        range=b.range_text,
    )


def band_meta(key: str) -> Band:
    """The band definition for a key (for filter labels / distribution rows)."""
    return _BY_KEY.get(key, _NO_DATA)


# ---------------------------------------------------------------------------
# Per-metric health + likely contributor — deterministic, telemetry-only.
# ---------------------------------------------------------------------------
# (warn, bad) ceilings per metric. Below warn = ok. VoIP rules of thumb:
# loss tolerable ≤1%, poor >3%; jitter fine ≤30 ms, poor >50 ms; one-way
# latency fine ≤150 ms, poor >200 ms (ITU-T G.114).
_METRIC_LIMITS = {
    "loss": (1.0, 3.0),      # percent
    "jitter": (30.0, 50.0),  # milliseconds
    "latency": (150.0, 200.0),  # milliseconds
}

_CONTRIBUTOR_LABEL = {
    "loss": "High packet loss",
    "jitter": "High jitter",
    "latency": "High latency",
}


def metric_health(kind: str, value: float | None) -> str:
    """"ok" / "warn" / "bad" / "none" for a single telemetry metric, so the UI
    can highlight the ones dragging a score down."""
    limits = _METRIC_LIMITS.get(kind)
    if value is None or limits is None:
        return "none"
    warn, bad = limits
    if value >= bad:
        return "bad"
    if value >= warn:
        return "warn"
    return "ok"


def contributors(
    loss_pct: float | None = None,
    jitter_ms: float | None = None,
    latency_ms: float | None = None,
) -> list[str]:
    """Metrics that have crossed their "bad" threshold — the likely contributors
    to a low score. Empty when the collected telemetry doesn't support a claim."""
    found = []
    for kind, value in (
        ("loss", loss_pct),
        ("jitter", jitter_ms),
        ("latency", latency_ms),
    ):
        if metric_health(kind, value) == "bad":
            found.append(_CONTRIBUTOR_LABEL[kind])
    return found


def likely_issue(
    loss_pct: float | None = None,
    jitter_ms: float | None = None,
    latency_ms: float | None = None,
) -> str | None:
    """A single "likely contributor" summary, or ``None`` if the telemetry
    doesn't point at anything. Deterministic — never inferred beyond the data."""
    found = contributors(loss_pct, jitter_ms, latency_ms)
    if not found:
        return None
    if len(found) >= 2:
        return "Multiple quality impairments"
    return found[0]
