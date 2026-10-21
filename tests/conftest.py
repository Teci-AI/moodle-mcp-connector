"""Shared test setup.

server.py reads its configuration at import time, so the environment is set
here, before any test imports it. Write tools are enabled so every tool is
registered; no test talks to a real Moodle site.
"""

import os

os.environ.update(
    MOODLE_URL="https://moodle.test",
    MOODLE_TOKEN="SECRET-TEST-TOKEN",
    MOODLE_ALLOW_WRITE="true",
    MOODLE_TOOLSET="",
)
