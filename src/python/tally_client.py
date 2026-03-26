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