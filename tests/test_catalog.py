"""Model catalog — maps CUCM's free-text model strings to PoE class, lifecycle
and a replacement. Aliases and exclusions here decide what's counted, budgeted,
and shown, so a regression quietly corrupts every plan and PoE number.
"""

from app.catalog import get_catalog


def catalog():
    # The shipped config/models.yaml (no DB overrides in a fixture context).
    return get_catalog()


class TestExtractKey:
    def test_pulls_model_number_from_cucm_string(self):
        c = catalog()
        assert c.extract_key("Cisco 8811") == "8811"
        assert c.extract_key("Cisco IP Phone 7841") == "7841"

    def test_unknown_string_returns_none(self):
        assert catalog().extract_key("Some Random Device") is None

    def test_none_input_safe(self):
        assert catalog().extract_key(None) is None


class TestAliasesAndExclusions:
    def test_analog_ata_alias_maps_to_family(self):
        c = catalog()
        # ATA 186/187 variants collapse to the ata186 catalog entry.
        assert c.extract_key("Cisco ATA 186") == "ata186"

    def test_soft_clients_excluded(self):
        c = catalog()
        assert c.is_excluded("Cisco Unified Client Services Framework") is True
        assert c.is_excluded("Cisco Jabber") is True

    def test_real_phone_not_excluded(self):
        assert catalog().is_excluded("Cisco 8811") is False


class TestLookup:
    def test_known_model_has_poe_and_lifecycle(self):
        info = catalog().lookup("Cisco 8811")
        assert info.key == "8811"
        assert info.poe_class is not None
        assert info.poe_watts >= 0
        assert info.lifecycle in {"current", "eos", "eol", "unknown"}

    def test_unknown_model_falls_back_not_crash(self):
        # A string with no model number extracts nothing -> the catch-all key.
        info = catalog().lookup("Some Random Device")
        assert info.key == "unknown"

    def test_unrecognized_number_kept_as_key(self):
        # A digit run IS extracted even if it's not in the catalog, so it shows
        # up by that number (not silently merged into "unknown").
        assert catalog().lookup("Totally Unknown 9999X").key == "9999"

    def test_verified_flag_present(self):
        # The dashboard relies on this to flag unverified models.
        info = catalog().lookup("Cisco 8811")
        assert isinstance(info.verified, bool)


class TestPoeMath:
    def test_watts_for_class_uses_ieee_ceilings(self):
        c = catalog()
        # IEEE PD ceilings: class 1 < 2 < 3 < 4 (class 0 is the legacy high
        # default, so it's excluded from the ordering). These are what a switch
        # reserves per port — the number that runs a closet out of power.
        w1, w2, w3, w4 = (c.watts_for_class(n) for n in (1, 2, 3, 4))
        assert 0 < w1 < w2 < w3 < w4
        assert w4 > 25  # class 4 (~25.5-30W), not a typical-draw figure
