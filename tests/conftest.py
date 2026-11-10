import os
import sys
import tempfile
from pathlib import Path

# Configure before app.py is imported: throwaway data dir, a known token.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="swt-test-")
os.environ["API_TOKEN"] = "test-token"
os.environ.setdefault("IDLE_UNLOAD_SECONDS", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
