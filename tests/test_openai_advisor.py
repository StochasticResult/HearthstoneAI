import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openai_advisor import (AdvisorError, OpenAIAdvisor, action_menu, public_view,
                            request_payload, parse_response, MAX_REQUESTS, DEFAULT_MODEL)
from practice_driver import Driver, Stop, NotReady
from test_practice_driver import sample


def state():
    s = sample()
    s.update(end_turn_allowed=True, game_turn="3", visual_turn="3", game_step="MAIN_ACTION", visual_step="MAIN_ACTION")
    return s


def response(action_id="a0", status="completed"):
    return {"status": status, "model": DEFAULT_MODEL, "usage": {"total_tokens": 100}, "output": [{"type": "function_call", "name": "choose_action",
            "arguments": json.dumps({"action_id": action_id, "reason": "test"})}]}


class APITests(unittest.TestCase):
    def test_evaluated_sol_default_preserves_function_contract(self):
        self.assertEqual(DEFAULT_MODEL, "gpt-6.1-sol")
        payload, _ = request_payload(DEFAULT_MODEL, public_view(state()), action_menu(state()))
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertNotIn("temperature", payload)
    def test_only_current_practice_actions_and_targets(self):
        menu = action_menu(state())
        self.assertIn({"kind": "attack", "source_id": 33, "target_id": 47}, menu.values())
        self.assertIn({"kind": "end_turn"}, menu.values())
        for key, value in (("game_type", "GT_CASUAL"), ("own_turn", False), ("options_id", None)):
            s = state(); s[key] = value
            with self.assertRaises(AdvisorError):
                action_menu(s)

    def test_target_required_with_no_valid_target_is_never_targetless(self):
        s = state(); s["legal_options"][0].update(targets=[], requires_target=True)
        self.assertNotIn(14, [a.get("source_id") for a in action_menu(s).values()])

    def test_private_and_unknown_fields_are_not_uploaded(self):
        s = state()
        s.update(enemy_hand=["private-opponent-card"], account_name="private-account", key="private-key")
        s["my_hand"][0]["private_field"] = "private-nested"
        encoded = json.dumps(public_view(s))
        self.assertNotIn("private-", encoded)
        self.assertIn("card_definitions", encoded)

    def test_custom_trial_text_is_resolved_per_observed_entity(self):
        s = state()
        s["my_hand"][0].update(card_id="CAP_405tb3", effect_card_ids=["CAP_405t4", "CAP_405t9"])
        view = public_view(s)
        self.assertEqual(view["my_hand"][0]["custom_text"], "In 4 turns, Draw 3 cards. Summon a Voidlord.")
        self.assertIn("CAP_405t4", view["card_definitions"])

    def test_forced_single_function_and_no_remote_storage(self):
        payload, _ = request_payload("gpt-5.6-terra", public_view(state()), action_menu(state()))
        self.assertIs(payload["store"], False)
        self.assertIs(payload["parallel_tool_calls"], False)
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "choose_action"})
        self.assertTrue(payload["tools"][0]["strict"])

    def test_arbitrary_incomplete_multiple_or_missing_usage_responses_stop(self):
        menu = action_menu(state())
        bad = [response("shell"), response(status="incomplete"), {"status": "completed", "output": []}]
        more = response(); more["output"] *= 2; bad.append(more)
        more = response(); more.pop("usage"); bad.append(more)
        more = response(); more["usage"]["total_tokens"] = True; bad.append(more)
        for value in bad:
            with self.assertRaises(AdvisorError):
                parse_response(value, menu)

    def test_budget_is_checked_before_network_and_one_call_per_decision(self):
        calls = []
        advisor = OpenAIAdvisor({"api_key": "fake-key-not-a-secret-123"}, lambda p: (calls.append(p) or response()))
        self.assertEqual(advisor.ask(state())["kind"], "play")
        self.assertEqual((advisor.calls, advisor.tokens, len(calls)), (1, 100, 1))
        advisor.calls = MAX_REQUESTS
        with self.assertRaises(AdvisorError):
            advisor.ask(state())
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fake-key", json.dumps(calls))

    def test_api_has_no_automatic_retry_or_strategy_fallback(self):
        calls = []
        def fail(payload):
            calls.append(payload)
            raise AdvisorError("simulated failure")
        advisor = OpenAIAdvisor({"api_key": "fake-key-not-a-secret-123"}, fail)
        with self.assertRaises(AdvisorError):
            advisor.ask(state())
        self.assertEqual(len(calls), 1)

    def test_network_timeout_has_fixed_diagnostic_without_provider_body(self):
        advisor = OpenAIAdvisor({"api_key":"fake-key-not-a-secret-123"})
        fake = SimpleNamespace(returncode=2, stdout='{"transport_error":"timeout","body":"private-provider-data"}')
        with patch('openai_advisor.subprocess.run',return_value=fake):
            with self.assertRaisesRegex(AdvisorError,'API network timed out') as error:
                advisor._transport({})
        self.assertNotIn('private',str(error.exception))

    def test_only_end_turn_needs_no_paid_decision(self):
        calls = []
        advisor = OpenAIAdvisor({"api_key": "fake-key-not-a-secret-123"}, lambda p: calls.append(p))
        s = state(); s["legal_options"] = []
        with patch("builtins.print"):
            self.assertEqual(advisor.ask(s), {"kind": "end_turn"})
        self.assertEqual((calls, advisor.calls, advisor.tokens), ([], 0, 0))

    def test_model_mismatch_does_not_execute_a_fallback(self):
        wrong = response(); wrong["model"] = "gpt-5.6-terra"
        advisor = OpenAIAdvisor({"api_key": "fake-key-not-a-secret-123"}, lambda p: wrong)
        with self.assertRaisesRegex(AdvisorError, "model does not match"):
            advisor.ask(state())

    def test_stale_state_stops_before_any_input(self):
        driver = Driver.__new__(Driver)
        driver.api_mode = True
        driver.poll = lambda: None
        old = state()
        newer = copy.deepcopy(old); newer["mana"] = 0
        driver.snapshot = lambda own: newer
        with patch("practice_driver.click") as click, patch("practice_driver.drag") as drag:
            with self.assertRaisesRegex(Stop, "state changed"):
                driver.revalidate(1, old, {"kind": "play", "source_id": 14})
            click.assert_not_called(); drag.assert_not_called()

    def test_brief_unavailable_mirror_is_rechecked_against_unchanged_state(self):
        driver = Driver.__new__(Driver)
        driver.poll = lambda: None
        observed = state()
        driver.snapshot = lambda own: copy.deepcopy(observed)
        driver.identify = lambda status: 1
        with patch("practice_driver.mirror", side_effect=[NotReady("null scene"), {}]) as read, \
             patch("practice_driver.time.sleep"), patch("practice_driver.click") as click:
            driver.revalidate(1, observed, {"kind": "play", "source_id": 14})
            self.assertEqual(read.call_count, 2)
            click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
