"""Entry point — delegates everything to server.handler."""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from server.handler import main

if __name__ == "__main__":
    sys.exit(main())
