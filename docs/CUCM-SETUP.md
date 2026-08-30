# CUCM setup

Everything here is read-only access. The app never writes to CUCM.

---

## 1. Service account

**User Management → Application User → Add New**

- User ID: `phoneinv-ro`
- Password: generate something long; it goes in `.env.prod`, nowhere else

Add these roles under **Permissions Information → Add to Access Control
Group**:

| Access control group | Gives us |
|---|---|
| `Standard AXL API Access` | `executeSQLQuery` — the phone configuration |
| `Standard CCM Admin Users` | RisPort authorization |
| `Standard CCM Server Monitoring` | RisPort device state |
| `Standard RealtimeAndTraceCollection` | RisPort on some releases |

If you want to be more careful than the standard groups allow, create a custom
role with read-only rights to *Standard AXL API Access* and the serviceability
pages, and skip the admin group. The connectivity check will tell you if you
cut too deep.

**Point the app at the publisher.** AXL runs only there. A subscriber returns
an error that reads like a permissions problem and will cost you an hour.

## 2. Verify before anything else

```bash
python scripts/test_cucm.py
```

It tests AXL, RisPort, and a phone scrape separately and names the likely
missing role for whichever one fails. Run this any time collection breaks —
in practice it is nearly always the service account, not the app.

## 3. Phone web access

Serial numbers and switch/port data come from the phones themselves, not from
CUCM. Each phone needs **Web Access = Enabled**.

Set it on the **Common Phone Profile** (Device → Device Settings → Common
Phone Profile) rather than per phone, then reset the affected devices. Doing
it per phone across a few thousand devices is not a good use of an afternoon.

Two things to know:

- Web access on a phone exposes its configuration, including the switch it is
  attached to, to anyone who can reach it on port 80. Some organizations
  disable it deliberately. If yours did, that is a conversation to have rather
  than route around — and the app degrades gracefully: those two columns are
  simply blank.
- Newer firmware may serve this only over HTTPS with a device certificate.
  `phoneweb.py` tries HTTP then HTTPS with verification off, which is
  appropriate for scraping a device on your own management network.

## 4. Network reachability

The VM must reach:

- the CUCM publisher on **TCP 8443**
- every phone subnet on **TCP 80 and 443**

Without the second one the app still works, but the PoE page will be empty and
the serial column blank. Those are the highest-value columns, so it is worth
the firewall request.

## 5. AXL schema version

`CUCM_AXL_VERSION` must be less than or equal to your cluster version. `12.5`
works against clusters from 12.5 through 15 — the schema is backward
compatible and the queries we run are stable across all of them. Only raise it
if you need a field that a newer schema added.

---

## Later: CDR

Not needed for inventory. When you get to the CDR work in `docs/ROADMAP.md`,
the CUCM side is **Serviceability → Tools → CDR Management → Billing
Application Server**, pointing at this VM over SFTP. CUCM pushes files to you;
there is nothing to poll.
