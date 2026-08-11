"""Regression checks for per-instance dashboard operational health."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dashboard_server import DataAggregator  # noqa: E402


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts = root / "artifacts" / "dev_candidate_scheduler"
        artifacts.mkdir(parents=True)
        aggregator = DataAggregator(artifacts, root, dashboard_port=18077)
        observed_ports: list[int] = []
        aggregator._port_listener_pid = lambda port: observed_ports.append(port) or os.getpid()
        aggregator._project_processes = lambda: []
        payload = aggregator.get_process_health()

        assert observed_ports == [18077], observed_ports
        assert payload["pid_files"]["dashboard_port_listener"] == os.getpid(), payload
        assert aggregator.dashboard_port == 18077

    print("[PASS] dashboard process health uses its configured instance port")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
