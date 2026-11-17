"""Template entry point: python src/agent.py "Your research question here"

The agent lives in the `receipts` package; this file keeps the hackathon template's run command working.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from receipts.__main__ import cli  # noqa: E402

if __name__ == "__main__":
    cli()
