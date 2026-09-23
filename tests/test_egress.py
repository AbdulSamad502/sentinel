"""What may and may not leave the machine in a prompt.

Both guards here exist because the model provider is swappable: on `bedrock`,
`agentcore` or any hosted API the prompt crosses a network to somebody else's
computer. The verdict never does - it is computed offline from the change record
- so these tests are about confidentiality and spend, not correctness.

    python -m unittest tests.test_egress -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel import llm
from sentinel.action_monitor.actions import Verdict
from sentinel.explainer.config import ExplainConfig
from sentinel.explainer.diff import MODIFIED, FileChange, diff_block, is_secret_path
from sentinel.explainer.explain import session_prompt
from sentinel.norms.checker import statement_prompt
from sentinel.norms.norms import Norm

SECRET = "sk-live-51H8xQvNever0nTheWire"
OLD_SECRET = "sk-live-oldkeyvaluethatalsomustnotleak"

ENV_CHANGE = FileChange(
    path=".env",
    status=MODIFIED,
    before=f"STRIPE_KEY={OLD_SECRET}\n",
    after=f"STRIPE_KEY={SECRET}\n",
    added_lines=[f"STRIPE_KEY={SECRET}"],
    removed_lines=[f"STRIPE_KEY={OLD_SECRET}"],
    diff=f"--- before/.env\n+++ after/.env\n-STRIPE_KEY={OLD_SECRET}\n+STRIPE_KEY={SECRET}\n",
)

ORDINARY_CHANGE = FileChange(
    path="src/checkout.py",
    status=MODIFIED,
    before="total = 1\n",
    after="total = 2\n",
    added_lines=["total = 2"],
    removed_lines=["total = 1"],
    diff="--- before/src/checkout.py\n+++ after/src/checkout.py\n-total = 1\n+total = 2\n",
)


class TestSecretPathsAreRecognised(unittest.TestCase):
    def test_the_usual_secret_carriers(self):
        for path in (
            ".env",
            ".env.production",
            "config/.env",
            "deploy/server.pem",
            "keys/id_rsa",
            "app/secrets.yaml",
            "infra/secrets/db.yml",
            "aws_credentials.csv",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_secret_path(path), f"{path} should be withheld")

    def test_ordinary_source_is_not_withheld(self):
        for path in ("src/checkout.py", "README.md", "dashboard/src/api.ts", "keyboard.py"):
            with self.subTest(path=path):
                self.assertFalse(is_secret_path(path), f"{path} should be shown")


class TestSecretsNeverReachAPrompt(unittest.TestCase):
    """The values are withheld; the fact of the change is not."""

    def test_the_block_withholds_the_values_but_names_the_file(self):
        block = diff_block(ENV_CHANGE, 120)
        self.assertNotIn(SECRET, block)
        self.assertNotIn(OLD_SECRET, block)
        self.assertIn(".env", block)
        self.assertIn(MODIFIED, block)

    def test_session_narration_withholds_them(self):
        prompt = session_prompt([ENV_CHANGE, ORDINARY_CHANGE], [], ExplainConfig())
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn(OLD_SECRET, prompt)
        # The unremarkable file is still shown in full, or the feature is useless.
        self.assertIn("total = 2", prompt)

    def test_the_norm_judgement_withholds_them(self):
        """The verdict path - the one reached by 'can we ship?'."""
        # No patterns, so this norm is judged by the model - which is what sends the diff.
        norms = [Norm(id="no-secrets", statement="Never commit a secret.", severity=Verdict.BLOCKED)]
        prompt = statement_prompt([ENV_CHANGE, ORDINARY_CHANGE], norms, 120)
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn(OLD_SECRET, prompt)
        self.assertIn(".env", prompt)


class TestEveryProviderInheritsThePromptCap(unittest.TestCase):
    def test_an_oversized_prompt_is_refused_before_a_provider_is_chosen(self):
        """Refused for `bedrock` too, which had no cap of its own."""
        with self.assertRaises(llm.ModelSetupError) as caught:
            llm.run_agent("system", "x" * (llm.MAX_PROMPT_CHARS + 1))
        self.assertIn("caps one model call", str(caught.exception))

    def test_the_cap_is_one_number(self):
        from sentinel import remote

        self.assertEqual(remote.MAX_PROMPT_CHARS, llm.MAX_PROMPT_CHARS)


if __name__ == "__main__":
    unittest.main()
