"""LIVE test (real HTTP) for the Tally response-validation fix (cdfbcea / tally-dep-02).

Unlike test_response_validation.py (which mocks requests and calls _check_response
directly), this spins up the mock Tally on a real socket, points tally_client at
it, and does a real HTTP round-trip through _fetch (= _post + _check_response).
That exercises the actual network path a degraded TallyPrime would hit.

Run: py -3 tests/test_live_tally.py      (needs the real `requests` package)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_tally import start_mock  # noqa: E402
import tally_client  # noqa: E402

EXPORT = "<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER></ENVELOPE>"


def _run(mode: str):
    httpd, port = start_mock(mode)
    tally_client.TALLY_URL = f"http://127.0.0.1:{port}"
    try:
        try:
            body = tally_client._fetch(EXPORT)
            return "ok", (body or "")[:50]
        except RuntimeError as exc:
            return "rejected", str(exc).split(".")[0][:55]
    finally:
        httpd.shutdown()


def main():
    cases = [
        ("html", "rejected"),     # license/activation HTML page
        ("empty", "rejected"),    # empty body
        ("status0", "rejected"),  # <STATUS>0</STATUS> (no company)
        ("normal", "ok"),         # valid XML passes through
    ]
    all_ok = True
    for mode, expected in cases:
        outcome, detail = _run(mode)
        ok = outcome == expected
        all_ok = all_ok and ok
        print(f"  [{'ok ' if ok else 'FAIL'}] mode={mode:<8} expected={expected:<8} got={outcome:<8} :: {detail}")
    print("\nPASS: live Tally response-validation over real HTTP" if all_ok else "\nFAIL")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
