"""Tests for project norms: the catalog, the config, and both checkers.

The catalog tests are the important ones. Every built-in pattern is asserted
against a line it must catch *and* a line it must not, because this repo has
already shipped one keyword that matched the wrong thing (`*admin*` scoring
`admin/css/widgets.css` as permissions code) and nobody noticed until it ran
against a real repo. Each "must not match" case below is a real false positive
found by auditing the catalog against botocore, sqlalchemy and numpy.

The model path is tested with a stub, so the suite stays offline and fast.

    python -m unittest tests.test_norms -v
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.action_monitor.actions import Verdict
from sentinel.explainer.diff import ADDED, DELETED, MODIFIED, to_change
from sentinel.norms import catalog, checker
from sentinel.norms.config import load_norms
from sentinel.norms.norms import BY_MODEL, BY_PATTERN, Norm, NormFinding, worst_severity
from sentinel.shared.config_file import ConfigError

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


def change(path: str, added: list[str], status: str = MODIFIED):
    """A FileChange whose diff really does contain `added` as added lines.

    Built through a real unified diff rather than by setting the fields
    directly, so the line numbers the checker reports are exercised too.
    """
    first = "unchanged first line\n"
    before = None if status == ADDED else first
    after = None if status == DELETED else (first if status == MODIFIED else "") + "\n".join(added) + "\n"
    return to_change(path, before, after)


# Each entry: norm id -> (lines it must catch, lines it must leave alone).
PATTERN_CASES = {
    "no-hardcoded-credentials": (
        [
            'api = "sk-abcdefghij0123456789XYZ"',
            'AWS_KEY = "AKIA1B2C3D4E5F6G7H8I"',
            'tok = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
        [
            # AWS ships thousands of these in its SDK fixture data.
            '"AccessKeyId": "AKIAIOSFODNN7EXAMPLE",',
            'key = os.environ["API_KEY"]',
            'sk = "sk-short"',
        ],
    ),
    "no-secrets-assigned-inline": (
        ['api_key = "abcdef1234567890xyz"', 'client_secret: "s3cr3tvalue-aaaa"'],
        [
            # botocore: constants naming an env var, not holding a secret.
            "SECRET_KEY = 'AWS_SECRET_ACCESS_KEY'",
            "SECRET_KEY = 'aws_secret_access_key'",
            "SECRET_KEY = 'AWSSecretKey'",
            'api_key = os.environ["K"]',
            'password = "short"',
        ],
    ),
    "no-disabled-certificate-checks": (
        [
            "requests.get(url, verify=False)",
            "session.post(url, data=d, verify=False)",
            "rejectUnauthorized: false",
            "InsecureSkipVerify: true",
            "curl -k https://example.com",
        ],
        [
            # numpy: an unrelated `verify` parameter. This norm is BLOCKED, so
            # matching this would mean a false STOP.
            "array_function_dispatch(_dispatcher, verify=False, module='numpy')",
            "requests.get(url, verify=True)",
        ],
    ),
    "no-wildcard-cors": (
        ['"Access-Control-Allow-Origin": "*"', 'allow_origins=["*"]'],
        ['allow_origins=["https://a.com"]'],
    ),
    "no-hardcoded-model-ids": (
        ['model = "gpt-4o"', 'MODEL="claude-opus-4"', 'm = "gemini-1.5-pro"', 'x = "llama-3.1-8b"'],
        ["model = settings.MODEL", "# we support gpt style models", 'name = "gpt-turbo"'],
    ),
    "no-debug-output": (
        ['  console.log("x")', "    debugger;", "breakpoint()", "import pdb; pdb.set_trace()"],
        ['logger.log("x")', 'logging.debug("x")'],
    ),
    "no-unnamed-error-catching": (
        ["except:", "    except:  # noqa", "} catch (e) {}"],
        # sqlalchemy has 27 bare excepts, most of them legitimate re-raises,
        # which is why the statement says "without naming them" and not
        # "silently swallowed".
        ["except ValueError:", "except Exception as exc:"],
    ),
    "no-skipped-tests": (
        ['it.skip("x")', "@pytest.mark.skip", '@unittest.skip("why")', 't.Skip("x")'],
        ['it("works")', "# do not skip this"],
    ),
}


class TestEveryCatalogPatternIsAudited(unittest.TestCase):
    def test_every_pattern_norm_has_a_case_in_this_file(self):
        """A new pattern must arrive with evidence that it behaves."""
        missing = [n.id for n in catalog.all_norms() if n.is_deterministic and n.id not in PATTERN_CASES]
        self.assertEqual(missing, [], "add positive and negative cases for these before trusting them")

    def test_patterns_catch_what_they_claim_to(self):
        for norm in catalog.all_norms():
            if not norm.is_deterministic:
                continue
            expressions = [re.compile(pattern) for pattern in norm.patterns]
            for line in PATTERN_CASES[norm.id][0]:
                with self.subTest(norm=norm.id, line=line):
                    self.assertTrue(any(e.search(line) for e in expressions))

    def test_patterns_leave_alone_the_false_positives_found_in_real_repos(self):
        for norm in catalog.all_norms():
            if not norm.is_deterministic:
                continue
            expressions = [re.compile(pattern) for pattern in norm.patterns]
            for line in PATTERN_CASES[norm.id][1]:
                with self.subTest(norm=norm.id, line=line):
                    self.assertFalse(any(e.search(line) for e in expressions))

    def test_every_norm_has_a_statement_a_human_can_read(self):
        for norm in catalog.all_norms():
            with self.subTest(norm=norm.id):
                self.assertTrue(norm.statement.strip().endswith("."), "statements are sentences")
                self.assertGreater(len(norm.statement.split()), 4)

    def test_ids_are_unique(self):
        ids = [norm.id for norm in catalog.all_norms()]
        self.assertEqual(len(ids), len(set(ids)))


class TestPatternChecking(unittest.TestCase):
    def test_a_violation_is_reported_with_its_line_and_evidence(self):
        norm = Norm(id="no-models", statement="No model ids.", patterns=[r"gpt-4"])
        findings = checker.check_patterns([change("src/a.py", ['MODEL = "gpt-4o"'])], [norm])

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].norm_id, "no-models")
        self.assertEqual(findings[0].source, BY_PATTERN)
        self.assertEqual(findings[0].line, 2)
        self.assertIn("gpt-4o", findings[0].evidence)

    def test_only_added_lines_are_blamed(self):
        """Inherited debt is not this change's fault."""
        norm = Norm(id="no-models", statement="No model ids.", patterns=[r"gpt-4"])
        # The violation was already there; this change only appended a line.
        inherited = to_change("src/a.py", 'M = "gpt-4o"\n', 'M = "gpt-4o"\nx = 1\n')
        self.assertEqual(checker.check_patterns([inherited], [norm]), [])

    def test_a_deleted_file_adds_nothing_so_breaks_nothing(self):
        norm = Norm(id="no-models", statement="No model ids.", patterns=[r"gpt-4"])
        deleted = change("src/a.py", [], status=DELETED)
        self.assertEqual(checker.check_patterns([deleted], [norm]), [])

    def test_one_finding_per_norm_per_file(self):
        norm = Norm(id="no-logs", statement="No logs.", patterns=[r"console\.log"])
        noisy = change("src/a.py", ["console.log(1)", "console.log(2)", "console.log(3)"])
        self.assertEqual(len(checker.check_patterns([noisy], [norm])), 1)

    def test_except_paths_win_over_applies_to(self):
        norm = Norm(
            id="no-logs",
            statement="No logs.",
            patterns=[r"console\.log"],
            applies_to=["src/*"],
            except_paths=["src/vendor/*"],
        )
        vendored = change("src/vendor/lib.js", ["console.log(1)"])
        self.assertEqual(checker.check_patterns([vendored], [norm]), [])

    def test_applies_to_limits_the_norm_to_named_paths(self):
        norm = Norm(id="no-logs", statement="No logs.", patterns=[r"console\.log"], applies_to=["frontend/*"])
        self.assertEqual(checker.check_patterns([change("backend/a.js", ["console.log(1)"])], [norm]), [])
        self.assertEqual(len(checker.check_patterns([change("frontend/a.js", ["console.log(1)"])], [norm])), 1)

    def test_statement_only_norms_are_not_pattern_checked(self):
        norm = Norm(id="ask-first", statement="Ask before redesigning.")
        self.assertEqual(checker.check_patterns([change("src/a.py", ["anything"])], [norm]), [])

    def test_severity_comes_from_the_norm(self):
        norm = Norm(id="hard", statement="Never.", severity=Verdict.BLOCKED, patterns=["boom"])
        findings = checker.check_patterns([change("src/a.py", ["boom"])], [norm])
        self.assertEqual(findings[0].severity, Verdict.BLOCKED)

    def test_checking_is_deterministic(self):
        norms = catalog.all_norms()
        changes = [change("src/a.py", ['MODEL = "gpt-4o"', "console.log(1)"])]
        first = [f.describe() for f in checker.check_patterns(changes, norms)]
        second = [f.describe() for f in checker.check_patterns(changes, norms)]
        self.assertEqual(first, second)


class TestTheModelIsNeverTrustedBlindly(unittest.TestCase):
    """Everything the model reports is checked against the change we saw."""

    def setUp(self):
        self.norms = [Norm(id="ask-first", statement="Ask before redesigning.", severity=Verdict.FLAGGED)]
        self.changes = [change("src/a.py", ["x = 1"])]

    def test_a_finding_about_an_untouched_file_is_dropped(self):
        reported = [{"norm_id": "ask-first", "path": "src/never_touched.py", "evidence": "x"}]
        self.assertEqual(checker._verify(reported, self.norms, self.changes), [])

    def test_a_finding_about_an_unknown_norm_is_dropped(self):
        reported = [{"norm_id": "invented-rule", "path": "src/a.py", "evidence": "x"}]
        self.assertEqual(checker._verify(reported, self.norms, self.changes), [])

    def test_a_valid_finding_survives_and_is_tagged_as_the_models(self):
        reported = [{"norm_id": "ask-first", "path": "src/a.py", "evidence": "x = 1"}]
        findings = checker._verify(reported, self.norms, self.changes)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].source, BY_MODEL)
        self.assertEqual(findings[0].severity, Verdict.FLAGGED)

    def test_the_model_cannot_choose_the_severity(self):
        """Severity is human-authored config. The model only reports a break."""
        reported = [{"norm_id": "ask-first", "path": "src/a.py", "evidence": "x", "severity": "BLOCKED"}]
        findings = checker._verify(reported, self.norms, self.changes)
        self.assertEqual(findings[0].severity, Verdict.FLAGGED)

    def test_duplicate_findings_collapse(self):
        reported = [
            {"norm_id": "ask-first", "path": "src/a.py", "evidence": "x"},
            {"norm_id": "ask-first", "path": "src/a.py", "evidence": "x again"},
        ]
        self.assertEqual(len(checker._verify(reported, self.norms, self.changes)), 1)


class TestReadingTheModelsAnswer(unittest.TestCase):
    def test_a_plain_json_array_is_read(self):
        self.assertEqual(checker._parse_findings('[{"norm_id": "a"}]'), [{"norm_id": "a"}])

    def test_an_empty_array_means_nothing_was_broken(self):
        self.assertEqual(checker._parse_findings("[]"), [])

    def test_a_code_fence_is_tolerated(self):
        """Small local models add fences however firmly the prompt says not to."""
        self.assertEqual(checker._parse_findings('```json\n[{"norm_id": "a"}]\n```'), [{"norm_id": "a"}])

    def test_surrounding_chatter_is_tolerated(self):
        self.assertEqual(checker._parse_findings('Sure!\n[{"norm_id": "a"}]\nHope that helps'), [{"norm_id": "a"}])

    def test_unparseable_output_is_not_silently_an_empty_result(self):
        """None means "could not check", which caps the verdict. [] means "clean"."""
        self.assertIsNone(checker._parse_findings("I could not analyse this."))
        self.assertIsNone(checker._parse_findings(""))


class TestStatementCheckingDegradesHonestly(unittest.TestCase):
    def test_no_statement_norms_means_nothing_to_ask_and_nothing_unchecked(self):
        norms = [Norm(id="p", statement="Patterned.", patterns=["x"])]
        findings, unchecked = checker.check_statements([change("a.py", ["x"])], norms)
        self.assertEqual(findings, [])
        self.assertEqual(unchecked, [])

    def test_no_changes_means_no_model_call(self):
        norms = [Norm(id="ask-first", statement="Ask first.")]
        findings, unchecked = checker.check_statements([], norms)
        self.assertEqual((findings, unchecked), ([], []))

    def test_a_model_failure_lands_in_unchecked_naming_the_norms(self):
        """It must never look like the norms were checked and found clean."""
        norms = [Norm(id="ask-first", statement="Ask first.")]
        import sentinel.norms.checker as module

        original = module.statement_prompt
        module.statement_prompt = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model exploded"))
        try:
            findings, unchecked = module.check_statements([change("a.py", ["x"])], norms)
        finally:
            module.statement_prompt = original

        self.assertEqual(findings, [])
        self.assertEqual(len(unchecked), 1)
        self.assertIn("ask-first", unchecked[0])

    def test_the_prompt_shows_the_rules_and_the_diff(self):
        norms = [Norm(id="ask-first", statement="Ask before redesigning.")]
        prompt = checker.statement_prompt([change("src/a.py", ["x = 1"])], norms, 120)
        self.assertIn("ask-first", prompt)
        self.assertIn("Ask before redesigning.", prompt)
        self.assertIn("src/a.py", prompt)


class TestCorrectionRequest(unittest.TestCase):
    def test_it_names_the_rule_the_file_and_the_line(self):
        finding = NormFinding(
            norm_id="no-models",
            statement="Model ids belong in config.",
            path="src/a.py",
            severity=Verdict.FLAGGED,
            evidence='MODEL = "gpt-4o"',
            source=BY_PATTERN,
            line=12,
        )
        text = checker.correction_request([finding])
        self.assertIn("src/a.py:12", text)
        self.assertIn("Model ids belong in config.", text)
        self.assertIn("gpt-4o", text)

    def test_nothing_to_correct_reads_as_such(self):
        self.assertIn("Nothing to correct", checker.correction_request([]))

    def test_it_is_ascii_for_the_windows_console(self):
        finding = NormFinding("n", "Rule.", "a.py", Verdict.FLAGGED, "x", BY_PATTERN, 1)
        checker.correction_request([finding]).encode("ascii")


class TestNormConfig(unittest.TestCase):
    def _write(self, body: str) -> Path:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "sentinel.config.json"
        path.write_text(body, encoding="utf-8")
        return path

    def test_the_repos_own_config_loads(self):
        norms = load_norms(REPO_CONFIG)
        self.assertTrue(norms, "the shipped config should carry a starter set of norms")
        for norm in norms:
            self.assertIn(norm.severity, {Verdict.FLAGGED, Verdict.BLOCKED})

    def test_a_missing_section_means_no_norms(self):
        self.assertEqual(load_norms(self._write("{}")), [])

    def test_a_norm_without_a_statement_is_rejected_by_name(self):
        path = self._write('{"norms": {"rules": [{"id": "x", "patterns": ["a"]}]}}')
        with self.assertRaises(ConfigError) as caught:
            load_norms(path)
        self.assertIn("'x'", str(caught.exception))
        self.assertIn("statement", str(caught.exception))

    def test_bad_regex_is_reported_against_the_norm_that_owns_it(self):
        path = self._write('{"norms": {"rules": [{"id": "x", "statement": "No.", "patterns": ["([unclosed"]}]}}')
        with self.assertRaises(ConfigError) as caught:
            load_norms(path)
        self.assertIn("'x'", str(caught.exception))
        self.assertIn("regex", str(caught.exception))

    def test_allowed_is_not_a_severity_a_norm_can_have(self):
        """A norm that is fine to break is not a norm."""
        path = self._write('{"norms": {"rules": [{"id": "x", "statement": "No.", "severity": "ALLOWED"}]}}')
        with self.assertRaises(ConfigError) as caught:
            load_norms(path)
        self.assertIn("FLAGGED", str(caught.exception))

    def test_duplicate_ids_are_rejected(self):
        path = self._write('{"norms": {"rules": [{"id": "x", "statement": "A."}, {"id": "x", "statement": "B."}]}}')
        with self.assertRaises(ConfigError):
            load_norms(path)

    def test_severity_is_case_insensitive(self):
        path = self._write('{"norms": {"rules": [{"id": "x", "statement": "No.", "severity": "blocked"}]}}')
        self.assertEqual(load_norms(path)[0].severity, Verdict.BLOCKED)


class TestWorstSeverity(unittest.TestCase):
    def test_no_findings_is_allowed(self):
        self.assertEqual(worst_severity([]), Verdict.ALLOWED)

    def test_blocked_beats_flagged(self):
        findings = [
            NormFinding("a", "A.", "x.py", Verdict.FLAGGED, "e", BY_PATTERN),
            NormFinding("b", "B.", "y.py", Verdict.BLOCKED, "e", BY_PATTERN),
        ]
        self.assertEqual(worst_severity(findings), Verdict.BLOCKED)


if __name__ == "__main__":
    unittest.main()
