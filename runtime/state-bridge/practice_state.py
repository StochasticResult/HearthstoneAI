"""Prepare a verified, limited GT_VS_AI observation for current-turn MCTS."""
import json
from pathlib import Path

from incremental_board import UnsafeState

META = json.loads((Path(__file__).with_name("card-meta.json")).read_text(encoding="utf-8"))
HAND = {"BE_086", "BE_094", "BE_085", "BE_726", "BE_729", "BE_728", "TIME_026", "TIME_025",
        "CORE_SCH_713", "CORE_UNG_205", "CATA_210", "TLC_451", "TLC_603", "CAP_405", "EDR_856",
        "BE_090", "TIME_029", "GAME_005", "CAP_405tb1", "CAP_405tb2", "CAP_405tb3"}
BOARD = {"BE_729", "BE_728", "TIME_025", "CORE_SCH_713", "CORE_UNG_205", "CATA_210",
         "CATA_210t", "BE_085t", "TLC_603", "CAP_405", "EDR_856", "BE_090", "TIME_029"}
ENEMY_REACTION_MODELS = {"EX1_399"}


def prepare(snapshot, *, identity_verified):
    if not identity_verified or snapshot.get("game_type") != "GT_VS_AI" or not snapshot.get("own_turn"):
        raise UnsafeState("active practice identity and own turn required")
    if snapshot.get("options_id") is None:
        raise UnsafeState("legal options not observed")
    result = dict(snapshot)
    result["my_hand"] = []
    for original in snapshot["my_hand"]:
        card = dict(original)
        cid = card["card_id"]
        if cid not in HAND:
            raise UnsafeState("unmodeled own hand card")
        info = META.get(cid, {})
        if card["card_type"] == "MINION":
            # Zero-valued base attack is omitted from real logs; do not use a
            # nonzero printed stat when a possibly changed stat is unavailable.
            if card.get("attack") is None and info.get("attack") == 0:
                card["attack"] = 0
            if card.get("attack") is None or card.get("health") is None:
                raise UnsafeState("minion hand stats are incomplete")
        elif card["card_type"] == "SPELL":
            card["attack"], card["health"] = 0, 1
        else:
            raise UnsafeState("unsupported hand card type")
        result["my_hand"].append(card)
    result["my_board"] = []
    for original in snapshot["my_board"]:
        card = dict(original)
        if card["card_id"] not in BOARD:
            raise UnsafeState("unmodeled friendly board effect")
        result["my_board"].append(card)
    result["enemy_board"] = []
    for original in snapshot["enemy_board"]:
        card = dict(original)
        if card["card_id"] in ENEMY_REACTION_MODELS:
            card["model_card_id"] = card["card_id"]
            result["enemy_board"].append(card)
            continue
        info = META.get(card["card_id"])
        # The restricted metadata marks battlecries with no known lasting
        # clause; other written effects remain unmodeled and stop the driver.
        if not info or (info.get("text") and not info.get("resolved_battlecry_only") and not info.get("taunt_only")):
            raise UnsafeState("unmodeled opposing board effect")
        card["model_card_id"] = "VANILLA"
        result["enemy_board"].append(card)
    result["identity_verified"] = True
    return result
