import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path():
    # Retain synthetic artifacts: no automatic permanent deletion.
    return Path(tempfile.mkdtemp(prefix="insane-review-test-"))


@pytest.fixture
def engine():
    source = Path(__file__).parents[1] / "bin" / "pack_and_ask.py"
    spec = importlib.util.spec_from_file_location("review_engine_test", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
