"""Phone web scraper - serial numbers and the switch port each phone sits on.

CUCM does not reliably hold either of these. The phone itself does: every
Cisco IP phone with web access enabled serves its own device info and its CDP
neighbour over HTTP. That neighbour is the access switch and port, which is
exactly what a refresh project needs - you learn which closet each phone lives
in and can budget PoE per switch before ordering anything.

Requires "Web Access = Enabled" on the phone or its Common Phone Profile.
Phones are polled concurrently; unreachable phones are skipped silently.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEVICE_PATHS = ("/DeviceInformationX", "/DeviceInformation")
NETWORK_PATHS = ("/NetworkConfigurationX", "/NetworkConfiguration")

# Tag names vary across firmware generations, so try several per field.
SERIAL_TAGS = ("serialnumber", "serial", "devserialnumber")
HW_TAGS = ("hardwarerevision", "hwrevision")
MODEL_TAGS = ("modelnumber", "model")
MAC_TAGS = ("macaddress", "mac")
FW_TAGS = ("versionid", "apploadid", "phonedn_load", "loadinformation")

SWITCH_NAME_TAGS = ("cdpneighbordeviceid", "lldpneighbordeviceid", "neighbordeviceid")
SWITCH_PORT_TAGS = ("cdpneighborport", "lldpneighborport", "neighborport")
SWITCH_IP_TAGS = ("cdpneighborip", "lldpneighborip", "neighborip")
VLAN_TAGS = ("vlanid", "operationalvlanid", "adminvlanid")


@dataclass
class PhoneWebInfo:
    reachable: bool = False
    serial_number: str | None = None
    hardware_revision: str | None = None
    model_number: str | None = None
    mac_address: str | None = None
    firmware: str | None = None
    switch_name: str | None = None
    switch_port: str | None = None
    switch_ip: str | None = None
    vlan_id: str | None = None
    error: str | None = None


def _flatten(xml_bytes: bytes) -> dict[str, str]:
    """Collapse a phone XML document into {lowercase_tag: text}."""
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        tag = elem.tag.split("}", 1)[-1].lower()
        text = (elem.text or "").strip()
        if text and tag not in out:
            out[tag] = text
    return out


def _pick(data: dict[str, str], tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        value = data.get(tag)
        if value and value.lower() not in {"unknown", "n/a", "none", "0.0.0.0"}:
            return value
    return None


def _get_xml(client: httpx.Client, ip: str, paths: tuple[str, ...]) -> dict[str, str]:
    for scheme in ("http", "https"):
        for path in paths:
            url = f"{scheme}://{ip}{path}"
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                continue
            if resp.status_code == 200 and resp.content:
                data = _flatten(resp.content)
                if data:
                    return data
    return {}


def fetch_one(ip: str, timeout: float = 4.0) -> PhoneWebInfo:
    info = PhoneWebInfo()
    if not ip:
        info.error = "no IP address"
        return info
    try:
        with httpx.Client(
            timeout=timeout, verify=False, follow_redirects=True
        ) as client:
            device = _get_xml(client, ip, DEVICE_PATHS)
            network = _get_xml(client, ip, NETWORK_PATHS)
    except Exception as exc:  # noqa: BLE001 - a phone is never worth crashing on
        info.error = str(exc)
        return info

    if not device and not network:
        info.error = "no response (web access disabled or phone unreachable)"
        return info

    info.reachable = True
    info.serial_number = _pick(device, SERIAL_TAGS)
    info.hardware_revision = _pick(device, HW_TAGS)
    info.model_number = _pick(device, MODEL_TAGS)
    info.mac_address = _pick(device, MAC_TAGS)
    info.firmware = _pick(device, FW_TAGS)
    info.switch_name = _pick(network, SWITCH_NAME_TAGS)
    info.switch_port = _pick(network, SWITCH_PORT_TAGS)
    info.switch_ip = _pick(network, SWITCH_IP_TAGS)
    info.vlan_id = _pick(network, VLAN_TAGS)
    return info


def fetch_many(
    ips: list[str], *, concurrency: int = 25, timeout: float = 4.0
) -> dict[str, PhoneWebInfo]:
    """Scrape many phones at once. Returns {ip: PhoneWebInfo}."""
    targets = [ip for ip in ips if ip]
    if not targets:
        return {}
    results: dict[str, PhoneWebInfo] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for ip, info in zip(
            targets, pool.map(lambda i: fetch_one(i, timeout), targets)
        ):
            results[ip] = info
    reachable = sum(1 for i in results.values() if i.reachable)
    log.info("Phone web: %s/%s phones responded", reachable, len(targets))
    return results
