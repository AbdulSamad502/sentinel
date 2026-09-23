"""Tests for the Code Risk Analyzer, with no network involved.

The heuristics are pure functions over a list of `ChangedFile`, so all of this
runs offline against mock data (instructions.md #5). The GitHub client is
exercised separately in `test_github.py`, and the live check against a real
public repo is `tests/live_pr_check.py`.

    python -m unittest tests.test_code_risk -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.code_risk.analyzer import (
    ChangeRisk,
    RiskLevel,
    assess_change,
    assess_file,
    highest,
    is_source_file,
    is_test_file,
    sensitive_areas,
)
from sentinel.code_risk.config import RiskConfig, load_risk_config
from sentinel.shared.config_file import ConfigError
from sentinel.shared.github import ChangedFile

REPO_CONFIG = Path(__file__).resolve().parent.parent / "sentinel.config.json"


def changed(path: str, additions: int = 5, deletions: int = 2, status: str = "modified") -> ChangedFile:
    return ChangedFile(path=path, status=status, additions=additions, deletions=deletions)


class TestFileClassification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_risk_config(REPO_CONFIG)

    def test_recognises_test_files(self):
        for path in ("tests/test_auth.py", "test_main.py", "src/foo_test.go", "app/user.spec.ts"):
            with self.subTest(path=path):
                self.assertTrue(is_test_file(path, self.config))

    def test_does_not_mistake_source_for_tests(self):
        for path in ("src/main.py", "app/latest.js", "docs/contest.md"):
            with self.subTest(path=path):
                self.assertFalse(is_test_file(path, self.config))

    def test_recognises_source_files(self):
        self.assertTrue(is_source_file("src/main.py", self.config))
        self.assertTrue(is_source_file("app/index.tsx", self.config))

    def test_docs_and_data_are_not_source(self):
        for path in ("README.md", "data/users.csv", "assets/logo.png"):
            with self.subTest(path=path):
                self.assertFalse(is_source_file(path, self.config))


class TestSensitiveAreas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_risk_config(REPO_CONFIG)

    def test_payment_code_is_sensitive(self):
        self.assertIn("payments", sensitive_areas("src/payments/charge.py", self.config))

    def test_auth_code_is_sensitive(self):
        self.assertIn("authentication", sensitive_areas("app/auth/login.py", self.config))

    def test_migrations_are_sensitive(self):
        self.assertIn("data loss", sensitive_areas("db/migrations/003_drop.sql", self.config))

    def test_ordinary_code_is_not_sensitive(self):
        self.assertEqual(sensitive_areas("src/utils/strings.py", self.config), [])

    def test_stylesheets_are_not_treated_as_permissions_code(self):
        # Found live against django/django: 'admin/css/widgets.css' was scored
        # HIGH purely because 'admin' appears in the path.
        for path in (
            "django/contrib/admin/static/admin/css/widgets.css",
            "docs/auth.md",
            "assets/login-icon.svg",
        ):
            with self.subTest(path=path):
                self.assertEqual(sensitive_areas(path, self.config), [])

    def test_real_code_in_the_same_directory_is_still_sensitive(self):
        # The exemption must be about the file type, not the directory.
        self.assertIn("permissions", sensitive_areas("django/contrib/admin/options.py", self.config))

    def test_a_file_can_belong_to_several_areas(self):
        areas = sensitive_areas("src/auth/billing_config.py", self.config)
        self.assertIn("authentication", areas)
        self.assertIn("payments", areas)


class TestFileScoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_risk_config(REPO_CONFIG)

    def test_ordinary_small_change_is_low_risk(self):
        risk = assess_file(changed("src/utils/strings.py"), self.config)
        self.assertIs(risk.level, RiskLevel.LOW)
        self.assertEqual(risk.reasons, [])

    def test_sensitive_path_is_high_risk(self):
        risk = assess_file(changed("src/payments/charge.py"), self.config)
        self.assertIs(risk.level, RiskLevel.HIGH)
        self.assertIn("payments", risk.describe())

    def test_large_diff_is_medium_risk(self):
        risk = assess_file(changed("src/big.py", additions=300, deletions=10), self.config)
        self.assertIs(risk.level, RiskLevel.MEDIUM)

    def test_enormous_diff_is_high_risk(self):
        risk = assess_file(changed("src/huge.py", additions=900, deletions=0), self.config)
        self.assertIs(risk.level, RiskLevel.HIGH)

    def test_deletion_is_called_out(self):
        risk = assess_file(changed("src/gone.py", status="removed"), self.config)
        self.assertIs(risk.level, RiskLevel.MEDIUM)
        self.assertIn("deleted", risk.describe())

    def test_high_churn_is_called_out(self):
        risk = assess_file(changed("src/unstable.py"), self.config, churn=25)
        self.assertIs(risk.level, RiskLevel.MEDIUM)
        self.assertIn("unstable", risk.describe())

    def test_churn_at_the_lookback_cap_is_reported_as_a_minimum(self):
        # We only look back `churn_lookback` commits, so hitting that number
        # means "at least", not "exactly". Stating it as exact would be wrong.
        risk = assess_file(changed("src/hot.py"), self.config, churn=self.config.churn_lookback)
        self.assertIn(f"at least {self.config.churn_lookback}", risk.describe())

    def test_churn_below_the_cap_is_reported_exactly(self):
        risk = assess_file(changed("src/warm.py"), self.config, churn=12)
        self.assertIn("changed in 12 recent commits", risk.describe())

    def test_low_churn_is_not_called_out(self):
        risk = assess_file(changed("src/stable.py"), self.config, churn=2)
        self.assertIs(risk.level, RiskLevel.LOW)

    def test_unknown_churn_does_not_invent_a_finding(self):
        # churn=None means "we couldn't check", which must not read as "fine".
        risk = assess_file(changed("src/stable.py"), self.config, churn=None)
        self.assertEqual(risk.reasons, [])

    def test_worst_finding_decides_but_all_are_reported(self):
        risk = assess_file(
            changed("src/payments/charge.py", additions=400, deletions=0), self.config, churn=30
        )
        self.assertIs(risk.level, RiskLevel.HIGH)
        self.assertEqual(len(risk.reasons), 3)


class TestMissingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_risk_config(REPO_CONFIG)

    def test_source_change_with_no_test_is_flagged(self):
        risk = assess_change([changed("src/main.py")], self.config)
        self.assertIs(risk.level, RiskLevel.MEDIUM)
        self.assertIn("no test file touched", risk.summary())

    def test_source_change_with_a_test_is_not_flagged(self):
        risk = assess_change([changed("src/main.py"), changed("tests/test_main.py")], self.config)
        self.assertIs(risk.level, RiskLevel.LOW)

    def test_docs_only_change_is_not_asked_for_tests(self):
        risk = assess_change([changed("README.md"), changed("docs/guide.md")], self.config)
        self.assertIs(risk.level, RiskLevel.LOW)
        self.assertEqual(risk.reasons, [])

    def test_test_only_change_is_not_asked_for_tests(self):
        risk = assess_change([changed("tests/test_main.py")], self.config)
        self.assertIs(risk.level, RiskLevel.LOW)


class TestChangeScale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_risk_config(REPO_CONFIG)

    def test_many_files_is_flagged(self):
        files = [changed(f"src/mod_{i}.py") for i in range(30)] + [changed("tests/test_all.py")]
        self.assertIn("hard to review as a unit", assess_change(files, self.config).summary())

    def test_large_total_diff_is_flagged(self):
        files = [
            changed("src/a.py", additions=600, deletions=0),
            changed("tests/test_a.py", additions=500, deletions=0),
        ]
        self.assertIn("1100 lines changed in total", assess_change(files, self.config).summary())

    def test_empty_change_is_low_risk_and_says_so(self):
        risk = assess_change([], self.config)
        self.assertIs(risk.level, RiskLevel.LOW)
        self.assertIn("no files changed", risk.reasons)


class TestReporting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_risk_config(REPO_CONFIG)

    def test_notable_files_are_worst_first_and_exclude_low_risk(self):
        risk = assess_change(
            [
                changed("src/utils/plain.py"),
                changed("src/big.py", additions=350),
                changed("src/payments/charge.py"),
                changed("tests/test_all.py"),
            ],
            self.config,
        )
        notable = risk.notable_files
        self.assertIs(notable[0].level, RiskLevel.HIGH)
        self.assertTrue(all(f.level is not RiskLevel.LOW for f in notable))

    def test_unchecked_signals_are_surfaced_not_hidden(self):
        # instructions.md #7: a failed check degrades to "unable to verify",
        # it must never silently read as "nothing found".
        risk = assess_change([changed("src/main.py")], self.config, unchecked=["churn history (rate limited)"])
        self.assertIn("could not check", risk.summary())

    def test_summary_mentions_the_file_count(self):
        risk = assess_change([changed("a.py"), changed("b.py")], self.config)
        self.assertIn("2 changed file(s)", risk.summary())


class TestRiskOrdering(unittest.TestCase):
    def test_highest_of(self):
        self.assertIs(highest([RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.MEDIUM]), RiskLevel.HIGH)
        self.assertIs(highest([]), RiskLevel.LOW)


class TestRiskConfigLoading(unittest.TestCase):
    def test_repo_config_loads(self):
        config = load_risk_config(REPO_CONFIG)
        self.assertIn("payments", config.sensitive_paths)
        self.assertIn(".py", config.source_extensions)
        self.assertGreater(config.large_file_changes, 0)

    def test_defaults_apply_when_the_section_is_absent(self):
        import json
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write(json.dumps({}))
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))

        config = load_risk_config(Path(handle.name))
        self.assertEqual(config.large_file_changes, RiskConfig().large_file_changes)

    def test_a_nonsense_threshold_is_rejected_with_the_key_name(self):
        import json
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write(json.dumps({"code_risk": {"thresholds": {"many_files": -3}}}))
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))

        with self.assertRaises(ConfigError) as caught:
            load_risk_config(Path(handle.name))
        self.assertIn("many_files", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
