#!/usr/bin/env python3
"""Contract tests for the hourly pipeline drivers.

The drivers live in the OpenClaw workspace, outside this repository, so these
tests skip when the workspace is absent. They lock in the safeguards that
fixed the 0-byte pool_scan incident: staged write, JSON validation, atomic
publish, loud failure, and a position-scan step before Sheldon.
"""

import os
import re
import unittest

_DRIVER_DIR = os.environ.get(
    "PIPELINE_DRIVER_DIR", "/root/.openclaw-devdash/workspace"
)
_DRIVERS = [
    os.path.join(_DRIVER_DIR, "run_pipeline.sh"),
    os.path.join(_DRIVER_DIR, "run_pipeline_meteora_only.sh"),
]

def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

@unittest.skipUnless(
    all(os.path.exists(path) for path in _DRIVERS),
    "pipeline drivers are not present in this workspace",
)
class TestPipelineDrivers(unittest.TestCase):
    def test_pool_scan_is_staged_and_published_atomically(self):
        for path in _DRIVERS:
            text = _read(path)
            with self.subTest(driver=os.path.basename(path)):
                self.assertIn('POOL_SCREEN_TMP="$POOL_SCAN_FILE.tmp"', text)
                self.assertIn('> "$POOL_SCREEN_TMP"', text)
                self.assertNotIn("|| true", text)
                # Publish only after validation.
                publish = text.index('mv "$POOL_SCREEN_TMP" "$POOL_SCAN_FILE"')
                validate = text.index("assert isinstance(data, list)")
                self.assertLess(validate, publish)
                self.assertIn("ERROR: Missy pool screener failed", text)
                self.assertIn("exit 1", text)

    def test_position_scan_runs_before_sheldon(self):
        for path in _DRIVERS:
            text = _read(path)
            with self.subTest(driver=os.path.basename(path)):
                position_step = text.index("position_screener/run_positions.sh")
                sheldon_step = text.index("run_cycle.py")
                self.assertLess(position_step, sheldon_step)
                self.assertIn("assert isinstance(data.get(\"positions\"), list)", text)
                self.assertIn("ERROR: Missy position screener failed", text)

    def test_sheldon_exit_code_is_not_swallowed(self):
        for path in _DRIVERS:
            text = _read(path)
            with self.subTest(driver=os.path.basename(path)):
                self.assertIn("SHELDON_RC=0", text)
                self.assertIn("|| SHELDON_RC=$?", text)
                self.assertIn('exit "$SHELDON_RC"', text)
                self.assertIn("ERROR: Sheldon scoring failed", text)

    def test_george_dispatcher_is_opt_in_and_dry_run_only(self):
        # An unattended scheduler run must never start trading on its own.
        for path in _DRIVERS:
            text = _read(path)
            with self.subTest(driver=os.path.basename(path)):
                self.assertIn('PIPELINE_START_DISPATCHER="${PIPELINE_START_DISPATCHER:-0}"', text)
                self.assertIn('if [ "$PIPELINE_START_DISPATCHER" != "1" ]', text)
                self.assertIn('"dry_run"', text)
                self.assertIn("REFUSING to start George dispatcher", text)

    def test_signal_writing_is_opt_in(self):
        for path in _DRIVERS:
            text = _read(path)
            with self.subTest(driver=os.path.basename(path)):
                self.assertIn('PIPELINE_WRITE_SIGNALS="${PIPELINE_WRITE_SIGNALS:-0}"', text)
                self.assertIn('if [ "$PIPELINE_WRITE_SIGNALS" = "1" ]', text)
                # --write-signals may only appear inside the opt-in branch.
                opt_in = text.index('if [ "$PIPELINE_WRITE_SIGNALS" = "1" ]')
                flag = text.index("--write-signals")
                self.assertLess(opt_in, flag)

if __name__ == "__main__":
    unittest.main()
