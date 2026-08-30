"""Model catalog: turns a CUCM model string into refresh-planning facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "models.yaml"

# "Cisco 7962", "Cisco IP Phone 7962G", "CP-7962G", "DP-9841" -> 7962 / 9841
_MODEL_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")

# Replacement families the PoE/plan pages can switch between. The default
# "9800" mapping is whatever config/models.yaml points each model at. The
# "8800" family substitutes a same-tier 8800-series desk phone for those
# 9800-series desk targets, so the tool can compare the two programmes side by
# side; non-desk targets (conference, wireless, video) have no 8800 analog and
# are kept as configured. "keep" plans no replacement at all.
FAMILIES: dict[str, str] = {
    "9800": "9800 series",
    "8800": "8800 series",
    "keep": "Keep current",
}
DEFAULT_FAMILY = "9800"

# 9800 desk target -> its previous-generation 8800 equivalent. The PoE class of
# each 8800 model already lives in the `models:` section of models.yaml, so no
# new facts are invented here — only which existing model to plan around.
_ALT_8800: dict[str, str] = {
    "9841": "8841",
    "9851": "8851",
    "9861": "8865",
    "9871": "8865",
}


@dataclass(frozen=True)
class ModelInfo:
    key: str
    family: str
    generation: str
    poe_class: int
    poe_watts: float
    lifecycle: str
    lifecycle_label: str
    verified: bool
    replacement_key: str | None
    replacement_name: str | None
    replacement_poe_class: int | None
    replacement_poe_watts: float | None
    replacement_role: str | None
    replacement_requires: str | None

    @property
    def needs_replacement(self) -> bool:
        return self.replacement_key is not None

    @property
    def poe_delta(self) -> float:
        """Extra watts per port the replacement will draw. Can be negative."""
        if self.replacement_poe_watts is None:
            return 0.0
        return round(self.replacement_poe_watts - self.poe_watts, 2)


@dataclass(frozen=True)
class ReplacementChoice:
    """The model a phone is planned to become under a chosen family."""

    key: str
    name: str
    poe_class: int
    poe_watts: float

    @property
    def spec(self) -> str:
        """`class 3 · 12.95 W`, the caption used across the plan/PoE cards."""
        return f"class {self.poe_class} · {self.poe_watts:.2f} W"


class Catalog:
    def __init__(self, path: Path = CATALOG_PATH) -> None:
        raw = yaml.safe_load(path.read_text())
        self._class_watts: dict[int, float] = {
            int(k): float(v) for k, v in raw["poe_class_watts"].items()
        }
        self._labels: dict[str, str] = raw.get("lifecycle_labels", {})
        self._replacements: dict[str, dict] = raw.get("replacements", {})
        self._models: dict[str, dict] = raw.get("models", {})
        self._default: dict = raw.get("default", {})

    def watts_for_class(self, poe_class: int | None) -> float:
        if poe_class is None:
            return 0.0
        return self._class_watts.get(int(poe_class), 12.95)

    @staticmethod
    def extract_key(model_string: str | None) -> str | None:
        if not model_string:
            return None
        match = _MODEL_RE.search(model_string)
        return match.group(1) if match else None

    def lookup(self, model_string: str | None) -> ModelInfo:
        key = self.extract_key(model_string) or "unknown"
        entry = {**self._default, **(self._models.get(key) or {})}

        poe_class = entry.get("poe_class")
        replacement_key = entry.get("replacement")
        rep = self._replacements.get(replacement_key or "", {}) or {}
        rep_class = rep.get("poe_class")

        return ModelInfo(
            key=key,
            family=entry.get("family") or "unknown",
            generation=entry.get("generation") or "unknown",
            poe_class=int(poe_class) if poe_class is not None else 3,
            poe_watts=self.watts_for_class(poe_class),
            lifecycle=entry.get("lifecycle") or "unknown",
            lifecycle_label=self._labels.get(
                entry.get("lifecycle") or "unknown", "Unknown"
            ),
            verified=bool(entry.get("verified", False)),
            replacement_key=replacement_key,
            replacement_name=rep.get("name"),
            replacement_poe_class=int(rep_class) if rep_class is not None else None,
            replacement_poe_watts=(
                self.watts_for_class(rep_class) if rep_class is not None else None
            ),
            replacement_role=rep.get("role"),
            replacement_requires=rep.get("requires"),
        )

    def replacement_for(
        self, model_string: str | None, family: str = DEFAULT_FAMILY
    ) -> ReplacementChoice | None:
        """The replacement a model maps to under the given family.

        Returns None when the family is "keep", or when the model has no
        configured replacement (already a current/target platform).
        """
        info = self.lookup(model_string)
        base_key = info.replacement_key
        if family == "keep" or not base_key:
            return None

        if family == "8800":
            alt_key = _ALT_8800.get(base_key)
            if alt_key:
                raw_class = (self._models.get(alt_key) or {}).get("poe_class")
                poe_class = (
                    int(raw_class)
                    if raw_class is not None
                    else (info.replacement_poe_class or 3)
                )
                name = (self._replacements.get(alt_key) or {}).get(
                    "name"
                ) or f"Cisco IP Phone {alt_key}"
                return ReplacementChoice(
                    key=alt_key,
                    name=name,
                    poe_class=poe_class,
                    poe_watts=self.watts_for_class(poe_class),
                )
            # No 8800 analog (conference/wireless/video): keep as configured.

        poe_class = info.replacement_poe_class or 3
        return ReplacementChoice(
            key=base_key,
            name=info.replacement_name or f"Cisco {base_key}",
            poe_class=poe_class,
            poe_watts=(
                info.replacement_poe_watts
                if info.replacement_poe_watts is not None
                else self.watts_for_class(poe_class)
            ),
        )


@lru_cache
def get_catalog() -> Catalog:
    return Catalog()
