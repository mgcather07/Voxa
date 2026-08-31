"""RisPort70 client - live registration state for every phone.

AXL tells you what is configured. RisPort tells you what is actually plugged
in: current IP, which call manager node it registered to, running firmware
load, and why anything is unregistered.

Requires the Application User to hold "Standard CCM Admin Users (Read Only)"
and "Standard Serviceability (Read Only)".
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .soap import post_soap, text_of

log = logging.getLogger(__name__)

# RisPort hard-caps a single reply at 1000 devices regardless of what you ask.
MAX_DEVICES_PER_CALL = 1000

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:soap="http://schemas.cisco.com/ast/soap">
  <soapenv:Header/>
  <soapenv:Body>
    <soap:selectCmDeviceExt>
      <soap:StateInfo>{state_info}</soap:StateInfo>
      <soap:CmSelectionCriteria>
        <soap:MaxReturnedDevices>{max_devices}</soap:MaxReturnedDevices>
        <soap:DeviceClass>Phone</soap:DeviceClass>
        <soap:Model>255</soap:Model>
        <soap:Status>Any</soap:Status>
        <soap:NodeName></soap:NodeName>
        <soap:SelectBy>Name</soap:SelectBy>
        <!-- "Match all devices" is an EMPTY Item, not "*". CUCM 15's RISService70
             rejects a bare wildcard ("SelectItems cannot contain *"), and both an
             empty <SelectItems> container ("cannot be null") and an omitted one
             (ADB parse error) fault too. An empty Item returns the whole fleet;
             fetch_all() then pages past the 1000/reply cap via StateInfo. -->
        <soap:SelectItems>
          <soap:item>
            <soap:Item></soap:Item>
          </soap:item>
        </soap:SelectItems>
        <soap:Protocol>Any</soap:Protocol>
        <soap:DownloadStatus>Any</soap:DownloadStatus>
      </soap:CmSelectionCriteria>
    </soap:selectCmDeviceExt>
  </soapenv:Body>
</soapenv:Envelope>"""


@dataclass
class RisDevice:
    name: str
    status: str | None = None
    status_reason: str | None = None
    ip_address: str | None = None
    active_load: str | None = None
    inactive_load: str | None = None
    cm_node: str | None = None
    product: str | None = None
    model_enum: str | None = None
    dir_number: str | None = None
    http_supported: bool = False


def _first_ipv4(device: ET.Element) -> str | None:
    """selectCmDeviceExt returns IPAddress as a list of typed entries."""
    container = device.find("IPAddress")
    if container is None:
        # Older schemas expose a flat element.
        return text_of(device, "IPAddress")
    for item in list(container):
        addr = text_of(item, "IP")
        if addr and ":" not in addr:
            return addr
    for item in list(container):
        addr = text_of(item, "IP")
        if addr:
            return addr
    return None


class RisPortClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify: bool = False,
        timeout: float = 120.0,
    ) -> None:
        self.url = (
            f"https://{host}:8443/realtimeservice2/services/RISService70"
        )
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = timeout

    def _call(self, state_info: str) -> ET.Element:
        body = ENVELOPE.format(
            state_info=state_info, max_devices=MAX_DEVICES_PER_CALL
        )
        return post_soap(
            self.url,
            body,
            username=self.username,
            password=self.password,
            verify=self.verify,
            timeout=self.timeout,
        )

    def fetch_all(self, max_pages: int = 100) -> dict[str, RisDevice]:
        """Return every phone RisPort knows about, keyed by device name."""
        devices: dict[str, RisDevice] = {}
        state_info = ""
        total_expected: int | None = None

        for page in range(max_pages):
            root = self._call(state_info)
            result = root.find(".//SelectCmDeviceResult")
            if result is None:
                log.warning("RisPort returned no SelectCmDeviceResult")
                break

            if total_expected is None:
                raw_total = text_of(result, "TotalDevicesFound")
                total_expected = int(raw_total) if raw_total else None

            found_this_page = 0
            nodes = result.find("CmNodes")
            for node in list(nodes) if nodes is not None else []:
                node_name = text_of(node, "Name")
                cm_devices = node.find("CmDevices")
                for dev in list(cm_devices) if cm_devices is not None else []:
                    name = text_of(dev, "Name")
                    if not name:
                        continue
                    found_this_page += 1
                    devices[name] = RisDevice(
                        name=name,
                        status=text_of(dev, "Status"),
                        status_reason=text_of(dev, "StatusReason"),
                        ip_address=_first_ipv4(dev),
                        active_load=text_of(dev, "ActiveLoadID"),
                        inactive_load=text_of(dev, "InactiveLoadID"),
                        cm_node=node_name,
                        product=text_of(dev, "Product"),
                        model_enum=text_of(dev, "Model"),
                        dir_number=text_of(dev, "DirNumber"),
                        http_supported=(
                            (text_of(dev, "Httpd") or "").lower() == "yes"
                        ),
                    )

            log.info(
                "RisPort page %s: %s devices (%s total known)",
                page + 1,
                found_this_page,
                len(devices),
            )

            if found_this_page == 0:
                break
            if total_expected is not None and len(devices) >= total_expected:
                break

            next_state = root.findtext(".//StateInfo") or ""
            if not next_state or next_state == state_info:
                break
            state_info = next_state

        return devices
