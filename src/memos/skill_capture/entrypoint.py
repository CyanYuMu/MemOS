"""Standalone hook/daemon entry: avoid initializing MemOS models in a Claude hook."""

import sys

from pathlib import Path


if __name__ == "__main__":
    # -I intentionally removes cwd/PYTHONPATH; only add this trusted source tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from skill_capture.cli import main

    raise SystemExit(main())
