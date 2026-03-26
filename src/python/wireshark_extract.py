"""
Extracts HTTP POST bodies from a Wireshark .pcapng file.
No pyshark needed — uses raw byte parsing.
Usage: python wireshark_extract.py wireshark1.pcapng
"""
import sys
import os
import re

pcap_file = sys.argv[1] if len(sys.argv) > 1 else "wireshark1.pcapng"
out_dir = "wireshark_requests"
os.makedirs(out_dir, exist_ok=True)

with open(pcap_file, "rb") as f:
    raw = f.read()

# Find all HTTP POST blocks by looking for the POST header bytes
# Then extract the XML body after the HTTP headers
chunks = raw.split(b"POST / HTTP/1.1")

print(f"Found {len(chunks)-1} POST requests\n")

count = 0
for chunk in chunks[1:]:
    try:
        # Find end of HTTP headers (double CRLF)
        header_end = chunk.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        
        body = chunk[header_end+4:]
        
        # Try UTF-16 decode first (BizAnalyst uses UTF-16)
        try:
            decoded = body.decode("utf-16-le", errors="ignore")
        except:
            decoded = body.decode("utf-8", errors="ignore")
        
        # Only keep if it looks like XML
        if "<ENVELOPE>" not in decoded and "<envelope>" not in decoded.lower():
            continue
        
        # Clean up — remove null bytes and non-printable chars
        decoded = decoded.replace("\x00", "").strip()
        
        count += 1
        fname = os.path.join(out_dir, f"request_{count:03d}.xml")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(decoded)
        
        # Print preview
        preview = decoded[:200].replace("\n", " ")
        print(f"[{count}] Saved: {fname}")
        print(f"  {preview}\n")

    except Exception as e:
        continue

print(f"\nDone. {count} XML requests saved to {out_dir}/")