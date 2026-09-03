"""
Test package initialization.

Must run before app.config is imported — which is why it lives here and not in
base.py. Without it, a real .env file sitting on the developer's machine is
loaded into the tests, and they then fail or pass for reasons unrelated to the
code.
"""
from __future__ import annotations

import os

# A path that is not a file — loading .env returns an empty dict immediately.
os.environ["ENV_FILE"] = os.devnull
