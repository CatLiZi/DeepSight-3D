"""Run the local user-operated servo test page, with autonomous tracking absent."""
import argparse
from datetime import datetime
from pathlib import Path
import sys

import uvicorn

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from locator_demo.web.servo_debug import create_servo_debug_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or PROJECT.parent / "hardware_inventory/servo-validation-20261024" / datetime.now().strftime("manual-%H%M%S")
    app = create_servo_debug_app(PROJECT / "locator_demo_settings.json", output)
    uvicorn.run(app, host="127.0.0.1", port=args.port)
