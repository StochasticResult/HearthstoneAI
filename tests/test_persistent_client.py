import contextlib
import io
import json
import datetime
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import session
from game_boundary import FaultedMatchEnd, LogBoundary, idle_log_start, result_dismissal
from incremental_board import IncrementalBoard, LogFollower
from capture_practice import Capture
from navigation import Stop, DECK_ID
from ranked_autoplay import run_autoplay, stop_reason, AfterGameStop
from strategy import PACK
from test_incremental_board import fixture, power


class BoundaryTests(unittest.TestCase):
    def test_recycle_requires_confirmed_end_same_pid_and_no_new_match(self):
        client = session.PersistentClient(os.getppid(),0)
        client.pid = 777
        client.watchdog = Mock()
        client.boundary = Mock()
        client.previous = {'outcome':'WON','game_pid':777,'match_workers_closed':True}
        normal={'reasons':[], 'memory_pressure_level':1}
        busy={'memory_pressure_level':2, 'swapout_mib_per_minute':512}
        sustained=dict(busy,sustained_paging_mib_per_minute=[468,512])
        with patch('session.processes',return_value=[(777,2400*session.MIB,0)]) as processes, \
             patch('ranked_autoplay.read_small',return_value=None) as request,patch('session.emit'):
            for sample in (normal,busy,dict(sustained,memory_pressure_level=1)):
                client.recycle_at_boundary(sample)
            client.watchdog.send_signal.assert_not_called()
            client.match_at_risk = True
            client.recycle_at_boundary(sustained)
            client.watchdog.send_signal.assert_not_called()
            client.match_at_risk = False
            client.boundary.check.side_effect = Stop('a new game')
            with self.assertRaises(Stop):client.recycle_at_boundary(sustained)
            client.watchdog.send_signal.assert_not_called()
            client.boundary.check.side_effect = None
            request.return_value = {'reason':'user_requested_stop'}
            with self.assertRaises(AfterGameStop):client.recycle_at_boundary(sustained)
            client.watchdog.send_signal.assert_not_called()
            request.return_value = None
            processes.return_value=[(777,1356*session.MIB,0)]
            with self.assertRaisesRegex(session.SessionError,'局间回收'):client.recycle_at_boundary(sustained)
            client.watchdog.send_signal.assert_called_once_with(signal.SIGHUP)
            processes.return_value=[(778,1811*session.MIB,0)]
            with self.assertRaisesRegex(session.SessionError,'身份改变'):client.recycle_at_boundary(sustained)
            client.watchdog.send_signal.assert_called_once()

    def test_only_a_verified_closed_resource_guard_can_reset_the_client(self):
        client = session.PersistentClient(os.getppid(),0)
        watch=Mock();watch.stdout=None;client.watchdog=watch;client.pid=777
        client.match_at_risk=True;client.launched=True;client.guard_reason='disk reserve'
        with patch('session.processes',return_value=[(777,0,0)]) as processes,patch('session.stop_child'):
            watch.poll.return_value=2
            self.assertIsNone(client.recover_closed_guard())
            processes.return_value=[]
            client.guard_reason='game exited'
            self.assertIsNone(client.recover_closed_guard())
            client.guard_reason='disk reserve'
            self.assertEqual(client.recover_closed_guard(),'disk reserve')
            self.assertIsNone(client.pid);self.assertIsNone(client.watchdog)
            self.assertFalse(client.match_at_risk or client.launched)

    def test_existing_lobby_starts_at_current_end_without_launching_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            began = datetime.datetime(2026, 11, 2, 22, 14, 57)
            folder = root / began.strftime('Hearthstone_%Y_%m_%d_%H_%M_%S')
            folder.mkdir()
            log = folder / 'Power.log'
            stamp = int(began.timestamp())
            self.assertEqual(idle_log_start(stamp, root), (log, 0))
            log.write_bytes(b'an older settled match\n')
            client = session.PersistentClient(222, 0)
            for mode, stage in ((3, 0), (7, 1)):
                with patch('session.process_started', return_value=stamp), patch('session.read_status',
                        return_value={'scene': {'mode': mode, 'sceneLoaded': True}}), patch('session.preflight') as guard, \
                        patch('session.idle_log_start', side_effect=lambda start: idle_log_start(start, root)):
                    self.assertEqual(client.adopt_existing(777), ['--adopt-pid', '777', '--adopt-start', str(stamp)])
                    self.assertEqual(client.driver_log_args(), ['--log', str(log), '--start-offset', str(log.stat().st_size)])
                    self.assertEqual(client.initial_stage, stage)
                    guard.assert_called_once_with(existing_pid=777)
            log.write_bytes(b'partial')
            with self.assertRaisesRegex(Stop, 'between lines'):
                idle_log_start(stamp, root)
            with self.assertRaisesRegex(Stop, 'identify'):
                idle_log_start(stamp+86400, root)

    def test_only_verified_ended_match_in_same_process_can_be_readopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'Power.log'
            log.write_bytes(b'settled game\n')
            info = log.stat()
            marker = {'pid': 777, 'started': 100, 'outcome': 'WON', 'cursor': {'source': str(log),
                      'device': info.st_dev, 'inode': info.st_ino, 'offset': info.st_size}}
            state = {'scene': {'mode': 4, 'sceneLoaded': True}}
            with patch('session.process_started', return_value=100), patch('session.read_status', return_value=state), \
                    patch('coach_policy.read_json', return_value=marker), patch('session.preflight'), \
                    patch('session.LogBoundary', side_effect=lambda c: LogBoundary(c, root)):
                client = session.PersistentClient(222, 0)
                client.adopt_existing(777)
                self.assertEqual(client.previous, {'outcome': 'WON'})
                with self.assertRaises(session.SessionError):
                    session.PersistentClient(222, 0).adopt_existing(888)
                state['mulligan_waiting'] = True
                with self.assertRaises(session.SessionError):
                    session.PersistentClient(222, 0).adopt_existing(777)
                del state['mulligan_waiting']
                with log.open('ab') as stream:
                    stream.write(b'CREATE_GAME\n')
                with self.assertRaisesRegex(Stop, 'new game'):
                    session.PersistentClient(222, 0).adopt_existing(777)

    def test_immediate_stop_does_not_terminate_ranked_game_guard_in_worker_finally(self):
        client = session.PersistentClient(222, 0)
        client.watchdog = Mock()
        client.watchdog.poll.return_value = None
        client.watchdog.stdout = None
        with patch.object(client, 'prepare'), patch('session.session_directory', side_effect=session.SessionCancelled('stop')), \
                patch('session.processes', return_value=[(777, 0, 0)]), patch('session.emit'), patch('session.stop_child') as stop:
            with self.assertRaises(session.SessionCancelled):
                session.run({}, 222, mode='ranked', client=client)
            self.assertNotIn(unittest.mock.call(client.watchdog), stop.call_args_list)
            client.close()
            client.watchdog.send_signal.assert_called_once_with(session.signal.SIGUSR2)

    def test_fault_watch_uses_only_complete_authoritative_lines_in_this_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'Power.log'
            create=power('CREATE_GAME').encode()
            terminal=power('TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE').encode()
            old=create+terminal
            path.write_bytes(old+create+terminal.replace(b'GameState.',b'PowerTaskList.')+terminal[:-1])
            stat=path.stat()
            cursor={'source':str(path),'device':stat.st_dev,'inode':stat.st_ino,'start_offset':len(old)}
            watch=FaultedMatchEnd(cursor,root)
            self.assertFalse(watch.poll())
            with path.open('ab') as f:f.write(b'\n')
            self.assertTrue(watch.poll())
            with path.open('ab') as f:f.write(create)
            self.assertFalse(watch.poll())
            path.rename(root/'old');path.write_bytes(old)
            with self.assertRaisesRegex(Stop,'changed'):watch.poll()

    def test_ai_fault_keeps_guard_until_verified_lobby_and_never_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=session.PersistentClient(None,0);c.pid=777;c.match_at_risk=True
            c.watchdog=Mock();c.watchdog.poll.return_value=None;c.on_match_hold=Mock()
            clock=[0]
            def sleep(seconds):clock[0]+=seconds
            matched={'scene':{'mode':4,'sceneLoaded':True,'transitioning':False}}
            lobby={'scene':{'mode':7,'sceneLoaded':True,'transitioning':False}}
            readings = [None,matched,lobby,matched,lobby,lobby]
            def status():
                self.assertTrue(c.match_at_risk)
                return readings.pop(0)
            with patch('session.time.monotonic',side_effect=lambda:clock[0]),patch('session.time.sleep',side_effect=sleep), \
                 patch('session.read_status',side_effect=status),patch('session.emit'),patch('session.drain_guards') as drain, \
                 patch('session.navigation_click') as click,patch('session.stop_child') as stop:
                c.hold_after_ai_fault(Path(tmp),{}, {},False)
            self.assertFalse(readings)
            self.assertFalse(c.match_at_risk)
            self.assertGreater(drain.call_count,5)
            c.on_match_hold.assert_called_once()
            click.assert_not_called()
            stop.assert_not_called()

    def test_last_resort_cleanup_hands_live_match_to_guard_without_sigterm(self):
        c=session.PersistentClient(222,0);c.watchdog=Mock();c.watchdog.poll.return_value=None
        c.match_at_risk=True;c.streams={}
        with patch('session.stop_child') as stop:
            c.close()
        c.watchdog.send_signal.assert_called_once_with(session.signal.SIGUSR1)
        stop.assert_not_called()
        c.match_at_risk=False
        with patch('session.stop_child') as stop:
            c.close()
        self.assertEqual(c.watchdog.send_signal.call_args.args,(session.signal.SIGUSR2,))
        stop.assert_not_called()
        c.match_at_risk = True
        c.watchdog.send_signal.reset_mock()
        with patch('session.stop_child') as stop:
            c.close(user_stopped=True)
        c.watchdog.send_signal.assert_called_once_with(session.signal.SIGUSR2)
        stop.assert_not_called()

    def test_new_game_at_old_result_boundary_protects_client_even_during_planned_pause(self):
        c=session.PersistentClient(222,0);c.previous={'outcome':'WON'};c.launched=True
        c.boundary=Mock();c.tick=Mock()
        c.boundary.check.side_effect=Stop('A new game already started outside the queue controller')
        with patch('ranked_autoplay.read_small',return_value={'reason':'review_update'}), \
             patch('session.navigation_click') as click,patch('session.emit'):
            with self.assertRaisesRegex(Stop,'new game'):
                c.return_to_menu()
        self.assertTrue(c.match_at_risk)
        click.assert_not_called()

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
    def test_worker_crash_and_a_second_review_error_cannot_terminate_a_live_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp); dest=data/'runs/run-fault';dest.mkdir(parents=True)
            (dest/'actions.log').write_text('practice driver stopped: test observation failure\n')
            client=session.PersistentClient(os.getppid(),0)
            watch=FakeChild(901);watch.send_signal=Mock()
            worker=FakeChild(902); statuses=iter((None,None,2))
            def worker_status():
                worker.returncode=next(statuses,2)
                return worker.returncode
            worker.poll=worker_status
            client.watchdog=watch;client.pid=777;client.prepare=Mock()
            live={'scene':{'mode':4,'sceneLoaded':True,'transitioning':False},'game_type':7,'format':2}
            def cannot_save_review(_dest,_streams,_sources,entered):
                self.assertTrue(entered)
                self.assertTrue(client.match_at_risk)
                self.assertEqual(watch.terminated,0)
                raise OSError('test failure while recording handoff')
            client.hold_after_ai_fault=Mock(side_effect=cannot_save_review)
            with patch('session.DATA',data),patch('session.session_directory',return_value=dest), \
                 patch('session.subprocess.Popen',return_value=worker),patch('session.drain_guards'), \
                 patch('session.read_json_line',side_effect=[{'preflight':{}},{'started_pid':902}]), \
                 patch('session.launch_hearthstone'),patch('session.focus_hearthstone'), \
                 patch('session.read_status',return_value=live),patch('session.time.sleep'), \
                 patch('session.processes',return_value=[(777,0,0)]),patch('session.emit'), \
                 patch('session.navigation_click') as click:
                with self.assertRaisesRegex(OSError,'recording handoff'):
                    session.run({'api_key':'fixture','model':'test'},os.getppid(),'ranked',0,client)
                client.close()
            self.assertEqual(watch.terminated,0)
            watch.send_signal.assert_called_once_with(session.signal.SIGUSR1)
            client.hold_after_ai_fault.assert_called_once()
            click.assert_not_called()

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
