import xmltodict
from datetime import date

def safe_float(val) -> float:
    try:
        s = str(val).replace(",", "").strip()
        # Tally uses negative sign at end sometimes: "1000 Dr" / "1000 Cr"
        if s.endswith("Dr"):
            return float(s.replace("Dr", "").strip())
        if s.endswith("Cr"):
            return -float(s.replace("Cr", "").strip())
        return float(s)
    except:
        return 0.0

def parse_date(val: str):
    try:
        return date(int(val[:4]), int(val[4:6]), int(val[6:8])).isoformat()
    except:
        return None

def ensure_list(val):
    if val is None:
        return []
    return val if isinstance(val, list) else [val]

def parse_ledgers(xml_text: str) -> list:
    try:
        raw = xmltodict.parse(xml_text)
        messages = ensure_list(
            raw.get("ENVELOPE", {}).get("BODY", {})
               .get("DATA", {}).get("TALLYMESSAGE")
        )
        result = []
        for msg in messages:
            if not msg:
                continue
            ledger = msg.get("LEDGER", {})
            if not ledger or not ledger.get("NAME"):
                continue
            result.append({
                "name": ledger.get("NAME", ""),
                "group_name": ledger.get("PARENT", ""),
                "opening_balance": safe_float(ledger.get("OPENINGBALANCE", 0)),
                "closing_balance": safe_float(ledger.get("CLOSINGBALANCE", 0)),
            })
        return result
    except Exception as e:
        print(f"[Parser] ledger error: {e}")
        return []

def parse_vouchers(xml_text: str) -> list:
    try:
        raw = xmltodict.parse(xml_text)
        messages = ensure_list(
            raw.get("ENVELOPE", {}).get("BODY", {})
               .get("DATA", {}).get("TALLYMESSAGE")
        )
        result = []
        for msg in messages:
            if not msg:
                continue
            v = msg.get("VOUCHER", {})
            if not v:
                continue
            inv_entries = ensure_list(v.get("ALLINVENTORYENTRIES.LIST"))
            items = []
            for inv in inv_entries:
                if not inv:
                    continue
                items.append({
                    "stock_item_name": inv.get("STOCKITEMNAME", ""),
                    "quantity": safe_float(inv.get("ACTUALQTY", 0)),
                    "unit": inv.get("UNIT", "NOS"),
                    "rate": safe_float(inv.get("RATE", 0)),
                    "discount_pct": safe_float(inv.get("DISCOUNT", 0)),
                    "amount": safe_float(inv.get("AMOUNT", 0)),
                })
            result.append({
                "tally_guid": v.get("GUID", ""),
                "voucher_number": v.get("VOUCHERNUMBER", ""),
                "voucher_type": v.get("VOUCHERTYPENAME", ""),
                "date": parse_date(v.get("DATE", "")),
                "party_name": v.get("PARTYLEDGERNAME", ""),
                "amount": safe_float(v.get("AMOUNT", 0)),
                "narration": v.get("NARRATION", ""),
                "is_cancelled": v.get("ISCANCELLED", "No") == "Yes",
                "items": items,
            })
        return result
    except Exception as e:
        print(f"[Parser] voucher error: {e}")
        return []

def parse_stock(xml_text: str) -> list:
    try:
        raw = xmltodict.parse(xml_text)
        messages = ensure_list(
            raw.get("ENVELOPE", {}).get("BODY", {})
               .get("DATA", {}).get("TALLYMESSAGE")
        )
        result = []
        for msg in messages:
            if not msg:
                continue
            s = msg.get("STOCKITEM", {})
            if not s or not s.get("NAME"):
                continue
            result.append({
                "name": s.get("NAME", ""),
                "group_name": s.get("PARENT", ""),
                "unit": s.get("BASEUNITS", "NOS"),
                "closing_qty": safe_float(s.get("CLOSINGBALANCE", 0)),
                "closing_value": safe_float(s.get("CLOSINGVALUE", 0)),
            })
        return result
    except Exception as e:
        print(f"[Parser] stock error: {e}")
        return []

def parse_outstanding(xml_text: str, type_: str) -> list:
    try:
        raw = xmltodict.parse(xml_text)
        messages = ensure_list(
            raw.get("ENVELOPE", {}).get("BODY", {})
               .get("DATA", {}).get("TALLYMESSAGE")
        )
        result = []
        today = date.today()
        for msg in messages:
            if not msg:
                continue
            b = msg.get("BILL", {})
            if not b:
                continue
            due_str = parse_date(b.get("BILLDATE", ""))
            days_overdue = 0
            if due_str:
                try:
                    delta = (today - date.fromisoformat(due_str)).days
                    days_overdue = max(0, delta)
                except:
                    pass
            result.append({
                "party_name": b.get("PARTYNAME", ""),
                "type": type_,
                "voucher_number": b.get("NAME", ""),
                "voucher_date": parse_date(b.get("DATE", "")),
                "due_date": due_str,
                "original_amount": safe_float(b.get("AMOUNT", 0)),
                "pending_amount": safe_float(b.get("PENDINGAMOUNT", 0)),
                "days_overdue": days_overdue,
            })
        return result
    except Exception as e:
        print(f"[Parser] outstanding error: {e}")
        return []