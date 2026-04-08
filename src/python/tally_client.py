import os
import re
from xml.sax.saxutils import escape

import requests

TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
TALLY_COMPANY = os.environ.get("TALLY_COMPANY", "")
HEADERS = {"Content-Type": "text/xml;charset=utf-8"}
SESSION = requests.Session()


class TallyError(RuntimeError):
    """Base error for Tally transport failures."""


class TallyTimeoutError(TallyError):
    """Raised when Tally does not respond within the configured timeout."""


class TallyConnectionError(TallyError):
    """Raised when Tally is unreachable or closes the connection."""


def get_connect_timeout_seconds() -> int:
    try:
        return max(3, int(os.environ.get("TB_TALLY_CONNECT_TIMEOUT_SECONDS", "5")))
    except ValueError:
        return 5


def get_read_timeout_seconds() -> int:
    try:
        return max(10, int(os.environ.get("TB_TALLY_READ_TIMEOUT_SECONDS", "45")))
    except ValueError:
        return 45


def _xml_escape(value: str) -> str:
    return escape(value or "")


def _decode_response(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "").lower()
    content = response.content or b""
    looks_utf16 = (
        "utf-16" in content_type
        or content[:2] in (b"\xff\xfe", b"\xfe\xff")
        or b"\x00<" in content[:64]
        or b"<\x00" in content[:64]
    )

    if looks_utf16:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                decoded = content.decode(encoding)
                if "<ENVELOPE" in decoded.upper() or "<RESPONSE" in decoded.upper():
                    return decoded
            except UnicodeDecodeError:
                continue

    response.encoding = response.encoding or "utf-8"
    return response.text


def _post(xml: str, *, connect_timeout: int | None = None, read_timeout: int | None = None) -> str:
    try:
        response = SESSION.post(
            TALLY_URL,
            data=xml.strip().encode("utf-8"),
            headers=HEADERS,
            timeout=(
                connect_timeout or get_connect_timeout_seconds(),
                read_timeout or get_read_timeout_seconds(),
            ),
        )
        response.raise_for_status()
        return _decode_response(response)
    except requests.exceptions.Timeout as exc:
        raise TallyTimeoutError(
            "Timed out waiting for Tally XML response. "
            "This often happens on large ERP 9 exports; retry with a smaller date window."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise TallyConnectionError(
            "Could not reach the Tally XML server. Verify Tally is open and listening on the configured port."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise TallyError(f"Tally request failed: {exc}") from exc


def _check_response(xml_text: str) -> str:
    """Check Tally response STATUS tag. Raises on failure (STATUS=0)."""
    # Look for <STATUS>0</STATUS> indicating failure
    status_match = re.search(r'<STATUS>\s*(\d+)\s*</STATUS>', xml_text, re.IGNORECASE)
    if status_match and status_match.group(1) == "0":
        # Try to extract error description
        err_match = re.search(r'<DATA>\s*(.*?)\s*</DATA>', xml_text, re.IGNORECASE | re.DOTALL)
        err_desc = err_match.group(1).strip() if err_match else "Unknown error"
        # Clean HTML/XML from error
        err_desc = re.sub(r'<[^>]+>', ' ', err_desc).strip()
        raise RuntimeError(f"Tally returned error: {err_desc}")

    line_error = re.search(r'<LINEERROR>\s*(.*?)\s*</LINEERROR>', xml_text, re.IGNORECASE | re.DOTALL)
    if line_error:
        err_desc = re.sub(r'<[^>]+>', ' ', line_error.group(1)).strip()
        raise RuntimeError(f"Tally returned error: {err_desc}")

    return xml_text


def _fetch(
    xml: str,
    *,
    connect_timeout: int | None = None,
    read_timeout: int | None = None,
) -> str:
    """Post XML to Tally and validate the response."""
    return _check_response(
        _post(
            xml,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    )


def detect_tally_product() -> dict:
    """Best-effort server identity probe using the plain HTTP endpoint."""
    try:
        response = SESSION.get(
            TALLY_URL,
            timeout=(get_connect_timeout_seconds(), min(get_read_timeout_seconds(), 10)),
        )
        response.raise_for_status()
        body = _decode_response(response)
        product_name = None
        product_version = None

        if "Tally.ERP 9" in body:
            product_name = "Tally.ERP 9"
        elif "TallyPrime" in body:
            product_name = "TallyPrime"

        version_match = re.search(
            r"(?:Release|Version)\s+([0-9]+(?:\.[0-9]+)*)",
            body,
            re.IGNORECASE,
        )
        if version_match:
            product_version = version_match.group(1)

        return {
            "product_name": product_name,
            "product_version": product_version,
            "raw": body.strip()[:200],
        }
    except Exception:
        return {
            "product_name": None,
            "product_version": None,
            "raw": None,
        }


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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
        <ID>Group</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="Group" ISMODIFY="No">
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
        <ID>Ledger</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="Ledger" ISMODIFY="No">
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


# ── Stock Items / Summary ────────────────────────────────────────

def get_stock_items() -> str:
    """Fetch stock details via a structured StockItem collection when available."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>StockItem</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="StockItem" ISMODIFY="No">
                <TYPE>StockItem</TYPE>
                <FETCH>NAME, PARENT, BASEUNITS, CLOSINGBALANCE, CLOSINGVALUE, CLOSINGRATE</FETCH>
              </COLLECTION>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""")


def get_stock_summary_report() -> str:
    """Fallback stock summary report export for older/stricter Tally responses."""
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
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
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")
