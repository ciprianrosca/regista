"""The three plugin manifests name the plugin Regista (DOC-1), and so do the skills, agents and
templates (DOC-3).

.claude-plugin/plugin.json, .claude-plugin/marketplace.json and .codex-plugin/plugin.json must name the
plugin "regista" at version "0.9.0" with the address https://github.com/ciprianrosca/regista, say nothing
about "daedalus" in any case, and keep their CRLF line endings on every line, including the last.

Everything under skills/, agents/ and templates/ uses the new names (Regista, /regista:<skill>,
regista:<role>, .regista/, scripts/regista.py, REGISTA_<X>, regista/<name>), says nothing about
"daedalus" or dd.py, and keeps its line endings: the files in CRLF_FILES use CRLF, all others LF.

Run: python3 -m unittest tests.test_docs -v"""
import json
import os
import re
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CLAUDE_PLUGIN = os.path.join(REPO, ".claude-plugin", "plugin.json")
MARKETPLACE = os.path.join(REPO, ".claude-plugin", "marketplace.json")
CODEX_PLUGIN = os.path.join(REPO, ".codex-plugin", "plugin.json")
MANIFESTS = [CLAUDE_PLUGIN, MARKETPLACE, CODEX_PLUGIN]

NAME = "regista"
VERSION = "0.9.0"
URL = "https://github.com/ciprianrosca/regista"


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def load(path):
    return json.loads(read_bytes(path).decode("utf-8"))


class ClaudePluginManifest(unittest.TestCase):
    def setUp(self):
        self.data = load(CLAUDE_PLUGIN)

    def test_name_is_regista(self):  # covers: DOC-1
        self.assertEqual(self.data.get("name"), NAME)

    def test_version_is_0_9_0(self):  # covers: DOC-1
        self.assertEqual(self.data.get("version"), VERSION)

    def test_homepage_and_repository(self):  # covers: DOC-1
        self.assertEqual(self.data.get("homepage"), URL)
        self.assertEqual(self.data.get("repository"), URL)


class CodexPluginManifest(unittest.TestCase):
    def setUp(self):
        self.data = load(CODEX_PLUGIN)

    def test_name_is_regista(self):  # covers: DOC-1
        self.assertEqual(self.data.get("name"), NAME)

    def test_version_is_0_9_0(self):  # covers: DOC-1
        self.assertEqual(self.data.get("version"), VERSION)

    def test_homepage_and_repository(self):  # covers: DOC-1
        self.assertEqual(self.data.get("homepage"), URL)
        self.assertEqual(self.data.get("repository"), URL)

    def test_keeps_skills_and_hooks_fields(self):  # covers: DOC-1
        self.assertEqual(self.data.get("skills"), "./skills/")
        self.assertEqual(self.data.get("hooks"), "./hooks/codex-hooks.json")


class MarketplaceManifest(unittest.TestCase):
    def setUp(self):
        self.data = load(MARKETPLACE)

    def test_marketplace_name_is_regista(self):  # covers: DOC-1
        self.assertEqual(self.data.get("name"), NAME)

    def test_exactly_one_plugin_entry(self):  # covers: DOC-1
        plugins = self.data.get("plugins")
        self.assertIsInstance(plugins, list)
        self.assertEqual(len(plugins), 1)

    def test_plugin_entry_fields(self):  # covers: DOC-1
        plugins = self.data.get("plugins") or [{}]
        entry = plugins[0]
        self.assertEqual(entry.get("name"), NAME)
        self.assertEqual(entry.get("source"), "./")
        self.assertEqual(entry.get("version"), VERSION)
        self.assertEqual(entry.get("homepage"), URL)
        self.assertEqual(entry.get("repository"), URL)


class AllManifests(unittest.TestCase):
    def test_no_daedalus_in_any_case(self):  # covers: DOC-1
        for path in MANIFESTS:
            with self.subTest(manifest=os.path.relpath(path, REPO)):
                text = read_bytes(path).decode("utf-8")
                self.assertIsNone(re.search("daedalus", text, re.IGNORECASE),
                                  "the manifest still says daedalus")

    def test_every_line_ends_in_crlf(self):  # covers: DOC-1
        for path in MANIFESTS:
            with self.subTest(manifest=os.path.relpath(path, REPO)):
                raw = read_bytes(path)
                self.assertTrue(raw.endswith(b"\r\n"), "the last line does not end in CRLF")
                bare = [i for i in range(len(raw)) if raw[i:i + 1] == b"\n" and (i == 0 or raw[i - 1:i] != b"\r")]
                self.assertEqual(bare, [], "line feeds without a carriage return at byte offsets %s" % bare[:5])

    def test_every_manifest_is_valid_json(self):  # covers: DOC-1
        for path in MANIFESTS:
            with self.subTest(manifest=os.path.relpath(path, REPO)):
                self.assertIsInstance(load(path), dict)


# --- DOC-3: skills, agents and templates use the new names ----------------------------------------

SKILLS = ["build", "change", "gate", "init", "learn", "oversee", "pause", "save", "start", "try", "verify"]
AGENTS = ["builder", "researcher", "reviewer", "test-writer"]
RENAMED_DIRS = ["skills", "agents", "templates"]

CRLF_FILES = [
    "skills/build/SKILL.md",
    "skills/init/SKILL.md",
    "skills/pause/SKILL.md",
    "skills/change/agents/openai.yaml",
    "skills/verify/agents/openai.yaml",
    "agents/reviewer.md",
    "templates/config.json",
]

TEMPLATE_CONFIG = "templates/config.json"
TEMPLATE_PATH_KEYS = ["plan", "status_file", "board_file", "state_dir", "memory_dir", "knowledge_file"]
# The key order of the template before the rename, which must be kept: the top level, then the
# keys of gates[0] and of each nested object listed.
TEMPLATE_TOP_KEYS = ["project", "plan", "status_file", "board_file", "state_dir", "memory_dir", "knowledge_file",
                     "gates", "protected", "no_gates_for", "push", "stop", "auto_snapshot", "context", "roles",
                     "review", "milestones", "mode", "requirements"]
TEMPLATE_FIRST_GATE_KEYS = ["name", "run", "when", "timeout"]
TEMPLATE_NESTED_KEYS = {
    "context": ["warn_pct", "limit_pct", "window"],
    "roles": ["source", "tests", "test_config"],
    "review": ["required_for", "min_changed_lines", "max_rounds", "ignore"],
    "requirements": ["enforce_coverage", "milestone_needs_e2e"],
}

CLI_PATH = "${CLAUDE_PLUGIN_ROOT}/scripts/regista.py"


def skill_md(skill):
    return os.path.join(REPO, "skills", skill, "SKILL.md")


def read_text(rel):
    return read_bytes(os.path.join(REPO, rel)).decode("utf-8")


def renamed_files():
    """Every file under skills/, agents/ and templates/, as paths relative to the repository."""
    found = []
    for top in RENAMED_DIRS:
        for root, dirs, files in os.walk(os.path.join(REPO, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".pyc"):
                    continue
                found.append(os.path.relpath(os.path.join(root, name), REPO).replace(os.sep, "/"))
    return found


def frontmatter_name(text):
    """The `name` field between the first two `---` lines, or None."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        m = re.match(r"^name:\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip("\"'")
    return None


def find_keys(value, wanted, found):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in wanted:
                found.setdefault(k, []).append(v)
            find_keys(v, wanted, found)
    elif isinstance(value, list):
        for v in value:
            find_keys(v, wanted, found)
    return found


class SkillsAgentsTemplatesExist(unittest.TestCase):
    def test_all_eleven_skill_folders_have_skill_md(self):  # covers: DOC-3
        for skill in SKILLS:
            with self.subTest(skill=skill):
                self.assertTrue(os.path.isfile(skill_md(skill)), "skills/%s/SKILL.md is missing" % skill)

    def test_four_agents_exist(self):  # covers: DOC-3
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.assertTrue(os.path.isfile(os.path.join(REPO, "agents", agent + ".md")),
                                "agents/%s.md is missing" % agent)

    def test_crlf_files_exist(self):  # covers: DOC-3
        for rel in CRLF_FILES:
            with self.subTest(file=rel):
                self.assertTrue(os.path.isfile(os.path.join(REPO, rel)), "%s is missing" % rel)


class NoOldNamesLeft(unittest.TestCase):
    longMessage = False  # list file:line for each hit, not the raw list
    def offending_lines(self, pattern, flags=0):
        hits = []
        for rel in renamed_files():
            text = read_bytes(os.path.join(REPO, rel)).decode("utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(pattern, line, flags):
                    hits.append("%s:%d: %s" % (rel, number, line.strip()[:120]))
        return hits

    def test_no_daedalus_in_any_case(self):  # covers: DOC-3
        hits = self.offending_lines("daedalus", re.IGNORECASE)
        self.assertFalse(hits, "%d lines still say daedalus:\n%s" % (len(hits), "\n".join(hits[:40])))

    def test_no_dd_py(self):  # covers: DOC-3
        hits = self.offending_lines(r"\bdd\.py\b")
        self.assertFalse(hits, "%d lines still name dd.py:\n%s" % (len(hits), "\n".join(hits[:40])))


class NewNamesUsed(unittest.TestCase):
    longMessage = False  # show which file lacks which name, not the whole file

    def assertMentions(self, rel, needle):
        self.assertIn(needle, read_text(rel), "%s does not mention %s" % (rel, needle))

    def test_cli_path_in_build_start_save(self):  # covers: DOC-3
        for skill in ["build", "start", "save"]:
            with self.subTest(skill=skill):
                self.assertMentions("skills/%s/SKILL.md" % skill, CLI_PATH)

    def test_build_names_the_regista_agents(self):  # covers: DOC-3
        for agent in ["regista:builder", "regista:test-writer", "regista:reviewer", "regista:researcher"]:
            with self.subTest(agent=agent):
                self.assertMentions("skills/build/SKILL.md", agent)

    def test_build_names_the_regista_folder(self):  # covers: DOC-3
        self.assertMentions("skills/build/SKILL.md", ".regista/")

    def test_codex_commands_in_openai_yaml(self):  # covers: DOC-3
        for skill in ["verify", "change"]:
            with self.subTest(skill=skill):
                self.assertMentions("skills/%s/agents/openai.yaml" % skill, "$regista:%s" % skill)

    def test_oversee_names_slot_variable_and_worker_branch(self):  # covers: DOC-3
        self.assertMentions("skills/oversee/SKILL.md", "REGISTA_SLOT")
        self.assertMentions("skills/oversee/SKILL.md", "regista/<name>")


class SkillFrontmatter(unittest.TestCase):
    def test_name_matches_folder(self):  # covers: DOC-3
        for skill in SKILLS:
            with self.subTest(skill=skill):
                text = read_bytes(skill_md(skill)).decode("utf-8")
                self.assertEqual(frontmatter_name(text), skill,
                                 "the frontmatter name of skills/%s/SKILL.md is not %r" % (skill, skill))


class LineEndingsKept(unittest.TestCase):
    def test_crlf_files_use_crlf_on_every_line(self):  # covers: DOC-3
        for rel in CRLF_FILES:
            with self.subTest(file=rel):
                raw = read_bytes(os.path.join(REPO, rel))
                self.assertTrue(raw.endswith(b"\r\n"), "%s: the last line does not end in CRLF" % rel)
                lines = raw.split(b"\n")[:-1]
                bare = [n for n, line in enumerate(lines, 1) if not line.endswith(b"\r")]
                self.assertEqual(bare, [], "%s: lines without CRLF: %s" % (rel, bare[:10]))

    def test_other_files_use_lf_only(self):  # covers: DOC-3
        offenders = []
        for rel in renamed_files():
            if rel in CRLF_FILES:
                continue
            raw = read_bytes(os.path.join(REPO, rel))
            if b"\r" in raw:
                first = raw[:raw.index(b"\r")].count(b"\n") + 1
                offenders.append("%s (first at line %d)" % (rel, first))
        self.assertFalse(offenders, "files that should be LF contain carriage returns:\n%s"
                         % "\n".join(offenders))


class TemplateConfig(unittest.TestCase):
    def setUp(self):
        self.raw = read_bytes(os.path.join(REPO, TEMPLATE_CONFIG))
        self.text = self.raw.decode("utf-8")
        self.data = json.loads(self.text)

    def test_parses_as_json_object(self):  # covers: DOC-3
        self.assertIsInstance(self.data, dict)

    def test_keeps_its_keys_in_the_same_order(self):  # covers: DOC-3
        self.assertEqual(list(self.data), TEMPLATE_TOP_KEYS,
                         "templates/config.json no longer has the same top-level keys in the same order")
        gates = self.data.get("gates")
        self.assertIsInstance(gates, list)
        self.assertTrue(gates, "templates/config.json has no gates")
        self.assertEqual(list(gates[0]), TEMPLATE_FIRST_GATE_KEYS, "the first gate's keys changed or moved")
        for key, expected in TEMPLATE_NESTED_KEYS.items():
            with self.subTest(key=key):
                self.assertIsInstance(self.data.get(key), dict)
                self.assertEqual(list(self.data[key]), expected, "the keys of %r changed or moved" % key)

    def test_formatted_like_json_dumps_indent_2_with_crlf(self):  # covers: DOC-3
        expected = json.dumps(self.data, indent=2)
        actual = self.text.replace("\r\n", "\n")
        if actual.endswith("\n"):
            actual = actual[:-1]
        self.assertEqual(actual, expected, "templates/config.json is not formatted as 2-space JSON")
        self.assertTrue(self.raw.endswith(b"\r\n"), "templates/config.json does not end in CRLF")
        self.assertNotIn(b"\n", self.raw.replace(b"\r\n", b""), "templates/config.json has bare line feeds")

    def test_paths_use_the_regista_folder(self):  # covers: DOC-3
        found = find_keys(self.data, set(TEMPLATE_PATH_KEYS), {})
        for key in TEMPLATE_PATH_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, found, "templates/config.json has no %r" % key)
                for value in found.get(key, []):
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.startswith(".regista/"),
                                    "%s is %r, which does not start with .regista/" % (key, value))


# --- DOC-4: the README uses the new name, with one line and one section about Daedalus ------------

README = "README.md"
MIGRATE_HEADING = "Moving a project from Daedalus"
TOP_LINES = 15
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def headings(lines):
    """(index, level, text) for every Markdown heading, skipping lines inside fenced code blocks."""
    found, fenced = [], False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = HEADING_RE.match(line)
        if m:
            found.append((i, len(m.group(1)), m.group(2)))
    return found


class Readme(unittest.TestCase):
    longMessage = False  # show the offending lines, not the whole README

    def setUp(self):
        self.raw = read_bytes(os.path.join(REPO, README))
        self.text = self.raw.decode("utf-8")
        self.lines = self.text.split("\n")

    def migrate_section(self):
        """The (start, end) line indexes of the migrate section, heading included, end exclusive; or None."""
        heads = headings(self.lines)
        for n, (i, level, text) in enumerate(heads):
            if text.strip() == MIGRATE_HEADING:
                end = len(self.lines)
                for j, other_level, _ in heads[n + 1:]:
                    if other_level <= level:
                        end = j
                        break
                return i, end
        return None

    def section_text(self):
        span = self.migrate_section()
        self.assertIsNotNone(span, "README.md has no heading %r" % MIGRATE_HEADING)
        return "\n".join(self.lines[span[0]:span[1]])

    def top_daedalus_lines(self):
        """Indexes of the lines among the first 15, outside the migrate section, that mention Daedalus."""
        span = self.migrate_section() or (len(self.lines), len(self.lines))
        return [i for i, line in enumerate(self.lines[:TOP_LINES])
                if re.search("daedalus", line, re.IGNORECASE) and not span[0] <= i < span[1]]

    def test_title_names_regista(self):  # covers: DOC-4
        self.assertIn("Regista", self.lines[0], "the README's first line does not name Regista: %r" % self.lines[0])

    def test_one_line_near_the_top_says_it_was_called_daedalus_until_0_8(self):  # covers: DOC-4
        found = self.top_daedalus_lines()
        self.assertEqual(len(found), 1, "the first %d lines of README.md should mention Daedalus exactly once, "
                         "found %d: %s" % (TOP_LINES, len(found), ["%d: %s" % (i + 1, self.lines[i]) for i in found]))
        line = self.lines[found[0]]
        self.assertIn("Daedalus", line, "line %d does not name Daedalus: %r" % (found[0] + 1, line))
        self.assertIn("0.8", line, "line %d does not say until version 0.8: %r" % (found[0] + 1, line))

    def test_has_the_moving_from_daedalus_section(self):  # covers: DOC-4
        self.assertIsNotNone(self.migrate_section(), "README.md has no heading %r" % MIGRATE_HEADING)

    def test_section_shows_the_migrate_command_and_its_preview(self):  # covers: DOC-4
        section = self.section_text()
        self.assertIn("regista.py migrate", section, "the section does not show `regista.py migrate`")
        self.assertIn("migrate --check", section, "the section does not show `migrate --check`")

    def test_section_says_the_user_says_yes_through_start(self):  # covers: DOC-4
        section = self.section_text()
        self.assertIn("yes", section, "the section does not say the move needs the user's yes")
        self.assertIn("/regista:start", section, "the section does not mention /regista:start")

    def test_no_old_names_outside_the_note_and_the_section(self):  # covers: DOC-4
        allowed = set(self.top_daedalus_lines()[:1])
        span = self.migrate_section()
        if span:
            allowed.update(range(*span))
        hits = ["%d: %s" % (i + 1, line.strip()[:120]) for i, line in enumerate(self.lines)
                if i not in allowed and (re.search("daedalus", line, re.IGNORECASE) or "dd.py" in line)]
        self.assertFalse(hits, "%d README lines still say daedalus or dd.py:\n%s" % (len(hits), "\n".join(hits[:40])))

    def test_mentions_the_new_names(self):  # covers: DOC-4
        for needle in ["regista@regista", "scripts/regista.py", ".regista/", "/regista:build", "regista-builder"]:
            with self.subTest(name=needle):
                self.assertIn(needle, self.text, "README.md does not mention %s" % needle)

    def test_mentions_the_github_address(self):  # covers: DOC-4
        self.assertIn("ciprianrosca/regista", self.text, "README.md does not give github.com/ciprianrosca/regista")

    def test_keeps_lf_endings(self):  # covers: DOC-4
        self.assertNotIn(b"\r", self.raw, "README.md contains carriage returns; it should use LF only")


if __name__ == "__main__":
    unittest.main()
