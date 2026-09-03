from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def check_parity(fixture_path: Path) -> None:
    fixture = json.loads(fixture_path.read_text())
    expected = {scenario["id"]: scenario["expected"] for scenario in fixture["scenarios"]}
    python_output = _run([sys.executable, "-m", "src.contract_fixture_runner", str(fixture_path)])
    deno_output = _run([
        "deno", "run", f"--allow-read={fixture_path.parent}",
        "tests/contract_fixture_runner.ts", str(fixture_path),
    ])
    if python_output != expected:
        raise AssertionError("Python contract fixture output differs from golden expectations")
    if deno_output != expected:
        raise AssertionError("Deno contract fixture output differs from golden expectations")
    if python_output != deno_output:
        raise AssertionError("Python and Deno contract fixture outputs differ")


if __name__ == "__main__":
    check_parity(Path(sys.argv[1]))