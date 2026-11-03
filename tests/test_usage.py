"""Unit and integration tests for the usage meter's reader: read_usage, format_usage and `regista.py usage`
(the end-to-end checks of the printed text are in tests/e2e/test_usage.py).

Every test runs against the local fake server in tests/fake_usage.py, with a temporary home folder, a temporary
credentials file holding a fake token, and its own cache folder. Nothing here reaches the network or the real
~/.claude/.credentials.json.

Run: python3 -m unittest discover -s tests"""
import atexit
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

TEST_HOME = tempfile.mkdtemp(prefix="regista-usage-home-")
atexit.register(shutil.rmtree, TEST_HOME, True)
os.environ.update(HOME=TEST_HOME, CODEX_HOME=os.path.join(TEST_HOME, ".codex"))

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.abspath(os.path.join(HERE, "..", "scripts", "regista.py"))
sys.path.insert(0, os.path.dirname(TOOL))
sys.path.insert(0, HERE)
import regista  # noqa: E402
from fake_usage import (FAKE_TOKEN, SESSION_RESETS_AT, WEEK_RESETS_AT, FakeUsageServer,  # noqa: E402
                        closed_port_url, limits_list, rising_replies, usage_reply, write_credentials)

PY = sys.executable
NOT_PLAN_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
KEYS = {"state", "reason", "source", "at", "session", "week"}


def same_instant(a, b):
    return datetime.fromisoformat(a) == datetime.fromisoformat(b)


class UsageBase(unittest.TestCase):
    """A temporary folder with a credentials file, a cache folder, and os.environ pointed at them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regista-usage-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.creds = write_credentials(os.path.join(self.tmp, "credentials.json"))
        self.cache_home = os.path.join(self.tmp, "cache")
        self.cache_file = os.path.join(self.cache_home, "regista", "usage.json")
        self.srv = FakeUsageServer().start()
        self.addCleanup(self.srv.stop)
        env = mock.patch.dict(os.environ, {"REGISTA_USAGE_URL": self.srv.url, "REGISTA_CREDENTIALS": self.creds,
                                           "XDG_CACHE_HOME": self.cache_home, "TZ": "Europe/Bucharest"})
        env.start()
        self.addCleanup(env.stop)
        for k in NOT_PLAN_VARS + ("REGISTA_USAGE_CACHE_SECONDS",):
            os.environ.pop(k, None)

    def read(self, cfg=None, fresh=False):
        return regista.read_usage({} if cfg is None else cfg, fresh=fresh)

    # --- running the real CLI

    def cli_env(self, **extra):
        drop = ("REGISTA_", "DAEDALUS_", "GIT_")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(drop) and not k.endswith("_PROJECT_DIR") and k not in NOT_PLAN_VARS}
        env.update(HOME=TEST_HOME, REGISTA_USAGE_URL=self.srv.url, REGISTA_CREDENTIALS=self.creds,
                   XDG_CACHE_HOME=self.cache_home, TZ="Europe/Bucharest", PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def make_project(self):
        """A git repository with `regista.py init` run in it; returns its root."""
        root = os.path.join(self.tmp, "project")
        os.makedirs(root)
        env = self.cli_env()
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
                    ["git", "config", "user.name", "T"]):
            subprocess.run(cmd, cwd=root, env=env, check=True, capture_output=True)
        subprocess.run([PY, TOOL, "init", "--name", "Demo"], cwd=root, env=env, check=True, capture_output=True)
        return root

    def cli(self, *args, cwd=None, **extra):
        return subprocess.run([PY, TOOL, "usage", *args], cwd=cwd or self.tmp, capture_output=True, text=True,
                              encoding="utf-8", env=self.cli_env(**extra), timeout=60)


class ReadUsageTest(UsageBase):
    def test_reads_session_and_week_from_endpoint(self):  # covers: USAGE-7
        reading = self.read()
        self.assertEqual(set(reading), KEYS)
        self.assertEqual(reading["state"], "ok")
        self.assertEqual(reading["reason"], "")
        self.assertEqual(reading["source"], "endpoint")
        datetime.fromisoformat(reading["at"])
        self.assertEqual(reading["session"]["pct"], 37.0)
        self.assertEqual(reading["week"]["pct"], 82.0)
        self.assertTrue(same_instant(reading["session"]["resets_at"], SESSION_RESETS_AT))
        self.assertTrue(same_instant(reading["week"]["resets_at"], WEEK_RESETS_AT))

    def test_request_is_get_with_bearer_token_and_beta_header(self):  # covers: USAGE-4
        self.read()
        self.assertEqual(len(self.srv.requests), 1)
        req = self.srv.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/api/oauth/usage")
        self.assertEqual(req["headers"].get("authorization"), "Bearer " + FAKE_TOKEN)
        self.assertEqual(req["headers"].get("anthropic-beta"), "oauth-2025-04-20")


class CacheTest(UsageBase):
    def test_young_cache_is_used_without_contacting_endpoint(self):  # covers: USAGE-6
        self.srv.configure(replies=rising_replies(3))
        first = self.read()
        self.assertEqual(first["source"], "endpoint")
        self.assertTrue(os.path.isfile(self.cache_file), "the reading should be cached in $XDG_CACHE_HOME/regista")
        second = self.read()
        self.assertEqual(second["source"], "cache")
        self.assertEqual(second["state"], "ok")
        self.assertEqual(second["session"]["pct"], first["session"]["pct"])
        self.assertEqual(self.srv.count, 1)

    def test_fresh_contacts_endpoint_and_rewrites_cache(self):  # covers: USAGE-6
        self.srv.configure(replies=rising_replies(3, session_start=10.0, session_step=5.0))
        self.read()
        fresh = self.read(fresh=True)
        self.assertEqual(fresh["source"], "endpoint")
        self.assertEqual(fresh["session"]["pct"], 15.0)
        self.assertEqual(self.srv.count, 2)
        cached = self.read()  # the rewritten cache holds the fresh reading
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(cached["session"]["pct"], 15.0)
        self.assertEqual(self.srv.count, 2)

    def test_old_cache_is_refreshed(self):  # covers: USAGE-6
        self.srv.configure(replies=rising_replies(3, session_start=10.0, session_step=5.0))
        self.read()
        os.environ["REGISTA_USAGE_CACHE_SECONDS"] = "0"
        again = self.read()
        self.assertEqual(again["source"], "endpoint")
        self.assertEqual(again["session"]["pct"], 15.0)
        self.assertEqual(self.srv.count, 2)
        os.environ["REGISTA_USAGE_CACHE_SECONDS"] = "60"
        self.assertEqual(self.read()["session"]["pct"], 15.0)
        self.assertEqual(self.srv.count, 2)

    def test_cli_fresh_flag_skips_cache(self):  # covers: USAGE-6
        self.srv.configure(replies=rising_replies(3))
        self.assertEqual(self.cli("--json").returncode, 0)
        self.assertEqual(json.loads(self.cli("--json").stdout)["source"], "cache")
        self.assertEqual(self.srv.count, 1)
        r = self.cli("--json", "--fresh")
        self.assertEqual(json.loads(r.stdout)["source"], "endpoint")
        self.assertEqual(self.srv.count, 2)


class JsonOutputTest(UsageBase):
    def test_json_flag_prints_one_object(self):  # covers: USAGE-7
        r = self.cli("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)  # one JSON object and nothing else
        self.assertIsInstance(obj, dict)
        self.assertEqual(set(obj), KEYS)
        self.assertEqual(obj["state"], "ok")
        self.assertEqual(obj["source"], "endpoint")
        self.assertEqual(obj["session"]["pct"], 37.0)
        self.assertEqual(obj["week"]["pct"], 82.0)
        self.assertTrue(same_instant(obj["week"]["resets_at"], WEEK_RESETS_AT))
        datetime.fromisoformat(obj["at"])

    def test_json_when_unknown_has_null_session_and_week(self):  # covers: USAGE-7
        r = self.cli("--json", REGISTA_USAGE_URL=closed_port_url())
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(set(obj), KEYS)
        self.assertEqual(obj["state"], "unknown")
        self.assertTrue(obj["reason"])
        self.assertIsNone(obj["session"])
        self.assertIsNone(obj["week"])


class TokenSecrecyTest(UsageBase):
    def assert_no_token_under(self, folder):
        for dirpath, _, files in os.walk(folder):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as f:
                    self.assertNotIn(FAKE_TOKEN.encode(), f.read(), os.path.join(dirpath, name))

    def test_token_never_in_output_cache_or_project_files(self):  # covers: USAGE-5
        root = self.make_project()
        for args in ((), ("--json",), ("--fresh",), ("--json", "--fresh")):
            r = self.cli(*args, cwd=root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(self.cache_file))
        self.assert_no_token_under(self.cache_home)
        # the endpoint fails and repeats the Authorization header in its error body
        self.srv.configure(status=401, echo_token=True)
        for args in (("--fresh",), ("--json", "--fresh")):
            r = self.cli(*args, cwd=root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        self.assert_no_token_under(self.cache_home)
        self.assert_no_token_under(os.path.join(root, ".regista"))

    def test_token_never_in_error_reasons(self):  # covers: USAGE-5
        self.srv.configure(status=401, echo_token=True)
        reading = self.read(fresh=True)
        self.assertEqual(reading["state"], "unknown")
        self.assertNotIn(FAKE_TOKEN, json.dumps(reading))
        self.assertNotIn(FAKE_TOKEN, regista.format_usage(reading))
        self.srv.configure(status=200, body="not json " + FAKE_TOKEN)
        reading = self.read(fresh=True)
        self.assertEqual(reading["state"], "unknown")
        self.assertNotIn(FAKE_TOKEN, json.dumps(reading))


class LimitsListTest(UsageBase):
    def test_limits_list_used_when_objects_missing(self):  # covers: USAGE-8
        reply = {"limits": limits_list(session_pct=21.0, week_pct=64.0, scoped_pct=3.0)}
        self.srv.configure(reply=reply)
        reading = self.read()
        self.assertEqual(reading["state"], "ok")
        self.assertEqual(reading["session"]["pct"], 21.0)
        self.assertEqual(reading["week"]["pct"], 64.0)  # weekly_all, not weekly_scoped
        self.assertTrue(same_instant(reading["session"]["resets_at"], SESSION_RESETS_AT))
        self.assertTrue(same_instant(reading["week"]["resets_at"], WEEK_RESETS_AT))

    def test_limits_list_used_when_seven_day_missing(self):  # covers: USAGE-8
        self.srv.configure(reply=usage_reply(21.0, 64.0, seven_day=False, limits=True))
        reading = self.read()
        self.assertEqual(reading["state"], "ok")
        self.assertEqual(reading["week"]["pct"], 64.0)
        self.assertEqual(reading["session"]["pct"], 21.0)


class NotOnPlanTest(UsageBase):
    def assert_not_plan(self):
        reading = self.read(fresh=True)
        self.assertEqual(reading["state"], "not_plan")
        self.assertEqual(self.srv.count, 0)

    def test_bedrock(self):  # covers: USAGE-9
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        self.assert_not_plan()

    def test_vertex(self):  # covers: USAGE-9
        os.environ["CLAUDE_CODE_USE_VERTEX"] = "true"
        self.assert_not_plan()

    def test_bedrock_set_to_zero_or_empty_is_ignored(self):  # covers: USAGE-9
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "0"
        os.environ["CLAUDE_CODE_USE_VERTEX"] = ""
        self.assertEqual(self.read()["state"], "ok")

    def test_api_key_without_oauth_token(self):  # covers: USAGE-9
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-FAKE"
        os.environ["REGISTA_CREDENTIALS"] = os.path.join(self.tmp, "missing.json")
        self.assert_not_plan()

    def test_api_key_with_oauth_token_still_reads_plan(self):  # covers: USAGE-9
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-FAKE"
        self.assertEqual(self.read()["state"], "ok")

    def test_cli_says_not_on_a_claude_plan(self):  # covers: USAGE-9
        r = self.cli(CLAUDE_CODE_USE_BEDROCK="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not on a Claude plan", r.stdout)
        self.assertEqual(self.srv.count, 0)


class ConfigTest(UsageBase):
    def test_disabled_meter_is_unknown_without_contacting_endpoint(self):  # covers: USAGE-10
        reading = self.read(cfg={"usage": {"enabled": False}}, fresh=True)
        self.assertEqual(reading["state"], "unknown")
        self.assertIn("off", reading["reason"].lower())
        self.assertEqual(self.srv.count, 0)

    def test_missing_usage_section_or_keys_use_defaults(self):  # covers: USAGE-10
        self.assertEqual(self.read(cfg={}, fresh=True)["state"], "ok")
        self.assertEqual(self.read(cfg={"usage": {"stop_at_weekly_pct": 95}}, fresh=True)["state"], "ok")
        self.assertEqual(self.read(cfg={"usage": {}}, fresh=True)["state"], "ok")
        self.assertEqual(self.srv.count, 3)

    def test_project_without_usage_section_still_works(self):  # covers: USAGE-10
        root = self.make_project()
        with open(os.path.join(root, ".regista", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.pop("usage", None)
        with open(os.path.join(root, ".regista", "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        r = subprocess.run([PY, TOOL, "status"], cwd=root, capture_output=True, text=True, encoding="utf-8",
                           env=self.cli_env(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("--json", cwd=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["state"], "ok")

    def test_project_config_disabling_meter_is_respected_by_cli(self):  # covers: USAGE-10
        root = self.make_project()
        path = os.path.join(root, ".regista", "config.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["usage"] = {"enabled": False}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        r = self.cli("--json", "--fresh", cwd=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["state"], "unknown")
        self.assertEqual(self.srv.count, 0)


class MacKeychainTest(UsageBase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.args_log = os.path.join(self.tmp, "security-args.txt")
        os.environ["REGISTA_CREDENTIALS"] = os.path.join(self.tmp, "no-such-credentials.json")
        os.environ["PATH"] = self.bin
        patcher = mock.patch.object(sys, "platform", "darwin")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_security(self, output, code=0):
        script = os.path.join(self.bin, "security")
        payload = os.path.join(self.tmp, "security-out.txt")
        with open(payload, "w", encoding="utf-8") as f:
            f.write(output)
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{self.args_log}'\ncat '{payload}'\nexit {code}\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def test_token_read_from_keychain(self):  # covers: USAGE-11
        self.fake_security(json.dumps({"claudeAiOauth": {"accessToken": FAKE_TOKEN}}) + "\n")
        reading = self.read()
        self.assertEqual(reading["state"], "ok")
        self.assertEqual(self.srv.requests[0]["headers"].get("authorization"), "Bearer " + FAKE_TOKEN)
        with open(self.args_log, encoding="utf-8") as f:
            args = f.read().splitlines()
        self.assertEqual(args, ["find-generic-password", "-s", "Claude Code-credentials", "-w"])

    def test_failing_security_command_is_unknown(self):  # covers: USAGE-11
        self.fake_security("security: SecKeychainSearchCopyNext: The specified item could not be found.\n", code=44)
        reading = self.read()
        self.assertEqual(reading["state"], "unknown")
        self.assertEqual(self.srv.count, 0)

    def test_absent_security_command_is_unknown(self):  # covers: USAGE-11
        reading = self.read()
        self.assertEqual(reading["state"], "unknown")
        self.assertEqual(self.srv.count, 0)


class NeverRaisesTest(UsageBase):
    def write_cache(self, text):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        with open(self.cache_file, "w", encoding="utf-8") as f:
            f.write(text)

    def test_bad_cache_file_does_not_raise(self):  # covers: USAGE-12
        for text in ("{broken", "[]", '"text"', '{"at": "yesterday", "session": 5}', '{"state": "ok"}'):
            with self.subTest(cache=text):
                self.write_cache(text)
                reading = self.read()
                self.assertIn(reading["state"], ("ok", "unknown"))
                self.assertEqual(set(reading), KEYS)

    def test_bad_cache_file_and_unreachable_endpoint_is_unknown(self):  # covers: USAGE-12
        self.write_cache("{broken")
        os.environ["REGISTA_USAGE_URL"] = closed_port_url()
        self.assertEqual(self.read()["state"], "unknown")

    def test_unwritable_cache_folder_does_not_raise(self):  # covers: USAGE-12
        blocker = os.path.join(self.tmp, "a-file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        os.environ["XDG_CACHE_HOME"] = blocker  # the cache folder can't be created under a file
        reading = self.read()
        self.assertIn(reading["state"], ("ok", "unknown"))
        self.assertEqual(set(reading), KEYS)

    def test_odd_reply_shapes_are_unknown(self):  # covers: USAGE-12
        odd = ([], "text", 42, None, {}, {"five_hour": "high", "seven_day": []},
               {"five_hour": {"utilization": "lots"}, "seven_day": {"utilization": None}},
               {"limits": "none"}, {"limits": [{"kind": "session"}, 7]})
        for reply in odd:
            with self.subTest(reply=reply):
                self.srv.configure(reply=reply)
                reading = self.read(fresh=True)
                self.assertEqual(reading["state"], "unknown")
                self.assertEqual(set(reading), KEYS)

    def test_bad_cache_lifetime_value_does_not_raise(self):  # covers: USAGE-12
        os.environ["REGISTA_USAGE_CACHE_SECONDS"] = "soon"
        self.assertIn(self.read()["state"], ("ok", "unknown"))


if __name__ == "__main__":
    unittest.main()
