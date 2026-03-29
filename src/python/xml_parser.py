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
    """Handles strings, numbers, AND dicts (xmltodict wraps tagged values as dicts)"""
    if not val:
        return 0.0
    # xmltodict returns dicts for tags with attributes e.g. {"#text":"5000","@TYPE":"Amount"}
    if isinstance(val, dict):
        val = val.get("#text") or val.get("@amount") or ""
    if isinstance(val, (int, float)):
        return float(val)
    try:
        s = str(val).strip()
        s = s.split("/")[0].strip()   # "500.00/Nos" → "500.00"
        s = s.split()[0].strip()      # " 10 Nos" → "10"
        s = s.replace(",", "")
        return float(s)
    except:
        return 0.0

def safe_int(val) -> int:
    """Same as safe_float but returns int — safe for ALTERID etc."""
    if isinstance(val, dict):
        val = val.get("#text") or val.get("@amount") or "0"
    try:
        return int(str(val).strip().split()[0])
    except:
        return 0

def parse_tally_date(val: str):
    if not val:
        return None
    val = str(val).strip()
    if len(val) == 8 and val.isdigit():
        try:
            return date(int(val[:4]), int(val[4:6]), int(val[6:8])).isoformat()
        except:
            return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except:
            continue
    return None

def get_messages(raw: dict) -> list:
    body = raw.get("ENVELOPE", {}).get("BODY", {})
    p = body.get("IMPORTDATA", {}).get("REQUESTDATA", {}).get("TALLYMESSAGE")
    if p:
        return p if isinstance(p, list) else [p]
    p = body.get("DATA", {}).get("TALLYMESSAGE")
    if p:
        return p if isinstance(p, list) else [p]
    return []

# ── change detection ─────────────────────────────────────────────

def parse_alter_ids(xml_text: str) -> dict:
    """Parse ALTERID/ALTVCHID/ALTMSTID using safe_int to handle dict values."""
    try:
        raw = xmltodict.parse(clean_xml(xml_text))
        body = raw.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {})
        collection = body.get("COLLECTION", {})
        company = collection.get("COMPANY", {})
        if isinstance(company, list):
            company = company[0] if company else {}

        return {
            "alter_id":         str(safe_int(company.get("ALTERID",   0))),
            "alt_vch_id":       str(safe_int(company.get("ALTVCHID",  0))),
            "alt_mst_id":       str(safe_int(company.get("ALTMSTID",  0))),
            "last_voucher_date": parse_tally_date(
                company.get("LASTVOUCHERDATE", "") if isinstance(
                    company.get("LASTVOUCHERDATE"), str) else ""
            ),
        }
    except Exception as e:
        print(f"[Parser] alter_ids error: {e}")
        return {}

# ── ledgers ───────────────────────────────────────────────────────

def parse_ledgers(xml_text: str) -> list:
    try:
        raw = xmltodict.parse(clean_xml(xml_text))
        messages = get_messages(raw)
        result = []
        for msg in messages:
            if not msg:
                continue
            ledger = msg.get("LEDGER")
            if not ledger:
                continue
            name = ledger.get("@NAME", "").strip()
            if not name or name == "?":
                continue
            result.append({
                "name":            name,
                "group_name":      ledger.get("PARENT", ""),
                "opening_balance": safe_float(ledger.get("OPENINGBALANCE", 0)),
                "closing_balance": safe_float(ledger.get("CLOSINGBALANCE", 0)),
            })
        return result
    except Exception as e:
        print(f"[Parser] ledger error: {e}")
        return []

# ── vouchers ──────────────────────────────────────────────────────

def parse_vouchers(xml_text: str) -> list:
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

            vtype  = v.get("@VCHTYPE") or v.get("VOUCHERTYPENAME", "")
            guid   = v.get("@REMOTEID") or v.get("GUID", "")
            vnum   = v.get("VOUCHERNUMBER", "")
            vdate  = parse_tally_date(v.get("DATE", ""))
            party  = v.get("PARTYLEDGERNAME", "")
            amount = safe_float(v.get("AMOUNT", 0))

            raw_inv = v.get("ALLINVENTORYENTRIES.LIST") or \
                      v.get("INVENTORYENTRIES.LIST") or []
            if isinstance(raw_inv, dict):
                raw_inv = [raw_inv]

            items = []
            for inv in raw_inv:
                if not inv:
                    continue
                qty_str   = str(inv.get("ACTUALQTY", "0")).strip()
                qty_parts = qty_str.split()
                items.append({
                    "stock_item_name": inv.get("STOCKITEMNAME", ""),
                    "quantity":        safe_float(qty_parts[0]) if qty_parts else 0.0,
                    "unit":            qty_parts[1] if len(qty_parts) > 1 else "Nos",
                    "rate":            safe_float(inv.get("RATE", 0)),
                    "discount_pct":    safe_float(inv.get("DISCOUNT", 0)),
                    "amount":          safe_float(inv.get("AMOUNT", 0)),
                })

            # If voucher-level amount is 0, derive it from items
            if amount == 0 and items:
                amount = sum(abs(i["amount"]) for i in items)

            result.append({
                "tally_guid":    guid,
                "voucher_number": vnum,
                "voucher_type":  vtype,
                "date":          vdate,
                "party_name":    party,
                "amount":        abs(amount),
                "narration":     v.get("NARRATION", ""),
                "is_cancelled":  v.get("ISCANCELLED", "No") == "Yes",
                "items":         items,
            })
        return result
    except Exception as e:
        print(f"[Parser] voucher error: {e}")
        return []

# ── stock summary ─────────────────────────────────────────────────

def parse_stock(xml_text: str) -> list:
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
            qty_str   = qtys[i].strip() if i < len(qtys) else ""
            qty_parts = qty_str.split()
            result.append({
                "name":          name,
                "group_name":    "",
                "unit":          qty_parts[1] if len(qty_parts) > 1 else "Nos",
                "closing_qty":   abs(safe_float(qty_parts[0])) if qty_parts else 0.0,
                "closing_value": abs(safe_float(values[i])) if i < len(values) else 0.0,
                "rate":          safe_float(rates[i]) if i < len(rates) else 0.0,
            })
        return result
    except Exception as e:
        print(f"[Parser] stock error: {e}")
        return []

# ── outstanding ───────────────────────────────────────────────────

def parse_outstanding(xml_text: str, type_: str) -> list:
    try:
        cleaned = clean_xml(xml_text)
        bill_blocks = re.findall(
            r'<BILLFIXED>(.*?)</BILLFIXED>\s*'
            r'<BILLCL>([^<]*)</BILLCL>\s*'
            r'<BILLDUE>([^<]*)</BILLDUE>\s*'
            r'<BILLOVERDUE>([^<]*)</BILLOVERDUE>',
            cleaned, re.DOTALL
        )
        result = []
        for block, cl, due_str, overdue_str in bill_blocks:
            date_m  = re.search(r'<BILLDATE>([^<]+)</BILLDATE>', block)
            ref_m   = re.search(r'<BILLREF>([^<]+)</BILLREF>', block)
            party_m = re.search(r'<BILLPARTY>([^<]+)</BILLPARTY>', block)
            party   = party_m.group(1).strip() if party_m else ""
            if not party:
                continue
            result.append({
                "party_name":      party,
                "type":            type_,
                "voucher_number":  ref_m.group(1).strip() if ref_m else "",
                "voucher_date":    parse_tally_date(date_m.group(1).strip()) if date_m else None,
                "due_date":        parse_tally_date(due_str.strip()),
                "original_amount": abs(safe_float(cl)),
                "pending_amount":  abs(safe_float(cl)),
                "days_overdue":    int(safe_float(overdue_str)) if overdue_str.strip() else 0,
            })
        return result
    except Exception as e:
        print(f"[Parser] outstanding error: {e}")
        return []

# ── profit & loss ─────────────────────────────────────────────────

def parse_profit_and_loss(xml_text: str) -> list:
    """
    P&L display format. Tag names confirmed from 09_profit_and_loss.xml.
    Uses BSMAINAMT (shared with Balance Sheet) — sign determines side.
    account_name/amount/side matches Supabase profit_loss table schema.
    """
    try:
        cleaned = clean_xml(xml_text)
        names    = re.findall(r'<DSPDISPNAME>([^<]+)</DSPDISPNAME>', cleaned)
        # Try PLSUBAMT first, fall back to BSMAINAMT
        amounts  = re.findall(r'<PLSUBAMT>([^<]*)</PLSUBAMT>', cleaned)
        if not amounts:
            amounts = re.findall(r'<BSMAINAMT>([^<]*)</BSMAINAMT>', cleaned)

        result = []
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue
            raw = safe_float(amounts[i]) if i < len(amounts) else 0.0
            result.append({
                "account_name": name,
                "amount":       abs(raw),
                "side":         "credit" if raw < 0 else "debit",
                "level":        0,
            })
        return result
    except Exception as e:
        print(f"[Parser] P&L error: {e}")
        return []

# ── balance sheet ─────────────────────────────────────────────────

def parse_balance_sheet(xml_text: str) -> list:
    """
    account_name/amount/side matches Supabase balance_sheet table schema.
    """
    try:
        cleaned  = clean_xml(xml_text)
        names    = re.findall(r'<DSPDISPNAME>([^<]+)</DSPDISPNAME>', cleaned)
        bs_main  = re.findall(r'<BSMAINAMT>([^<]*)</BSMAINAMT>', cleaned)
        bs_sub   = re.findall(r'<BSSUBAMT>([^<]*)</BSSUBAMT>', cleaned)
        dr_amts  = re.findall(r'<DSPCLDRAMTA>([^<]*)</DSPCLDRAMTA>', cleaned)
        cr_amts  = re.findall(r'<DSPCLCRAMTA>([^<]*)</DSPCLCRAMTA>', cleaned)

        result = []
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue

            if bs_main and i < len(bs_main):
                raw = safe_float(bs_main[i]) if bs_main[i].strip() else \
                      safe_float(bs_sub[i]) if i < len(bs_sub) else 0.0
                result.append({
                    "account_name": name,
                    "amount":       abs(raw),
                    "side":         "liabilities" if raw > 0 else "assets",
                    "level":        0,
                })
            elif dr_amts and i < len(dr_amts):
                dr = abs(safe_float(dr_amts[i]))
                cr = abs(safe_float(cr_amts[i])) if i < len(cr_amts) else 0.0
                result.append({
                    "account_name": name,
                    "amount":       dr if dr else cr,
                    "side":         "assets" if dr else "liabilities",
                    "level":        0,
                })
        return result
    except Exception as e:
        print(f"[Parser] balance sheet error: {e}")
        return []

# ── trial balance ─────────────────────────────────────────────────

def parse_trial_balance(xml_text: str) -> list:
    """
    account_name/debit_amount/credit_amount matches Supabase trial_balance schema.
    """
    try:
        cleaned = clean_xml(xml_text)
        names   = re.findall(r'<DSPDISPNAME>([^<]+)</DSPDISPNAME>', cleaned)
        dr_amts = re.findall(r'<DSPCLDRAMTA>([^<]*)</DSPCLDRAMTA>', cleaned)
        cr_amts = re.findall(r'<DSPCLCRAMTA>([^<]*)</DSPCLCRAMTA>', cleaned)

        result = []
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue
            result.append({
                "account_name":  name,
                "debit_amount":  abs(safe_float(dr_amts[i])) if i < len(dr_amts) else 0.0,
                "credit_amount": abs(safe_float(cr_amts[i])) if i < len(cr_amts) else 0.0,
                "level":         0,
            })
        return result
    except Exception as e:
        print(f"[Parser] trial balance error: {e}")
        return []