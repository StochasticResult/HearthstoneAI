"""Explicit mode boundaries shared by menu, log and memory checks."""
MODES = {"practice": ("GT_VS_AI", 1), "ranked": ("GT_RANKED", 7)}


def expected_type(mode):
    if mode not in MODES:
        raise ValueError("Unsupported game mode")
    return MODES[mode]


def matching_game(meta, mirror, mode="practice"):
    log_type, numeric = expected_type(mode)
    return (meta.get("GameType") == log_type and mirror.get("game_type") == numeric
            and (mirror.get("scene") or {}).get("mode") == 4
            and (mode != "ranked" or (meta.get("FormatType") == "FT_STANDARD" and mirror.get("format") == 2)))
