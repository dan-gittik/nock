import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.basicConfig(
    level=logging.DEBUG, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
