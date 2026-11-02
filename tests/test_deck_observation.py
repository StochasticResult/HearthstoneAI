import json
import tempfile
import unittest
from pathlib import Path

from deck_observation import DECK_ID, public_selected_deck, save_selected_deck
from strategy import PACK


class DeckObservationTests(unittest.TestCase):
    def deck(self):
        return {"id": DECK_ID, "name": "private-deck-name", "account": "private-account", "cards":
                [{"cardId": cid, "count": n, "private": "private-row"} for cid, n in PACK["deck"]["cards"].items()]}

    def test_only_complete_expected_deck_and_public_fields_are_saved(self):
        deck = self.deck()
        self.assertEqual(sum(r["count"] for r in public_selected_deck(deck)["cards"]), 30)
        self.assertNotIn("private", json.dumps(public_selected_deck(deck)))
        for change in ({"id": 12}, {"cards": None}, {"cards": []}):
            self.assertIsNone(public_selected_deck({**deck, **change}))
        deck["cards"][0]["count"] = True
        self.assertIsNone(public_selected_deck(deck))

    def test_menu_snapshot_persists_once_and_rejects_later_deck_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"selected-deck.json"
            status = {"selected_deck": self.deck()}
            self.assertTrue(save_selected_deck(status,path))
            self.assertFalse(save_selected_deck(status,path))
            self.assertNotIn("private",path.read_text())
            status["selected_deck"]["cards"][0]["cardId"] = "OTHER_CARD"
            with self.assertRaisesRegex(ValueError,"changed"):
                save_selected_deck(status,path)

    def test_numeric_selected_id_must_match_resolved_deck_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'selected-deck.json'
            for selected in (None, 12):
                self.assertFalse(save_selected_deck({'selected_deck':selected,
                    'selected_deck_details':self.deck()}, path))
                self.assertFalse(path.exists())
            self.assertTrue(save_selected_deck({'selected_deck':DECK_ID,
                'selected_deck_details':self.deck()}, path))
            self.assertEqual(len(json.loads(path.read_text())['cards']),17)
