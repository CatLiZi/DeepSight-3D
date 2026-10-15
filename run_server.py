from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import uvicorn

from locator_demo.web.app import create_app


if __name__ == "__main__":
    port = int(os.environ.get("LOCATOR_DEMO_PORT", "8010"))
    simulated = os.environ.get("LOCATOR_DEMO_SIM", "0") == "1"
    uvicorn.run(create_app(simulated=simulated), host="127.0.0.1", port=port)