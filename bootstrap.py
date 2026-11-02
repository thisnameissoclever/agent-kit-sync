"""Run agent-kit-sync from this clone before it is installed: python3 -I -S bootstrap.py setup check"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_kit_sync.cli import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
