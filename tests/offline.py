"""Forcing a test to run without a model, and proving it.

This exists because the suite was twice only offline by accident. Tests that
reach `review_session()` will happily call a real Ollama if one happens to be
running, which took the suite from 0.6s to 83s the first time a model was up -
and made a green run depend on a service being *switched off*.

Use it in `setUp`:

    class MyTest(unittest.TestCase):
        def setUp(self):
            go_offline(self)

Anything that still answers with this in place is computed, not generated,
which is the property most of these tests are really pinning.
"""

import sys
import unittest


class _NoModel:
    """Make `from strands import Agent` fail, wherever it is imported."""

    def __enter__(self):
        self._had = "strands" in sys.modules
        self._real = sys.modules.get("strands")
        sys.modules["strands"] = None
        return self

    def __exit__(self, *exc):
        if self._had:
            sys.modules["strands"] = self._real
        else:
            sys.modules.pop("strands", None)
        return False


def go_offline(case: unittest.TestCase) -> None:
    """Block model access for the rest of this test. Undone automatically."""
    guard = _NoModel()
    guard.__enter__()
    case.addCleanup(guard.__exit__)
