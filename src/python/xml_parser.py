import re
import xmltodict
from datetime import date, datetime

# ── helpers ──────────────────────────────────────────────────────

def clean_xml(text: str) -> str:
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'&#(\d+);', lambda m: ''
                  if int(m.group(1)) < 32 and int(m.group(1)) not in (9,10,13)
                  else m.group(0), text)
    return text

def safe_float(val) -> float:
    if not val:
        return 0.0
    try:
        s = str(val).strip()
        # "500.00/Nos" → "500.00"
        s = s.split("/")[0].strip()
        # " 10 Nos" → "10"
        s = s.split()[0].strip()
        s = s.replace(",", "")
        return float(s)
    except:
        return 0.0

def parse_tally_date(val: str):
    """Handle both YYYYMMDD and '1-Apr-25' formats"""
    if not val:
        return None
    val = str(val).strip()
    # Format: 20250401
    if len(val) == 8 and val.isdigit():
        try:
            return date(int(val[:4]), int(val[4:6]), int(val[6:8])).isoformat()
        except:
            return None
    # Format: 1-Apr-25 or 01-Apr-2025
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except:
            continue
    return None

def get_messages(raw: dict) -> list:
    """Get TALLYMESSAGE list from any known Tally response path"""
    body = raw.get("ENVELOPE", {}).get("BODY", {})
    # TallyPrime returns: IMPORTDATA > REQUESTDATA > TALLYMESSAGE
    p = body.get("IMPORTDATA", {}).get("REQUESTDATA", {}).get("TALLYMESSAGE")
    if p:
        return p if isinstance(p, list) else [p]
    # Fallback: DATA > TALLYMESSAGE
    p = body.get("DATA", {}).get("TALLYMESSAGE")
    if p:
        return p if isinstance(p, list) else [p]
    return []

# ── ledgers ───────────────────────────────────────────────────────

def parse_ledgers(xml_text: str) -> list:
    """
    Each TALLYMESSAGE has one child. We only want LEDGER children.
    Ledger name is in the @NAME attribute (XML attribute → xmltodict prefix @).
    Structure: <LEDGER NAME="Cash" RESERVEDNAME="">
                 <PARENT>Cash-in-Hand</PARENT>
                 <OPENINGBALANCE/>
               </LEDGER>
    """
    try:
        raw = xmltodict.parse(clean_xml(xml_text))
        messages = get_messages(raw)
        result = []
        for msg in messages:
            if not msg:
                continue
            ledger = msg.get("LEDGER")
            if not ledger:
                continue  # skip GROUP, CURRENCY, STOCKITEM messages
            name = ledger.get("@NAME", "").strip()
            if not name or name == "?":
                continue
           result.append({
            "name": name,
            "group_name": ledger.get("PARENT", ""),
            "opening_balance": safe_float(ledger.get("OPENINGBALANCE", 0)),
            "closing_balance": safe_float(ledger.get("CLOSINGBALANCE", 0)),  # ← fixed
})
        return result
    except Exception as e:
        print(f"[Parser] ledger error: {e}")
        return []

# ── vouchers ──────────────────────────────────────────────────────

def parse_vouchers(xml_text: str) -> list:
    """
    Each TALLYMESSAGE with a VOUCHER child.
    Voucher type is @VCHTYPE attribute.
    Rate format: "500.00/Nos" → split on "/" → take first part.
    Qty format:  " 10 Nos"   → split on space → take first part.
    Structure:
      <VOUCHER REMOTEID="..." VCHTYPE="Sales" ...>
        <DATE>20250401</DATE>
        <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
        <PARTYLEDGERNAME>Rajesh Traders</PARTYLEDGERNAME>
        <VOUCHERNUMBER>1</VOUCHERNUMBER>
        <ALLINVENTORYENTRIES.LIST>
          <STOCKITEMNAME>Widget A</STOCKITEMNAME>
          <ACTUALQTY> 10 Nos</ACTUALQTY>
          <RATE>500.00/Nos</RATE>
          <AMOUNT>5000.00</AMOUNT>
        </ALLINVENTORYENTRIES.LIST>
      </VOUCHER>
    """
    try:
        raw = xmltodict.parse(clean_xml(xml_text))
        messages = get_messages(raw)
        result = []
        for msg in messages:
            if not msg:
                continue
            v = msg.get("VOUCHER")
            if not v:
                continue

            # Attributes use @ prefix in xmltodict
            vtype  = v.get("@VCHTYPE") or v.get("VOUCHERTYPENAME", "")
            guid   = v.get("@REMOTEID") or v.get("GUID", "")
            vnum   = v.get("VOUCHERNUMBER", "")
            vdate  = parse_tally_date(v.get("DATE", ""))
            party  = v.get("PARTYLEDGERNAME", "")
            amount = safe_float(v.get("AMOUNT", 0))

            # Inventory entries — can be list or single dict
            raw_inv = v.get("ALLINVENTORYENTRIES.LIST") or \
                      v.get("INVENTORYENTRIES.LIST") or []
            if isinstance(raw_inv, dict):
                raw_inv = [raw_inv]

            items = []
            for inv in raw_inv:
                if not inv:
                    continue
                qty_str = str(inv.get("ACTUALQTY", "0")).strip()
                qty_parts = qty_str.split()
                items.append({
                    "stock_item_name": inv.get("STOCKITEMNAME", ""),
                    "quantity": safe_float(qty_parts[0]) if qty_parts else 0.0,
                    "unit": qty_parts[1] if len(qty_parts) > 1 else "Nos",
                    "rate": safe_float(inv.get("RATE", 0)),
                    "discount_pct": safe_float(inv.get("DISCOUNT", 0)),
                    "amount": safe_float(inv.get("AMOUNT", 0)),
                })

            result.append({
                "tally_guid": guid,
                "voucher_number": vnum,
                "voucher_type": vtype,
                "date": vdate,
                "party_name": party,
                "amount": amount,
                "narration": v.get("NARRATION", ""),
                "is_cancelled": v.get("ISCANCELLED", "No") == "Yes",
                "items": items,
            })
        return result
    except Exception as e:
        print(f"[Parser] voucher error: {e}")
        return []

# ── stock summary ─────────────────────────────────────────────────

def parse_stock(xml_text: str) -> list:
    """
    Stock Summary returns a DISPLAY format — not TALLYMESSAGE based.
    Structure directly under ENVELOPE:
      <DSPACCNAME><DSPDISPNAME>Widget A</DSPDISPNAME></DSPACCNAME>
      <DSPSTKINFO>
        <DSPSTKCL>
          <DSPCLQTY>40 Nos</DSPCLQTY>
          <DSPCLRATE>400.00</DSPCLRATE>
          <DSPCLAMTA>-16000.00</DSPCLAMTA>
        </DSPSTKCL>
      </DSPSTKINFO>
    Use regex on raw text — xmltodict can't handle sibling-pair pattern.
    """
    try:
        cleaned = clean_xml(xml_text)
        names  = re.findall(r'<DSPDISPNAME>([^<]+)</DSPDISPNAME>', cleaned)
        qtys   = re.findall(r'<DSPCLQTY>([^<]*)</DSPCLQTY>', cleaned)
        rates  = re.findall(r'<DSPCLRATE>([^<]*)</DSPCLRATE>', cleaned)
        values = re.findall(r'<DSPCLAMTA>([^<]*)</DSPCLAMTA>', cleaned)

        result = []
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue

            qty_str = qtys[i].strip() if i < len(qtys) else ""
            qty_parts = qty_str.split()
            qty  = safe_float(qty_parts[0]) if qty_parts else 0.0
            unit = qty_parts[1] if len(qty_parts) > 1 else "Nos"
            rate = safe_float(rates[i]) if i < len(rates) else 0.0
            val  = abs(safe_float(values[i])) if i < len(values) else 0.0

            result.append({
                "name": name,
                "group_name": "",
                "unit": unit,
                "closing_qty": abs(qty),
                "closing_value": val,
                "rate": rate,
            })
        return result
    except Exception as e:
        print(f"[Parser] stock error: {e}")
        return []

# ── outstanding ───────────────────────────────────────────────────

def parse_outstanding(xml_text: str, type_: str) -> list:
    """
    Outstanding returns DISPLAY format — sibling groups under ENVELOPE.
    Structure:
      <BILLFIXED>
        <BILLDATE>1-Apr-25</BILLDATE>
        <BILLREF>1</BILLREF>
        <BILLPARTY>Rajesh Traders</BILLPARTY>
      </BILLFIXED>
      <BILLCL>-600.00</BILLCL>
      <BILLDUE>1-Apr-25</BILLDUE>
      <BILLOVERDUE>0</BILLOVERDUE>
    Use regex to extract each group.
    """
    try:
        cleaned = clean_xml(xml_text)
        today   = date.today()

        # Extract each BILLFIXED block + the values that follow it
        bill_blocks = re.findall(
            r'<BILLFIXED>(.*?)</BILLFIXED>\s*'
            r'<BILLCL>([^<]*)</BILLCL>\s*'
            r'<BILLDUE>([^<]*)</BILLDUE>\s*'
            r'<BILLOVERDUE>([^<]*)</BILLOVERDUE>',
            cleaned, re.DOTALL
        )

        result = []
        for block, cl, due_str, overdue_str in bill_blocks:
            date_match  = re.search(r'<BILLDATE>([^<]+)</BILLDATE>', block)
            ref_match   = re.search(r'<BILLREF>([^<]+)</BILLREF>', block)
            party_match = re.search(r'<BILLPARTY>([^<]+)</BILLPARTY>', block)

            bill_date  = parse_tally_date(date_match.group(1).strip()) if date_match else None
            ref        = ref_match.group(1).strip() if ref_match else ""
            party      = party_match.group(1).strip() if party_match else ""
            due_date   = parse_tally_date(due_str.strip())
            amount     = abs(safe_float(cl))
            days_over  = int(safe_float(overdue_str)) if overdue_str.strip() else 0

            if not party:
                continue

            result.append({
                "party_name": party,
                "type": type_,
                "voucher_number": ref,
                "voucher_date": bill_date,
                "due_date": due_date,
                "original_amount": amount,
                "pending_amount": amount,
                "days_overdue": days_over,
            })
        return result
    except Exception as e:
        print(f"[Parser] outstanding error: {e}")
        return []