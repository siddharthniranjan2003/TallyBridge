import requests
import os

TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
TALLY_COMPANY = os.environ.get("TALLY_COMPANY", "")
HEADERS = {"Content-Type": "text/xml;charset=utf-8"}

def _post(xml: str) -> str:
    response = requests.post(
        TALLY_URL,
        data=xml.strip().encode("utf-8"),
        headers=HEADERS,
        timeout=30
    )
    response.raise_for_status()
    return response.text


# ── Change Detection ─────────────────────────────────────────────
# Fetches ALTERID / ALTVCHID / ALTMSTID counters from TallyPrime.
# These increment whenever any master or voucher is created/modified.
# Compare with previous values to skip sync if nothing changed.

def get_company_alter_ids() -> str:
    """Fetch change-detection counters (ALTERID, ALTVCHID, ALTMSTID) for the current company."""
    return _post(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>CompanyAlterIds</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVFROMDATE TYPE="Date">01-Jan-1970</SVFROMDATE>
            <SVTODATE TYPE="Date">01-Jan-1970</SVTODATE>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="CompanyAlterIds" ISMODIFY="No">
                <TYPE>Company</TYPE>
                <FETCH>NAME,ALTERID,MASTERID,CMPVCHID,ALTVCHID,ALTMSTID,LASTVOUCHERDATE</FETCH>
                <FILTERS>NonAggrFilter</FILTERS>
              </COLLECTION>
              <SYSTEM TYPE="FORMULAE" NAME="NonAggrFilter">$isaggregate = "No"</SYSTEM>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Existing Data Fetchers ───────────────────────────────────────

def get_ledgers() -> str:
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>List of Accounts</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")

def get_vouchers(from_date: str, to_date: str) -> str:
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Day Book</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")

def get_stock_items() -> str:
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Stock Summary</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")

def get_outstanding_receivables() -> str:
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Bills Receivable</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")

def get_outstanding_payables() -> str:
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Bills Payable</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")


# ── NEW: Financial Reports ───────────────────────────────────────

def get_profit_and_loss(from_date: str, to_date: str) -> str:
    """Fetch Profit & Loss statement for the given date range."""
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Profit and Loss A/c</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")

def get_balance_sheet(from_date: str, to_date: str) -> str:
    """Fetch Balance Sheet for the given date range."""
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Balance Sheet</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")

def get_trial_balance(from_date: str, to_date: str) -> str:
    """Fetch Trial Balance for the given date range."""
    return _post(f"""
    <ENVELOPE>
      <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
      <BODY><EXPORTDATA><REQUESTDESC>
        <REPORTNAME>Trial Balance</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVFROMDATE>{from_date}</SVFROMDATE>
          <SVTODATE>{to_date}</SVTODATE>
          <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC></EXPORTDATA></BODY>
    </ENVELOPE>""")