"""Blinded, bounded comparison using the production prompt and Responses path.

No game imports or inputs. The separate model-key.json stays unread until the
decision review is recorded. Cases and rubric are written before any API call.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import sys
import time
from pathlib import Path

from openai_advisor import OpenAIAdvisor, action_menu, card_definition, enrich, public_view, request_payload
from strategy import PACK
from strategy_eval import base, card, cases, option

MODELS = ("gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna")
RUBRIC = {
    "tactical": "One point for a defensible action in a guaranteed lethal/survival/feasibility line; any factual contradiction is reviewed.",
    "heuristic": "0-2 points: 2 sound action with sound decisive reason; 1 defensible tradeoff or correct action with material reasoning flaw; 0 clear strategic loss.",
    "selection": "Exclude incomplete or invalid responses. Maximize critical tactical score first, then strategic score. Prefer Luna if tied for best tactical score and within one strategic point of the best. Otherwise prefer higher strategic score; break exact ties by median latency.",
    "limitations": "One sample per synthetic case; eight reused regression cases plus eight holdouts. No measured win rate, no multi-turn rollout, no mulligan test. Low reasoning and production limits for all models.",
}


def comparison_cases():
    result = copy.deepcopy(cases())
    def add(name, state, expected, why, category="tactical", choice=None, ranked=False, prior=None):
        for zone in ("my_hand", "my_board", "enemy_board"):
            for i, row in enumerate(state[zone]):
                row["position"] = i + 1
        result.append(dict(name=name, state=state, expected=expected, criterion=why,
                           category=category, choice=choice, ranked=ranked, prior=prior or []))

    s = base(); s["enemy_hero"]["health"] = 4
    s["my_hand"] = [card("BE_086", 100), card("BE_728", 101)]
    s["legal_options"] = [option(100), option(101), option(3)]
    add("tied_highest_cost_is_lethal", s, [{"kind":"play", "source_id":100}],
        "Blasphemy tied at highest cost still repeats for four lethal damage; Zakajz makes it unaffordable this turn.")

    s = base(); s["mana"] = 1; s["my_hero"]["health"] = 7
    s["enemy_hero"].update(card_id="HERO_02", attack=8)
    s["enemy_weapon"] = card("BE_349", 30, attack=8); s["enemy_weapon"]["durability"] = 2
    s["enemy_board"] = [card("CS2_172", 20, attack=1, health=2)]
    s["my_hand"] = [card("CORE_UNG_205", 100)]
    s["opponent_public_cards"] = [{"id":30,"card_id":"BE_349"}, {"id":31,"card_id":"JAIL_457"}]
    s["legal_options"] = [option(100, 2, 20)]
    add("freeze_buffed_weapon_hero", s, [{"kind":"play", "source_id":100,"target_id":2}],
        "The observed eight-attack weapon hero is lethal; freezing the one-attack minion is not survival. Also deny Eyemaul tokens.", ranked=True)

    s = base(); s["enemy_hero"]["health"] = 8
    s["my_board"] = [card("CORE_UNG_205", 10, attack=2, health=1), card("TIME_025", 11, attack=6, health=6)]
    s["enemy_board"] = [card("BE_085t", 20, attack=3, health=2)]
    s["enemy_board"][0].update(taunt=True, divine_shield=True)
    s["my_hand"] = [card("BE_086", 100)]
    s["legal_options"] = [option(100), option(10,20), option(11,20), option(3)]
    add("double_damage_through_divine_shield", s,
        [{"kind":"play","source_id":100}, {"kind":"attack","source_id":10,"target_id":20}],
        "Repeated Blasphemy removes shield then taunt and deals four face damage; save the six-attack body for lethal. Small attacker may pop shield first.")

    s = base(); s.update(phase="choose_one",mana=7); s["my_hero"]["health"] = 4
    s["my_board"] = [card("CAP_405", 10)]; s["my_board"][0]["exhausted"] = True
    s["enemy_board"] = [card("CS2_172",20,attack=8,health=8)]
    ch = {"shown":True,"source":10,"source_card_id":"CAP_405", "items":[{"card_id":cid} for cid in ("CAP_405tb1b","CAP_405tb2b","CAP_405tb3b")]}
    add("kazakus_prior_effects_and_deadline", s, [{"index":0}],
        "Seven mana pays for immediate theft of the sole enemy minion and twelve healing. Delayed trials allow the visible eight damage to kill us first.",
        choice=ch, prior=[{"source_id":10,"card_id":"CAP_405t2"},{"source_id":10,"card_id":"CAP_405t8"}])

    s = base(); s["mana"] = 0; s["enemy_hero"]["health"] = 15
    s["my_board"] = [card("TIME_025",10),card("TIME_025",11)]
    s["enemy_board"] = [card("DINO_410",20)]
    s["opponent_public_cards"] = [{"id":20,"card_id":"DINO_410"}]
    s["legal_options"] = [option(10,2,20),option(11,2,20)]
    add("egg_priest_do_not_advance_egg", s,
        [{"kind":"attack","source_id":i,"target_id":2} for i in (10,11)],
        "Push eight face damage toward next-turn lethal; attacking an unbuffed zero-attack non-taunt egg helps its hatch without preventing damage.", "heuristic", ranked=True)

    s = base(); s["mana"] = 1; s["enemy_mana_crystals"] = 4; s["enemy_hero"]["health"] = 22
    s["my_board"] = [card("TIME_025",10,health=5),card("TIME_025",11)]
    s["my_hand"] = [card("TIME_026",100),card("TIME_025",101)]
    s["opponent_public_cards"] = [{"id":30,"card_id":"JAIL_432"},{"id":31,"card_id":"JAIL_433"}]
    s["legal_options"] = [option(100),option(10,2),option(11,2)]
    add("priest_preserve_ruin_threshold", s,
        [{"kind":"attack","source_id":i,"target_id":2} for i in (10,11)],
        "Prefer face first and retain Continuity: +1 attack does not shorten the current three-attack-turn clock, but moves both bodies into possible Ruin. Ruin is NOT known in hand; a well-supported buff tradeoff may earn partial credit.", "heuristic", ranked=True)

    s = base(); s["mana"] = 1; s["my_hero"]["health"] = 9
    s["enemy_hero"].update(card_id="HERO_11",health=12)
    s["my_board"] = [card("TIME_025",10),card("TIME_025",11)]
    s["enemy_board"] = [card("TLC_436",20,attack=8,health=8)]
    s["enemy_board"][0]["lifesteal"] = True
    s["my_hand"] = [card("CORE_UNG_205",100)]
    s["opponent_public_cards"] = [{"id":20,"card_id":"TLC_436"},{"id":30,"card_id":"BE_207"}]
    s["legal_options"] = [option(100,2,20),option(10,2,20),option(11,2,20)]
    add("handbuff_dk_deny_lifesteal_race", s,
        [{"kind":"play","source_id":100,"target_id":20}]+[{"kind":"attack","source_id":i,"target_id":2} for i in (10,11)],
        "Freeze the large Lifesteal body and send eight face damage (either order), keeping next-turn lethal pressure. Trading both bodies gives the opponent a large heal and loses pressure.", "heuristic", ranked=True)

    s = base(); s["mana"] = 8; s["enemy_hero"]["health"] = 8
    s["my_board"] = [card("TIME_025",10,health=5)]
    s["enemy_board"] = [card("BE_085t",20,health=4)]; s["enemy_board"][0]["taunt"] = True
    s["my_hand"] = [card("BE_086",100),card("BE_728",101),card("TIME_025",102)]
    s["legal_options"] = [option(100),option(101),option(102),option(10,20),option(3)]
    add("zakajz_cost_change_lethal_order", s,
        [{"kind":"play","source_id":100},{"kind":"play","source_id":102}],
        "Cast repeated Blasphemy before Zakajz: four damage clears taunt and leaves four face health for the ready body. Timehopper first still leaves enough mana, but Zakajz first makes the spell cost five with only four remaining.")
    return result


def prepare(case):
    state = copy.deepcopy(case["state"])
    menu = ({"c%d" % i:{"index":i} for i in range(len(case["choice"]["items"]))}
            if case["choice"] else action_menu(state))
    if case.get("ranked"):
        state["game_type"] = "GT_RANKED"
    state["starting_deck"] = dict(PACK["deck"]["cards"])
    state["client_build"] = PACK["reviewed_client_build"]
    view = enrich(public_view(state))
    view["recent_confirmed_actions"] = []
    if case["choice"]:
        choice = case["choice"]
        view.update(source_card=choice["source_card_id"],
                    choices=[{"index":i,"card_id":item["card_id"],"card":card_definition(item["card_id"]),"observed_stats":{}}
                             for i,item in enumerate(choice["items"])],
                    confirmed_choices=case.get("prior", []))
        for item in view["confirmed_choices"]:
            view["card_definitions"][item["card_id"]] = card_definition(item["card_id"])
    return view, menu


class CaptureAdvisor(OpenAIAdvisor):
    def record(self, event):
        self.last_event = event


def save(path, obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n")
    os.chmod(path,0o600)


def evaluate(config, output):
    output = Path(output); output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    labels = list("ABC"); models = list(MODELS); random.SystemRandom().shuffle(models)
    mapping = dict(zip(labels,models))
    save(output.with_name("model-key.json"),mapping)
    prepared = []
    for case in comparison_cases():
        view, menu = prepare(case)
        digests = set()
        for model in MODELS:
            payload, _ = request_payload(model,view,menu)
            payload.pop("model")
            digests.add(hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest())
        if len(digests) != 1:
            raise ValueError("Models did not receive identical request bodies")
        prepared.append((case,view,menu,next(iter(digests))))
    save(output.with_name("cases.json"), {"rubric":RUBRIC,"cases":[{"name":c["name"],"category":c["category"],"expected":c["expected"],"criterion":c["criterion"],"view":v,"menu":m,"payload_sha256":h} for c,v,m,h in prepared]})
    advisors = {label:CaptureAdvisor({**config,"model":model}) for label,model in mapping.items()}
    config["api_key"] = ""
    rows, metrics = [], []
    try:
        with output.open("w") as report:
            os.chmod(output,0o600)
            for case_no,(case,view,menu,digest) in enumerate(prepared):
                order = labels[case_no % 3:] + labels[:case_no % 3]
                for label in order:
                    advisor = advisors[label]; advisor.last_event = {}
                    started = time.monotonic(); before_tokens = advisor.tokens
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            action = advisor.select(copy.deepcopy(view),copy.deepcopy(menu))
                        row = {"case":case["name"],"candidate":label,"category":case["category"],
                               "action":action,"reason":advisor.last_event.get("reason",""),
                               "expected_action":action in case["expected"],"payload_sha256":digest}
                    except Exception as error:
                        # Advisor errors are deliberately credential-free. Do
                        # not serialize arbitrary transport exceptions.
                        row = {"case":case["name"],"candidate":label,"category":case["category"],
                               "error":type(error).__name__,"expected_action":False,"payload_sha256":digest}
                    metrics.append({"case":case["name"],"candidate":label,
                                    "elapsed_ms":round((time.monotonic()-started)*1000),"tokens":advisor.tokens-before_tokens})
                    rows.append(row); report.write(json.dumps(row,ensure_ascii=False)+"\n"); report.flush()
                    print(json.dumps({"comparison_progress":len(rows),"total":len(prepared)*3,"candidate":label,"case":case["name"]}),flush=True)
            summary = {label:{"cases":sum(r["candidate"]==label for r in rows),
                              "expected_actions":sum(r["candidate"]==label and r["expected_action"] for r in rows),
                              "errors":sum(r["candidate"]==label and "error" in r for r in rows)} for label in labels}
            summary.update(api_calls=sum(a.calls for a in advisors.values()),api_tokens=sum(a.tokens for a in advisors.values()),blind=True)
            report.write(json.dumps({"summary":summary})+"\n")
            save(output.with_name("metrics.json"),metrics)
            print(json.dumps({"comparison_complete":True,"api_calls":summary["api_calls"]}),flush=True)
    finally:
        for advisor in advisors.values(): advisor.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: model_comparison.py OUTPUT_JSONL; config over stdin")
    evaluate(json.loads(sys.stdin.readline(4097)),sys.argv[1])
