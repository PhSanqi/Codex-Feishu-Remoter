"""Run the CFR CLI directly from a source checkout."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from cfr.cli.main import cli


if __name__ == '__main__':
    cli()
