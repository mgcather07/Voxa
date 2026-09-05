"""Phone web scrape parsing — flattens each phone's XML and picks fields by
several candidate tag names across firmware generations. The switch/port comes
from the CDP neighbour, which lives on different pages per phone family; these
lock the tag-name flexibility that keeps coverage from silently dropping.
"""

from app.cucm import phoneweb


class TestFlatten:
    def test_flattens_tags_lowercased(self):
        xml = b"<DeviceInformation><serialNumber>FCH123</serialNumber>" \
              b"<modelNumber>CP-8811</modelNumber></DeviceInformation>"
        data = phoneweb._flatten(xml)
        assert data["serialnumber"] == "FCH123"
        assert data["modelnumber"] == "CP-8811"

    def test_bad_xml_returns_empty_not_crash(self):
        assert phoneweb._flatten(b"<not valid xml") == {}

    def test_strips_namespace_prefix(self):
        xml = b'<x:root xmlns:x="urn:e"><x:serialNumber>S1</x:serialNumber></x:root>'
        assert phoneweb._flatten(xml).get("serialnumber") == "S1"


class TestPick:
    def test_picks_first_present_candidate(self):
        data = {"serial": "OLD1"}
        assert phoneweb._pick(data, phoneweb.SERIAL_TAGS) == "OLD1"

    def test_prefers_earlier_candidate(self):
        data = {"serialnumber": "NEW", "serial": "OLD"}
        # serialnumber comes first in SERIAL_TAGS.
        assert phoneweb._pick(data, phoneweb.SERIAL_TAGS) == "NEW"

    def test_ignores_placeholder_values(self):
        for junk in ("unknown", "N/A", "none", "0.0.0.0"):
            assert phoneweb._pick({"serialnumber": junk}, phoneweb.SERIAL_TAGS) is None

    def test_missing_returns_none(self):
        assert phoneweb._pick({}, phoneweb.SERIAL_TAGS) is None


class TestCdpNeighbourTags:
    def test_switch_name_and_port_across_tag_variants(self):
        # 78xx/88xx style CDP neighbour tags.
        data = {"cdpneighbordeviceid": "sw-closet-3", "cdpneighborport": "Gi1/0/12"}
        assert phoneweb._pick(data, phoneweb.SWITCH_NAME_TAGS) == "sw-closet-3"
        assert phoneweb._pick(data, phoneweb.SWITCH_PORT_TAGS) == "Gi1/0/12"

    def test_lldp_neighbour_fallback(self):
        data = {"lldpneighbordeviceid": "sw-lldp"}
        assert phoneweb._pick(data, phoneweb.SWITCH_NAME_TAGS) == "sw-lldp"
