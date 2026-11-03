"""End-to-end: `regista.py usage` run as a real subprocess in a sample project set up with Regista, against a local
fake usage server (tests/fake_usage.py). Every run gets a temporary home, a temporary credentials file with a
fake token (REGISTA_CREDENTIALS), its own cache folder (XDG_CACHE_HOME) and the fake server's address
(REGISTA_USAGE_URL). Nothing here reaches the network or the real ~/.claude/.credentials.json.

Run: python3 -m unittest discover -s tests/e2e"""
import os
import re
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.append(os.path.dirname(HERE))  # for fake_usage; appended so tests/ never shadows tests/e2e/
from harness import PY, TOOL, SampleProject  # noqa: E402
from fake_usage import (FAKE_TOKEN, SESSION_RESETS_TEXT_BUCHAREST, WEEK_RESETS_TEXT_BUCHAREST,  # noqa: E402
                        FakeUsageServer, closed_port_url, usage_reply, write_credentials)

import subprocess  # noqa: E402

NOT_PLAN_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


class UsageCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = SampleProject()
        cls.proj.set_up_regista()

    @classmethod
    def tearDownClass(cls):
        cls.proj.cleanup()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-", dir=self.proj.base)
        self.creds = write_credentials(os.path.join(self.tmp, "credentials.json"))

    def usage(self, url, *args, creds=None, timeout=60):
        """Run `regista.py usage <args>` with a fresh cache, Bucharest time and the given endpoint address."""
        env = {k: v for k, v in self.proj.env.items() if k not in NOT_PLAN_VARS}
        env.update(REGISTA_USAGE_URL=url, REGISTA_CREDENTIALS=creds or self.creds, TZ="Europe/Bucharest",
                   XDG_CACHE_HOME=os.path.join(self.tmp, "cache"))
        return subprocess.run([PY, TOOL, "usage", *args], cwd=self.proj.root, capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=timeout)

    def assert_unknown(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if "usage unknown" in ln.lower()]
        self.assertTrue(lines, f"no 'usage unknown' line in:\n{r.stdout}\n{r.stderr}")
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        return lines[0]

    def assert_unknown_with_reason(self, r):
        line = self.assert_unknown(r)
        rest = re.sub(r"(?i)usage unknown", "", line).strip(" :-—()[].")
        self.assertTrue(re.search(r"[A-Za-z]{3,}", rest), f"no reason given after 'usage unknown': {line!r}")
        self.assertLess(len(line), 300, "the reason should be short")

    # --- USAGE-1

    def test_prints_percentages_and_local_reset_times(self):  # covers: USAGE-1
        with FakeUsageServer(reply=usage_reply(session_pct=37.0, week_pct=82.0)) as srv:
            r = self.usage(srv.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertRegex(out, r"\b37(\.0+)?\s?%")
        self.assertRegex(out, r"\b82(\.0+)?\s?%")
        self.assertIn(SESSION_RESETS_TEXT_BUCHAREST, out)   # 15:00 UTC shown as 17:00 in Bucharest
        self.assertIn(WEEK_RESETS_TEXT_BUCHAREST, out)      # 05:00 UTC shown as 07:00 in Bucharest
        self.assertIn("session", out.lower())
        self.assertIn("week", out.lower())
        self.assertNotIn("usage unknown", out.lower())
        self.assertEqual(srv.count, 1)

    # --- USAGE-2

    def test_unreachable_endpoint_gives_usage_unknown(self):  # covers: USAGE-2
        self.assert_unknown_with_reason(self.usage(closed_port_url()))

    def test_error_status_gives_usage_unknown(self):  # covers: USAGE-2
        with FakeUsageServer(status=500) as srv:
            r = self.usage(srv.url)
        self.assert_unknown_with_reason(r)
        self.assertEqual(srv.count, 1)

    def test_invalid_json_gives_usage_unknown(self):  # covers: USAGE-2
        with FakeUsageServer(body="<html>not json</html>") as srv:
            r = self.usage(srv.url)
        self.assert_unknown_with_reason(r)

    def test_slow_endpoint_times_out_after_five_seconds(self):  # covers: USAGE-2
        with FakeUsageServer(delay=8) as srv:
            start = time.monotonic()
            r = self.usage(srv.url)
            elapsed = time.monotonic() - start
        self.assert_unknown_with_reason(r)
        self.assertLess(elapsed, 7.5, "the command should give up after about 5 seconds")

    # --- USAGE-3

    def check_no_credentials(self, creds):
        with FakeUsageServer() as srv:
            r = self.usage(srv.url, creds=creds)
        self.assert_unknown(r)
        self.assertEqual(srv.requests, [], "the endpoint must not be contacted without a token")

    def test_missing_credentials_file(self):  # covers: USAGE-3
        self.check_no_credentials(os.path.join(self.tmp, "does-not-exist.json"))

    def test_credentials_without_access_token(self):  # covers: USAGE-3
        self.check_no_credentials(write_credentials(os.path.join(self.tmp, "no-token.json"), token=None))

    def test_credentials_not_json(self):  # covers: USAGE-3
        path = os.path.join(self.tmp, "garbage.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.check_no_credentials(path)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read any file")
    def test_unreadable_credentials_file(self):  # covers: USAGE-3
        path = write_credentials(os.path.join(self.tmp, "locked.json"))
        os.chmod(path, 0)
        try:
            self.check_no_credentials(path)
        finally:
            os.chmod(path, 0o600)


if __name__ == "__main__":
    unittest.main()
