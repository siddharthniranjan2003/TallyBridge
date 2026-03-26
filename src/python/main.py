import os
import sys
import json
from datetime import date

from tally_client import (
    get_ledgers, get_vouchers, get_stock_items,
    get_outstanding_receivables, get_outstanding_payables,
)
from xml_parser import (
    parse_ledgers, parse_vouchers, parse_stock, parse_outstanding,
)
from cloud_pusher import push

COMPANY = os.environ.get("TALLY_COMPANY", "")

def main():
    print(f"[TallyBridge] Starting sync: {COMPANY}")

    today = date.today()
    fy_year = today.year - 1 if today.month < 4 else today.year
    from_date = f"{fy_year}0401"
    to_date = today.strftime("%Y%m%d")

    try:
        print("[Tally] Fetching ledgers...")
        ledgers = parse_ledgers(get_ledgers())
        print(f"[Tally] Got {len(ledgers)} ledgers")

        print("[Tally] Fetching vouchers...")
        vouchers = parse_vouchers(get_vouchers(from_date, to_date))
        print(f"[Tally] Got {len(vouchers)} vouchers")

        print("[Tally] Fetching stock items...")
        stock = parse_stock(get_stock_items())
        print(f"[Tally] Got {len(stock)} stock items")

        print("[Tally] Fetching outstanding...")
        outstanding = (
            parse_outstanding(get_outstanding_receivables(), "receivable") +
            parse_outstanding(get_outstanding_payables(), "payable")
        )
        print(f"[Tally] Got {len(outstanding)} outstanding entries")

        print("[Cloud] Pushing to backend...")
        push({
            "company_name": COMPANY,
            "ledgers": ledgers,
            "vouchers": vouchers,
            "stock_items": stock,
            "outstanding": outstanding,
        })

        print(json.dumps({
            "status": "success",
            "records": {
                "ledgers": len(ledgers),
                "vouchers": len(vouchers),
                "stock": len(stock),
                "outstanding": len(outstanding),
            }
        }))
        sys.exit(0)

    except Exception as e:
        print(f"[Error] Sync failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()