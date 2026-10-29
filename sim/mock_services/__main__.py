"""Serve all mock services on one port for manual demos:

python -m sim.mock_services [--port 8100]

Then e.g. GET http://127.0.0.1:8100/facilities/facilities, or POST /driver/_faults to inject
failures. Tests and replay do not use this; they call the apps in-process.
"""

import argparse

import uvicorn

from sim.mock_services.apps import create_root_app
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld


def main() -> None:
    parser = argparse.ArgumentParser(description="Cold-chain mock services (simulated)")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    uvicorn.run(create_root_app(MockWorld(), FaultInjector()), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
