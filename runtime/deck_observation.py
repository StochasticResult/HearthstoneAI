"""Carry a sanitized selected-deck observation from the menu into the game."""
import json
import re

DECK_ID = 3384315883


def public_selected_deck(deck):
    if not isinstance(deck, dict) or deck.get("id") != DECK_ID:
        return None
    rows = deck.get("cards")
    if not isinstance(rows, list) or not 15 <= len(rows) <= 30:
        return None
    cards = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        cid, count = row.get("cardId"), row.get("count")
        if (not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", cid)
                or type(count) is not int or not 1 <= count <= 2):
            return None
        cards[cid] = cards.get(cid, 0) + count
    if sum(cards.values()) != 30 or max(cards.values()) > 2:
        return None
    return {"id": DECK_ID, "cards": [{"cardId": cid, "count": n} for cid, n in sorted(cards.items())]}


def save_selected_deck(status, path):
    selected = status.get("selected_deck")
    details = status.get("selected_deck_details")
    if details is not None and selected != DECK_ID:
        return False
    if isinstance(selected, int) and selected != DECK_ID:
        return False
    deck = public_selected_deck(details if details is not None else selected)
    if deck is None:
        return False
    if path.exists():
        if json.loads(path.read_text()) != deck:
            raise ValueError("The selected deck changed during navigation")
        return False
    with path.open("x") as handle:
        handle.write(json.dumps(deck, separators=(",", ":")))
    path.chmod(0o600)
    return True
