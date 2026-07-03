import html
import os
import re
import time
from datetime import datetime
from xml.sax.saxutils import escape

import requests

TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
TALLY_COMPANY = os.environ.get("TALLY_COMPANY", "")
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


def get_request_encoding_mode() -> str:
    mode = (os.environ.get("TB_TALLY_REQUEST_ENCODING", "utf-8") or "utf-8").strip().lower()
    if mode not in {"utf-8", "utf8", "utf-16", "utf16", "utf-16-le", "utf16le"}:
        return "utf-8"
    return mode


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


def _encode_request_body(xml: str, request_encoding: str) -> tuple[bytes, dict]:
    normalized = request_encoding.strip().lower()
    if normalized in {"utf-16", "utf16", "utf-16-le", "utf16le"}:
        body = xml.strip().encode("utf-16-le")
        return body, {
            "Content-Type": "text/xml;charset=utf-16",
            "Content-Length": str(len(body)),
        }

    body = xml.strip().encode("utf-8")
    return body, {
        "Content-Type": "text/xml;charset=utf-8",
        "Content-Length": str(len(body)),
    }


def _post(
    xml: str,
    *,
    connect_timeout: int | None = None,
    read_timeout: int | None = None,
    request_encoding: str | None = None,
) -> str:
    encoded, headers = _encode_request_body(xml, request_encoding or get_request_encoding_mode())
    # One bounded retry, only for transient transport failures where the request
    # almost certainly didn't reach/run inside Tally: a dropped/refused connection
    # (Tally mid-restart) or a 5xx (a proxy in front of it). We deliberately do
    # NOT retry timeouts — a timeout means Tally is slow/busy and hitting it again
    # would compound the load on the single-threaded gateway.
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            response = SESSION.post(
                TALLY_URL,
                data=encoded,
                headers=headers,
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
            if attempt < max_attempts - 1:
                time.sleep(1.0)  # Tally may be mid-restart; give it a moment
                continue
            raise TallyConnectionError(
                "Could not reach the Tally XML server. Verify Tally is open and listening on the configured port."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            if 500 <= status < 600 and attempt < max_attempts - 1:
                time.sleep(1.0)
                continue
            raise TallyError(f"Tally request failed: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise TallyError(f"Tally request failed: {exc}") from exc
    # Unreachable (the loop either returns or raises), but keeps type-checkers happy.
    raise TallyError("Tally request failed after retries.")


def _clean_tally_error(raw: str) -> str:
    """Turn Tally's raw XML error text into a clean, actionable message.

    Strips embedded tags and decodes XML entities (so `&apos;` shows as `'`
    instead of leaking through to the UI). Also rewrites TallyPrime's confusing
    "Could not find Company '<name>'!" error — when no company is loaded Tally
    echoes an empty name (`''`), which tells the user nothing — into guidance
    that names the configured company and what to do about it.
    """
    text = re.sub(r'<[^>]+>', ' ', raw or '')
    text = html.unescape(text).strip()
    text = re.sub(r'\s+', ' ', text)

    if re.match(r"could not find company", text, re.IGNORECASE):
        echoed = re.search(r"company\s+'([^']*)'", text, re.IGNORECASE)
        echoed_name = echoed.group(1).strip() if echoed else ""
        company = echoed_name or (TALLY_COMPANY or "").strip()
        target = f'"{company}"' if company else "the configured company"
        return (
            f"Could not find {target} in TallyPrime. "
            "Open that company in TallyPrime (Gateway of Tally) and keep it loaded, then retry the sync."
        )

    return text or "Unknown error"


def _assert_data_response(xml_text: str) -> str:
    """Reject clearly-non-data Tally responses (review findings tally-dep-02 /
    push-02). A busy/locked TallyPrime, one with no company loaded, or one
    showing a license/activation dialog answers port 9000 with HTTP 200 whose
    body is NOT XML data — an HTML license/info page or an empty body. These
    contain neither STATUS=0 nor LINEERROR, so on the READ path they used to
    parse to 0 rows (a cloud-wipe path), and on the PUSH path they used to parse
    to created=0/errors=0 and get acked as 'pushed' (silent voucher loss).
    Reject the unambiguous non-data shapes loudly so the caller skips/retries
    instead. (Conservative: a near-empty <ENVELOPE/> is left to the wipe guards
    / the push created==0 check.) Returns the text on success.

    lstrip the BOM first so a UTF-16 degraded Tally's BOM-only body still counts
    as empty.
    """
    stripped = (xml_text or "").lstrip("\ufeff").strip()
    if not stripped:
        raise RuntimeError(
            "TallyPrime returned an empty response. Make sure a company is loaded "
            "and TallyPrime is not showing a dialog, then retry."
        )
    head = stripped[:512].lower()
    if head.startswith("<!doctype html") or head.startswith("<html") or "<html>" in head:
        raise RuntimeError(
            "TallyPrime returned a web/license page instead of XML data. Open the "
            "company in TallyPrime and make sure no activation/license dialog is open, "
            "then retry."
        )
    return xml_text


def _check_response(xml_text: str) -> str:
    """Check Tally response STATUS tag. Raises on failure (STATUS=0)."""
    _assert_data_response(xml_text)

    # Look for <STATUS>0</STATUS> indicating failure
    status_match = re.search(r'<STATUS>\s*(\d+)\s*</STATUS>', xml_text, re.IGNORECASE)
    if status_match and status_match.group(1) == "0":
        # Try to extract error description
        err_match = re.search(r'<DATA>\s*(.*?)\s*</DATA>', xml_text, re.IGNORECASE | re.DOTALL)
        err_desc = err_match.group(1).strip() if err_match else "Unknown error"
        raise RuntimeError(f"Tally returned error: {_clean_tally_error(err_desc)}")

    line_error = re.search(r'<LINEERROR>\s*(.*?)\s*</LINEERROR>', xml_text, re.IGNORECASE | re.DOTALL)
    if line_error:
        raise RuntimeError(f"Tally returned error: {_clean_tally_error(line_error.group(1))}")

    return xml_text


def _fetch(
    xml: str,
    *,
    connect_timeout: int | None = None,
    read_timeout: int | None = None,
    request_encoding: str | None = None,
) -> str:
    """Post XML to Tally and validate the response."""
    return _check_response(
        _post(
            xml,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            request_encoding=request_encoding,
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


def get_company_period() -> str:
    """Fetch the loaded company's financial-year period (STARTINGFROM/ENDINGAT).

    Scoped by SVCURRENTCOMPANY — the same context a push uses — so the period
    returned is the period of the company a push would actually write to.
    STARTINGFROM/ENDINGAT are the reliable FY-range fields (BOOKSFROM can come
    back anomalous on some data sets). Used by the push guard to refuse a voucher
    whose date falls outside the loaded company's financial year (e.g. a current
    voucher while last year's same-named company is open)."""
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>CompanyPeriod</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{_xml_escape(TALLY_COMPANY)}</SVCURRENTCOMPANY>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="CompanyPeriod" ISMODIFY="No">
                <TYPE>Company</TYPE>
                <FETCH>NAME, STARTINGFROM, ENDINGAT</FETCH>
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


def get_loaded_company_list() -> str:
    """List the companies currently LOADED/open in Tally (NAME + GUID).

    Deliberately omits SVCURRENTCOMPANY so it does NOT set/touch the
    current-company context. get_company_info()'s SVCURRENTCOMPANY directive,
    issued while Tally is still loading a company (the few-second window right
    after TallyPrime starts), can crash TallyPrime — so this safe, context-free
    company enumeration is used as a readiness gate BEFORE any SVCURRENTCOMPANY
    request. Mirrors the desktop status-bar probe (which runs this shape every
    10s without crashing). A Collection of Company returns the loaded companies,
    so an empty result means no company is loaded yet."""
    return _fetch("""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>TBLoadedCompanies</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVFROMDATE TYPE="Date">01-Jan-1970</SVFROMDATE>
            <SVTODATE TYPE="Date">01-Jan-1970</SVTODATE>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="TBLoadedCompanies" ISMODIFY="No">
                <TYPE>Company</TYPE>
                <FETCH>NAME, GUID</FETCH>
                <FILTERS>TBNonAggrFilter</FILTERS>
              </COLLECTION>
              <SYSTEM TYPE="FORMULAE" NAME="TBNonAggrFilter">$isaggregate = "No"</SYSTEM>
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

def _format_report_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%d-%b-%Y")
        except ValueError:
            return raw
    return raw


def get_vouchers(from_date: str, to_date: str) -> str:
    """Fetch Day Book using the report-style request shape."""
    report_from_date = _format_report_date(from_date)
    report_to_date = _format_report_date(to_date)
    return _fetch(f"""
    <ENVELOPE>
      <HEADER>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
      </HEADER>
      <BODY>
        <EXPORTDATA>
          <REQUESTDESC>
            <REPORTNAME>Day Book</REPORTNAME>
            <STATICVARIABLES>
              <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
              <SVFROMDATE TYPE="Date">{report_from_date}</SVFROMDATE>
              <SVTODATE TYPE="Date">{report_to_date}</SVTODATE>
            </STATICVARIABLES>
          </REQUESTDESC>
        </EXPORTDATA>
      </BODY>
    </ENVELOPE>""")


def get_vouchers_legacy_data_request(from_date: str, to_date: str) -> str:
    """Fetch Day Book using the older TYPE=Data/ID=Day Book envelope."""
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
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")


def get_vouchers_collection_tdl(from_date: str, to_date: str) -> str:
    """Fetch vouchers using a collection-style inline TDL request."""
    from_date_report = _format_report_date(from_date)
    to_date_report = _format_report_date(to_date)
    return _fetch(
        f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Vouchers</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE TYPE="Date">{from_date_report}</SVFROMDATE>
            <SVTODATE TYPE="Date">{to_date_report}</SVTODATE>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="Vouchers" ISMODIFY="No">
                <TYPE>Voucher</TYPE>
                <FETCH>GUID,ALTERID,MASTERID,DATE,VOUCHERTYPENAME,VOUCHERNUMBER,PARTYLEDGERNAME,NARRATION,AMOUNT,REFERENCE,PERSISTEDVIEW,ISINVOICE,ISCANCELLED,ISOPTIONAL,LEDGERENTRIES.LIST,ALLINVENTORYENTRIES.LIST,BILLALLOCATIONS.LIST</FETCH>
                <FILTERS>NotCancelledVouchers,NotOptionalVouchers,DateRangeFilter</FILTERS>
              </COLLECTION>
              <SYSTEM TYPE="FORMULAE" NAME="NotCancelledVouchers">NOT $IsCancelled</SYSTEM>
              <SYSTEM TYPE="FORMULAE" NAME="NotOptionalVouchers">NOT $IsOptional</SYSTEM>
              <SYSTEM TYPE="FORMULAE" NAME="DateRangeFilter">$Date &gt;= ##SVFromDate AND $Date &lt;= ##SVToDate</SYSTEM>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""",
        read_timeout=max(get_read_timeout_seconds(), 75),
        request_encoding="utf-8",
    )


def get_voucher_headers_erp9(from_date: str, to_date: str) -> str:
    """Fetch lightweight ERP 9 voucher headers only, with date filtering."""
    from_date_report = _format_report_date(from_date)
    to_date_report = _format_report_date(to_date)
    return _fetch(
        f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>VoucherHeadersERP9</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE TYPE="Date">{from_date_report}</SVFROMDATE>
            <SVTODATE TYPE="Date">{to_date_report}</SVTODATE>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="VoucherHeadersERP9" ISMODIFY="No">
                <TYPE>Voucher</TYPE>
                <FETCH>GUID,ALTERID,MASTERID,DATE,VOUCHERTYPENAME,VOUCHERNUMBER,PARTYLEDGERNAME,REFERENCE,AMOUNT,ISCANCELLED,ISOPTIONAL</FETCH>
                <FILTERS>NotCancelledVouchers,NotOptionalVouchers,DateRangeFilter</FILTERS>
              </COLLECTION>
              <SYSTEM TYPE="FORMULAE" NAME="NotCancelledVouchers">NOT $IsCancelled</SYSTEM>
              <SYSTEM TYPE="FORMULAE" NAME="NotOptionalVouchers">NOT $IsOptional</SYSTEM>
              <SYSTEM TYPE="FORMULAE" NAME="DateRangeFilter">$Date &gt;= ##SVFromDate AND $Date &lt;= ##SVToDate</SYSTEM>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""",
        read_timeout=max(get_read_timeout_seconds(), 45),
        request_encoding="utf-8",
    )


def get_voucher_details_erp9_batch(master_ids: list[int | str]) -> str:
    """Fetch full ERP 9 voucher details for a small batch of voucher master ids."""
    normalized_ids: list[str] = []
    for master_id in master_ids:
        text = str(master_id).strip()
        if not text or not text.isdigit():
            continue
        normalized_ids.append(text)

    if not normalized_ids:
        raise ValueError("ERP 9 voucher detail batch received no valid master ids.")

    formula = " OR ".join(f"$MasterID = {master_id}" for master_id in normalized_ids)
    return _fetch(
        f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>VoucherDetailBatchERP9</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          </STATICVARIABLES>
          <TDL>
            <TDLMESSAGE>
              <COLLECTION NAME="VoucherDetailBatchERP9" ISMODIFY="No">
                <TYPE>Voucher</TYPE>
                <FETCH>GUID,ALTERID,MASTERID,DATE,VOUCHERTYPENAME,VOUCHERNUMBER,PARTYLEDGERNAME,NARRATION,AMOUNT,REFERENCE,PERSISTEDVIEW,ISINVOICE,ISCANCELLED,ISOPTIONAL,LEDGERENTRIES.LIST,ALLINVENTORYENTRIES.LIST,BILLALLOCATIONS.LIST</FETCH>
                <FILTERS>MasterIdFilter</FILTERS>
              </COLLECTION>
              <SYSTEM TYPE="FORMULAE" NAME="MasterIdFilter">{formula}</SYSTEM>
            </TDLMESSAGE>
          </TDL>
        </DESC>
      </BODY>
    </ENVELOPE>""",
        read_timeout=max(get_read_timeout_seconds(), 45),
        request_encoding="utf-8",
    )


# ── Stock Items / Summary ────────────────────────────────────────

def get_stock_items() -> str:
    """Fetch stock details via a structured StockItem collection when available.

    NOTE: CLOSINGVALUE / CLOSINGRATE (valuation) are TEMPORARILY not fetched —
    computing per-item valuation is a major TallyPrime freeze/crash source, so it
    was stopped to test whether it is a freeze cause (mirrored in
    structured_sections.json and odbc_sections.json). It IS used downstream (stock
    value KPIs + the backend reorder classifier); re-add these fields to restore
    valuation if the freeze persists without them. The reorder report can instead
    recompute value as closing_qty x last-purchase-rate from vouchers. Kept:
    name/parent/units/closing-qty. See TB_SYNC_REPORTS / TB_SYNC_OUTSTANDING in
    sync_main for the related, reversible report/outstanding cuts."""
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
                <FETCH>NAME, PARENT, BASEUNITS, CLOSINGBALANCE, MAILINGNAME</FETCH>
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
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>""")
