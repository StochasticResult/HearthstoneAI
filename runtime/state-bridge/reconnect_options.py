"""Conservatively carry forward one observed practice option list on reconnect."""
import copy

from incremental_board import UnsafeState, board

STATE_KEYS = ("game_type", "format", "own_player_id", "own_turn", "game_turn", "game_step", "mana",
              "my_hero", "enemy_hero", "my_hand", "my_board", "enemy_board", "active_rituals")
PLAYER_TAGS = ("CURRENT_PLAYER", "RESOURCES", "RESOURCES_USED", "TEMP_RESOURCES", "PLAYSTATE",
               "NUM_OPTIONS_PLAYED_THIS_TURN", "NUM_MINIONS_PLAYED_THIS_TURN", "NUM_CARDS_PLAYED_THIS_TURN",
               "NUM_FRIENDLY_MINIONS_THAT_ATTACKED_THIS_TURN", "COMBO_ACTIVE")


def fingerprint(parser, owner):
    if parser.game is None:
        raise UnsafeState("missing reconnect game")
    entity = board._game_entity(parser.game)
    seed = entity and entity["tags"].get("GAME_SEED")
    if not seed or seed == "0":
        raise UnsafeState("game seed unavailable")
    state = parser.snapshot(owner, allow_practice=True)
    if state["game_type"] != "GT_VS_AI" or not state["own_turn"] or state["game_step"] != "MAIN_ACTION":
        raise UnsafeState("practice turn unavailable")
    players = sorted(board._players(parser.game), key=lambda p: board._tag_int(p, "PLAYER_ID"))
    player_tags = [(board._tag_int(p, "PLAYER_ID"), tuple(str(p["tags"].get(k, "0")) for k in PLAYER_TAGS)) for p in players]
    return seed, tuple((k, state[k]) for k in STATE_KEYS), player_tags


def restore_observed_options(current, previous, owner):
    """Return False unless seed, turn, public state and player counters match."""
    if current.snapshot(owner, allow_practice=True)["legal_options"]:
        return False
    if current.options and not any(o.get("id") is None and o.get("error") == "NONE" for o in current.options):
        return False
    old = previous.snapshot(owner, allow_practice=True)
    if not old["legal_options"] or previous.options_id is None:
        return False
    if fingerprint(current, owner) != fingerprint(previous, owner):
        return False
    current.options = copy.deepcopy(previous.options)
    current.options_id = previous.options_id
    return bool(current.snapshot(owner, allow_practice=True)["legal_options"])
