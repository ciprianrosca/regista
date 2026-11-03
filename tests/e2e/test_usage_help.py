"""End-to-end: the command line's help lists the usage meter's commands. `regista.py --help` and
`regista.py estimate --help` exit 0 and show the estimate and usage commands.

Nothing here reaches the network or the real home folder: the sample project has its own temporary home.

Run: python3 -m unittest discover -s tests/e2e"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import SampleProject  # noqa: E402


def lists_command(text, name):
    """Whether the help lists `name` as a command: in argparse's {a,b,c} choices or on a line of its own."""
    return bool(re.search(r"(?m)[{,]" + name + r"[,}]|^\s+" + name + r"\b", text))


class HelpEndToEnd(unittest.TestCase):
    def setUp(self):
        self.p = SampleProject()
        self.addCleanup(self.p.cleanup)

    def help(self, *args):
        r = self.p.cli(*args)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("Traceback", out)
        return r.stdout

    def test_help_shows_the_estimate_and_usage_commands(self):  # covers: UDOC-8
        top = self.help("--help")
        self.assertTrue(lists_command(top, "estimate"), f"--help should list the estimate command:\n{top}")
        self.assertTrue(lists_command(top, "usage"), f"--help should list the usage command:\n{top}")
        sub = self.help("estimate", "--help")
        self.assertIn("estimate", sub)
        self.assertRegex(sub, r"\ball\b|M<n>|M\d|milestone", f"estimate --help should say what it estimates:\n{sub}")
