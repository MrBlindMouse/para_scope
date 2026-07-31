"""Runnable check: multi-series packing must not null-pad on a category union."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_CHECK = Path(__file__).resolve().parents[1] / "static" / "js" / "widget-charts-pack-check.js"


def test_widget_charts_pack_no_null_pad():
    result = subprocess.run(
        ["node", str(_CHECK)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
