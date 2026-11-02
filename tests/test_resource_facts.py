import unittest
from incremental_board import IncrementalBoard
from test_incremental_board import fixture, power
from openai_advisor import CATALOG, OpenAIAdvisor, public_view, enrich
from resource_facts import coin_result, resource_facts
from rule_transfer import cases


class ResourceFactsTests(unittest.TestCase):
    def test_coin_capacity_cost_and_small_turn_crystals(self):
        self.assertEqual(coin_result(10,0,10),{'mana_after':10,'net_gain':0,'wasted_at_cap':1})
        self.assertEqual(coin_result(10,2,10)['mana_after'],9)
        self.assertEqual(coin_result(3,0,10)['mana_after'],4)
        self.assertEqual(coin_result(10,0,20)['mana_after'],11)
        self.assertIsNone(coin_result(1,2,10))
        self.assertIsNone(coin_result(10,0,None))

    def test_double_tax_holdout_exposes_exact_spend_then_coin_budget(self):
        s=cases()[3]['state'];facts=resource_facts(s,CATALOG)
        scenarios=facts['coins'][0]['conditional_scenarios']
        self.assertEqual(scenarios[0]['coin_cost'],2)
        self.assertEqual(scenarios[0]['play_now']['mana_after'],9)
        self.assertEqual(scenarios[2]['play_now']['wasted_at_cap'],1)
        self.assertIn({'spend_before_coin':7,'mana_after_coin':4},scenarios[2]['spend_first'])
        self.assertEqual(facts['spell_cost_if_mawmen_leave'][0],{'id':101,'cost_by_removed_count':[6,5,4]})
        self.assertEqual(enrich(public_view(s))['resource_calculations'],facts)

    def test_cost_modifier_disables_hypothetical_aura_inversion(self):
        s=cases()[3]['state'];s['my_hand'][2]['cost']=0
        row=resource_facts(s,CATALOG)['coins'][0]
        self.assertNotIn('conditional_scenarios',row)
        self.assertEqual(row['play_now']['wasted_at_cap'],1)
        s=cases()[3]['state'];s['my_hand'][2]['modifiers']=[{'card_id':'UNRECOGNIZED_COST_EFFECT'}]
        projected=enrich(public_view(s))
        self.assertEqual(projected['my_hand'][2]['unknown_modifier_count'],1)
        self.assertNotIn('conditional_scenarios',projected['resource_calculations']['coins'][0])

    def test_log_resources_include_temporary_locked_and_observed_cap(self):
        p=IncrementalBoard();p.feed(fixture())
        p.feed(''.join(power('TAG_CHANGE Entity=2 tag=%s value=%s'%r) for r in
                      [('MAXRESOURCES',10),('RESOURCES_USED',1),('OVERLOAD_LOCKED',2),('TEMP_RESOURCES',1)]).encode())
        s=p.snapshot(1,extended=True)
        self.assertEqual(s['mana'],2)  # 4 permanent - 1 spent - 2 locked + 1 temporary.
        self.assertEqual(s['mana_cap'],10)
        self.assertEqual((s['mana_spent'],s['overload_locked'],s['temporary_mana']),(1,2,1))
        p.feed(power('TAG_CHANGE Entity=2 tag=TEMP_RESOURCES value=0').encode())
        self.assertEqual(p.snapshot(1,extended=True)['mana'],1)

    def test_confirmed_coin_is_reconciled_with_next_observed_mana(self):
        s=cases()[3]['state'];s['mana']=3
        s['my_hand'][2]['cost']=0
        advisor=OpenAIAdvisor({'api_key':'fake-key-not-a-secret-123'})
        advisor.confirm({'kind':'play','source_id':102},s)
        s['mana']=4
        advisor.view(s)
        self.assertEqual(advisor.confirmed[-1]['mana_before'],3)
        self.assertEqual(advisor.confirmed[-1]['mana_after_observed'],4)
        self.assertEqual(advisor.confirmed[-1]['observed_cost'],0)
        s['mana']=2
        advisor.view(s)
        self.assertEqual(advisor.confirmed[-1]['mana_after_observed'],4)  # Do not rewrite the first observation.
