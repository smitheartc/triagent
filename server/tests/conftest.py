import os
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

# Tests never touch the network; make sure nothing accidentally picks up a real key or cluster.
os.environ["NVIDIA_API_KEY"] = "test-key-not-real"
os.environ["ES_URL"] = "http://127.0.0.1:9"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_model_network(monkeypatch):
    """Any real NVIDIA call in a test fails at once instead of going out over the network."""
    import llm

    def refuse(*args, **kwargs):
        raise RuntimeError("network disabled in tests: patch llm.chat (see fakes.fake_chat)")
    monkeypatch.setattr(llm, "_client", refuse)
