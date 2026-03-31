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

def safe_str(val) -> str:
    """Extract a string from a value that might be a dict or None."""
    if val is None:
        return ""
    if isinstance(val, dict):
        return str(val.get("#text", "")).strip()
    return str(val).strip()

def parse_tally_date(val: str):
    if not val:
        return None
    val = str(val).strip()
    if len(val) == 8 and val.isdigit():
        try:
            return date(int(val[:4]), int(val[4:6]), int(val[6:8])).isoformat()
        except:
            return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y"):
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

def _ensure_list(val) -> list:
    """Convert None to [], single dict to [dict], leave lists as-is."""
    if val is None:
        return []
    if isinstance(val, dict):
        return [val]
    return val


# ── company info ─────────────────────────────────────────────────

def parse_company_info(xml_text: str) -> dict:
    """Extract company metadata: FY dates, address, GSTIN, etc."""
    try:
        raw = xmltodict.parse(clean_xml(xml_text))
        body = raw.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {})
        collection = body.get("COLLECTION", {})
        company = collection.get("COMPANY", {})
        if isinstance(company, list):
            company = company[0] if company else {}

        books_from = safe_str(company.get("BOOKSFROM", ""))
        books_to = safe_str(company.get("BOOKSTO", ""))

        return {
            "name":        safe_str(company.get("NAME")),
            "books_from":  parse_tally_date(books_from),
            "books_to":    parse_tally_date(books_to),
            "books_from_raw": books_from,
            "books_to_raw":   books_to,
            "guid":        safe_str(company.get("GUID")),
            "master_id":   safe_int(company.get("MASTERID", 0)),
            "address":     safe_str(company.get("BASICCOMPANYADDRESS")),
            "state":       safe_str(company.get("STATENAME")),
            "country":     safe_str(company.get("COUNTRYNAME")),
            "pincode":     safe_str(company.get("PINCODE")),
            "email":       safe_str(company.get("EMAIL")),
            "phone":       safe_str(company.get("PHONENUMBER")),
            "gst_type":    safe_str(company.get("GSTREGISTRATIONTYPE")),
            "gstin":       safe_str(company.get("PARTYGSTIN")),
            "pan":         safe_str(company.get("INCOMETAXNUMBER")),
        }
    except Exception as e:
        print(f"[Parser] company_info error: {e}")
        return {}


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
            "vch_id":           str(safe_int(company.get("CMPVCHID",  0))),
            "alt_mst_id":       str(safe_int(company.get("ALTMSTID",  0))),
            "last_voucher_date": parse_tally_date(safe_str(company.get("LASTVOUCHERDATE", ""))),
        }
    except Exception as e:
        print(f"[Parser] alter_ids error: {e}")
        return {}


# ── groups ────────────────────────────────────────────────────────

def parse_groups(xml_text: str) -> list:
    """Parse accounting groups from Collection response."""
    try:
        raw = xmltodict.parse(clean_xml(xml_text))
        body = raw.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {})
        collection = body.get("COLLECTION", {})
        groups = _ensure_list(collection.get("GROUP", []))

        result = []
        for g in groups:
            if not g:
                continue
            name = safe_str(g.get("NAME") or g.get("@NAME", ""))
            if not name:
                continue
            result.append({
                "name":             name,
                "parent":           safe_str(g.get("PARENT", "")),
                "master_id":        safe_int(g.get("MASTERID", 0)),
                "is_revenue":       safe_str(g.get("ISREVENUE", "No")),
                "affects_stock":    safe_str(g.get("AFFECTSSTOCK", "No")),
                "is_subledger":     safe_str(g.get("ISSUBLEDGER", "No")),
            })
        return result
    except Exception as e:
        print(f"[Parser] groups error: {e}")
        return []


# ── ledgers ───────────────────────────────────────────────────────

def parse_ledgers(xml_text: str) -> list:
    """Parse ledgers from Collection+FETCH response (with extended fields)."""
    try:
        raw = xmltodict.parse(clean_xml(xml_text))

        # Collection response format: ENVELOPE > BODY > DATA > COLLECTION > LEDGER
        body = raw.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {})
        collection = body.get("COLLECTION", {})
        ledgers = _ensure_list(collection.get("LEDGER", []))

        # If Collection format didn't work, fall back to TALLYMESSAGE format
        if not ledgers:
            messages = get_messages(raw)
            for msg in messages:
                if not msg:
                    continue
                ledger = msg.get("LEDGER")
                if ledger:
                    ledgers.append(ledger)

        result = []
        for ledger in ledgers:
            if not ledger:
                continue
            name = safe_str(ledger.get("NAME") or ledger.get("@NAME", ""))
            if not name or name == "?":
                continue
            result.append({
                "name":            name,
                "group_name":      safe_str(ledger.get("PARENT", "")),
                "opening_balance": safe_float(ledger.get("OPENINGBALANCE", 0)),
                "closing_balance": safe_float(ledger.get("CLOSINGBALANCE", 0)),
                "master_id":       safe_int(ledger.get("MASTERID", 0)),
                "email":           safe_str(ledger.get("EMAIL", "")),
                "phone":           safe_str(ledger.get("LEDGERPHONE", "")),
                "mobile":          safe_str(ledger.get("LEDGERMOBILE", "")),
                "pincode":         safe_str(ledger.get("PINCODE", "")),
                "gstin":           safe_str(ledger.get("PARTYGSTIN", "")),
                "state":           safe_str(ledger.get("LEDSTATENAME", "")),
                "country":         safe_str(ledger.get("COUNTRYNAME", "")),
                "credit_period":   safe_str(ledger.get("CREDITPERIOD", "")),
                "credit_limit":    safe_float(ledger.get("CREDITLIMIT", 0)),
                "bank_account":    safe_str(ledger.get("BANKACCOUNT", "")),
                "ifsc_code":       safe_str(ledger.get("IFSCODE", "")),
                "pan":             safe_str(ledger.get("INCOMETAXNUMBER", "")),
                "mailing_name":    safe_str(ledger.get("MAILINGNAME", "")),
                "guid":            safe_str(ledger.get("GUID", "")),
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
            reference = safe_str(v.get("REFERENCE", ""))
            persisted_view = safe_str(v.get("PERSISTEDVIEW", ""))
            is_invoice = safe_str(v.get("ISINVOICE", "No"))

            # ── Inventory items ──────────────────────────────
            raw_inv = v.get("ALLINVENTORYENTRIES.LIST") or \
                      v.get("INVENTORYENTRIES.LIST") or []
            raw_inv = _ensure_list(raw_inv)

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

            # ── Ledger entries (accounting allocations) ──────
            raw_ledger_entries = v.get("ALLLEDGERENTRIES.LIST") or \
                                 v.get("LEDGERENTRIES.LIST") or []
            raw_ledger_entries = _ensure_list(raw_ledger_entries)

            ledger_entries = []
            for le in raw_ledger_entries:
                if not le:
                    continue
                le_name = safe_str(le.get("LEDGERNAME", ""))
                if not le_name:
                    continue
                le_amount = safe_float(le.get("AMOUNT", 0))
                is_party = safe_str(le.get("ISPARTYLEDGER", "No"))
                is_deemed = safe_str(le.get("ISDEEMEDPOSITIVE", "No"))

                # Bill allocations
                raw_bills = _ensure_list(le.get("BILLALLOCATIONS.LIST", []))
                bill_allocs = []
                for bill in raw_bills:
                    if not bill:
                        continue
                    bill_allocs.append({
                        "name":      safe_str(bill.get("NAME", "")),
                        "bill_type": safe_str(bill.get("BILLTYPE", "")),
                        "amount":    safe_float(bill.get("AMOUNT", 0)),
                    })

                ledger_entries.append({
                    "ledger_name":        le_name,
                    "amount":             le_amount,
                    "is_party_ledger":    is_party == "Yes",
                    "is_deemed_positive": is_deemed == "Yes",
                    "bill_allocations":   bill_allocs,
                })

            # If voucher-level amount is 0, derive it from items or ledger entries
            if amount == 0:
                if items:
                    amount = sum(abs(i["amount"]) for i in items)
                elif ledger_entries:
                    # Take the party ledger amount, or the largest absolute amount
                    party_amounts = [abs(le["amount"]) for le in ledger_entries if le["is_party_ledger"]]
                    if party_amounts:
                        amount = max(party_amounts)
                    else:
                        amount = max(abs(le["amount"]) for le in ledger_entries) if ledger_entries else 0

            result.append({
                "tally_guid":      guid,
                "voucher_number":  vnum,
                "voucher_type":    vtype,
                "date":            vdate,
                "party_name":      party,
                "amount":          abs(amount),
                "narration":       v.get("NARRATION", ""),
                "is_cancelled":    v.get("ISCANCELLED", "No") == "Yes",
                "reference":       reference,
                "is_invoice":      is_invoice == "Yes",
                "view":            persisted_view,
                "items":           items,
                "ledger_entries":  ledger_entries,
            })
        return result
    except Exception as e:
        print(f"[Parser] voucher error: {e}")
        return []

# ── stock summary ─────────────────────────────────────────────────

def parse_stock(xml_text: str) -> list:
    try:
        cleaned = clean_xml(xml_text)
        try:
            raw = xmltodict.parse(cleaned)
            body = raw.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {})
            collection = body.get("COLLECTION", {})
            stock_items = _ensure_list(collection.get("STOCKITEM", []))

            result = []
            for item in stock_items:
                if not item:
                    continue

                name = safe_str(item.get("NAME") or item.get("@NAME", ""))
                if not name:
                    continue

                qty_val = (
                    item.get("STKCLBALANCE")
                    or item.get("STKCLOSINGBALANCE")
                    or item.get("CLOSINGBALANCE")
                    or item.get("CLOSINGQTY")
                )
                value_val = (
                    item.get("CLOSINGVALUE")
                    or item.get("STKCLOSINGVALUE")
                    or item.get("CLOSINGAMOUNT")
                )
                rate_val = item.get("CLOSINGRATE") or item.get("RATE")

                result.append({
                    "name":          name,
                    "group_name":    safe_str(item.get("PARENT", "")),
                    "unit":          safe_str(item.get("BASEUNITS", "")) or "Nos",
                    "closing_qty":   abs(safe_float(qty_val)),
                    "closing_value": abs(safe_float(value_val)),
                    "rate":          safe_float(rate_val),
                })

            if result:
                return result
        except Exception:
            pass

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
# Output keys match Supabase profit_loss table: particulars, amount, is_debit

def parse_profit_and_loss(xml_text: str) -> list:
    """
    P&L display format. Uses BSMAINAMT / PLSUBAMT tags.
    Output keys: particulars, amount, is_debit — matches DB schema.
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
                "particulars": name,
                "amount":      abs(raw),
                "is_debit":    raw >= 0,   # positive = debit in Tally convention
            })
        return result
    except Exception as e:
        print(f"[Parser] P&L error: {e}")
        return []

# ── balance sheet ─────────────────────────────────────────────────
# Output keys match Supabase balance_sheet table: particulars, amount, side

def parse_balance_sheet(xml_text: str) -> list:
    """
    Output keys: particulars, amount, side ('asset'/'liability') — matches DB schema.
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
                    "particulars": name,
                    "amount":      abs(raw),
                    "side":        "liability" if raw > 0 else "asset",
                })
            elif dr_amts and i < len(dr_amts):
                dr = abs(safe_float(dr_amts[i]))
                cr = abs(safe_float(cr_amts[i])) if i < len(cr_amts) else 0.0
                result.append({
                    "particulars": name,
                    "amount":      dr if dr else cr,
                    "side":        "asset" if dr else "liability",
                })
        return result
    except Exception as e:
        print(f"[Parser] balance sheet error: {e}")
        return []

# ── trial balance ─────────────────────────────────────────────────
# Output keys match Supabase trial_balance table: particulars, debit, credit

def parse_trial_balance(xml_text: str) -> list:
    """
    Output keys: particulars, debit, credit — matches DB schema.
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
                "particulars":   name,
                "debit":         abs(safe_float(dr_amts[i])) if i < len(dr_amts) else 0.0,
                "credit":        abs(safe_float(cr_amts[i])) if i < len(cr_amts) else 0.0,
            })
        return result
    except Exception as e:
        print(f"[Parser] trial balance error: {e}")
        return []
