#!/usr/bin/env python3
"""Hermetic tests for the position-scan wrapper (run_positions.sh).

A fake python3 stands in for the screener so the wrapper's publish contract
(valid JSON under position_scan-*, .failed record on error) is tested without
touching the network or the live data directory.
"""

import glob
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WRAPPER = os.path.join(_HERE, "run_positions.sh")

_FAKE_PYTHON = """#!/bin/bash
if [ "$1" = "-c" ]; then
  exec %s "$@"
fi
if [ -n "${FAKE_STDERR:-}" ]; then
  cat "$FAKE_STDERR" >&2
fi
cat "$FAKE_STDOUT"
exit "${FAKE_EXIT:-0}"
""" % sys.executable

class TestRunPositionsWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outdir = os.path.join(self.tmp.name, "scans")
        self.bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bindir)
        fake = os.path.join(self.bindir, "python3")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_PYTHON)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.stdout_path = os.path.join(self.tmp.name, "stdout.json")
        self.stderr_path = os.path.join(self.tmp.name, "stderr.txt")

    def _run(self, payload, exit_code=0, stderr=""):
        with open(self.stdout_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        with open(self.stderr_path, "w", encoding="utf-8") as fh:
            fh.write(stderr)
        env = {
            "PATH": self.bindir + os.pathsep + os.environ.get("PATH", ""),
            "SOLANA_PUBLIC_WALLET": "B8qv53pKyW2sgP4VYik1GkkZ6NHNZZn6K5VTyVm55tTa",
            "SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com",
            "POSITION_SCAN_DIR": self.outdir,
            "FAKE_STDOUT": self.stdout_path,
            "FAKE_STDERR": self.stderr_path,
            "FAKE_EXIT": str(exit_code),
            "TZ": "UTC",
        }
        return subprocess.run(
            ["bash", _WRAPPER],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_success_publishes_valid_json(self):
        result = self._run('{"positions": [], "errors": {}}')
        self.assertEqual(result.returncode, 0, result.stderr)
        scans = glob.glob(os.path.join(self.outdir, "position_scan-*.json"))
        self.assertEqual(len(scans), 1)
        with open(scans[0], encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"positions": [], "errors": {}})
        self.assertEqual(result.stdout.strip(), scans[0])

    def test_failure_writes_failed_record_and_no_final_file(self):
        result = self._run("", exit_code=3, stderr="Error during position scan: RPC rate limited\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(
            glob.glob(os.path.join(self.outdir, "position_scan-*.json")), []
        )
        failed = glob.glob(os.path.join(self.outdir, "position_scan-*.json.failed"))
        self.assertEqual(len(failed), 1)
        with open(failed[0], encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["scan_status"], "failed")
        self.assertEqual(record["exit_code"], 3)
        self.assertIn("RPC rate limited", record["error"])

    def test_invalid_json_is_quarantined(self):
        result = self._run("not json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            glob.glob(os.path.join(self.outdir, "position_scan-*.json")), []
        )
        self.assertEqual(
            len(glob.glob(os.path.join(self.outdir, "position_scan-*.json.invalid"))),
            1,
        )

if __name__ == "__main__":
    unittest.main()
