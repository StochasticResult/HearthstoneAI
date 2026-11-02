"""Verify practice identity by reconciling HSTracker's local board and Power.log."""
from incremental_board import UnsafeState, board
from collections import Counter


def mulligan_identity(parser, mirror):
    """Identify our side from the locally visible opening cards, without a tracker UI."""
    if not parser.game or parser.game["meta"].get("GameType") != "GT_VS_AI":
        raise UnsafeState("practice log not confirmed")
    if mirror.get("game_type") != 1 or (mirror.get("scene") or {}).get("mode") != 4:
        raise UnsafeState("mirror is not in practice game")
    mulligan = mirror.get("mulligan") or {}
    rows = mulligan.get("mulliganCards") or []
    if not mirror.get("mulligan_waiting") or not mulligan.get("waitingForUserInput") or len(rows) not in (3, 4):
        raise UnsafeState("opening hand is not ready")
    visible = Counter(row.get("cardId") for row in rows)
    if not all(visible):
        raise UnsafeState("opening hand has an unreadable card")
    candidates = []
    for player in (1, 2):
        choice = parser.pending_choice(player)
        if choice and choice["type"] == "MULLIGAN" and Counter(item["card_id"] for item in choice["items"]) == visible:
            candidates.append(player)
    if len(candidates) != 1:
        raise UnsafeState("opening hand cannot uniquely verify identity")
    return candidates[0]


def _mirror_rows(side):
    # HearthMirror represents an empty side as null in a real practice match.
    if side is None:
        return set()
    try:
        rows = side["boardCards"]
        result = {(int(row["entityId"]), row["cardId"], int(row["zonePosition"])) for row in rows}
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsafeState("incomplete mirror board") from exc
    if len(result) != len(rows) or len(result) > 7 or any(not cid or pos < 1 for _, cid, pos in result):
        raise UnsafeState("invalid mirror board")
    return result


def _log_rows(game, player):
    if any(e.get("zone") == "PLAY" and e.get("controller") == player and board._ctype(e) == "LOCATION"
           for e in game["entities"].values()):
        raise UnsafeState("locations not supported for identity")
    return {(int(e["id"]), e["cardId"], int(e["zone_pos"]))
            for e in board._of_side(game, player, "PLAY", "MINION")}


def practice_identity(game, mirror):
    if not game or game.get("meta", {}).get("GameType") != "GT_VS_AI":
        raise UnsafeState("practice log not confirmed")
    if mirror.get("game_type") != 1 or (mirror.get("scene") or {}).get("mode") != 4:
        raise UnsafeState("mirror is not in practice game")
    mirror_board = mirror.get("board") or {}
    if "friendly" not in mirror_board or "opposing" not in mirror_board:
        raise UnsafeState("incomplete mirror board")
    friendly = _mirror_rows(mirror_board["friendly"])
    opposing = _mirror_rows(mirror_board["opposing"])
    if not friendly and not opposing:
        raise UnsafeState("empty mirror board cannot verify game identity")
    candidates = [pid for pid, other in ((1, 2), (2, 1))
                  if _log_rows(game, pid) == friendly and _log_rows(game, other) == opposing]
    if len(candidates) != 1:
        raise UnsafeState("mirror and log boards disagree")
    return candidates[0]
