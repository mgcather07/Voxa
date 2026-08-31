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
    def __init__(
        self, path: Path = CATALOG_PATH, overrides: dict[str, dict] | None = None
    ) -> None:
        raw = yaml.safe_load(path.read_text())
        self._class_watts: dict[int, float] = {
            int(k): float(v) for k, v in raw["poe_class_watts"].items()
        }
        self._labels: dict[str, str] = raw.get("lifecycle_labels", {})
        self._replacements: dict[str, dict] = raw.get("replacements", {})
        self._models: dict[str, dict] = raw.get("models", {})
        self._default: dict = raw.get("default", {})
        # Name-based recognition for devices without a 4-digit model number in
        # the CUCM string (analog adapters, gateway ports): substring -> key.
        self._aliases: dict[str, str] = raw.get("model_aliases", {})
        # CUCM device-class "Phone" entries that are not physical phones (soft
        # clients, virtual ports, templates): excluded from the inventory.
        self._exclude: list[str] = [s.lower() for s in raw.get("exclude_models", [])]
        # Admin edits from the DB, keyed by model. Each value may set poe_class,
        # lifecycle, replacement, verified; a set field wins over the YAML.
        self._overrides: dict[str, dict] = overrides or {}

    def base_entry(self, key: str) -> dict:
        """The YAML-only entry for a model (defaults + models.yaml, no DB edits).
        Used to tell whether an admin's submitted values differ from default."""
        return {**self._default, **(self._models.get(key) or {})}

    def _eff(self, key: str) -> dict:
        """YAML entry merged with any admin override (override wins per field)."""
        entry = dict(self._models.get(key) or {})
        ov = self._overrides.get(key) or {}
        if ov.get("poe_class") is not None:
            entry["poe_class"] = ov["poe_class"]
        if ov.get("lifecycle"):
            entry["lifecycle"] = ov["lifecycle"]
        if ov.get("verified") is not None:
            entry["verified"] = ov["verified"]
        rep = ov.get("replacement")
        if rep is not None:
            entry["replacement"] = None if rep == "none" else rep
        return entry

    def effective(self, key: str) -> dict:
        """The merged entry (defaults + YAML + override) an admin is editing."""
        return {**self._default, **self._eff(key)}

    def has_override(self, key: str) -> bool:
        return key in self._overrides

    def lifecycle_options(self) -> list[tuple[str, str]]:
        return list(self._labels.items())

    def replacement_options(self) -> list[tuple[str, str]]:
        return [(k, v.get("name") or k) for k, v in self._replacements.items()]

    def poe_classes(self) -> list[int]:
        return sorted(self._class_watts)

    def watts_for_class(self, poe_class: int | None) -> float:
        if poe_class is None:
            return 0.0
        return self._class_watts.get(int(poe_class), 12.95)

    def extract_key(self, model_string: str | None) -> str | None:
        if not model_string:
            return None
        low = model_string.lower()
        for sub, key in self._aliases.items():
            if sub.lower() in low:
                return key
        match = _MODEL_RE.search(model_string)
        return match.group(1) if match else None

    def is_excluded(self, model_string: str | None) -> bool:
        """True for CUCM 'Phone' devices that are not physical phones (soft
        clients, virtual ports, templates) — kept out of the inventory."""
        if not model_string:
            return False
        low = model_string.lower()
        return any(sub in low for sub in self._exclude)

    def lookup(self, model_string: str | None) -> ModelInfo:
        key = self.extract_key(model_string) or "unknown"
        entry = {**self._default, **self._eff(key)}

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
                raw_class = self._eff(alt_key).get("poe_class")
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


def _load_overrides() -> dict[str, dict]:
    """Admin catalog edits from the DB, keyed by model. Returns empty if the
    table isn't ready yet (e.g. the very first boot before init_db runs)."""
    try:
        from .db import session_scope
        from .models import CatalogOverride

        with session_scope() as session:
            return {
                row.model_key: {
                    "poe_class": row.poe_class,
                    "lifecycle": row.lifecycle,
                    "replacement": row.replacement,
                    "verified": row.verified,
                }
                for row in session.query(CatalogOverride).all()
            }
    except Exception:  # noqa: BLE001 - never let catalog loading crash a page
        return {}


@lru_cache
def get_catalog() -> Catalog:
    return Catalog(overrides=_load_overrides())


def reapply_to_phones(session) -> int:
    """Re-derive every phone's catalog fields from the current catalog, without
    contacting CUCM. Called after an admin edits the catalog so the change shows
    across the dashboard/plan/PoE immediately. Returns phones updated."""
    from .models import Phone

    catalog = get_catalog()
    phones = session.query(Phone).all()
    for phone in phones:
        info = catalog.lookup(phone.model_raw)
        phone.model_key = info.key
        phone.family = info.family
        phone.generation = info.generation
        phone.lifecycle = info.lifecycle
        phone.poe_class = info.poe_class
        phone.poe_watts = info.poe_watts
        phone.replacement_key = info.replacement_key
        phone.replacement_name = info.replacement_name
        phone.replacement_poe_watts = info.replacement_poe_watts
    return len(phones)
