"""AXL client - configuration data straight out of the CUCM database.

AXL exposes `executeSQLQuery`, which runs read-only SQL against the CUCM
Informix schema. That is dramatically faster and richer than paging
`listPhone`: one query gets model, description, device pool, firmware, and
primary DN for every phone in the cluster.

Requires the Application User to hold "Standard AXL API Access".
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Iterator

from .soap import CucmError, post_soap

log = logging.getLogger(__name__)

# tkclass 1 = Phone. SELECT FIRST n is Informix's LIMIT.
PHONE_SQL = """
SELECT FIRST {limit}
       d.pkid AS pkid,
       d.name AS name,
       d.description AS description,
       tm.name AS model,
       tp.name AS protocol,
       dp.name AS device_pool,
       -- CUCM 15's device table has no `loadinformation` column; the per-device
       -- firmware-load override lives in `specialloadinformation` (blank unless
       -- an admin overrides the model default). Running load comes from RisPort.
       d.specialloadinformation AS load_information,
       np.dnorpattern AS directory_number
  FROM device AS d
  LEFT JOIN typemodel AS tm ON tm.enum = d.tkmodel
  LEFT JOIN typedeviceprotocol AS tp ON tp.enum = d.tkdeviceprotocol
  LEFT JOIN devicepool AS dp ON dp.pkid = d.fkdevicepool
  LEFT JOIN devicenumplanmap AS dnpm
         ON dnpm.fkdevice = d.pkid AND dnpm.numplanindex = 1
  LEFT JOIN numplan AS np ON np.pkid = dnpm.fknumplan
 WHERE d.tkclass = 1
   AND d.pkid > '{after}'
 ORDER BY d.pkid
"""

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ns="http://www.cisco.com/AXL/API/{version}">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:executeSQLQuery>
      <sql>{sql}</sql>
    </ns:executeSQLQuery>
  </soapenv:Body>
</soapenv:Envelope>"""


@dataclass
class AxlPhone:
    pkid: str
    name: str
    description: str | None = None
    model: str | None = None
    protocol: str | None = None
    device_pool: str | None = None
    load_information: str | None = None
    directory_number: str | None = None
    extra: dict = field(default_factory=dict)


class AxlClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        version: str = "12.5",
        verify: bool = False,
        timeout: float = 120.0,
    ) -> None:
        self.url = f"https://{host}:8443/axl/"
        self.username = username
        self.password = password
        self.version = version
        self.verify = verify
        self.timeout = timeout

    def execute_sql(self, sql: str) -> list[dict[str, str | None]]:
        body = ENVELOPE.format(version=self.version, sql=html.escape(sql.strip()))
        root = post_soap(
            self.url,
            body,
            username=self.username,
            password=self.password,
            soap_action=f"CUCM:DB ver={self.version} executeSQLQuery",
            verify=self.verify,
            timeout=self.timeout,
        )
        rows: list[dict[str, str | None]] = []
        for row in root.iter("row"):
            record: dict[str, str | None] = {}
            for col in row:
                value = (col.text or "").strip()
                record[col.tag] = value or None
            rows.append(record)
        return rows

    def iter_phones(self, page_size: int = 1000) -> Iterator[AxlPhone]:
        """Page through every phone in the cluster, keyed on pkid order.

        executeSQLQuery caps its response size, so we page rather than asking
        for 30,000 rows at once and getting a truncated or refused reply.
        """
        after = ""
        seen = 0
        while True:
            sql = PHONE_SQL.format(limit=page_size, after=after)
            rows = self.execute_sql(sql)
            if not rows:
                break
            for row in rows:
                yield AxlPhone(
                    pkid=row.get("pkid") or "",
                    name=row.get("name") or "",
                    description=row.get("description"),
                    model=row.get("model"),
                    protocol=row.get("protocol"),
                    device_pool=row.get("device_pool"),
                    load_information=row.get("load_information"),
                    directory_number=row.get("directory_number"),
                )
            seen += len(rows)
            after = rows[-1].get("pkid") or ""
            log.info("AXL: fetched %s phones so far", seen)
            if len(rows) < page_size:
                break

    def test_connection(self) -> str:
        """Cheap round trip that proves credentials and roles are right."""
        rows = self.execute_sql("SELECT FIRST 1 version FROM componentversion")
        if rows:
            return rows[0].get("version") or "unknown"
        raise CucmError("AXL responded but returned no version row.")
