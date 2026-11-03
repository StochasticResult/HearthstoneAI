import copy
import datetime
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opponent_decks import class_plan, load_snapshot, parse_page, predict, refresh
from card_metadata import CATALOG
from coaching import strategy_sample
from coaching import thinking_review
from coach_policy import hints
from matchup_drills import exercise


class OpponentDeckTests(unittest.TestCase):
    def page(self, date, count=15):
        items=''.join(f'<li><img src="https://art.hearthstonejson.com/v1/render/latest/enUS/256x/CARD_{n}.png"><div class="card-quantity">x2</div></li>' for n in range(count))
        return (f'<title>Rogue Decks - Standard Ladder</title><!-- Last Update: {date.day}th {date:%B %Y} 14:05 -->'
                f"<h4><a href='/hearthstone/deck/14/'>Combo Rogue<div>#14</div></a></h4>"
                f"<div>#Games: 73</div><ul class='card-list'>{items}</ul>")

    def test_refresh_keeps_last_valid_source_and_expires_changed_builds(self):
        today=datetime.date.today()
        with self.assertRaises(ValueError):parse_page(self.page(today,14),'Rogue',today)
        with self.assertRaises(ValueError):parse_page(self.page(today,15),'Rogue',today+datetime.timedelta(days=9))
        with tempfile.TemporaryDirectory() as tmp, patch('opponent_decks.CLASSES',('Rogue',)):
            p=Path(tmp); calls=[]
            def source(name):
                calls.append(name)
                return self.page(today)
            refresh(p,'patch1',source); refresh(p,'patch1',source)
            self.assertEqual(calls,['Rogue'])
            self.assertEqual(len(load_snapshot(p,'patch1',today)[0]),1)
            db=sqlite3.connect(p/'opponent-decks.sqlite3')
            try:
                with db:db.execute('UPDATE sync SET next_check=0')
            finally:db.close()
            refresh(p,'patch1',lambda _:self.page(today,13))
            self.assertEqual(len(load_snapshot(p,'patch1',today)[0]),1)
            self.assertEqual(load_snapshot(p,'other',today)[0],[])
            self.assertEqual(load_snapshot(p,'patch1',today+datetime.timedelta(days=8))[0],[])

    def test_public_match_keeps_only_shared_threats_and_abstains_on_contradiction(self):
        base={'Marker1':2,'Marker2':2,'CS2_072':2,'Common':2}
        rows=[('ROGUE','Combo Rogue',dict(base,X=1),90,'feed/1','today'),
              ('ROGUE','Combo Rogue',dict(base,Y=1),80,'feed/2','today'),
              ('ROGUE','Other Rogue',{'Other1':2,'Other2':2,'Common':2},70,'feed/3','today'),
              ('MAGE','Mage',{'Common':2},60,'feed/4','today'),('WARLOCK','Warlock',{'Common':2},60,'feed/5','today')]
        def card(i,c,generated=False):return {'id':i,'card_id':c,'generated':generated,'played_publicly':True}
        v={'game_type':'GT_RANKED','format':'FT_STANDARD','strategy':{'matchup':{'enemy_class':'ROGUE'}},
           'opponent_public_cards':[card(1,'Marker1'),card(2,'Marker2'),card(3,'Common'),card(4,'Other1',True)]}
        past=[('ROGUE',{'Marker1','Marker2','CS2_072'})]*3
        self.assertIsNone(predict(dict(v,opponent_public_cards=v['opponent_public_cards'][:1]),rows,past,CATALOG))
        result=predict(v,rows,past,CATALOG)
        self.assertEqual(result['matching_lists'],2)
        threat=result['unseen_watch'][0]
        self.assertEqual((threat['name'],threat['unseen_if_list_matches'],threat['seen_in_similar_past_games']),('Backstab',2,3))
        for extra in ([card(5,'Oddball')],[card(5,'Marker1'),card(6,'Marker1')]):
            self.assertIsNone(predict(dict(v,opponent_public_cards=v['opponent_public_cards']+extra),rows,past,CATALOG))
        used=v['opponent_public_cards']+[card(5,'CORE_CS2_072')]
        self.assertEqual(predict(dict(v,opponent_public_cards=used),rows,[],CATALOG)['unseen_watch'][0]['unseen_if_list_matches'],1)
        exhausted=predict(dict(v,opponent_public_cards=used+[card(6,'CS2_072')]),rows,[],CATALOG)
        self.assertEqual(exhausted['examples'],['Combo Rogue'])
        self.assertEqual(exhausted['unseen_watch'],[])
        for key,value in [('generated',True),('played_publicly',False)]:
            changed=copy.deepcopy(v);changed['opponent_public_cards'][1][key]=value
            self.assertIsNone(predict(changed,rows,past,CATALOG))

        obs=dict(v,client_build='patchA',game_turn='1')
        obs['opponent_public_cards']+= [dict(id=7,card_id='Summoned',generated=False)]
        events=[{'event':'proposed','decision_id':1,'observation':obs,'action':{'kind':'end_turn'}},
                {'event':'confirmed','decision_id':1}]
        sample=strategy_sample(events)
        self.assertEqual(sample['opponent_played_originals'],['Marker1','Marker2','Common'])
        self.assertEqual((sample['opponent_build'], sample['opponent_class']),('patchA','ROGUE'))

    def test_backstab_case_scores_only_the_conditional_board(self):
        vanilla={'type':'MINION','text':'','attack':2,'health':2,'cost':2}
        catalog={'A':vanilla,'B':dict(vanilla,health=3),'CS2_072':CATALOG['CORE_CS2_072']}
        lists=[('ROGUE','Combo',{'CS2_072':2},80,'source','today')]
        view={'game_type':'GT_RANKED','format':'FT_STANDARD','phase':'main','game_turn':'3','enemy_secret_count':0,
              'strategy':{'matchup':{'enemy_class':'ROGUE'}},'enemy_mana_crystals':1,'my_board':[],'enemy_board':[],
              'my_hand':[{'id':10,'card_id':'A','cost':2},{'id':11,'card_id':'B','cost':2}]}
        menu={'a':{'kind':'play','source_id':10},'b':{'kind':'play','source_id':11},'z':{'kind':'end_turn'}}
        e={'decision_id':3,'observation':view,'actions':menu,'action':menu['a']}
        trial=exercise(e,lists,catalog)
        self.assertEqual(trial['better_on_all_scored_dimensions'],[menu['b']])
        self.assertEqual(trial['basis'],'hypothetical_class_examples')
        self.assertEqual([r['friendly_attack_if_played']['CS2_072'] for r in trial['alternatives']],[0,2,0])
        self.assertIsNone(exercise(dict(e,observation=dict(view,enemy_secret_count=1)),lists,catalog))

    def test_matchup_lesson_quiet_without_its_evidence_and_paid_repeat_explained(self):
        rule={'phase':'main','zone':'my_hand','card':'A','advice':'keep a refill','exception':'take lethal',
              'opponent_class':'ROGUE','opponent_archetype':'Combo'}
        view={'phase':'main','game_turn':'3','my_hand':[{'card_id':'A'}],'strategy':{'matchup':{'enemy_class':'ROGUE'}}}
        self.assertFalse(hints({'lessons':[rule]},view))
        self.assertTrue(hints({'lessons':[rule]},dict(view,opponent_list_hypothesis={'examples':['Combo']})))
        event={'event':'proposed','model':'gpt-6.1-sol','observation':view,'actions':{'a':{'kind':'end_turn'}},'elapsed_ms':9000,
               'server_options_id':3,'decision_id':1}
        summary=thinking_review([event,dict(event,decision_id=2),dict(event,decision_id=3,server_options_id=4)])
        self.assertEqual(summary['repeated_identical_windows'],[[1,2]])
        self.assertEqual(summary['single_option_calls'],[1,2,3])

    def test_class_opening_counts_each_archetype_once(self):
        cheap=('ROGUE','Cheap',{f'A{i}':2 for i in range(15)},100,'s','today')
        slow=('ROGUE','Slow',{f'B{i}':2 for i in range(15)},40,'s','today')
        catalog={c:{'type':'MINION','cost':2} for c in cheap[2]}
        catalog.update({c:{'type':'MINION','cost':7} for c in slow[2]})
        initial=class_plan('ROGUE',[cheap,cheap,slow],catalog)
        self.assertEqual((initial['published_archetypes'],initial['first_four_with_two_or_cheaper_minion_pct']),(2,50))
        self.assertNotIn('unseen_watch',initial)
