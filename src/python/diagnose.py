"""
TallyBridge Diagnostic Tool
Run this once to capture exact XML from your TallyPrime.
It saves every response to /tally-responses/ folder so you can inspect them.
Run: python diagnose.py
"""

import requests
import os
from datetime import date

TALLY_URL = "http://localhost:9000"
COMPANY = "Demo Trading Co"   # ← change to your exact company name
OUTPUT_DIR = "tally-responses"

os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {"Content-Type": "text/xml;charset=utf-8"}

def send(name: str, xml: str):
    print(f"\n{'='*60}")
    print(f"REQUEST: {name}")
    print(f"{'='*60}")
    try:
        r = requests.post(TALLY_URL, data=xml.strip().encode("utf-8"),
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        response = r.text

        # Save full response to file
        filepath = os.path.join(OUTPUT_DIR, f"{name}.xml")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(response)

        # Print first 600 chars to console
        print(f"STATUS: {r.status_code}")
        print(f"LENGTH: {len(response)} chars")
        print(f"SAVED TO: {filepath}")
        print(f"\nFIRST 600 CHARS:")
        print(response[:600])
        return response
    except Exception as e:
        print(f"ERROR: {e}")
        return None

# ── 1. List of Companies ──────────────────────────────────────────
send("01_companies", """
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>List of Companies</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 2. List of Ledgers ────────────────────────────────────────────
send("02_ledgers", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>List of Accounts</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 3. Day Book (Vouchers) ────────────────────────────────────────
today = date.today()
fy_year = today.year - 1 if today.month < 4 else today.year
from_date = f"{fy_year}0401"
to_date = today.strftime("%Y%m%d")

send("03_vouchers_daybook", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Day Book</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVFROMDATE>{from_date}</SVFROMDATE>
      <SVTODATE>{to_date}</SVTODATE>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 4. Stock Summary ──────────────────────────────────────────────
send("04_stock_summary", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Stock Summary</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 5. Stock Items (master data) ──────────────────────────────────
send("05_stock_items_master", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>List of Stock Items</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 6. Outstanding Receivables ────────────────────────────────────
send("06_outstanding_receivables", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Bills Receivable</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 7. Outstanding Payables ───────────────────────────────────────
send("07_outstanding_payables", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Bills Payable</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 8. Trial Balance ──────────────────────────────────────────────
send("08_trial_balance", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Trial Balance</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 9. Profit and Loss ────────────────────────────────────────────
send("09_profit_and_loss", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Profit and Loss</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

# ── 10. Cash Book ─────────────────────────────────────────────────
send("10_cash_book", f"""
<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY><EXPORTDATA><REQUESTDESC>
    <REPORTNAME>Cash Book</REPORTNAME>
    <STATICVARIABLES>
      <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      <SVCURRENTCOMPANY>{COMPANY}</SVCURRENTCOMPANY>
    </STATICVARIABLES>
  </REQUESTDESC></EXPORTDATA></BODY>
</ENVELOPE>""")

print(f"\n{'='*60}")
print(f"DONE. All responses saved to: {OUTPUT_DIR}/")
print(f"Open each .xml file in VS Code to inspect the exact structure.")
print(f"{'='*60}")