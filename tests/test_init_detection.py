"""Tests for the init --auto detection fixes: git worktrees (FIX-4), the unittest gate when pytest isn't
available (FIX-5) and the source globs of a Python project without a package (FIX-6).
Run: python -m unittest tests.test_init_detection -v"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_regista import PY, regista, sh  # noqa: E402  (also isolates HOME for every tool run)

# The interpreter the unittest gate may name: the one running Regista, or the usual python commands.
PYTHONS = {PY, shlex.quote(PY), '"%s"' % PY, "python3", "python"}


class DetectCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="regista-detect-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, text="", root=None):
        fp = os.path.join(root or self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def fresh(self):
        """A second empty project folder, for subtests that need a clean slate."""
        root = tempfile.mkdtemp(prefix="regista-detect-")
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def gate_run(self, root=None):
        d = regista.detect(root or self.root)
        gates = [g for g in d["gates"] if g["name"] == "tests"]
        self.assertEqual(len(gates), 1, d["gates"])
        return gates[0]["run"]


class GitDetection(DetectCase):
    def init_repo(self, root):
        sh(root, "git", "init", "-q", "-b", "main")
        sh(root, "git", "config", "user.email", "t@example.com")
        sh(root, "git", "config", "user.name", "T")
        self.write("README.md", "demo\n", root=root)
        sh(root, "git", "add", "-A")
        sh(root, "git", "commit", "-q", "-m", "init")

    def test_a_git_worktree_is_in_git(self):  # covers: FIX-4
        self.init_repo(self.root)
        parent = self.fresh()
        wt = os.path.join(parent, "wt")
        sh(self.root, "git", "worktree", "add", "-q", "-b", "feature", wt)
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")), "a worktree's .git should be a file")
        self.write("pyproject.toml", '[project]\nname="x"\n', root=wt)
        self.assertIs(regista.detect(wt)["git"], True)

    def test_a_plain_repository_is_in_git(self):  # covers: FIX-4
        self.init_repo(self.root)
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".git")))
        self.assertIs(regista.detect(self.root)["git"], True)

    def test_a_folder_outside_git_is_not(self):  # covers: FIX-4
        self.write("pyproject.toml", '[project]\nname="x"\n')
        self.assertIs(regista.detect(self.root)["git"], False)


class UnittestGate(DetectCase):
    def assertUnittestGate(self, run, folder):
        m = re.fullmatch(r"(.+?) -m unittest discover -s (\S+)", run)
        self.assertIsNotNone(m, "expected '<python> -m unittest discover -s %s', got %r" % (folder, run))
        self.assertIn(m.group(1), PYTHONS, run)
        self.assertEqual(os.path.normpath(m.group(2)), os.path.normpath(folder), run)

    def assertPytestGate(self, run, runner=None):
        self.assertIn("pytest -q", run)
        self.assertNotIn("unittest", run)
        if runner:
            self.assertTrue(run.startswith(runner + " "), run)

    def test_unittest_tests_in_tests_folder(self):  # covers: FIX-5
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_calc.py", "import unittest\n\nclass T(unittest.TestCase):\n    pass\n")
        self.assertUnittestGate(self.gate_run(), "tests")

    def test_unittest_tests_in_test_folder_with_a_pyproject(self):  # covers: FIX-5
        self.write("pyproject.toml", '[project]\nname="calc"\ndependencies=["requests"]\n')
        self.write("calc.py", "")
        self.write("test/calc_test.py", "import unittest\n")
        self.assertUnittestGate(self.gate_run(), "test")

    def test_unittest_tests_at_the_root(self):  # covers: FIX-5
        self.write("requirements.txt", "requests\n")
        self.write("calc.py", "")
        self.write("test_calc.py", "import unittest\n")
        self.assertUnittestGate(self.gate_run(), ".")

    def test_detection_does_not_import_or_run_pytest(self):  # covers: FIX-5
        self.write("calc.py", "")
        self.write("tests/test_calc.py", "import unittest\n")
        real_run, real_popen = subprocess.run, subprocess.Popen

        def guard(real):
            def call(args, *a, **kw):
                text = args if isinstance(args, str) else " ".join(map(str, args))
                if "pytest" in text:
                    raise AssertionError("detection ran pytest: %s" % text)
                return real(args, *a, **kw)
            return call

        real_find_spec = __import__("importlib.util").util.find_spec

        def find_spec(name, *a, **kw):
            if name.split(".")[0] == "pytest":
                raise AssertionError("detection looked for the pytest module")
            return real_find_spec(name, *a, **kw)

        sys.modules.pop("pytest", None)
        with mock.patch("subprocess.run", guard(real_run)), mock.patch("subprocess.Popen", guard(real_popen)), \
                mock.patch("importlib.util.find_spec", find_spec):
            run = self.gate_run()
        self.assertNotIn("pytest", sys.modules)
        self.assertUnittestGate(run, "tests")

    def test_pytest_in_pyproject_dependencies_keeps_pytest(self):  # covers: FIX-5
        for deps in ('[project]\nname="calc"\ndependencies=["pytest>=8"]\n',
                     '[project]\nname="calc"\ndependencies=[]\n[project.optional-dependencies]\ndev=["pytest"]\n',
                     '[project]\nname="calc"\ndependencies=[]\n[dependency-groups]\ndev=["pytest", "ruff"]\n'):
            with self.subTest(deps=deps):
                root = self.fresh()
                self.write("pyproject.toml", deps, root=root)
                self.write("calc.py", "", root=root)
                self.write("tests/test_calc.py", "import unittest\n", root=root)
                self.assertPytestGate(self.gate_run(root))

    def test_pytest_in_requirements_keeps_pytest(self):  # covers: FIX-5
        for name in ("requirements.txt", "requirements-dev.txt"):
            with self.subTest(file=name):
                root = self.fresh()
                self.write(name, "requests\npytest==8.3.0\n", root=root)
                self.write("calc.py", "", root=root)
                self.write("tests/test_calc.py", "", root=root)
                self.assertPytestGate(self.gate_run(root))

    def test_a_conftest_keeps_pytest(self):  # covers: FIX-5
        self.write("calc.py", "")
        self.write("tests/conftest.py", "")
        self.write("tests/test_calc.py", "")
        self.assertPytestGate(self.gate_run())

    def test_a_uv_or_poetry_project_keeps_pytest_with_its_runner(self):  # covers: FIX-5
        for lock, runner in (("uv.lock", "uv run"), ("poetry.lock", "poetry run")):
            with self.subTest(lock=lock):
                root = self.fresh()
                self.write("pyproject.toml", '[project]\nname="calc"\ndependencies=[]\n', root=root)
                self.write(lock, "", root=root)
                self.write("calc.py", "", root=root)
                self.write("tests/test_calc.py", "import unittest\n", root=root)
                self.assertPytestGate(self.gate_run(root), runner)


class SourceGlobs(DetectCase):
    def source(self):
        return regista.detect(self.root)["roles"]["source"]

    def test_a_scripts_folder_is_the_source(self):  # covers: FIX-6
        self.write("scripts/tool.py", "")
        self.write("tests/test_tool.py", "")
        self.assertEqual(self.source(), ["scripts/**"])

    def test_root_modules_and_a_folder_give_both(self):  # covers: FIX-6
        self.write("pyproject.toml", '[project]\nname="tool"\n')
        self.write("tool.py", "")
        self.write("lib/helpers.py", "")
        self.write("docs/index.md", "")
        self.write("test/test_tool.py", "")
        self.assertCountEqual(self.source(), ["lib/**", "*.py"])

    def test_root_tests_alone_do_not_make_root_source(self):  # covers: FIX-6
        self.write("scripts/tool.py", "")
        self.write("test_tool.py", "")
        self.assertEqual(self.source(), ["scripts/**"])

    def test_a_package_project_is_unchanged(self):  # covers: FIX-6
        self.write("pyproject.toml", '[project]\nname="calc"\n')
        self.write("calc/__init__.py", "")
        self.write("scripts/tool.py", "")
        self.write("tests/test_calc.py", "")
        src = self.source()
        self.assertIn("calc/**", src)
        self.assertNotIn("scripts/**", src)
        self.assertNotIn("*.py", src)

    def test_a_src_project_is_unchanged(self):  # covers: FIX-6
        self.write("pyproject.toml", '[project]\nname="calc"\n')
        self.write("src/calc/__init__.py", "")
        self.write("scripts/tool.py", "")
        self.write("tests/test_calc.py", "")
        src = self.source()
        self.assertIn("src/**", src)
        self.assertNotIn("scripts/**", src)


if __name__ == "__main__":
    unittest.main()
