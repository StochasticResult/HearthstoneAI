import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import practice_driver as driver
import session
from ranked_autoplay import AfterGameStop
from ranked_focus import FocusLost, PointCovered
from incremental_board import IncrementalBoard
from openai_advisor import OpenAIAdvisor
from test_incremental_board import fixture, power
from test_mulligan_wait import waiting_driver
from test_openai_advisor import response
from test_practice_driver import sample


class ForegroundRecoveryTests(unittest.TestCase):
    def test_native_shortcut_requires_two_separate_presses_and_rejects_foreign_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory)/'focus-test')
            source = Path(__file__).with_name('ranked_focus_native.c')
            subprocess.run(['/usr/bin/xcrun','clang','-Os','-Wall','-Wextra','-Werror','-Wno-deprecated-declarations',
                            str(source),'-framework','Carbon','-framework','ApplicationServices','-o',binary],check=True)
            subprocess.run([binary],check=True,timeout=5)

    def test_result_focus_race_reobserves_and_does_not_click_old_point(self):
        c = session.PersistentClient(None, 0)
        c.previous = {'outcome':'WON'}
        c.pid = 59343
        c.boundary, c.tick = Mock(), Mock()
        result = {'scene':{'mode':4, 'sceneLoaded':True, 'transitioning':False}}
        menu = {'scene':{'mode':7, 'sceneLoaded':True, 'transitioning':False}}
        with tempfile.TemporaryFile() as lease:
            lease.write(bytes(9)); lease.flush()
            c.focus = Mock(file=lease)
            # Exact postgame pre-input refusal after a successful foreground check.
            c.focus.state.side_effect = [b'F', b'F', b'W', b'L', b'F']
            with patch('ranked_autoplay.read_small',return_value=None), patch('session.time.sleep'), \
                 patch('session.read_status',side_effect=[result,menu]), patch('session.emit'), \
                 patch('session.next_ranked_navigation',return_value=(1070,735)), \
                 patch('session.navigation_click', side_effect=FocusLost('pre-input')) as click, \
                 patch('session.stop_child') as stop:
                c.return_to_menu()
            click.assert_called_once_with((735,833), managed=True, game_pid=59343)
            stop.assert_not_called()
            self.assertEqual(c.boundary.check.call_count,4)
            self.assertTrue(all(x.args==(59343,) for x in c.focus.bind.call_args_list))

    def test_user_after_game_stop_during_focus_wait_wins_without_input(self):
        c = session.PersistentClient(None,0); c.previous={'outcome':'WON'}; c.pid=59343
        c.tick, c.boundary = Mock(), Mock()
        with patch.object(c,'foreground_ready',return_value=False), \
             patch('ranked_autoplay.read_small',side_effect=[None,{'reason':'user_requested_stop'}]), \
             patch('session.emit'), patch('session.time.sleep'), patch('session.navigation_click') as click:
            with self.assertRaises(AfterGameStop):c.return_to_menu()
        click.assert_not_called()

    def test_rejected_queue_click_and_uncertain_failure_take_different_paths(self):
        c=session.PersistentClient(None,0); c.focus=Mock()
        ok = subprocess.CompletedProcess([],0,'{"x":0,"y":33,"width":1470,"height":846}','')
        rejected = subprocess.CompletedProcess([],1,'','Hearthstone is not the frontmost app')
        uncertain = subprocess.CompletedProcess([],1,'','Cannot create mouse event')
        with patch.object(c,'foreground_ready',return_value=True), \
             patch('session.subprocess.run',side_effect=[ok,rejected,ok,uncertain]) as run:
            self.assertFalse(c.click_if_ready((1070,735)))
            with self.assertRaisesRegex(session.SessionError,'Cannot create mouse event'):
                c.click_if_ready((1070,735))
            self.assertEqual(run.call_count,4)

    def test_foreground_overlay_cannot_receive_the_result_click(self):
        c=session.PersistentClient(None,0); c.pid=67558; c.focus=Mock()
        geometry=subprocess.CompletedProcess([],0,'{"x":0,"y":33,"width":1470,"height":846}','')
        click=subprocess.CompletedProcess([],0,'','')
        with patch.object(c,'foreground_ready',return_value=True), \
             patch('session.check_point',side_effect=[PointCovered(),None]) as hit, \
             patch('session.subprocess.run',side_effect=[geometry,geometry,click]) as run:
            # The actual covered point and clear point measured after the 14:50 game.
            self.assertTrue(c.click_if_ready((735,845),((735,833),)))
            self.assertEqual([x.args for x in hit.call_args_list],[(67558,735,845),(67558,735,833)])
            self.assertEqual([x.args[0][-2:] for x in run.call_args_list if 'click' in x.args[0]],[['735','833']])
        with patch.object(c,'foreground_ready',return_value=True),patch('session.check_point',side_effect=PointCovered()), \
             patch('session.subprocess.run',return_value=geometry) as run:
            self.assertFalse(c.click_if_ready((735,833),((735,845),)))
            self.assertFalse(any('click' in x.args[0] for x in run.call_args_list))

    def test_observe_game_end_during_locked_focus_wait_without_api_or_mouse(self):
        d=waiting_driver()
        d.game_result=Mock(side_effect=[None,None,'WON'])
        with tempfile.TemporaryFile() as lease:
            lease.write(bytes(4)+b'L'); lease.flush()
            with patch.object(driver,'FOCUS_LEASE',lease), patch.object(driver.time,'sleep'), \
                 patch.object(driver,'mirror') as mirror, patch.object(driver,'click') as click, \
                 contextlib.redirect_stdout(io.StringIO()):
                d.run(0,0)
        self.assertEqual(d.poll.call_count,3)
        click.assert_not_called();mirror.assert_not_called();d.advisor.ask.assert_not_called()
        d.advisor.record.assert_called_once_with({'event':'game_complete','outcome':'WON'})

    def test_native_refusal_posts_nothing_and_successful_drag_is_counted_once(self):
        with tempfile.TemporaryFile() as lease:
            lease.write(bytes(4)+b'F');lease.flush()
            rejected = subprocess.CompletedProcess([],1,'','Hearthstone is not the frontmost app')
            ok = subprocess.CompletedProcess([],0,'','')
            with patch.object(driver,'FOCUS_LEASE',lease), patch.object(driver,'posted_inputs',0), \
                 patch.object(driver,'check_point'), \
                 patch('practice_driver.subprocess.run',side_effect=[rejected,ok]) as native:
                cmd=['native','--input','drag',1,2,3,4]
                with self.assertRaises(FocusLost):driver.run_checked(cmd)
                self.assertEqual(driver.posted_inputs,0)
                driver.run_checked(cmd);self.assertEqual(driver.posted_inputs,1)
                os.pwrite(lease.fileno(),b'W',4)
                with self.assertRaises(FocusLost):driver.run_checked(cmd)
                self.assertEqual(native.call_count,2)

    def test_covered_drag_destination_never_starts_a_drag(self):
        with tempfile.TemporaryFile() as lease:
            lease.write((67558).to_bytes(4,'little')+b'F');lease.flush()
            with patch.object(driver,'FOCUS_LEASE',lease),patch.object(driver,'posted_inputs',0), \
                 patch.object(driver,'check_point',side_effect=[None,PointCovered()]) as hit, \
                 patch('practice_driver.subprocess.run') as native:
                with self.assertRaises(PointCovered):driver.run_checked(['native','--input','drag',700,817,735,510])
                self.assertEqual([c.args for c in hit.call_args_list],[(67558,700,817),(67558,735,510)])
                native.assert_not_called()
                self.assertEqual(driver.posted_inputs,0)

    def test_live_driver_reuses_only_an_unsent_decision_on_an_identical_board(self):
        for board_changed in (False, True):
            with self.subTest(board_changed=board_changed), tempfile.TemporaryFile() as lease:
                lease.write(bytes(4)+b'F');lease.flush()
                d = driver.Driver.__new__(driver.Driver)
                d.parser = IncrementalBoard()
                d.parser.feed(fixture()+power('TAG_CHANGE Entity=1 tag=TURN value=16').encode())
                d.owner=1; d.identity_game=d.parser.game; d.poll=Mock(); d.identify=Mock(return_value=1)
                source = [14]
                def snapshot(_own):
                    s = sample()
                    s.update(game_type='GT_RANKED',format='FT_STANDARD',game_turn='16',game_step='MAIN_ACTION',
                             visual_turn='16',visual_step='MAIN_ACTION',options_id=source[0],phase='main')
                    s['my_hand'][0]['id'] = s['legal_options'][0]['id'] = source[0]
                    return s
                d.snapshot=snapshot
                d.advisor=OpenAIAdvisor({'api_key':'offline-test-placeholder'}, lambda _p: response('a0'))
                posted = []
                def execute(_own, state, action, *_):
                    if not posted:
                        posted.append(None)
                        if board_changed:source[0]=41
                        raise FocusLost('rejected before mouse down')
                    posted.append(action['source_id'])
                    d.advisor.confirm(action,state)
                d.execute_main_action=execute
                with patch.object(driver,'FOCUS_LEASE',lease), patch.object(driver,'RUN_MODE','ranked'), \
                     patch.object(driver,'mirror',return_value={}), patch.object(driver,'checked_geometry'), \
                     patch.object(driver.time,'sleep'), contextlib.redirect_stdout(io.StringIO()):
                    d.run(4,1)
                self.assertEqual(posted,[None,41 if board_changed else 14])
                self.assertEqual(d.advisor.calls,2 if board_changed else 1)
                self.assertEqual(d.advisor.summary['withdrawn_decisions'],1)
                self.assertEqual(d.advisor.summary['confirmed_decisions'],1)
                self.assertEqual(d.advisor.summary['unconfirmed_decisions'],[])

    def test_already_posted_battlecry_drag_is_never_saved_for_replay(self):
        d=driver.Driver.__new__(driver.Driver); d.advisor=Mock(); d.wait_for_action_boundary=Mock(return_value=False)
        game=object(); window=(game,18,11); state={'options_id':81,'game_turn':'18'}
        advice={'kind':'play','source_id':53,'target_id':28}
        with patch.object(driver,'posted_inputs',5):
            self.assertIsNone(d.recover_pointer(1,window,state,advice,4,FocusLost('pre-target rejection')))
        d.wait_for_action_boundary.assert_called_once_with(1,window,81)
        self.assertEqual(d.advisor.record.call_args.args[0]['event'],'resumed_after_uncertain_input')
