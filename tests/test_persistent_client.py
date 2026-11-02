import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import session
from game_boundary import LogBoundary, result_dismissal
from incremental_board import IncrementalBoard, LogFollower
from capture_practice import Capture
from navigation import Stop, DECK_ID
from ranked_autoplay import run_autoplay, stop_reason, AfterGameStop
from strategy import PACK
from test_incremental_board import fixture, power


class BoundaryTests(unittest.TestCase):
    def test_result_animations_can_exceed_one_minute_and_four_clicks(self):
        client=session.PersistentClient(None,0);client.pid=444
        client.previous={'outcome':'WON'};client.boundary=Mock();client.tick=Mock()
        clock=[0]
        result={'scene':{'mode':4,'sceneLoaded':True,'transitioning':False}}
        menu={'scene':{'mode':7,'sceneLoaded':True,'transitioning':False}}
        def delay(_):clock[0]+=10
        with patch('ranked_autoplay.read_small',return_value=None), \
             patch('session.read_status',side_effect=[result]*8+[menu]), \
             patch('session.next_ranked_navigation',return_value=(1070,735)), \
             patch('session.time.monotonic',side_effect=lambda:clock[0]),patch('session.time.sleep',side_effect=delay), \
             patch('session.navigation_click') as click,patch('session.emit'):
            client.return_to_menu()
        self.assertEqual(clock[0],80)
        self.assertEqual(click.call_count,8)
        self.assertTrue(all(c.args==((735,845),) for c in click.call_args_list))
        self.assertEqual(client.tick.call_count,9)
        self.assertEqual(client.boundary.check.call_count,9)

    def test_postgame_stop_prevents_any_further_navigation(self):
        client=session.PersistentClient(None,0);client.previous={'outcome':'WON'}
        client.boundary=Mock();client.tick=Mock()
        with patch('ranked_autoplay.read_small',return_value={'reason':'user_requested_stop'}), \
             patch('session.navigation_click') as click,patch('session.read_status') as read,patch('session.emit'):
            with self.assertRaises(AfterGameStop):client.return_to_menu()
        click.assert_not_called();read.assert_not_called()

    def test_a_stop_in_result_screen_does_not_create_an_incomplete_match(self):
        client=Mock();client.prepare.side_effect=AfterGameStop()
        with patch('session.session_directory') as directory,patch('session.emit'):
            with self.assertRaises(AfterGameStop):session.run({},None,'ranked',client=client)
        directory.assert_not_called()

    def test_fatal_client_scene_is_still_a_real_failure(self):
        client=session.PersistentClient(None,0);client.previous={'outcome':'WON'}
        client.boundary=Mock();client.tick=Mock()
        with patch('ranked_autoplay.read_small',return_value=None), \
             patch('session.read_status',return_value={'scene':{'mode':9}}), \
             patch('session.navigation_click') as click,patch('session.emit'):
            with self.assertRaisesRegex(session.SessionError,'错误场景'):client.return_to_menu()
        click.assert_not_called()

    def test_new_reader_does_not_replay_previous_match_or_carry_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'Power.log'
            path.write_bytes(fixture()+power('TAG_CHANGE Entity=1 tag=TURN value=25').encode())
            stat=path.stat();cursor={'source':str(path),'device':stat.st_dev,'inode':stat.st_ino,'offset':stat.st_size}
            boundary=LogBoundary(cursor,root);offset=boundary.queue_offset()
            parser=IncrementalBoard();reader=LogFollower(path,parser,start_offset=offset)
            capture=Capture(path,root/'evidence.log',start_offset=offset)
            try:
                self.assertEqual(reader.poll(),0);self.assertIsNone(parser.game)
                with path.open('ab') as out:out.write(fixture()+power('TAG_CHANGE Entity=1 tag=TURN value=1').encode())
                with self.assertRaisesRegex(Stop,'already started'):boundary.check()
                reader.poll();capture.poll()
                self.assertEqual(parser.snapshot(1)['game_turn'],'1')
                self.assertNotIn(b'value=25',(root/'evidence.log').read_bytes())
                path.write_bytes(b'changed')
                with self.assertRaises(Stop):boundary.check()
            finally:reader.close();capture.close()

    def test_no_result_click_for_an_unfinished_covered_or_new_game(self):
        status={'scene':{'mode':4,'sceneLoaded':True,'transitioning':False}}
        self.assertEqual(result_dismissal(status,'WON'),(735,845))
        with self.assertRaises(Stop):result_dismissal(status,'INCOMPLETE')
        for change in ({'popup':True},{'shop_open':True},{'game_menu':True},{'mulligan_waiting':True}):
            self.assertIsNone(result_dismissal({**status,**change},'WON'))
        self.assertIsNone(result_dismissal({'scene':{'mode':7,'sceneLoaded':True}},'WON'))

    def test_retained_client_requires_confirmed_worker_cleanup(self):
        review={'outcome':'WON','game_type':'GT_RANKED','game_closed':False,'game_preserved':True,
                'match_workers_closed':True,'game_pid':444,'mulligan_confirmed':True}
        self.assertIsNone(stop_reason(review))
        for change in ({'match_workers_closed':False},{'game_pid':None},{'game_preserved':False}):
            self.assertEqual(stop_reason({**review,**change}),'game_cleanup_not_verified')


class FakeChild:
    def __init__(self,pid,driver=False):
        self.pid=pid;self.driver=driver;self.returncode=None;self.polls=0;self.json_reads=0
        self.stdin=io.BytesIO();self.stdout=io.BytesIO();self.stderr=None;self.terminated=0
    def poll(self):
        self.polls+=1
        if self.driver and self.polls>=2:self.returncode=0
        return self.returncode
    def terminate(self):self.terminated+=1;self.returncode=0
    def wait(self,**_):return self.returncode


class PersistentIntegrationTests(unittest.TestCase):
    def test_two_game_lifecycle_launches_client_once_and_recycles_only_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);source=data/'Power.log';source.write_bytes(b'completed previous log line\n')
            children=[];watch=FakeChild(222);awake=FakeChild(111)
            def spawn(args,**kwargs):
                if args[0]=='/usr/bin/caffeinate':return awake
                if str(session.ROOT/'game_watchdog.py') in args:
                    self.assertFalse(children);return watch
                self.assertIn(str(session.ROOT/'resource_guard.py'),args)
                self.assertEqual(watch.terminated,0)
                if children:
                    self.assertNotIn('--new-session',args)
                    self.assertEqual(args[args.index('--log')+1],str(Path(args[args.index('--evidence')+1]).parent/'actions.log'))
                    self.assertIn('--start-offset',args)
                else:self.assertIn('--new-session',args)
                dest=Path(args[args.index('--evidence')+1]).parent
                (dest/'actions.log').write_text('mulligan: kept starting hand\n{"game_result":"WON"}\n')
                (dest/'decisions.jsonl').write_text(json.dumps({'decision_id':1,'event':'proposed','observation':{'game_type':'GT_RANKED'}})+'\n'+json.dumps({'decision_id':1,'event':'confirmed'})+'\n')
                st=source.stat();(dest/'log-cursor.json').write_text(json.dumps({'source':str(source),'device':st.st_dev,'inode':st.st_ino,'offset':st.st_size}))
                child=FakeChild(300+len(children),True);children.append(child)
                if len(children)==2:(data/'learning/pause-after-game.json').write_text('{"reason":"user_requested_stop"}')
                return child
            def read(child,*_):
                if child is watch:return {'watchdog':'armed'}
                child.json_reads+=1
                return {'preflight':{}} if child.json_reads==1 else {'started_pid':child.pid}
            def drain(streams,sources,observer=None):
                if observer:observer({'claimed_pid':444},'游戏守护')
            status={'scene':{'mode':7,'sceneLoaded':True,'transitioning':False},'selected_deck':DECK_ID,
                    'selected_deck_details':{'id':DECK_ID,'cards':[{'cardId':c,'count':n} for c,n in PACK['deck']['cards'].items()]},
                    'deck_picker':{'visualsFormatType':2,'selectedDeck':DECK_ID}}
            count=0
            def directory():
                nonlocal count
                count+=1;p=data/'runs'/('run-%d'%count);p.mkdir(parents=True);return p
            with patch('session.DATA',data), patch('session.preflight'), patch('session.subprocess.Popen',side_effect=spawn), \
                 patch('session.read_json_line',side_effect=read),patch('session.drain_guards',side_effect=drain), \
                 patch('session.processes',return_value=[(444,1024,0)]),patch('session.read_status',return_value=status), \
                 patch('session.LogBoundary',side_effect=lambda value:LogBoundary(value,data)), \
                 patch('session.session_directory',side_effect=directory),patch('session.time.sleep'), \
                 patch('session.launch_hearthstone') as launch,patch('session.navigation_click') as click, \
                 patch('ranked_autoplay.readiness',return_value={'ready':True,'reasons':[]}), \
                 patch('ranked_autoplay.swap_used',return_value=0),contextlib.redirect_stdout(io.StringIO()):
                state=run_autoplay({'api_key':'fake-secret-not-a-key'},None,data,session.run,session.emit,
                                   pause=Mock(),client_factory=session.PersistentClient)
            self.assertEqual(state['completed_games'],2);self.assertEqual(state['game_pid'],444)
            launch.assert_called_once();click.assert_not_called()
            self.assertEqual(watch.terminated,1);self.assertTrue(watch.stdout.closed)
            self.assertTrue(all(p.stdout.closed and p.stdin.closed for p in children))
            history=json.loads((data/'learning/match-history.json').read_text())
            self.assertTrue(all(x['game_preserved'] and x['match_workers_closed'] and not x['game_closed'] for x in history))
