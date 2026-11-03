import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from openai_advisor import AdvisorError, OpenAIAdvisor, REASON_TAGS, parse_response, request_payload, usage_parts
from test_openai_advisor import response


class CompactGameOutputTests(unittest.TestCase):
    def test_same_ranked_menu_and_plan_with_a_reason_tag(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/ranked-turn-plan-20261102.json').read_text())
        steps=fixture['steps']
        row=steps[0]
        v,m=row['observation'],row['actions']
        prefix=next(k for k,a in m.items() if a==steps[0]['confirmed_action'])
        tail=[next(k for k,a in m.items() if a==s['confirmed_action']) for s in steps[1:3]]
        prose,_=request_payload('gpt-6.1-sol',v,m,plan_prefixes=[prefix])
        tags,_=request_payload('gpt-6.1-sol',v,m,plan_prefixes=[prefix],compact_output=True)
        self.assertEqual(json.loads(prose['input']),json.loads(tags['input']))
        params=tags['tools'][0]['parameters']['properties']
        self.assertEqual(params['reason']['enum'],list(REASON_TAGS))
        self.assertNotIn('enum',prose['tools'][0]['parameters']['properties']['reason'])
        reply=response(prefix)
        reply['output'][0]['arguments']=json.dumps({'reason':'face','action_id':prefix,'follow_up_ids':tail})
        act,reason,_,follow=parse_response(reply,m,with_plan=True,compact_output=True)
        self.assertEqual((act,reason,follow),(steps[0]['confirmed_action'],REASON_TAGS['face'],[m[k] for k in tail]))
        reply['output'][0]['arguments']=json.dumps({'reason':'long explanation','action_id':prefix})
        with self.assertRaises(AdvisorError): parse_response(reply,m,compact_output=True)

    @patch('builtins.print')
    def test_forced_action_uses_no_request_but_unknown_options_still_require_a_choice(self,_):
        view={'phase':'main','game_type':'GT_RANKED','format':'FT_STANDARD','my_hand':[]}
        menu={'a0':{'kind':'choose_target','target_id':123}}
        reply=response('a0')
        reply['output'][0]['arguments']=json.dumps({'action_id':'a0','reason':'survive'})
        reply['usage']={'input_tokens':1420,'output_tokens':84,'total_tokens':1504,'output_tokens_details':{'reasoning_tokens':58}}
        api=Mock(return_value=reply)
        ai=OpenAIAdvisor({'api_key':'offline-placeholder-for-test','compact_output':True},api)
        self.addCleanup(ai.close)
        self.assertEqual(ai.select(view,menu),menu['a0'])
        api.assert_not_called()
        self.assertEqual(ai.select(dict(view,unavailable_interactions=[{'source_id':9}]),menu),menu['a0'])
        api.assert_called_once()
        totals=ai.summary['model_token_usage']['gpt-6.1-sol']
        self.assertEqual(totals['output_tokens'],84)
        self.assertEqual(totals['reasoning_tokens'],58)
        self.assertNotIn('cached_input_tokens',totals)
        self.assertEqual(usage_parts({'usage':{'total_tokens':123}}),{})
