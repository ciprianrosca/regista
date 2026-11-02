"""End-to-end: refusals a session met are readable afterwards with `regista.py refusals --json`, labels included,
so other tools never need to parse hooks.log. Run: python3 -m unittest discover -s tests/e2e"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import PY, TOOL, SampleProject, bash, decision  # noqa: E402

FIELDS = {"number", "kind", "rule", "role", "session", "worker", "label", "label_by", "label_why", "at", "tool",
          "reason"}


def user_cli(project, *args):
    """`regista.py <args>` from the user's own terminal: the project's clean environment (harness variables already
    dropped) without any Claude Code or Codex session variable, since labelling refuses to run inside a tool shell."""
    env = {k: v for k, v in project.env.items() if not k.startswith(("CLAUDE", "CODEX_")) or k == "CODEX_HOME"}
    return subprocess.run([PY, TOOL, *args], cwd=project.root, capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=600)


class RefusalsJson(unittest.TestCase):
    def setUp(self):
        self.p = SampleProject()
        self.addCleanup(self.p.cleanup)
        self.p.set_up_regista()

    def pre_tool(self, payload):
        answer = self.p.hook("pre-tool", {"session_id": "e2e-s1", **payload})
        return decision(answer)

    def listing(self):
        r = self.p.cli("refusals", "--json")
        self.assertEqual(r.returncode, 0, f"refusals --json failed:\n{r.stdout}\n{r.stderr}")
        rows = json.loads(r.stdout)
        self.assertIsInstance(rows, list)
        return rows

    def test_a_sessions_refusals_and_labels_read_back_as_json(self):  # covers: PRE-6
        p = self.p
        self.assertEqual(self.pre_tool(bash("git commit --no-verify -m x")), "deny")
        self.assertEqual(self.pre_tool(bash("git push origin main")), "ask")
        reviewer = {"agent_id": "r1", "agent_type": "regista:reviewer", "tool_name": "Edit",
                    "tool_input": {"file_path": p.path("calc.py"), "old_string": "a + b", "new_string": "b + a"}}
        self.assertEqual(self.pre_tool(reviewer), "deny")
        p.hook("session-end", {"session_id": "e2e-s1", "reason": "exit"})

        rows = self.listing()
        mine = [x for x in rows if x.get("session") == "e2e-s1"]
        self.assertEqual(len(mine), 3, rows)
        for row in mine:
            self.assertTrue(FIELDS <= set(row), f"missing {FIELDS - set(row)} in {row}")
        # newest first
        self.assertEqual([(x["kind"], x["rule"], x["role"]) for x in mine],
                         [("deny", "role_guard", "reviewer"), ("ask", "push_policy", "main"),
                          ("deny", "no_verify", "main")])
        self.assertEqual([(x["label"], x["label_by"]) for x in mine],
                         [("unsure", "rule"), ("real", "rule"), ("real", "rule")])
        self.assertTrue(all(x["worker"] is None for x in mine))

        # the user labels the reviewer's refusal; the JSON shows it, with the reason why
        r = user_cli(p, "refusals", "label", str(mine[0]["number"]), "false_alarm", "--why", "the reviewer only proposed it")
        self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
        labelled = [x for x in self.listing() if x["number"] == mine[0]["number"]]
        self.assertEqual(len(labelled), 1)
        self.assertEqual((labelled[0]["label"], labelled[0]["label_by"], labelled[0]["label_why"]),
                         ("false_alarm", "user", "the reviewer only proposed it"))


if __name__ == "__main__":
    unittest.main()
