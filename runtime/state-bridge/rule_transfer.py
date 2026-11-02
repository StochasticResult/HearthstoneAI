"""Changed-number holdouts after a generic mechanics/selection-order fix."""
import copy
import json
import sys
from hard_cases import hard_cases
from hard_comparison import evaluate
from strategy_eval import card,option


def cases():
    original={c['name']:c for c in hard_cases()};result=[]
    c=copy.deepcopy(original['lifesteal_overkill_survival_target']);c['name']='overkill_ten_and_four_attack'
    c['state']['my_hero']['health']=6
    c['state']['enemy_board']=[card('CS2_172',20,attack=10,health=10),card('CATA_210t',21,attack=4,health=1)]
    c['criterion']='Kill 4/1: heal five to eleven, survive the remaining ten at one. Hitting 10/10 leaves fourteen attack and loses.';result.append(c)
    c=copy.deepcopy(original['lifesteal_overkill_survival_target']);c['name']='shield_blocks_lifesteal'
    c['state']['my_hero']['health']=3
    c['state']['enemy_board']=[card('CS2_172',20,attack=5,health=1),card('CATA_210t',21,attack=2,health=1)]
    c['state']['enemy_board'][0]['divine_shield']=True
    c['criterion']='Kill unshielded 2/1: heal five to eight, leaving five attack and three life. Hitting the shield heals zero and leaves lethal.';result.append(c)
    c=copy.deepcopy(original['reborn_heals_twice_and_windfury']);c['name']='fragile_windfury_cannot_clear_reborn'
    c['state']['my_board'][2]['health']=1
    c['criterion']='After buff the Windfury body has only two Health and dies to the reborn 2/1. Use the normal body to clear (or damage Golem before the spell) so both Windfury face attacks remain. Count both three-health deathrattles. Same exact thirteen-effective-damage witness.';result.append(c)
    c=copy.deepcopy(original['coin_tax_spellpower_and_highest_cost']);c['name']='two_taxes_coin_must_wait'
    s=c['state'];s['my_board']=[card('BE_729',10,health=1),card('BE_729',11,health=1),card('TIME_025',12),card('EX1_012',13)]
    s['enemy_board']=[card('BE_085t',20,attack=5,health=4),card('BE_085t',21,attack=5,health=4)]
    for x in s['enemy_board']:x['taunt']=True
    s['my_hand']=[card('BE_090',100,cost=7),card('BE_086',101,cost=6),card('GAME_005',102,cost=2)]
    s['legal_options']=[option(100),option(101),option(102)]+[option(i,20,21) for i in (10,11,12,13)]+[option(3)]
    c['expected']=[{'kind':'attack','source_id':i,'target_id':j} for i in (10,11) for j in (20,21)]+[{'kind':'play','source_id':100}]
    c['witness']=[{'kind':'attack','source_id':10,'target_id':20},{'kind':'attack','source_id':11,'target_id':21},{'kind':'play','source_id':100},{'kind':'play','source_id':102},{'kind':'play','source_id':101},{'kind':'attack','source_id':12,'target_id':2},{'kind':'attack','source_id':13,'target_id':2}]
    c['criterion']='Both Mawmen must die clearing taunts. Pay seven for the CURRENT-cost Kithix, then the now-zero-cost Coin raises three mana to four for Blasphemy. Spellpower gives six damage and surviving attacks five. Playing the two-cost Coin first loses one usable mana and the exact lethal.';result.append(c)
    for c in result:
        for zone in ('my_hand','my_board','enemy_board'):
            for i,x in enumerate(c['state'][zone]):x['position']=i+1
    return result


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: rule_transfer.py OUTPUT_JSONL; config over stdin')
    evaluate(json.loads(sys.stdin.readline(4097)),sys.argv[1],cases_factory=cases,rubric={
        'purpose':'Four counterfactual holdouts after generic mechanics facts and reason-before-selection schema order. Two models, two menu orders, sixteen calls. This is not an independent model ranking benchmark.',
        'score':'0-2 per trial: 2 correct action and decisive continuation; 1 correct first action with incomplete/wrong continuation; 0 loses available line. Eight trials per model, sixteen possible points.',
        'limitations':'Small synthetic transfer check; no causal isolation of rule hints versus schema order, no real-game win-rate claim.'})
