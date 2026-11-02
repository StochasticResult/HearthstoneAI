"""Constructed stress positions grounded in the current deck and public rules.

These are NOT claimed to be tournament replays. Tactical witnesses are kept
outside model input. Strategic judgments allow multiple defensible plans.
"""
import copy
from openai_advisor import card_definition
from model_comparison import prepare as prepare_basic
from strategy_eval import base, card, option

SOURCES = [
    {"url":"https://www.vicioussyndicate.com/vs-data-reaper-report-360/", "use":"2026-10-29 opponent archetypes; not an oracle for individual moves"},
    {"url":"https://hearthstone.blizzard.com/en-gb/cards/129805-floating-lessons/", "use":"Current-cost scaling card text, also in local catalog"},
    {"url":"https://hearthstone.blizzard.com/en-gb/cards/129802-blasphemy/", "use":"Highest-cost condition and repeated damage, also in local catalog"},
    {"url":"https://us.forums.blizzard.com/en/hearthstone/t/can-we-get-rid-of-leeches-already/151331", "use":"Community corroboration of Lifesteal including excess damage; not an official developer ruling"},
]


def hard_cases():
    result=[]
    def add(name,s,expected,criterion,category="tactical",choice=None,history=None,menu=None,witness=None):
        for zone in ('my_board','enemy_board','my_hand'):
            for i,c in enumerate(s[zone]): c['position']=i+1
        result.append(dict(name=name,state=s,expected=expected,criterion=criterion,category=category,
                           choice=choice,ranked=True,public_history=history or [],menu=menu,witness=witness))
    def attack(e,t): return {'kind':'attack','source_id':e,'target_id':t}
    def play(e,t=None): return dict(kind='play',source_id=e,**({'target_id':t} if t is not None else {}))

    s=base();s.update(mana=7,my_deck_count=0);s['enemy_hero'].update(health=7,armor=1)
    s['my_board']=[card('BE_729',10,health=1),card('CORE_UNG_205',11),card('CATA_210t',12,attack=1,health=1)]
    s['enemy_board']=[card('BE_085t',20,attack=5,health=4),card('BE_085t',21,health=2)]
    for c in s['enemy_board']:c['taunt']=True
    s['enemy_board'][1]['divine_shield']=True
    s['my_hand']=[card('BE_094',100,cost=2),card('BE_086',101,cost=5),card('BE_090',102)]
    s['legal_options']=[option(100,10,11,12,20,21),option(101),option(102),option(10,20,21),option(11,20,21),option(12,20,21),option(3)]
    add('buff_before_sacrificing_own_tax',s,[play(100,11),attack(12,21)],
        'Need six face attack plus a SINGLE two-damage Blasphemy. Buff Shard while Lessons costs 2; small Whelp breaks shield, Mawman dies killing 5/4, Blasphemy now costs 4 and clears the other taunt. Kithix 6 blocks its repeat. Counting four spell damage or buffing after Mawman dies loses lethal.',
        witness=[play(100,11),attack(12,21),attack(10,20),play(101),attack(11,2)])

    s=base();s.update(mana=5,my_deck_count=0);s['enemy_hero']['health']=13
    s['my_board']=[card('TIME_025',10),card('CAP_405',11),card('CORE_UNG_205',12,attack=2,health=2),card('CATA_210t',13,attack=2,health=2)]
    s['my_board'][2]['windfury']=True
    s['enemy_board']=[card('BE_861',20,health=4),card('BE_085t',21,attack=1,health=1)]
    s['enemy_board'][0].update(taunt=True,reborn=True);s['enemy_board'][1].update(taunt=True,divine_shield=True)
    s['my_hand']=[card('TIME_026',100),card('BE_086',101)]
    s['legal_options']=[option(100),option(101)]+[option(i,20,21) for i in (10,11,12,13)]+[option(3)]
    add('reborn_heals_twice_and_windfury',s,[play(100),play(101),attack(13,20)],
        'Golem heals enemy for 3 twice and returns as a 2/1 Taunt after its first death. Spend the normal 2/2 attack, not the Windfury body or a large attacker. After buff, 5+4+3+3 face damage plus four spell damage minus six healing is exactly thirteen.',
        witness=[play(100),attack(13,20),play(101),attack(10,2),attack(11,2),attack(12,2),attack(12,2)])

    s=base();s.update(mana=10,my_deck_count=0);s['enemy_hero'].update(health=14,armor=2)
    s['my_board']=[card('BE_729',10),card('CORE_UNG_205',11)]
    s['enemy_board']=[card('BE_729',20)]
    s['my_hand']=[card('BE_094',100,cost=3),card('BE_086',101,cost=6),card('BE_728',102)]
    s['legal_options']=[option(100,10,11,20),option(101),option(102),option(10,2,20),option(11,2,20),option(3)]
    add('two_auras_buff_before_enemy_aura_dies',s,[play(100,10),play(100,11),attack(10,2),attack(11,2)],
        'Lessons costs 3 while BOTH auras survive, giving six attack. Then six-mana double Blasphemy kills the enemy aura and deals four face. Total available attack 12 plus spell 4 equals health+armor 16. Blasphemy first lowers Lessons to 2 and loses two damage. Zakajz first makes the combined spell line unaffordable.',
        witness=[play(100,11),play(101),attack(10,2),attack(11,2)])

    s=base();s.update(phase='target',mana=0);s['my_hero']['health']=4
    s['enemy_board']=[card('CS2_172',20,attack=8,health=8),card('CATA_210t',21,attack=1,health=1)]
    menu={'t0':{'kind':'choose_target','target_id':20},'t1':{'kind':'choose_target','target_id':21}}
    add('lifesteal_overkill_survival_target',s,[{'kind':'choose_target','target_id':21}],
        'Glare heals five even on a 1-health minion: 4+5=9, then the remaining 8 attack leaves one life. Hitting the 8/8 leaves both attackers alive, totaling nine and killing us. Divine Shield/immune would change this, but neither is present.',menu=menu)

    s=base();s.update(phase='choose_one',mana=0,my_deck_count=0,my_fatigue=3);s['my_hero']['health']=2;s['enemy_hero']['health']=1
    s['my_board']=[card('CATA_210t',10+i,attack=1,health=1) for i in range(7)]
    for c in s['my_board']:c['exhausted']=True
    s['enemy_board']=[card('CS2_172',20,attack=20,health=20),card('CS2_172',21,attack=20,health=20)]
    choice={'shown':True,'source':70,'source_card_id':'BE_726','items':[{'card_id':c} for c in ('BE_013t1','BE_013t2','BE_013t3','BE_013t4')]}
    add('take_a_calculated_not_guaranteed_out',s,[{'index':0}],
        'Wrath wins on at least one hero hit: 1-(2/3)^7 = 2059/2187, about 94.15%, not certainty. No minion can die from the seven random points. Full board prevents taunts; Domain adds no taunts and Glare cannot remove a 20/20. No other choice stops the visible lethal.',choice=choice,category='uncertainty')

    s=base();s.update(mana=10,my_deck_count=0,my_fatigue=0);s['enemy_hero'].update(health=10,armor=1)
    s['my_board']=[card('BE_729',10,health=1),card('EX1_012',11),card('TIME_025',12)]
    s['enemy_board']=[card('BE_085t',20,attack=5,health=4)];s['enemy_board'][0]['taunt']=True
    s['my_hand']=[card('BE_090',100),card('BE_086',101,cost=5),card('GAME_005',102,cost=1)]
    s['legal_options']=[option(100),option(101),option(102),option(10,20),option(11,20),option(12,20),option(3)]
    add('coin_tax_spellpower_and_highest_cost',s,[attack(10,20),play(100),play(102)],
        'Mawman must die trading into taunt so Blasphemy drops from 5 to 4. Play Kithix 6 to enable repeat. Surviving Thalnos adds one to EACH wave (six spell damage), then 1+4 attacks give exact eleven including armor. Coin currently costs one so it is not net extra mana until the aura dies.',
        history=['No friendly Ritual has completed this game. The friendly Bloodmage was generated earlier; its Spell Damage +1 is active.'],
        witness=[attack(10,20),play(100),play(101),attack(11,2),attack(12,2)])

    for spent,keep in (('CORE_EX1_197','EDR_476'),('EDR_476','CORE_EX1_197')):
        s=base();s.update(mana=1,enemy_mana_crystals=5,enemy_hand_count=5);s['enemy_hero']['health']=23
        s['my_board']=[card('TIME_025',10),card('TIME_025',11)];s['my_hand']=[card('TIME_026',100),card('EDR_856',101)]
        s['opponent_public_cards']=[{'id':30,'card_id':'JAIL_432'},{'id':31,'card_id':spent,'generated':False},{'id':32,'card_id':spent,'generated':False}]
        s['legal_options']=[option(100),option(10,2),option(11,2)]
        name='priest_two_ruins_spent' if spent=='CORE_EX1_197' else 'priest_two_moonwells_spent'
        criterion=('Prefer buff before attacking to put both bodies above Moonwell four-damage range; the two natural Ruins were used. Future generated/copy removal is still possible.' if spent=='CORE_EX1_197' else 'Prefer face without buff: two natural Moonwells were used, while Ruin remains possible and a buff moves both into its five-attack range without shortening the current three-turn clock.')
        expected=[play(100)] if spent=='CORE_EX1_197' else [attack(10,2),attack(11,2)]
        add(name,s,expected,criterion,'judgment',history=['The two listed removal entities were separate original copies publicly played and resolved, not copied/generated cards. No opponent hand identity is known.'])

    s=base();s.update(mana=3,enemy_mana_crystals=5);s['enemy_hero'].update(card_id='HERO_07',health=30)
    s['my_board']=[card('BE_729',10,health=1),card('BE_729',11,health=5),card('TIME_025',12,health=5)]
    for c in s['my_board']:c['exhausted']=True
    s['my_hand']=[card('BE_094',100,cost=3),card('CORE_UNG_205',101)]
    s['opponent_public_cards']=[{'id':30,'card_id':'TIME_025'},{'id':31,'card_id':'TIME_026'}]
    s['legal_options']=[option(100,10,11,12),option(101,2),option(3)]
    add('buff_to_preserve_the_spell_tax_aura',s,[play(100,10)],
        'Prefer protecting the one-health Mawman. All buff targets gain the same attack, but protecting the fragile aura keeps enemy Blasphemy taxed to six; without it the tax can fall during removal. Do not state the opponent definitely has Blasphemy or its repeat condition.', 'judgment')

    s=base();s.update(mana=0);s['enemy_hero'].update(card_id='HERO_07',health=30)
    s['my_board']=[card('CATA_210t',10,attack=1,health=1),card('CATA_210t',11,attack=1,health=1),card('TIME_025',12),card('TIME_025',13)]+[card('CATA_210',14+i) for i in range(3)]
    s['enemy_board']=[card('CS2_172',20,attack=1,health=2)]
    s['active_rituals']=[{'id':40,'card_id':'BE_085','countdown_tag':1}]
    s['legal_options']=[option(i,2,20) for i in (10,11,12,13)]
    add('reserve_two_slots_for_next_ritual_wave',s,[attack(10,20),attack(11,20),attack(12,2),attack(13,2)],
        'Plan both one-health Whelps into the 1/2 to free two slots, while large attackers go face. Trading an Egg does not free a lasting slot because its deathrattle refills it. Sending a Whelp face loses the controllable two-slot line. An enemy clear may free slots anyway, but is not known.', 'judgment',
        history=['Publicly observed ritual timing: Ritual of Summoning will resolve at the start of our next turn if still active.'])

    s=base();s.update(mana=1,enemy_mana_crystals=5);s['my_hero']['health']=9
    s['enemy_hero'].update(card_id='HERO_10',health=10,attack=1)
    s['enemy_weapon']=card('BE_771',30);s['enemy_weapon']['durability']=2
    s['enemy_board']=[card('CS2_172',20,attack=8,health=8)]
    s['my_board']=[card('TIME_025',10),card('TIME_025',11)];s['my_hand']=[card('CORE_UNG_205',100)]
    s['opponent_public_cards']=[{'id':30,'card_id':'BE_771'},{'id':31,'card_id':'BE_014'}]
    s['legal_options']=[option(100,2,20),option(10,2,20),option(11,2,20)]
    add('weapon_trigger_versus_visible_eight_damage',s,[play(100,2),play(100,20),attack(10,2),attack(11,2)],
        'Compare freezing hero to deny Tentacle Lash CThun recovery/clear against freezing the 8/8 to protect life. Hero freeze plus face pressure leaves a fragile one-health survival against a face attack; minion freeze leaves weapon Glare healing/clear. Either freeze can be defended; full credit requires naming the tradeoff and unknown reach. Do not claim either line guarantees next-turn lethal.', 'judgment')

    s=base();s.update(mana=3,enemy_mana_crystals=5);s['enemy_hero'].update(card_id='HERO_07',health=25)
    s['my_board']=[card('CATA_210t',10,attack=3,health=3),card('CATA_210t',11,attack=3,health=3)]
    for c in s['my_board']:c['exhausted']=True
    s['my_hand']=[card('BE_729',100),card('TIME_025',101),card('CORE_UNG_205',102)]
    s['opponent_public_cards']=[{'id':30,'card_id':'CORE_CS2_062','generated':False},{'id':31,'card_id':'BE_861','generated':False},{'id':32,'card_id':'TIME_025','generated':True}]
    s['legal_options']=[option(100),option(101),option(102,2),option(3)]
    add('generated_card_is_not_archetype_evidence',s,[play(100),play(101),play(102,2)],
        'Compare one-card Mawman development against Timehopper plus Shard, retaining a rebuild plan. Crucially Mawman DOES NOT lock Hellfire out of six next-turn mana: it costs four. A generated Timehopper is not evidence their original deck is Zoo; original Hellfire plus Golem supports a slower candidate. One used Hellfire does not prove a second in hand. Multiple tempo choices are defensible; judge the inference and counterplay, not a unique move.', 'judgment',
        history=['Timehopper was publicly generated; its entity was not an original deck card.'])
    return result


def prepare(case):
    proxy=copy.deepcopy(case)
    if case.get('menu'):
        proxy['state']['phase']='main'
        proxy['state']['legal_options']=[option(3)]
    view,menu=prepare_basic(proxy)
    view['phase']=case['state']['phase']
    if case.get('menu'):
        menu=copy.deepcopy(case['menu'])
        view['target_effect']={'card_id':'BE_013t3','card':card_definition('BE_013t3')}
    if case.get('public_history'):
        view['public_history']=case['public_history']
    return view,menu
