import copy
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from incremental_board import IncrementalBoard, UnsafeState
from openai_advisor import OpenAIAdvisor, CATALOG, public_view, enrich, action_menu, AdvisorError, request_payload
from strategy import PACK, matchup_context, deck_context
from strategy_eval import cases, evaluate
from evaluation_session import consume_request
from test_incremental_board import fixture, power
from test_openai_advisor import response, state


class StrategyTests(unittest.TestCase):
    def test_reference_complete_and_every_linked_card_has_real_text(self):
        self.assertEqual(sum(PACK["deck"]["cards"].values()), 30)
        linked = set(PACK["deck"]["cards"])
        for profile in PACK["matchups"]:
            linked.update(profile["signals"] + profile["watch_cards"])
            self.assertTrue(profile["source"].startswith("https://www.vicioussyndicate.com/decks/"))
        self.assertFalse(linked - CATALOG.keys())

    def test_practice_never_inherits_ranked_archetype(self):
        s = state(); s["enemy_hero"]["card_id"] = "HERO_09"
        s["opponent_public_cards"] = [{"card_id": "JAIL_432"}]
        self.assertEqual(matchup_context(s, date(2026,11,1))["candidates"], [])

    def test_class_evidence_generated_cards_and_expiry(self):
        s = state(); s.update(game_type="GT_RANKED", opponent_public_cards=[{"card_id":"JAIL_432"}])
        s["enemy_hero"]["card_id"] = "HERO_09"
        result = matchup_context(s, date(2026,11,1))
        self.assertEqual(result["candidates"][0]["id"], "thief_priest")
        self.assertEqual(result["candidates"][0]["evidence"], ["JAIL_432"])
        s["opponent_public_cards"][0]["generated"] = True
        self.assertTrue(all(not c["evidence"] for c in matchup_context(s, date(2026,11,1))["candidates"]))
        self.assertEqual(matchup_context(s, date(2026,11,10))["candidates"], [])
        self.assertTrue(matchup_context(s, date(2026,11,10))["stale"])
        s["client_build"] = "37.2.changed"
        self.assertFalse(matchup_context(s,date(2026,11,1))["build_compatible"])
        self.assertEqual(matchup_context(s,date(2026,11,1))["candidates"],[])

    def test_reference_unseen_counts_are_not_claimed_exact(self):
        s = state(); s["own_seen_original"] = [{"id":4,"card_id":"UNG_205"}]
        ctx = deck_context(s)
        self.assertEqual(ctx["unseen_reference_copies"]["CORE_UNG_205"], 1)
        self.assertIn("Not exact", ctx["count_warning"])
        self.assertIn("not yet verified", ctx["verification"])

    def test_selected_deck_verified_locally_without_name_or_account_id_upload(self):
        advisor = OpenAIAdvisor({"api_key":"fake-key-not-a-secret-123"})
        selected = {"id":3384315883,"name":"private-deck-name", "cards":
                    [{"cardId":cid,"count":n} for cid,n in PACK["deck"]["cards"].items()]}
        with patch("builtins.print"): advisor.observe_deck(selected)
        view = advisor.view(state())
        self.assertEqual(sum(view["starting_deck"].values()),30)
        self.assertIn("30 cards read",view["strategy"]["deck"]["verification"])
        self.assertNotIn("private-deck-name",json.dumps(view)); self.assertNotIn("3384315883",json.dumps(view))
        wrong = copy.deepcopy(selected); wrong["cards"][0]["count"] = 0
        with self.assertRaises(AdvisorError): advisor.observe_deck(wrong)
        with self.assertRaises(AdvisorError): advisor.observe_deck({**selected,"id":123})

    def test_visible_weapon_and_modifiers_include_no_hidden_enemy_attachment(self):
        p = IncrementalBoard()
        data = fixture()
        for eid, cid, ctype, attached, owner in ((40,"BE_771","WEAPON",None,2),
                (41,"TIME_026e","ENCHANTMENT",20,1),(42,"ENEMY_PRIVATE_EFFECT","ENCHANTMENT",21,2)):
            data += (power("FULL_ENTITY - Creating ID=%s CardID=%s" % (eid,cid)) +
                     power("  tag=CARDTYPE value="+ctype)+power("  tag=ZONE value=PLAY")+
                     power("  tag=CONTROLLER value=%d" % owner)+power("  tag=ATK value=3")+
                     power("  tag=DURABILITY value=2")+
                     (power("  tag=ATTACHED value=%d" % attached) if attached else "")).encode()
        p.feed(data)
        s = p.snapshot(1,extended=True)
        self.assertEqual(s["enemy_weapon"]["card_id"],"BE_771")
        self.assertEqual(s["enemy_weapon"]["durability"],2)
        self.assertEqual(s["my_hand"][0]["modifiers"][0]["card_id"],"TIME_026e")
        self.assertNotIn("ENEMY_PRIVATE",json.dumps(s))

    def test_audit_separates_proposal_and_confirmation_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"decisions.jsonl"
            advisor = OpenAIAdvisor({"api_key":"fake-key-not-a-secret-123"},lambda p:response(),audit_path=path)
            s=state()
            with patch("builtins.print"): action=advisor.ask(s)
            rows=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual([x["event"] for x in rows],["proposed"])
            advisor.confirm(action,s); advisor.close()
            rows=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual([x["event"] for x in rows],["proposed","confirmed"])
            self.assertEqual(rows[0]["decision_id"],rows[1]["decision_id"])
            self.assertNotIn("fake-key",path.read_text())

    def test_choice_snapshot_is_read_only_and_contains_health_hand_and_board(self):
        p = IncrementalBoard(); p.feed(fixture())
        choice = {"id":5,"type":"GENERAL","source":20,"source_card_id":"BE_726", "shown":True,"sent":False,
                  "items":[{"id":80+i,"card_id":cid,"owner":1} for i,cid in enumerate(("BE_013t1","BE_013t2","BE_013t3"))]}
        p.choices[5] = choice; p.game["blocks"] = [{"type":"POWER"}]
        with self.assertRaisesRegex(UnsafeState,"resolving"):
            p.snapshot(1)
        s = p.snapshot(1, choice_id=5, extended=True)
        self.assertEqual(s["phase"], "choose_one")
        self.assertEqual(s["enemy_hero"]["health"], 9)
        self.assertEqual(s["my_hand"][0]["card_id"], "BE_086")
        self.assertNotIn("ENEMY_PRIVATE_CARD", json.dumps(s))
        self.assertFalse(s["legal_options"]); self.assertFalse(s["end_turn_allowed"])
        with self.assertRaises(AdvisorError): action_menu(s)
        with self.assertRaises(UnsafeState): p.snapshot(1, choice_id=6)
        with self.assertRaises(UnsafeState): p.snapshot(2, choice_id=5)

    def test_choices_send_full_context_and_only_confirmed_prior_choices(self):
        captured = []
        advisor = OpenAIAdvisor({"api_key":"fake-key-not-a-secret-123"}, lambda p: captured.append(p) or response("c0"))
        case = cases()[-3]; s, ch = case["state"], case["choice"]
        with patch("builtins.print"):
            advisor.choose(ch,s)
        view = json.loads(captured[0]["input"])["observation"]
        self.assertEqual(view["enemy_hero"]["health"], 7)
        self.assertIn("my_hand", view); self.assertIn("enemy_board", view)
        self.assertFalse(advisor.choice_history)
        advisor.confirm_choice(ch,0)
        with patch("builtins.print"): advisor.choose(ch,s)
        view = json.loads(captured[1]["input"])["observation"]
        self.assertEqual(view["confirmed_choices"][0]["card_id"], "BE_013t1")

    def test_all_eval_cases_use_available_actions_and_fit_context_budget(self):
        for case in cases():
            if case["choice"]:
                self.assertTrue(all(a["index"] < len(case["choice"]["items"]) for a in case["expected"]))
                continue
            menu = action_menu(case["state"])
            self.assertTrue(all(a in menu.values() for a in case["expected"]),case["name"])
            request_payload("gpt-6.1-terra",enrich(public_view(case["state"])),menu)

    def test_evaluation_request_consumed_once_and_stale_never_starts_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); path = data/"strategy-eval-request.json"
            self.assertFalse(consume_request(data))
            path.write_text(json.dumps({"suite":"strategy-v1","expires_at":time.time()+60}))
            self.assertTrue(consume_request(data)); self.assertFalse(consume_request(data))
            path.write_text(json.dumps({"suite":"strategy-v1","expires_at":time.time()-1}))
            with self.assertRaisesRegex(ValueError,"过期"): consume_request(data)
            self.assertFalse(path.exists())

    def test_evaluation_harness_completes_both_variants_without_game_imports(self):
        def fake_transport(self,payload):
            enum=payload["tools"][0]["parameters"]["properties"]["action_id"]["enum"]
            return response(enum[0])
        with tempfile.TemporaryDirectory() as tmp, patch.object(OpenAIAdvisor,"_transport",fake_transport), patch("builtins.print"):
            path=Path(tmp)/"results.jsonl"
            evaluate({"api_key":"fake-key-not-a-secret-123"},path)
            rows=[json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(len(rows),17)
            self.assertEqual(rows[-1]["summary"]["api_calls"],16)
            self.assertEqual(rows[-1]["summary"]["baseline"]["total"],8)
            self.assertNotIn("fake-key",path.read_text())
