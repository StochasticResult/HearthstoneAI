"""Small, dated matchup retrieval over public evidence; no hidden-card prediction.

Plans are card-text-based coaching hypotheses, not solved policies or measured
win rates. Practice opponents never inherit ranked-archetype priors.
"""
import json
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = json.loads((HERE / "strategy-pack.json").read_text())
HERO_CLASSES = json.loads((HERE / "hero-classes.json").read_text())


def canonical(cid):
    return cid.removeprefix("CORE_")


def deck_context(state):
    reference = PACK["deck"]
    actual = state.get("starting_deck")
    actual = actual if isinstance(actual, dict) and sum(actual.values()) == 30 else None
    starting = actual or reference["cards"]
    seen = Counter(canonical(row["card_id"]) for row in state.get("own_seen_original", []))
    counts = {cid: max(0, count - seen[canonical(cid)]) for cid, count in starting.items()}
    outside = sorted(set(seen) - {canonical(cid) for cid in reference["cards"]} - {"GAME_005"})
    matches = ({canonical(cid):n for cid,n in actual.items()} ==
               {canonical(cid):n for cid,n in reference["cards"].items()}) if actual else None
    remaining = dict(counts)
    for row in state.get('own_known_deck', []):
        if row.get('generated'):
            remaining[row['card_id']] = remaining.get(row['card_id'], 0) + 1
    reconciled = (bool(actual) and state.get('deck_tracking_from_mulligan') is True
                  and not state.get('own_card_identity_changed')
                  and sum(remaining.values()) == state.get('my_deck_count')
                  and not outside)
    return {"name": reference["name"], "reference_cards": reference["cards"], "selected_cards": actual,
            "verification": "30 cards read from the selected game deck" if actual else "reference list; actual selected deck not yet verified",
            "profile_matches_selected": matches,
            "unseen_reference_copies": counts,
            "reconciled_remaining_counts": {cid: n for cid, n in remaining.items() if n} if reconciled else None,
            "remaining_basis": "Selected list minus observed original cards outside deck, plus known generated cards still in deck; total reconciled with observed deck size" if reconciled else "composition not fully reconciled; do not assume exact draw odds",
            "count_warning": "Not exact remaining deck: reconnects, generation, returns and transforms can change counts.",
            "observed_outside_reference": outside,
            "plan": reference["plan"], "mulligan": reference["mulligan"],
            "sequencing": reference["sequencing"]}


def matchup_context(state, today=None):
    today = today or date.today()
    age = (today - date.fromisoformat(PACK["as_of"])).days
    result = {"as_of": PACK["as_of"], "stale": not 0 <= age <= 7,
              "kind": "card-based hypotheses, not an optimal policy",
              "enemy_class": HERO_CLASSES.get((state.get("enemy_hero") or {}).get("card_id"), "UNKNOWN")}
    current_build = state.get("client_build")
    result["build_compatible"] = current_build == PACK["reviewed_client_build"] if current_build else None
    if state.get("game_type") == "GT_VS_AI":
        result.update(status="practice opponent; no ranked archetype inference", candidates=[])
        return result
    if result["stale"] or result["build_compatible"] is False:
        result.update(status="meta snapshot expired or client build changed; use visible cards until reviewed", candidates=[])
        return result
    original_rows = {x.get('id', i): x for i, x in enumerate(state.get('opponent_public_cards', [])) if not x.get('generated')}
    seen_counts = Counter(canonical(x['card_id']) for x in original_rows.values())
    seen = set(seen_counts)
    candidates = []
    for profile in PACK["matchups"]:
        if profile["class"] != result["enemy_class"]:
            continue
        evidence = sorted(seen & {canonical(x) for x in profile["signals"]})
        template = {canonical(cid): n for cid, n in profile.get('template_cards', {}).items()}
        matches = sorted(seen & template.keys())
        unmatched = sorted(seen - template.keys())
        # Evidence ranking, not a probability model. Unusual tech cards can
        # contradict one published list without disproving its archetype.
        score = 2 * len(evidence) + len(matches) - (2 * len(unmatched) if profile.get('template_total') == 30 else 0)
        candidates.append((score, profile, evidence, matches, unmatched))
    candidates.sort(key=lambda x: -x[0])
    result["status"] = "uncertain candidates; evidence counts are NOT probabilities; unseen cards are NOT guaranteed"
    result['unlisted_or_modified_deck_possible'] = True
    result["candidates"] = [{"id": p["id"], "name": p["name"], "evidence": evidence,
                              "basis": "public signals" if evidence else "class-only possibility",
                              "matching_original_cards": matches, "unmatched_original_cards": unmatched,
                              "template_status": p.get('template_status', 'not available'),
                              "template_source": p['source'],
                              "if_template_matches_unseen_watch_cards": {
                                  cid: max(0, p.get('template_cards', {}).get(cid, 0) - seen_counts[canonical(cid)])
                                  if cid in p.get('template_cards', {}) else None for cid in p['watch_cards']},
                              "count_warning": "Conditional unseen copies, NOT known hand contents. Includes unseen hand/deck; generated copies and tech variants can differ.",
                              "plan": p["plan"], "mulligan": p["mulligan"],
                              "watch_cards": p["watch_cards"]}
                             for _, p, evidence, matches, unmatched in candidates[:2]]
    return result


def strategic_context(state):
    return {"deck": deck_context(state), "matchup": matchup_context(state)}
