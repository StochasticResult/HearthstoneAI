"""Bounded, offline-only incremental adapter for hearthstone-cli's board parser.

Uses its MIT-licensed packet parser at a pinned local checkout. No game input,
automatic queueing, uploading, memory reads, or network access is implemented.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "state-upstream" / "src"))
from hearthstone_cli import board  # noqa: E402

MAX_LINE = 64 * 1024
MAX_GAME_BYTES = 5 * 1024 * 1024
MAX_ENTITIES = 4096
MAX_EVENTS = 5000
MAX_BLOCK_DEPTH = 64
ZERO_COST_CARD_IDS = frozenset(json.loads((HERE / "zero-cost-card-ids.json").read_text(encoding="utf-8")))
ZERO_ATTACK_CARD_IDS = frozenset(json.loads((HERE / "zero-attack-card-ids.json").read_text(encoding="utf-8")))
TRIAL_PARTS = json.loads((HERE / "trial-parts.json").read_text())
TRIAL_CARDS = frozenset(("CAP_405tb1", "CAP_405tb2", "CAP_405tb3"))


class UnsafeState(Exception):
    pass


def custom_effects(entity):
    if entity.get("cardId") not in TRIAL_CARDS:
        return {}
    parts = [TRIAL_PARTS.get(str(entity["tags"].get("MODULAR_ENTITY_PART_%d" % i))) for i in (1, 2)]
    if any(p is None for p in parts):
        raise UnsafeState("custom trial effects are not observed or supported")
    return {"effect_card_ids": parts}


class IncrementalBoard:
    def __init__(self):
        self.game = None
        self.partial = b""
        self.game_bytes = 0
        self.line_count = 0
        self.blocked = "no complete game start observed"
        self.options_id = None
        self.options = []
        self.option = None
        self.choices = {}
        self.choice = None
        self.visual_step = None
        self.visual_turn = None
        self.visual_game_entity = False
        self.options_line = None

    def invalidate(self, why):
        self.game = None
        self.partial = b""
        self.blocked = why
        self.options_id = None
        self.options = []
        self.option = None
        self.options_line = None
        self.visual_step = self.visual_turn = None
        self.visual_game_entity = False

    def _observe_options(self, line):
        option_prefix = "GameState.DebugPrintOptions() -"
        if option_prefix not in line:
            return False
        message = line.split(option_prefix, 1)[1].strip()
        head = re.match(r"^id=(\d+)\b", message)
        if head:
            self.options_id = int(head.group(1))
            self.options_line = self.line_count
            self.options = []
            self.option = None
            return True
        match = re.match(r"^option \d+ type=(\S+) mainEntity=(.*?) error=(\S+)", message)
        if match:
            self.option = {"type": match.group(1), "id": board._ref_id(match.group(2)),
                           "error": match.group(3), "targets": []}
            self.options.append(self.option)
            return True
        target = re.match(r"^target \d+ entity=(.*?) error=(\S+)", message)
        if target and self.option is not None:
            target_id = board._ref_id(target.group(1))
            if target_id is not None:
                self.option["targets"].append((target_id, target.group(2)))
        return True

    def feed(self, chunk):
        if len(chunk) > 256 * 1024:
            self.invalidate("input chunk exceeds limit")
            raise UnsafeState(self.blocked)
        data = self.partial + chunk
        pieces = data.split(b"\n")
        self.partial = pieces.pop()
        if len(self.partial) > MAX_LINE:
            self.invalidate("incomplete line exceeds limit")
            raise UnsafeState(self.blocked)
        for raw in pieces:
            if len(raw) > MAX_LINE:
                self.invalidate("line exceeds limit")
                raise UnsafeState(self.blocked)
            try:
                line = raw.decode("utf-8").lstrip("\ufeff").rstrip("\r")
            except UnicodeDecodeError as exc:
                self.invalidate("invalid UTF-8")
                raise UnsafeState(self.blocked) from exc
            try:
                self._feed_line(line, len(raw) + 1)
            except Exception as exc:
                # The upstream parser can raise on references to missing entities.
                # Do not expose the log line or continue from a partly mutated state.
                self.invalidate("unsupported or inconsistent log event")
                raise UnsafeState(self.blocked) from exc

    def _feed_line(self, line, byte_count):
        self.line_count += 1
        # Always allow a subsequent complete CREATE_GAME to reset a failed prior game.
        pref = board.PREFIX_RE.match(line)
        payload = pref.group(1) if pref else None
        if payload is not None and board.CREATE_GAME_RE.match(payload):
            # On a real reconnect the current options preceded CREATE_GAME by
            # eleven lines. Preserve only a nearby first-session options list.
            keep_options = self.game is None and self.options_id is not None and self.options_line is not None and self.line_count - self.options_line <= 128
            self.game = board._new_game()
            self.game_bytes = byte_count
            self.blocked = None
            if not keep_options:
                self.options_id = None
                self.options = []
                self.option = None
                self.options_line = None
            self.choices = {}
            self.choice = None
            self.visual_step = None
            self.visual_turn = None
            self.visual_game_entity = False
            return
        if self.game is None:
            self._observe_options(line)
            return
        self.game_bytes += byte_count
        if self.game_bytes > MAX_GAME_BYTES:
            self.invalidate("game byte limit exceeded")
            return
        tm = board.TRUNCATE_RE.search(line)
        if tm:
            self.invalidate("game log truncated")
            return
        game = self.game
        visual_prefix = "PowerTaskList.DebugPrintPower() -"
        if visual_prefix in line:
            message = line.split(visual_prefix, 1)[1].strip()
            if re.match(r"^GameEntity EntityID=1\b", message):
                self.visual_game_entity = True
                return
            visual = re.match(r"^TAG_CHANGE Entity=GameEntity tag=(TURN|STEP) value=(\S+)", message)
            if not visual and self.visual_game_entity:
                visual = re.match(r"^tag=(TURN|STEP) value=(\S+)", message)
            if visual:
                if visual.group(1) == "TURN":
                    self.visual_turn = visual.group(2)
                else:
                    self.visual_step = visual.group(2)
            elif not message.startswith("tag="):
                self.visual_game_entity = False
            return
        choice_prefix = "GameState.DebugPrintEntityChoices() -"
        if choice_prefix in line:
            message = line.split(choice_prefix, 1)[1].strip()
            head = re.match(r"^id=(\d+)\s+Player=.*?\bChoiceType=(\S+)", message)
            if head:
                cid = int(head.group(1))
                self.choice = {"id": cid, "type": head.group(2), "source": None, "source_card_id": "",
                               "items": [], "shown": False, "sent": False}
                self.choices[cid] = self.choice
            elif self.choice is not None and message.startswith("Source="):
                ref = message[7:]
                self.choice["source"] = board._ref_id(ref)
                match = re.search(r"\bcardId=(\S*)", ref)
                self.choice["source_card_id"] = match.group(1) if match else ""
            elif self.choice is not None and (match := re.match(r"^Entities\[\d+\]=(.*)", message)):
                ref = match.group(1)
                eid = board._ref_id(ref)
                owner = re.search(r"\bplayer=(\d+)", ref)
                card_id = re.search(r"\bcardId=(\S*)", ref)
                self.choice["items"].append({"id": eid, "card_id": card_id.group(1) if card_id else "",
                                             "owner": int(owner.group(1)) if owner else None})
            board._handle_entity_choices(game, line)
            return
        shown = re.search(r"ChoiceCardMgr\.WaitThenShowChoices\(\) - id=(\d+) BEGIN", line)
        if shown and int(shown.group(1)) in self.choices:
            self.choices[int(shown.group(1))]["shown"] = True
        sent = re.search(r"GameState\.SendChoices\(\) - id=(\d+) ChoiceType=", line)
        if sent and int(sent.group(1)) in self.choices:
            self.choices[int(sent.group(1))]["sent"] = True
        if self._observe_options(line):
            return
        nm = board.PLAYER_NAME_RE.match(line)
        if nm:
            pid, pname = int(nm.group(1)), nm.group(2).strip()
            game["player_names"][pid] = pname
            for player in board._players(game):
                if board._tag_int(player, "PLAYER_ID") == pid:
                    player["name"] = player["name"] or pname
                    game["names"][pname] = player["id"]
                    break
            return
        mm = board.GAME_META_RE.match(line)
        if mm:
            game["meta"][mm.group(1)] = mm.group(2)
            return
        if "GameState.DebugPrintEntitiesChosen() -" in line:
            board._handle_entities_chosen(game, line)
            return
        if payload is None:
            return
        if payload[:1] in (" ", "\t"):
            stripped = payload.strip()
            tag = board.TAG_LINE_RE.match(stripped)
            if tag:
                if game["cur"] is not None:
                    board._apply_tag(game, game["cur"], tag.group(1), tag.group(2))
            else:
                game["cur"] = None
                board._handle_packet(game, stripped)
        else:
            game["cur"] = None
            board._handle_packet(game, payload)
        if len(game["entities"]) > MAX_ENTITIES or len(game["events"]) > MAX_EVENTS or len(game["blocks"]) > MAX_BLOCK_DEPTH:
            self.invalidate("state size limit exceeded")

    def pending_choice(self, own_player_id):
        if self.game is None or self.blocked or self.partial:
            raise UnsafeState("game state not ready for a choice")
        pending = [c for c in self.choices.values() if not c["sent"] and c["items"] and
                   {item["owner"] for item in c["items"] if item["owner"] is not None} == {own_player_id}]
        if len(pending) > 1:
            raise UnsafeState("multiple pending own choices")
        if not pending:
            return None
        choice = dict(pending[0])
        source = self.game["entities"].get(choice["source"], {})
        choice["source_card_id"] = choice["source_card_id"] or source.get("cardId", "")
        choice["items"] = [dict(item, card_id=item["card_id"] or self.game["entities"].get(item["id"], {}).get("cardId", ""))
                           for item in choice["items"]]
        return choice

    def snapshot(self, own_player_id, *, allow_practice=False, extended=False):
        if self.game is None or self.blocked:
            raise UnsafeState(self.blocked or "no game")
        game = self.game
        if self.partial:
            raise UnsafeState("incomplete trailing log line")
        if game["blocks"] or game.get("ec_choice") or game.get("ec_confirm"):
            raise UnsafeState("game is resolving an action or choice")
        if board.is_mulligan(game):
            raise UnsafeState("mulligan is not supported")
        is_practice = game["meta"].get("GameType") == "GT_VS_AI" and game["meta"].get("FormatType") in ("FT_WILD", "FT_STANDARD")
        if game["meta"].get("FormatType") != "FT_STANDARD" and not (allow_practice and is_practice):
            raise UnsafeState("standard format not confirmed")
        players = board._players(game)
        own = [p for p in players if board._tag_int(p, "PLAYER_ID") == own_player_id]
        rivals = [p for p in players if board._tag_int(p, "PLAYER_ID") != own_player_id]
        if len(own) != 1 or len(rivals) != 1:
            raise UnsafeState("player identity not confirmed")
        own, rival = own[0], rivals[0]
        if own.get("controller") != own_player_id or not rival.get("controller"):
            raise UnsafeState("player controller missing")
        own_hero = board._hero_ent(game, own_player_id)
        enemy_hero = board._hero_ent(game, rival["controller"])
        if not own_hero or not enemy_hero:
            raise UnsafeState("hero state incomplete")
        if not extended and (board._tag_int(own_hero, "ARMOR") or board._tag_int(enemy_hero, "ARMOR")):
            raise UnsafeState("armor is not modeled")
        active_rituals = []
        for entity in game["entities"].values():
            ctype = board._ctype(entity)
            if entity.get("zone") == "SECRET":
                if entity.get("controller") == own_player_id and entity.get("cardId") in {"BE_085", "BE_726", "CAP_405tb2", "CAP_405tb3"}:
                    active_rituals.append({"id": entity["id"], "card_id": entity["cardId"],
                        "countdown_tag": board._num(entity["tags"].get("OBJECTIVE_COUNTDOWN")),
                        "quest_progress": board._num(entity["tags"].get("QUEST_PROGRESS")),
                        "quest_progress_total": board._num(entity["tags"].get("QUEST_PROGRESS_TOTAL")),
                        "script_data_1": board._num(entity["tags"].get("TAG_SCRIPT_DATA_NUM_1")),
                        **custom_effects(entity)})
                else:
                    raise UnsafeState("secret state is not modeled")
            if entity.get("zone") == "PLAY" and ctype in ("LOCATION", "WEAPON"):
                raise UnsafeState("location or weapon is not modeled")
            if entity.get("zone") == "PLAY" and ctype in ("MINION", "HERO"):
                unsupported = ("DIVINE_SHIELD", "WINDFURY", "MEGA_WINDFURY", "STEALTH", "LIFESTEAL", "POISONOUS", "REBORN", "IMMUNE", "DORMANT", "SILENCED")
                if not extended and any(board._tag_int(entity, tag) for tag in unsupported):
                    raise UnsafeState("unsupported visible combat status")

        def required_number(entity, tag):
            value = board._num(entity["tags"].get(tag))
            # A real reconnect log omits COST for base-zero Coin and Cursed
            # Catacombs; a nonzero changed cost still needs its observed tag.
            if value is None and tag == "COST" and entity.get("cardId") in ZERO_COST_CARD_IDS:
                return 0
            if value is None and tag == "ATK" and entity.get("cardId") in ZERO_ATTACK_CARD_IDS:
                return 0
            if value is None:
                raise UnsafeState("missing " + tag)
            return value

        def hero_state(entity):
            return {"id": entity["id"], "card_id": entity["cardId"],
                    "health": required_number(entity, "HEALTH") - board._tag_int(entity, "DAMAGE"),
                    "armor": board._tag_int(entity, "ARMOR"), "attack": board._tag_int(entity, "ATK"),
                    "frozen": board._tag_int(entity, "FROZEN") == 1}

        def minion_state(entity):
            result = {"id": entity["id"], "card_id": entity["cardId"], "position": entity["zone_pos"],
                    "attack": required_number(entity, "ATK"), "health": required_number(entity, "HEALTH") - board._tag_int(entity, "DAMAGE"),
                    "taunt": board._tag_int(entity, "TAUNT") == 1, "frozen": board._tag_int(entity, "FROZEN") == 1,
                    "exhausted": board._tag_int(entity, "EXHAUSTED") == 1}
            if extended:
                for tag in ("DIVINE_SHIELD", "WINDFURY", "STEALTH", "LIFESTEAL", "POISONOUS", "REBORN", "IMMUNE", "DORMANT", "SILENCED"):
                    result[tag.lower()] = board._tag_int(entity, tag) != 0
                result["attacks_this_turn"] = board._tag_int(entity, "NUM_ATTACKS_THIS_TURN")
            return result

        hand = board._in_zone(game, own_player_id, "HAND")
        hand.sort(key=lambda e: (e["zone_pos"], e["id"]))
        own_hand = []
        for card in hand:
            if not card["cardId"] or card["zone_pos"] <= 0:
                raise UnsafeState("unknown hand card or position")
            ctype = board._ctype(card)
            attack = board._num(card["tags"].get("ATK"))
            health = board._num(card["tags"].get("HEALTH"))
            own_hand.append({"id": card["id"], "card_id": card["cardId"], "position": card["zone_pos"],
                             "cost": required_number(card, "COST"), "card_type": ctype,
                             "attack": attack, "health": health, **custom_effects(card)})
        own_board = board._of_side(game, own_player_id, "PLAY", "MINION")
        enemy_board = board._of_side(game, rival["controller"], "PLAY", "MINION")
        own_entities = {e["id"] for e in hand + own_board}
        for e in game["entities"].values():
            if e.get("controller") == own_player_id and e.get("zone") == "PLAY" and board._ctype(e) in ("HERO", "HERO_POWER"):
                own_entities.add(e["id"])
        public_targets = {e["id"] for e in own_board + enemy_board} | {own_hero["id"], enemy_hero["id"]}
        game_entity = board._game_entity(game)
        # END_TURN has no main entity. Real successful end-turn sends use
        # error=INVALID for that absent entity (observed before MAIN_END),
        # unlike a POWER/target error. Accept only this exact empty shape,
        # with our current visual and server turn both actionable.
        end_turn_allowed = bool(game_entity and board._tag_int(own, "CURRENT_PLAYER") == 1
            and game_entity["tags"].get("STEP") == "MAIN_ACTION" and self.visual_step == "MAIN_ACTION"
            and self.visual_turn is not None and self.visual_turn == game_entity["tags"].get("TURN")
            and any(o["type"] == "END_TURN" and o["id"] is None and not o["targets"]
                    and o["error"] in ("NONE", "INVALID") for o in self.options))
        legal = [{"id": opt["id"], "type": opt["type"], "requires_target": bool(opt["targets"]),
                  "targets": [eid for eid, error in opt["targets"] if error == "NONE" and eid in public_targets]}
                 for opt in self.options if opt["id"] in own_entities and opt["error"] == "NONE"]
        powers = board._of_side(game, own_player_id, "PLAY", "HERO_POWER")
        power = powers[0] if len(powers) == 1 else None
        return {
            "format": game["meta"].get("FormatType"), "game_type": game["meta"].get("GameType"),
            # EXHAUSTED alone does not prove attack readiness, and ritual timing is
            # not yet reconstructed. Consumers must not promote this to live play.
            "readiness_verified": False, "ritual_state_verified": False,
            "own_player_id": own_player_id, "own_turn": board._tag_int(own, "CURRENT_PLAYER") == 1,
            "mana": required_number(own, "RESOURCES") - board._tag_int(own, "RESOURCES_USED") + board._tag_int(own, "TEMP_RESOURCES"),
            "my_hero": hero_state(own_hero), "enemy_hero": hero_state(enemy_hero),
            "my_hero_power": {"id": power["id"], "card_id": power["cardId"],
                              "cost": board._num(power["tags"].get("COST")),
                              "exhausted": board._tag_int(power, "EXHAUSTED") == 1} if power else None,
            "my_hand": own_hand, "my_board": [minion_state(e) for e in own_board],
            "enemy_board": [minion_state(e) for e in enemy_board],
            "active_rituals": active_rituals,
            "options_id": self.options_id, "legal_options": legal,
            "end_turn_allowed": end_turn_allowed,
            "game_step": game_entity["tags"].get("STEP") if game_entity else None,
            "game_turn": game_entity["tags"].get("TURN") if game_entity else None,
            "visual_step": self.visual_step, "visual_turn": self.visual_turn,
        }


class LogFollower:
    """Finite poll; refuse file replacement/truncation rather than rebuilding a partial game."""
    def __init__(self, path, parser):
        self.path = Path(path)
        self.parser = parser
        self.file = self.path.open("rb")
        st = os.fstat(self.file.fileno())
        self.identity = (st.st_dev, st.st_ino)
        self.offset = 0

    def poll(self, max_bytes=64 * 1024):
        st = self.path.stat()
        if (st.st_dev, st.st_ino) != self.identity or st.st_size < self.offset:
            self.parser.invalidate("log rotated or truncated")
            raise UnsafeState(self.parser.blocked)
        data = self.file.read(min(max_bytes, 64 * 1024))
        self.offset += len(data)
        if data:
            self.parser.feed(data)
        return len(data)

    def close(self):
        self.file.close()


def main():
    ap = argparse.ArgumentParser(description="Read one existing Power.log in bounded chunks; no game control.")
    ap.add_argument("path", type=Path)
    ap.add_argument("--own-player-id", required=True, type=int, choices=(1, 2))
    ap.add_argument("--allow-practice", action="store_true", help="also accept GT_VS_AI as practice (never relabel its format)")
    args = ap.parse_args()
    if args.path.stat().st_size > 10 * 1024 * 1024:
        raise UnsafeState("file exceeds offline inspection limit")
    parser = IncrementalBoard()
    follower = LogFollower(args.path, parser)
    try:
        while follower.poll():
            pass
    finally:
        follower.close()
    print(json.dumps(parser.snapshot(args.own_player_id, allow_practice=args.allow_practice), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (UnsafeState, OSError) as error:
        # Never print raw log lines, battle tags, account names, or credentials.
        print(json.dumps({"status": "blocked", "reason": str(error)}), file=sys.stderr)
        sys.exit(2)
