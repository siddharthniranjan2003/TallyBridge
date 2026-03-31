import requests
import os
import re

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


def _check_response(xml_text: str) -> str:
    """Check Tally response STATUS tag. Raises on failure (STATUS=0)."""
    # Look for <STATUS>0</STATUS> indicating failure
    status_match = re.search(r'<STATUS>\s*(\d+)\s*</STATUS>', xml_text)
    if status_match and status_match.group(1) == "0":
        # Try to extract error description
        err_match = re.search(r'<DATA>\s*(.*?)\s*</DATA>', xml_text, re.DOTALL)
        err_desc = err_match.group(1).strip() if err_match else "Unknown error"
        # Clean HTML/XML from error
        err_desc = re.sub(r'<[^>]+>', ' ', err_desc).strip()
        raise RuntimeError(f"Tally returned error: {err_desc}")
    return xml_text


def _fetch(xml: str) -> str:
    """Post XML to Tally and validate the response."""
    return _check_response(_post(xml))


# ── Company Info ─────────────────────────────────────────────────

def get_company_info() -> str:
    """Fetch company metadata: FY dates, GSTIN, address, etc."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>CompanyInfo</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="CompanyInfo" ISMODIFY="No">
                <TYPE>Company</TYPE>
                <FETCH>NAME, BOOKSFROM, BOOKSTO, GUID, MASTERID,
                       BASICCOMPANYADDRESS, STATENAME, COUNTRYNAME,
                       PINCODE, EMAIL, PHONENUMBER,
                       GSTREGISTRATIONTYPE, PARTYGSTIN, INCOMETAXNUMBER</FETCH>
                <FILTERS>NonAggrFilter</FILTERS>
              </COLLECTION>
              <SYSTEM TYPE="FORMULAE" NAME="NonAggrFilter">$isaggregate = "No"</SYSTEM>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Change Detection ─────────────────────────────────────────────

def get_company_alter_ids() -> str:
    """Fetch change-detection counters (ALTERID, ALTVCHID, ALTMSTID) for the current company."""
    return _fetch(f"""
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


# ── Groups ───────────────────────────────────────────────────────

def get_groups() -> str:
    """Fetch all accounting groups (chart of accounts hierarchy)."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>AllGroups</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="AllGroups" ISMODIFY="No">
                <TYPE>Group</TYPE>
                <FETCH>NAME, PARENT, MASTERID, ISREVENUE, ISDEEMEDPOSITIVE,
                       SORTPOSITION, AFFECTSSTOCK, ISSUBLEDGER</FETCH>
              </COLLECTION>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Ledgers (Collection + FETCH — structured data) ──────────────

def get_ledgers() -> str:
    """Fetch all ledgers with extended fields using Collection+FETCH pattern."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>AllLedgers</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="AllLedgers" ISMODIFY="No">
                <TYPE>Ledger</TYPE>
                <FETCH>NAME, PARENT, OPENINGBALANCE, CLOSINGBALANCE, MASTERID,
                       EMAIL, LEDGERPHONE, LEDGERMOBILE, PINCODE,
                       PARTYGSTIN, LEDSTATENAME, COUNTRYNAME,
                       CREDITPERIOD, CREDITLIMIT,
                       BANKACCOUNT, IFSCODE, INCOMETAXNUMBER,
                       MAILINGNAME, GUID</FETCH>
              </COLLECTION>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Vouchers ─────────────────────────────────────────────────────

def get_vouchers(from_date: str, to_date: str) -> str:
    """Fetch all vouchers (Day Book) for the given date range."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Day Book</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE>{from_date}</SVFROMDATE>
            <SVTODATE>{to_date}</SVTODATE>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Stock Items ──────────────────────────────────────────────────

def get_stock_items() -> str:
    """Fetch stock summary report."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Stock Summary</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Outstanding ──────────────────────────────────────────────────

def get_outstanding_receivables() -> str:
    """Fetch Bills Receivable report."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Bills Receivable</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


def get_outstanding_payables() -> str:
    """Fetch Bills Payable report."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Bills Payable</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Financial Reports ────────────────────────────────────────────

def get_profit_and_loss(from_date: str, to_date: str) -> str:
    """Fetch Profit & Loss statement for the given date range."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Profit and Loss</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE>{from_date}</SVFROMDATE>
            <SVTODATE>{to_date}</SVTODATE>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


def get_balance_sheet(from_date: str, to_date: str) -> str:
    """Fetch Balance Sheet for the given date range."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Balance Sheet</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE>{from_date}</SVFROMDATE>
            <SVTODATE>{to_date}</SVTODATE>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


def get_trial_balance(from_date: str, to_date: str) -> str:
    """Fetch Trial Balance for the given date range."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Trial Balance</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE>{from_date}</SVFROMDATE>
            <SVTODATE>{to_date}</SVTODATE>
            <SVCURRENTCOMPANY>{TALLY_COMPANY}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")