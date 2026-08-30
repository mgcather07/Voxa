"""Minimal SOAP plumbing for the CUCM APIs.

We deliberately do *not* use zeep here. Zeep needs the AXL WSDL bundle, which
you have to download from the CUCM Plugins page and keep in sync with the
cluster version. Every call we make is a single fixed operation, so hand-rolled
XML over httpx is smaller, faster, and has no version-pinned artifacts to ship.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import httpx


class CucmError(RuntimeError):
    """Raised when CUCM returns a SOAP fault or an unexpected HTTP status."""


def strip_ns(elem: ET.Element) -> ET.Element:
    """Remove XML namespaces in place so we can use plain tag names."""
    for e in elem.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return elem


def post_soap(
    url: str,
    body: str,
    *,
    username: str,
    password: str,
    soap_action: str = "",
    verify: bool = False,
    timeout: float = 60.0,
) -> ET.Element:
    """POST a SOAP envelope and return the namespace-stripped response root."""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": soap_action,
        "Accept": "text/xml",
    }
    try:
        resp = httpx.post(
            url,
            content=body.encode("utf-8"),
            headers=headers,
            auth=(username, password),
            verify=verify,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:  # network-level failure
        raise CucmError(f"Could not reach {url}: {exc}") from exc

    if resp.status_code == 401:
        raise CucmError(
            "401 Unauthorized. Check CUCM_USER/CUCM_PASSWORD and that the "
            "Application User holds the required roles."
        )

    # A SOAP fault still parses, and its text is far more useful than the code.
    try:
        root = strip_ns(ET.fromstring(resp.content))
    except ET.ParseError as exc:
        raise CucmError(
            f"{url} returned non-XML (HTTP {resp.status_code}): "
            f"{resp.text[:300]}"
        ) from exc

    fault = root.find(".//Fault")
    if fault is not None:
        detail = fault.findtext("faultstring") or ET.tostring(
            fault, encoding="unicode"
        )
        raise CucmError(f"SOAP fault from {url}: {detail.strip()}")

    if resp.status_code >= 400:
        raise CucmError(f"HTTP {resp.status_code} from {url}: {resp.text[:300]}")

    return root


def text_of(elem: ET.Element | None, tag: str, default: Any = None) -> Any:
    if elem is None:
        return default
    value = elem.findtext(tag)
    if value is None:
        return default
    value = value.strip()
    return value or default
