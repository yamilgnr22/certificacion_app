from __future__ import annotations

import os
import unittest
from pathlib import Path

from services.agent_prompt import build_system_prompt

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "agent_system_prompt.txt"


class PromptSnapshotTest(unittest.TestCase):
    """F3-T1: el system prompt no debe cambiar por accidente.

    Si el cambio es intencional, regenerar el snapshot con:
        $env:UPDATE_SNAPSHOTS = "1"; python -m pytest tests/test_prompt_snapshot.py
    y revisar el diff del snapshot en el commit.
    """

    def test_system_prompt_matches_snapshot(self):
        current = build_system_prompt()
        if os.getenv("UPDATE_SNAPSHOTS", "").strip() == "1":
            SNAPSHOT_PATH.write_text(current, encoding="utf-8")
        expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            current,
            expected,
            "El system prompt cambio. Si es intencional, regenera el snapshot "
            "(UPDATE_SNAPSHOTS=1) y revisa el diff en el commit.",
        )

    def test_prompt_covers_critical_routing_cases(self):
        prompt = build_system_prompt()
        # Cada intent de mutacion/plan debe estar documentado en el prompt.
        for needle in [
            "target_balance_adjustment",
            "plan_multi_target_balance",
            "plan_non_negative_account",
            "plan_target_utility",
            "plan_multi_account_target_balance",
            "plan_compound_constraints",
            "monthly_override",
            "journal_entry",
            "repeat_last",
        ]:
            self.assertIn(needle, prompt, f"falta {needle} en el prompt")
        # Las distinciones ambiguas deben seguir explicadas.
        self.assertIn("oscile alrededor de", prompt)
        self.assertIn("no baje de", prompt)
        self.assertIn("EJEMPLOS", prompt)


if __name__ == "__main__":
    unittest.main()
