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
    return {"name": reference["name"], "reference_cards": reference["cards"], "selected_cards": actual,
            "verification": "30 cards read from the selected game deck" if actual else "reference list; actual selected deck not yet verified",
            "profile_matches_selected": matches,
            "unseen_reference_copies": counts,
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
    seen = {canonical(x["card_id"]) for x in state.get("opponent_public_cards", []) if not x.get("generated")}
    candidates = []
    for profile in PACK["matchups"]:
        if profile["class"] != result["enemy_class"]:
            continue
        evidence = sorted(seen & {canonical(x) for x in profile["signals"]})
        candidates.append((len(evidence), profile, evidence))
    candidates.sort(key=lambda x: -x[0])  # Stable pack order for class-only possibilities.
    result["status"] = "uncertain candidates; evidence counts are NOT probabilities; unseen cards are NOT guaranteed"
    result["candidates"] = [{"id": p["id"], "name": p["name"], "evidence": evidence,
                              "basis": "public signals" if evidence else "class-only possibility",
                              "plan": p["plan"], "mulligan": p["mulligan"],
                              "watch_cards": p["watch_cards"]}
                             for _, p, evidence in candidates[:2]]
    return result


def strategic_context(state):
    return {"deck": deck_context(state), "matchup": matchup_context(state)}
