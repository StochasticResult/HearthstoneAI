import json
import tempfile
import unittest
from pathlib import Path

import incremental_board as inc


def power(payload):
    return "D 10:00:00.0000000 GameState.DebugPrintPower() - " + payload + "\n"


def meta(name, value):
    return "D 10:00:00.0000000 GameState.DebugPrintGame() - " + name + "=" + value + "\n"


def fixture():
    lines = [power("CREATE_GAME"), meta("FormatType", "FT_STANDARD"), meta("GameType", "GT_RANKED"),
             power("GameEntity EntityID=1"), power("  tag=CARDTYPE value=GAME")]
    for ent, pid, hero, mana, turn in ((2, 1, 10, 4, 1), (3, 2, 11, 4, 0)):
        lines.extend([power("Player EntityID=%d PlayerID=%d" % (ent, pid)),
                      power("  tag=CARDTYPE value=PLAYER"), power("  tag=CONTROLLER value=%d" % pid),
                      power("  tag=HERO_ENTITY value=%d" % hero), power("  tag=RESOURCES value=%d" % mana),
                      power("  tag=RESOURCES_USED value=0"), power("  tag=CURRENT_PLAYER value=%d" % turn)])
    for ent, pid, health in ((10, 1, 30), (11, 2, 12)):
        lines.extend([power("FULL_ENTITY - Creating ID=%d CardID=HERO_FAKE" % ent),
                      power("  tag=CARDTYPE value=HERO"), power("  tag=ZONE value=PLAY"),
                      power("  tag=CONTROLLER value=%d" % pid), power("  tag=HEALTH value=%d" % health)])
    lines.extend([power("FULL_ENTITY - Creating ID=20 CardID=BE_086"), power("  tag=CARDTYPE value=SPELL"),
                  power("  tag=ZONE value=HAND"), power("  tag=CONTROLLER value=1"),
                  power("  tag=ZONE_POSITION value=1"), power("  tag=COST value=4"),
                  power("FULL_ENTITY - Creating ID=21 CardID=ENEMY_PRIVATE_CARD"), power("  tag=CARDTYPE value=SPELL"),
                  power("  tag=ZONE value=HAND"), power("  tag=CONTROLLER value=2"),
                  power("  tag=ZONE_POSITION value=1"), power("  tag=COST value=1"),
                  power("TAG_CHANGE Entity=11 tag=DAMAGE value=3")])
    return "".join(lines).encode("utf-8")


class IncrementalTests(unittest.TestCase):
    def test_client_option_submission_is_counted_without_exposing_payload(self):
        parser = inc.IncrementalBoard()
        parser.feed(fixture())
        parser.feed(b'D 10:00:01.0000000 GameState.SendOption() - selectedOption=0 selectedSubOption=-1 selectedTarget=0 selectedPosition=0\n')
        self.assertEqual(parser.sent_options,1)
        parser.feed(fixture())
        self.assertEqual(parser.sent_options,0)

    def test_chunked_matches_upstream_and_hides_other_hand(self):
        data = fixture()
        parser = inc.IncrementalBoard()
        for i in range(0, len(data), 13):
            parser.feed(data[i:i + 13])
        original, _, _ = inc.board.parse_power_log(data.decode("utf-8").splitlines())
        self.assertEqual(parser.game["entities"], original["entities"])
        snap = parser.snapshot(1)
        self.assertTrue(snap["own_turn"])
        self.assertFalse(snap["readiness_verified"])
        self.assertFalse(snap["ritual_state_verified"])
        self.assertEqual(snap["mana"], 4)
        self.assertEqual(snap["enemy_hero"]["health"], 9)
        self.assertEqual(snap["enemy_hero"]["max_health"], 12)
        self.assertEqual(snap["my_hand"][0]["card_id"], "BE_086")
        self.assertNotIn("ENEMY_PRIVATE_CARD", json.dumps(snap))
        other_view = parser.snapshot(2)
        self.assertFalse(other_view["own_turn"])
        self.assertNotIn("BE_086", json.dumps(other_view))

    def test_truncation_rejects_state_until_a_new_game(self):
        parser = inc.IncrementalBoard()
        parser.feed(fixture())
        parser.feed(b"Truncating log, which has reached the size limit of 10000KB\n")
        with self.assertRaisesRegex(inc.UnsafeState, "truncated"):
            parser.snapshot(1)
        parser.feed(fixture())
        self.assertEqual(parser.snapshot(1)["format"], "FT_STANDARD")

    def test_incomplete_line_and_unconfirmed_format_are_rejected(self):
        parser = inc.IncrementalBoard()
        parser.feed(fixture())
        parser.feed(b"D 10:00:00.0000000 incomplete")
        with self.assertRaisesRegex(inc.UnsafeState, "incomplete"):
            parser.snapshot(1)
        fresh = inc.IncrementalBoard()
        fresh.feed(fixture().replace(b"FormatType=FT_STANDARD", b"FormatType=FT_WILD"))
        with self.assertRaisesRegex(inc.UnsafeState, "standard"):
            fresh.snapshot(1)

    def test_practice_is_explicitly_allowed_without_relabeling_format(self):
        practice = inc.IncrementalBoard()
        practice.feed(fixture().replace(b"FormatType=FT_STANDARD", b"FormatType=FT_WILD").replace(b"GameType=GT_RANKED", b"GameType=GT_VS_AI"))
        with self.assertRaisesRegex(inc.UnsafeState, "standard"):
            practice.snapshot(1)
        snapshot = practice.snapshot(1, allow_practice=True)
        self.assertEqual(snapshot["format"], "FT_WILD")
        self.assertEqual(snapshot["game_type"], "GT_VS_AI")
        ordinary_wild = inc.IncrementalBoard()
        ordinary_wild.feed(fixture().replace(b"FormatType=FT_STANDARD", b"FormatType=FT_WILD"))
        with self.assertRaisesRegex(inc.UnsafeState, "standard"):
            ordinary_wild.snapshot(1, allow_practice=True)

    def test_omitted_base_zero_cost_is_supported_but_unknown_cost_is_not_guessed(self):
        zero = fixture().replace(b"CardID=BE_086", b"CardID=GAME_005").replace(power("  tag=COST value=4").encode(), b"")
        parser = inc.IncrementalBoard()
        parser.feed(zero)
        self.assertEqual(parser.snapshot(1)["my_hand"][0]["cost"], 0)
        unknown = zero.replace(b"CardID=GAME_005", b"CardID=BE_086")
        parser = inc.IncrementalBoard()
        parser.feed(unknown)
        with self.assertRaisesRegex(inc.UnsafeState, "COST"):
            parser.snapshot(1)

    def test_options_filter_for_valid_public_entities(self):
        def option(message):
            return ("D 10:00:01.0000000 GameState.DebugPrintOptions() - " + message + "\n").encode()
        parser = inc.IncrementalBoard()
        parser.feed(fixture() + option("id=7") +
                    option("option 0 type=POWER mainEntity=[id=20] error=NONE errorParam=") +
                    option("target 0 entity=[id=11] error=NONE errorParam=") +
                    option("target 1 entity=[id=21] error=NONE errorParam=") +
                    option("option 1 type=POWER mainEntity=[id=21] error=NONE errorParam=") +
                    option("option 2 type=POWER mainEntity=[id=20] error=REQ_ENOUGH_MANA errorParam="))
        result = parser.snapshot(1)
        self.assertEqual(result["options_id"], 7)
        self.assertEqual(result["legal_options"], [{"id": 20, "type": "POWER", "requires_target": True, "targets": [11]}])

    def test_visual_turn_and_step_are_tracked_separately(self):
        p = inc.IncrementalBoard()
        p.feed(fixture() + power("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION").encode() +
               b"D 10:00:01.0000000 PowerTaskList.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_READY\n")
        s = p.snapshot(1)
        self.assertEqual(s["game_step"], "MAIN_ACTION")
        self.assertEqual(s["visual_step"], "MAIN_READY")

    def test_observed_empty_end_turn_sentinel_requires_actionable_own_turn(self):
        prefix = (fixture() + power("TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION").encode()
                  + power("TAG_CHANGE Entity=GameEntity tag=TURN value=2").encode()
                  + b"D 10:00:01.0000000 PowerTaskList.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION\n"
                  + b"D 10:00:01.0000000 PowerTaskList.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=TURN value=2\n"
                  + b"D 10:00:01.0000000 GameState.DebugPrintOptions() - id=5\n")
        for kind, entity, error, expected in (("END_TURN", "", "INVALID", True), ("END_TURN", "", "NONE", True),
                ("POWER", "", "INVALID", False), ("END_TURN", "[id=20]", "INVALID", False),
                ("END_TURN", "", "REQ_YOUR_TURN", False)):
            p = inc.IncrementalBoard()
            p.feed(prefix + ("D 10:00:01.0000000 GameState.DebugPrintOptions() - option 0 type=%s mainEntity=%s error=%s errorParam=\n"
                             % (kind, entity, error)).encode())
            self.assertIs(p.snapshot(1)["end_turn_allowed"], expected)
            self.assertFalse(p.snapshot(2)["end_turn_allowed"])
            p.visual_step = "MAIN_READY"
            self.assertFalse(p.snapshot(1)["end_turn_allowed"])

    def test_reconnect_options_before_create_and_visual_initial_snapshot(self):
        prefix = ("D 10:00:00.0000000 GameState.DebugPrintOptions() - id=7\n"
                  "D 10:00:00.0000000 GameState.DebugPrintOptions() - option 0 type=POWER mainEntity=[id=20] error=NONE errorParam=\n").encode()
        visual = ("D 10:00:01.0000000 PowerTaskList.DebugPrintPower() - GameEntity EntityID=1\n"
                  "D 10:00:01.0000000 PowerTaskList.DebugPrintPower() -   tag=STEP value=MAIN_ACTION\n"
                  "D 10:00:01.0000000 PowerTaskList.DebugPrintPower() -   tag=TURN value=4\n").encode()
        p = inc.IncrementalBoard()
        p.feed(prefix + fixture() + visual)
        state = p.snapshot(1)
        self.assertEqual(state["options_id"], 7)
        self.assertEqual([x["id"] for x in state["legal_options"]], [20])
        self.assertEqual((state["visual_step"], state["visual_turn"]), ("MAIN_ACTION", "4"))
        p.feed(fixture())  # A second game cannot inherit the previous game's options.
        self.assertIsNone(p.snapshot(1)["options_id"])
        old = inc.IncrementalBoard()
        old.feed(prefix + b"noise\n" * 129 + fixture())
        self.assertIsNone(old.snapshot(1)["options_id"])

    def test_only_own_visible_choices_are_reported_and_sent_clears_them(self):
        def choice(message):
            return ("D 10:00:01.0000000 GameState.DebugPrintEntityChoices() - " + message + "\n").encode()
        p = inc.IncrementalBoard()
        p.feed(fixture() + choice("id=10 Player=private TaskList=1 ChoiceType=GENERAL") +
               choice("Source=[id=20 cardId=TLC_451 player=1]") +
               choice("Entities[0]=[id=80 cardId=TIME_025 player=1]") +
               choice("Entities[1]=[id=81 cardId=CATA_210 player=1]") +
               b"D 10:00:01.0000000 ChoiceCardMgr.WaitThenShowChoices() - id=10 BEGIN\n")
        self.assertIsNone(p.pending_choice(2))
        selected = p.pending_choice(1)
        self.assertTrue(selected["shown"])
        self.assertEqual(selected["source_card_id"], "TLC_451")
        self.assertEqual([x["card_id"] for x in selected["items"]], ["TIME_025", "CATA_210"])
        p.feed(b"D 10:00:02.0000000 GameState.SendChoices() - id=10 ChoiceType=GENERAL\n")
        self.assertIsNone(p.pending_choice(1))

    def test_rotation_and_truncation_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Power.log"
            path.write_bytes(fixture())
            parser = inc.IncrementalBoard()
            follower = inc.LogFollower(path, parser)
            try:
                while follower.poll():
                    pass
                self.assertEqual(parser.snapshot(1)["format"], "FT_STANDARD")
                path.write_bytes(b"short\n")
                with self.assertRaisesRegex(inc.UnsafeState, "truncated"):
                    follower.poll()
                with self.assertRaises(inc.UnsafeState):
                    parser.snapshot(1)
            finally:
                follower.close()

    def test_limits_fail_closed(self):
        parser = inc.IncrementalBoard()
        with self.assertRaisesRegex(inc.UnsafeState, "limit"):
            parser.feed(b"x" * (inc.MAX_LINE + 1))
        self.assertIsNone(parser.game)

    def test_lifetime_byte_counter_does_not_discard_a_bounded_live_state(self):
        parser = inc.IncrementalBoard()
        parser.feed(fixture())
        parser.game_bytes = 6*1024*1024
        parser.feed(power('TAG_CHANGE Entity=1 tag=TURN value=8').encode())
        self.assertEqual(parser.snapshot(1)['game_turn'],'8')
        self.assertGreater(parser.game_bytes,6*1024*1024)

    def test_unmodeled_armor_and_secret_are_blocked(self):
        parser = inc.IncrementalBoard()
        parser.feed(fixture() + power("TAG_CHANGE Entity=11 tag=ARMOR value=1").encode())
        with self.assertRaisesRegex(inc.UnsafeState, "armor"):
            parser.snapshot(1)
        other = inc.IncrementalBoard()
        secret = power("FULL_ENTITY - Creating ID=44 CardID=") + power("  tag=ZONE value=SECRET") + power("  tag=CONTROLLER value=2")
        other.feed(fixture() + secret.encode())
        with self.assertRaisesRegex(inc.UnsafeState, "secret"):
            other.snapshot(1)

    def test_extended_secret_count_never_exports_hidden_identity(self):
        p=inc.IncrementalBoard()
        secret=power("FULL_ENTITY - Creating ID=44 CardID=EX1_287")+power("  tag=CARDTYPE value=SPELL")+power("  tag=ZONE value=SECRET")+power("  tag=CONTROLLER value=2")
        p.feed(fixture()+secret.encode())
        state=p.snapshot(1,extended=True)
        self.assertEqual(state['enemy_secret_count'],1)
        self.assertNotIn('EX1_287',json.dumps(state))
        self.assertFalse(state['enemy_active_rituals'])

    def test_public_hero_enchantment_is_available_to_arithmetic_guard(self):
        p=inc.IncrementalBoard()
        effect=power('FULL_ENTITY - Creating ID=44 CardID=KNOWN_PUBLIC_EFFECT')+power('  tag=CARDTYPE value=ENCHANTMENT')+power('  tag=ZONE value=PLAY')+power('  tag=ATTACHED value=10')
        p.feed(fixture()+effect.encode())
        self.assertEqual(p.snapshot(1,extended=True)['my_hero']['modifiers'],[{'card_id':'KNOWN_PUBLIC_EFFECT'}])

    def test_public_enemy_ritual_is_separate_from_own_ritual_and_hidden_secret(self):
        p=inc.IncrementalBoard()
        ritual=power("FULL_ENTITY - Creating ID=44 CardID=BE_085")+power("  tag=CARDTYPE value=SPELL")+power("  tag=ZONE value=SECRET")+power("  tag=CONTROLLER value=2")+power("  tag=OBJECTIVE_COUNTDOWN value=1")
        p.feed(fixture()+ritual.encode())
        state=p.snapshot(1,extended=True)
        self.assertFalse(state['active_rituals'])
        self.assertEqual(state['enemy_active_rituals'][0]['card_id'],'BE_085')
        self.assertEqual(state['enemy_secret_count'],0)

    def test_custom_trial_uses_observed_parts_in_hand_and_active_zone(self):
        p = inc.IncrementalBoard()
        p.feed(fixture().replace(b"CardID=BE_086", b"CardID=CAP_405tb3"))
        with self.assertRaisesRegex(inc.UnsafeState, "custom trial effects"):
            p.snapshot(1)
        p.feed((power("TAG_CHANGE Entity=20 tag=MODULAR_ENTITY_PART_1 value=132838") +
                power("TAG_CHANGE Entity=20 tag=MODULAR_ENTITY_PART_2 value=132848")).encode())
        expected = ["CAP_405t4", "CAP_405t9"]
        self.assertEqual(p.snapshot(1)["my_hand"][0]["effect_card_ids"], expected)
        p.feed((power("TAG_CHANGE Entity=20 tag=ZONE value=SECRET") +
                power("TAG_CHANGE Entity=20 tag=QUEST_PROGRESS_TOTAL value=4") +
                power("TAG_CHANGE Entity=20 tag=QUEST_PROGRESS value=1")).encode())
        active = p.snapshot(1)["active_rituals"][0]
        self.assertEqual(active["effect_card_ids"], expected)
        self.assertEqual((active["quest_progress"], active["quest_progress_total"]), (1, 4))
        self.assertIsNone(active["countdown_tag"])
        p.feed(power("TAG_CHANGE Entity=20 tag=MODULAR_ENTITY_PART_1 value=999999").encode())
        with self.assertRaisesRegex(inc.UnsafeState, "custom trial effects"):
            p.snapshot(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
