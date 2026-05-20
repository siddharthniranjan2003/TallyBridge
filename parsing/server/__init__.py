import sys
from pathlib import Path

_parsing_dir = str(Path(__file__).resolve().parents[1])
if _parsing_dir not in sys.path:
    sys.path.insert(0, _parsing_dir)
