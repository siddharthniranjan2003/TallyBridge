> From: https://claude.ai/chat/3d614111-77f8-4e1c-97ed-bbf9be65054d

# you asked

get me all the resources for fetching data from tally prime and its erp versions , from opensource github repos, official docs , someone explaining it , blogs , whatever

---

# claude response

Here's a comprehensive resource dump — everything organized by category:

---

## 📘 Official Docs (Tally Help)

These are the most authoritative references. TallyPrime uses a consistent XML structure for communication, built around an `<ENVELOPE>` element containing `<HEADER>` and `<BODY>` — and it exposes its data via XML over HTTP, acting as both server and client.

| Resource | URL |
|---|---|
| **XML Integration Overview** | https://help.tallysolutions.com/xml-integration/ |
| **Integration Methods & Technologies** | https://help.tallysolutions.com/integration-methods-and-technologies/ |
| **Integration with TallyPrime (main)** | https://help.tallysolutions.com/integration-with-tallyprime/ |
| **XML Request/Response Case Studies** | https://help.tallysolutions.com/case-study-1/ |
| **TallyPrime as HTTP Client** | https://help.tallysolutions.com/developer-reference/integration-using-xml-interface/tallyprime-as-a_client/ |
| **Authentication Library: Get Data** | https://help.tallysolutions.com/developer-reference/tally-authentication-library/get-data/ |
| **ODBC Integration (ERP 9)** | https://help.tallysolutions.com/odbc-integrations/ |
| **TallyPrime Developer Page** | https://tallysolutions.com/tally-prime-developer/ |

Key concepts from official docs:
- `<TYPE>` can be `DATA`, `COLLECTION`, `OBJECT`, `FUNCTION`, or `ACTION`. `<FETCHLIST>` lists the fields/methods to retrieve. `<STATICVARIABLES>` holds globals like `SVCURRENTCOMPANY`, `SVFROMDATE`.
- External APIs initiate HTTP POST where XML data is in native Tally structure directly postable to TallyPrime, or HTTP GET to fetch in native/custom format using report attributes.
- For ODBC: Tally ERP 9 has a built-in SQL processor that processes `SELECT` statements on collections — e.g., `Select $Name, $ClosingBalance from Ledger where $$IsDr:$ClosingBalance`

---

## 🔧 Open Source GitHub Repos

### 🥇 TallyConnector (C# — most mature)
**TallyConnector** is a C# library bridging the Tally XML API. You interact with C# objects directly — no manual XML construction. Supports TallyPrime 4 through 7, targets .NET Core 8/9/10.

```
https://github.com/Accounting-Companion/TallyConnector
```

### 🐍 Tally.Py (Python)
**Tally.Py** provides a comprehensive Python client (`xmlFunctions.py`) with methods for data retrieval (ledgers, stock items, vouchers), master management, and transaction processing. Also includes experimental TDL files for deeper integration and AI service hookups (Claude, AI Studio).

```
https://github.com/aadil-sengupta/Tally.Py
pip install tally-integration
```

PyPI page: https://pypi.org/project/tally-integration/

### 🐍 Tally-Automation (Python)
Simpler Python automation scripts:
```
https://github.com/hashfx/Tally-Automation
```

### 📊 Tally-Connector (Excel/VBA + TDL + ODBC)
Excel connector via TDL and ODBC — useful for CAs wanting to pull Tally data into Excel for financial statement prep, GST audit, and ageing analysis.
```
https://github.com/ramajayam-CA/Tally-Connector
```

### GitHub Topic Pages (browse all repos)
- https://github.com/topics/tallyprime
- https://github.com/topics/tallyerp9
- https://github.com/topics/tally-integration

---

## 📬 Postman Collection (XML Request Reference)

**Sai Vineeth's Postman collection** — built while developing TallyConnector, documents all the XML requests for TallyPrime/ERP 9 data fetching.

```
https://documenter.getpostman.com/view/13855108/TzeRpAMt
```
This is a goldmine — has ready-to-fire XML for ledgers, vouchers, trial balance, stock items, etc.

---

## 📝 Blogs & Tutorials

### Sai Vineeth's Blog
Covers using TallyConnector from Python via `pythonnet` — including checking if Tally is running, getting the active company, listing all companies, and fetching masters.

```
https://blog.saivineeth.com/tallyconnector/Python-Tally-Integration_Getting-Started/
```

### api2books.com — TallyPrime GET API Plugin Docs
Documents a TDL plugin approach for extracting data to JSON format, with FTP/Web API push options and scheduled sync — useful to understand how commercial tools do it.

```
https://api2books.com/tallyprime-get-api-documentation/
```

---

## 🛠️ Third-Party / Commercial (worth knowing)

- **CData ODBC Driver for Tally** — models Tally data as relational tables (Ledgers, Vouchers, Stock Items, Cost Centers) over ODBC for BI tools like Power BI, Tableau, Excel. → https://www.cdata.com/drivers/tally/odbc/
- **Tally MCP Server** (excelkida.com) — the one you already use with Cline, good reference for what a production-grade Node.js bridge looks like

---

## 🧠 Key Technical Facts to Keep Handy

Since you've already debugged the UTF-16LE encoding issue in your TallyBridge project, you know the practical gotchas better than most docs admit. A few things worth cross-referencing:

- **Port:** Default `9000` (configurable in TallyPrime → F1 → Settings → Advanced Configuration)
- **Encoding:** UTF-16LE for requests (as you discovered via Wireshark — docs don't clearly state this)
- **TDL Collections vs Report requests:** Collections give raw structured data; Report requests give formatted output
- **`<FETCHLIST>`:** Critical tag — controls which fields come back; omitting it returns defaults only
- The `<STATICVARIABLES>` block is where you set company name, date ranges (`SVFROMDATE`, `SVTODATE`), and format

The **Postman collection** and **TallyConnector C# source code** are probably the two most useful things here for reverse-engineering what XML structures work for which data types — both are actively maintained and cover ERP 9 + TallyPrime.