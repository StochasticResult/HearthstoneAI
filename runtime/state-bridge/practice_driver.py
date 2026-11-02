#!/usr/bin/env python3
"""Standalone practice/Standard ranked driver using verified local observations.

This does not open the game, queue games, spend currency, or use
the earlier controller. A separately armed game watchdog is mandatory. Input
is checked against fresh legal options, then verified in the log. Unsupported
states, missing confirmation, loss of focus, or incomplete data stop the run.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from input_command import input_command
from deck_observation import public_selected_deck
from game_mode import expected_type

from capture_practice import Capture, CaptureError, LOG_ROOT
from incremental_board import IncrementalBoard, LogFollower, UnsafeState, board
from mirror_identity import practice_identity, mulligan_identity
from practice_state import META, prepare
from reconnect_options import restore_observed_options, restore_zero_hand_costs
from openai_advisor import AdvisorError, OpenAIAdvisor, action_menu, signature

ROOT = Path(__file__).resolve().parent.parent
FRAMEWORK = "/Applications/HSTracker.app/Contents/Frameworks/HearthMirror.framework/Versions/A/HearthMirror"
RUN_MODE = "practice"


class Stop(Exception):
    pass


class NotReady(Exception):
    pass


class ClientFatal(Stop):
    pass


def client_error(power_path):
    path = power_path.with_name("Hearthstone.log")
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            tail = handle.read(65536)
        if b"NetCache Error: TIMEOUT" in tail:
            return "炉石客户端设备服务请求超时（NetCache TIMEOUT）；已停止，不自动重开"
    except OSError:
        pass
    return "炉石客户端进入 FATAL_ERROR 场景；已停止，不自动重开"


def run_checked(args, timeout=4):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    # Clicking the app's "stop after this game" briefly activates its
    # control window before it returns focus to Hearthstone. A rejected
    # native command has sent no input, so allow this short handoff to finish.
    if (result.returncode and len(args) > 1 and args[1] == '--input'
            and result.stderr.strip() == 'Hearthstone is not the frontmost app'):
        deadline = time.monotonic() + .8
        while result.returncode and time.monotonic() < deadline:
            time.sleep(.15)
            result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
            if result.stderr.strip() != 'Hearthstone is not the frontmost app':
                break
    if result.returncode:
        detail = result.stderr.strip().replace("\n", " ")[:180]
        raise Stop(Path(args[0]).name + " failed" + (": " + detail if detail else ""))
    return result.stdout.strip()


def mirror():
    status = json.loads(run_checked([str(ROOT / "mirror-status"), FRAMEWORK, "--board"]))
    scene = status.get("scene") or {}
    if scene.get("mode") == 9:
        raise ClientFatal("Hearthstone client entered FATAL_ERROR")
    game_type = status.get("game_type")
    numeric_type = expected_type(RUN_MODE)[1]
    if scene.get("mode") == 4 and game_type not in (None, 0, numeric_type):
        raise Stop("mirror observed an unexpected game type: %r" % game_type)
    if game_type != numeric_type or scene.get("mode") != 4 or scene.get("sceneLoaded") is not True or scene.get("transitioning"):
        raise NotReady("practice scene is not ready: type=%r mode=%r loaded=%r transitioning=%r" %
                       (game_type, scene.get("mode"), scene.get("sceneLoaded"), scene.get("transitioning")))
    if RUN_MODE == "ranked" and status.get("format") != 2:
        raise Stop("ranked game is not confirmed Standard")
    return status


def watchdog_alive(pid):
    if pid <= 1:
        return False
    try:
        command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2, check=True).stdout
        return "game_watchdog.py" in command and ("--temporary-fps 30" in command or "--claim-pid " in command)
    except (OSError, subprocess.SubprocessError):
        return False


def checked_geometry():
    g = json.loads(run_checked(input_command("geometry")))
    expected = {"x": 0, "y": 33, "width": 1470, "height": 846}
    if any(abs(float(g[k]) - v) > 2 for k, v in expected.items()):
        raise Stop("uncalibrated game window geometry")


def click(point):
    run_checked(input_command("click", *point))


def drag(start, end):
    run_checked(input_command("drag", *start, *end))


def move(point):
    run_checked(input_command("move", *point))


def hover():
    end = time.monotonic() + 3
    while time.monotonic() < end:
        status = json.loads(run_checked([str(ROOT / "mirror-status"), FRAMEWORK, "--hover"]))
        scene = status.get("scene") or {}
        if status.get("game_type") == expected_type(RUN_MODE)[1] and scene.get("mode") == 4 and not status.get("popup") and not status.get("shop_open"):
            value = status.get("hover")
            return value if isinstance(value, dict) else {}
        time.sleep(.3)
    raise Stop("hover state unavailable: mode=%s game_type=%s popup=%s shop=%s" %
               (scene.get("mode"), status.get("game_type"), status.get("popup"), status.get("shop_open")))


def hover_matches(value, hand, source):
    card = next((c for c in hand if c["id"] == source), None)
    return bool(card and value.get("isHand") is True and value.get("side") == 1
                and value.get("cardId") == card["card_id"]
                and value.get("zonePosition") == card["position"]
                and value.get("zoneSize") == len(hand))


def waiting_for_first_mana(parser, own):
    """Second player has no RESOURCES tag until their first turn starts.

    Do not manufacture zero mana or ask the model to act in that interval.
    Missing mana on any later turn remains an error.
    """
    game = parser.game
    if not game:
        return False
    entity = board._game_entity(game)
    player = next((p for p in board._players(game) if board._tag_int(p, "PLAYER_ID") == own), None)
    return bool(entity and player and board._tag_int(entity, "TURN") <= 1
                and board._tag_int(player, "CURRENT_PLAYER") != 1
                and "RESOURCES" not in player["tags"])


def visual_ready(state):
    turn = state.get("game_turn")
    return bool(turn and state.get("visual_turn") == turn and state.get("game_step") == "MAIN_ACTION"
                and state.get("visual_step") == "MAIN_ACTION")


def hand_point(hand, eid):
    ordered = sorted(hand, key=lambda x: x["position"])
    n = len(ordered)
    if not 1 <= n <= 10 or len({x["position"] for x in ordered}) != n:
        raise Stop("invalid hand layout")
    try:
        index = next(i for i, card in enumerate(ordered) if card["id"] == eid)
    except StopIteration as exc:
        raise Stop("hand source missing") from exc
    gap = {10: 45, 9: 50, 8: 55, 7: 60, 6: 70, 5: 85}.get(n, 100)
    center = {7: 690, 8: 685, 9: 680, 10: 675}.get(n, 705)
    return round(center + (index - (n - 1) / 2) * gap), 817


def target_point(state, eid):
    if state["my_hero"]["id"] == eid:
        return (735, 690)
    if state["enemy_hero"]["id"] == eid:
        return (735, 208)
    for key, y in (("my_board", 510), ("enemy_board", 365)):
        rows = sorted(state[key], key=lambda x: x["position"])
        for i, card in enumerate(rows):
            if card["id"] == eid:
                return (round(735 + (i - (len(rows) - 1) / 2) * 105), y)
    raise Stop("target is not on a supported public board")


def plan_inputs(state, advice):
    kind = advice.get("kind")
    if kind == "end_turn":
        return ("click", (1180, 435), None)
    try:
        source = int(advice["source_id"])
        target = int(advice["target_id"]) if "target_id" in advice else None
    except (KeyError, ValueError, TypeError) as exc:
        raise Stop("invalid advisor output") from exc
    opt = next((o for o in state["legal_options"] if o["id"] == source), None)
    if opt is None or (target is not None and target not in opt["targets"]):
        raise Stop("advisor action is not in current legal options")
    if kind == "attack":
        if target is None or (not any(c["id"] == source for c in state["my_board"]) and state["my_hero"]["id"] != source):
            raise Stop("unsupported attack source or target")
        return ("drag", target_point(state, source), target_point(state, target))
    if kind == "hero_power":
        if target is not None or (state.get("my_hero_power") or {}).get("id") != source:
            raise Stop("unsupported hero power")
        return ("click_power", (870, 690), None)
    if kind == "play":
        card = next((c for c in state["my_hand"] if c["id"] == source), None)
        if card is None:
            raise Stop("play source is not in hand")
        start = hand_point(state["my_hand"], source)
        if card["card_type"] == "MINION" and target is not None:
            if target is None:
                raise Stop("freeze target missing")
            return ("battlecry", start, target_point(state, target))
        return ("drag", start, target_point(state, target) if target is not None else (735, 510))
    raise Stop("unsupported advisor action")


def choice_index(choice, board_count, mana):
    items = choice["items"]
    ids = [x["card_id"] for x in items]
    if not choice["shown"] or len(ids) not in (3, 4) or any(not x for x in ids):
        raise Stop("choice is not visibly ready or complete")
    if set(ids) == {"BE_090t1", "BE_090t2", "BE_090t3"}:
        return ids.index("BE_090t3" if board_count >= 2 else "BE_090t2")
    if set(ids) == {"CAP_405tb1b", "CAP_405tb2b", "CAP_405tb3b"}:
        return ids.index("CAP_405tb2b")
    if len(ids) == 3 and all(x in {"CAP_405t%d" % i for i in range(1, 10)} for x in ids):
        ranks = {"CAP_405t1": 2, "CAP_405t2": 5, "CAP_405t3": 2, "CAP_405t4": 6,
                 "CAP_405t5": 9, "CAP_405t6": 8, "CAP_405t7": 7, "CAP_405t8": 3, "CAP_405t9": 7}
        return max(range(len(ids)), key=lambda i: ranks[ids[i]])
    if ids == ["BE_013t1", "BE_013t2", "BE_013t3", "BE_013t4"]:
        return 3
    if choice.get("source_card_id") in ("TLC_451", "EDR_856") and all(cid in META for cid in ids):
        def value(i):
            cost = int(META[ids[i]].get("cost", 0))
            if choice["source_card_id"] == "TLC_451" and (cost > mana or (ids[i] == "BE_094" and not board_count)):
                return cost - 20
            return cost
        return max(range(len(ids)), key=value)
    raise Stop("unrecognized choice")


class Driver:
    def __init__(self, path, evidence, watchdog, advisor=None, deck_snapshot=None, reconnect_evidence=()):
        self.parser = IncrementalBoard()
        self.follower = LogFollower(path, self.parser)
        self.capture = Capture(path, evidence)
        self.watchdog = watchdog
        if advisor is None:
            raise Stop("Launch through the application with an API advisor")
        self.advisor = advisor
        self.owner = None
        self.identity_game = None
        self.board_mismatch_since = None
        self.deck_snapshot = deck_snapshot
        self.reconnect_evidence = tuple(reconnect_evidence)[:4]
        self.recovered_previous = None

    def close(self):
        self.advisor.close()
        self.follower.close()
        self.capture.close()

    def snapshot(self, own, **_kwargs):
        return self.parser.snapshot(own, allow_practice=True, extended=True)

    def recover_observed_zero_cost(self, own):
        paths, self.reconnect_evidence = self.reconnect_evidence, ()
        for path in paths:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            previous = IncrementalBoard()
            reader = LogFollower(path, previous)
            try:
                while reader.poll():
                    pass
            finally:
                reader.close()
            if restore_zero_hand_costs(self.parser, previous, own):
                self.recovered_previous = previous
                print("restored explicitly observed zero cost after matching the same game and turn", flush=True)
                return True
        return False

    def game_result(self):
        if not self.parser.game or not board.is_game_over(self.parser.game):
            return None
        players = [p for p in board._players(self.parser.game) if board._tag_int(p, "PLAYER_ID") == self.owner]
        return board._playstate(players[0]) if len(players) == 1 else "COMPLETE"

    def revalidate(self, own, old, advice):
        deadline = time.monotonic() + 4
        while True:
            self.poll()
            current = self.snapshot(own)
            if not visual_ready(current) or not current["own_turn"] or signature(current) != signature(old):
                raise Stop("state changed during decision; no input was sent")
            if advice not in action_menu(current).values():
                raise Stop("chosen action is no longer supported or legal")
            try:
                status = mirror()
                if status.get("popup") or status.get("shop_open") or status.get("game_menu"):
                    raise Stop("game is covered by a menu or popup")
                self.identify(status)
                return
            except NotReady as error:
                if time.monotonic() >= deadline:
                    raise Stop("observation did not recover before input: " + str(error)) from error
                time.sleep(.3)

    def poll(self):
        if not watchdog_alive(self.watchdog):
            raise Stop("game watchdog is not running")
        self.capture.poll()
        for _ in range(8):
            if not self.follower.poll():
                break
        if self.follower.offset < self.follower.path.stat().st_size:
            raise Stop("log reader fell behind")

    def identify(self, status):
        if self.parser.game is None or not self.parser.game["meta"].get("GameType"):
            raise NotReady("practice log is not ready")
        if self.parser.game["meta"].get("GameType") != expected_type(RUN_MODE)[0]:
            raise Stop("game does not match requested mode")
        if RUN_MODE == "ranked" and self.parser.game["meta"].get("FormatType") != "FT_STANDARD":
            raise Stop("ranked log is not confirmed Standard")
        if self.owner is not None and self.identity_game is not self.parser.game:
            raise Stop("game changed during a single-game session")
        try:
            crosschecked = practice_identity(self.parser.game, status, mode=RUN_MODE)
        except UnsafeState as error:
            sides = (status.get("board") or {})
            cards = (sides.get("friendly") or {}).get("boardCards", []) + (sides.get("opposing") or {}).get("boardCards", [])
            if cards:
                game_entity = board._game_entity(self.parser.game)
                turn = game_entity and game_entity["tags"].get("TURN")
                step = game_entity and game_entity["tags"].get("STEP")
                if (not turn or self.parser.visual_turn != turn or step != "MAIN_ACTION"
                        or self.parser.visual_step != "MAIN_ACTION"):
                    self.board_mismatch_since = None
                    raise NotReady("waiting for board animation") from error
                self.board_mismatch_since = self.board_mismatch_since or time.monotonic()
                if time.monotonic() - self.board_mismatch_since < 8:
                    raise NotReady("waiting for mirror and log to converge") from error
                raise Stop("tracker board and game log disagree") from error
            try:
                crosschecked = mulligan_identity(self.parser, status, mode=RUN_MODE)
            except UnsafeState:
                crosschecked = None
        if crosschecked is not None:
            self.board_mismatch_since = None
        candidate = crosschecked or self.owner
        if candidate not in (1, 2):
            raise NotReady("practice identity unavailable")
        if self.owner is not None and self.owner != candidate:
            raise Stop("local identity changed")
        self.owner = candidate
        self.identity_game = self.parser.game
        self.advisor.observe_deck(status.get("selected_deck_details", status.get("selected_deck")))
        if self.advisor.starting_deck is None and self.deck_snapshot and self.deck_snapshot.exists():
            if self.deck_snapshot.stat().st_size > 8192:
                raise Stop("selected deck observation exceeds its bound")
            deck = public_selected_deck(json.loads(self.deck_snapshot.read_text()))
            if deck is None:
                raise Stop("invalid selected deck observation")
            self.advisor.observe_deck(deck)
        return candidate

    def wait_until(self, predicate, seconds=8):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.poll()
            try:
                if predicate():
                    return
            except UnsafeState:
                pass
            time.sleep(.15)
        raise Stop("action was not confirmed in game log")

    def verify_hand_point(self, own, state, source, initial):
        """Find the intended hand card by asking Mirror, never by a blind click."""
        options_id = state["options_id"]
        expected_hand = [(c["id"], c["card_id"], c["position"]) for c in state["my_hand"]]

        def still_current():
            end = time.monotonic() + 10
            while time.monotonic() < end:
                self.poll()
                current = self.snapshot(own)
                hand = [(c["id"], c["card_id"], c["position"]) for c in current["my_hand"]]
                if (not current["own_turn"] or current.get("game_step") != "MAIN_ACTION"
                        or current["options_id"] != options_id or hand != expected_hand
                        or not any(o["id"] == source for o in current["legal_options"])):
                    raise Stop("hand or legal options changed while checking hover")
                if visual_ready(current):
                    return
                time.sleep(.3)
            raise Stop("current turn animation did not reach MAIN_ACTION")

        # An enlarged card can remain under the cursor after a prior action;
        # clear it first so Mirror cannot return an old enlarged-card result.
        observed = []
        # Poll the actual hover instead of always sleeping two seconds. If
        # hit-testing lags, keep the same bounded wait before scanning offsets.
        for dx in (0, -20, 20, -40, 40, -60, 60, -80, 80, 0, -20, 20):
            still_current()
            move((1050, 690))
            clear_deadline = time.monotonic() + 3
            while time.monotonic() < clear_deadline:
                if hover().get("isHand") is not True:
                    break
                time.sleep(.12)
            else:
                raise Stop("hand hover did not clear within three seconds")
            candidate = (initial[0] + dx, initial[1])
            if not watchdog_alive(self.watchdog):
                raise Stop("game watchdog is not running")
            move(candidate)
            probe_deadline = time.monotonic() + 1.8
            seen = {}
            while time.monotonic() < probe_deadline:
                seen = hover()
                if hover_matches(seen, state["my_hand"], source):
                    still_current()
                    return candidate
                time.sleep(.12)
            observed.append({"point": candidate, "card": seen.get("cardId"), "position": seen.get("zonePosition"),
                             "hand_size": seen.get("zoneSize"), "side": seen.get("side"), "is_hand": seen.get("isHand")})
        print(json.dumps({"hover_probe": observed}), flush=True)
        raise Stop("could not verify the requested hand card by hover")

    def revalidate_target(self, own, choice, state):
        self.poll()
        current = self.parser.snapshot(own, allow_practice=True, extended=True, choice_id=choice["id"])
        if self.parser.pending_choice(own) != choice or signature(current) != signature(state):
            raise Stop("target choice or board changed during decision")
        fresh_status = mirror()
        if fresh_status.get("popup") or fresh_status.get("shop_open") or fresh_status.get("game_menu"):
            raise Stop("target choice is covered by a menu")
        self.identify(fresh_status)
        checked_geometry()

    def execute_target(self, own, choice, state, advice):
        # Reconnect can expose the server choice before the client accepts a
        # click. Retry only the same still-pending target, never a sent choice.
        cid = choice["id"]
        def confirmed():
            return self.choice_confirmed(cid, [advice["target_id"]])
        for attempt in range(3):
            self.poll()
            if confirmed():
                return
            if self.parser.choices.get(cid, {}).get("sent"):
                self.wait_until(confirmed, 6)
                return
            self.revalidate_target(own, choice, state)
            point = target_point(state, advice["target_id"])
            print(json.dumps({"attempt":"choose_target", "target_id":advice["target_id"],
                              "point":point, "attempt_number":attempt+1}),flush=True)
            click(point)
            try:
                self.wait_until(confirmed, 4)
                return
            except Stop as error:
                if str(error) != "action was not confirmed in game log" or attempt == 2:
                    raise

    def choice_confirmed(self, cid, expected):
        observed = self.parser.choices.get(cid, {})
        if observed.get("confirmed") is not True:
            return False
        if observed.get("chosen_ids") != expected:
            raise Stop("game confirmed a different choice; no further input was sent")
        return True

    def execute_end_turn(self, own, state):
        sent_before = self.parser.sent_options
        def done():
            if self.game_result():
                return True
            try:
                return not self.snapshot(own)['own_turn']
            except UnsafeState:
                return False
        for attempt in range(3):
            self.poll()
            if done():
                return
            if self.parser.sent_options != sent_before:
                self.wait_until(done,6)
                return
            self.revalidate(own,state,{'kind':'end_turn'})
            click((1180,435))
            try:
                self.wait_until(done,4)
                return
            except Stop as error:
                if str(error) != 'action was not confirmed in game log' or attempt == 2:
                    raise

    def run(self, max_seconds, max_actions, diagnose_hover=False, previous=None):
        deadline = time.monotonic() + max_seconds if max_seconds else float('inf')
        actions = 0
        initial_wait = 120 if RUN_MODE == "ranked" else 35
        ready_deadline = time.monotonic() + initial_wait
        choice_deadline = None
        refreshed_reconnect = False
        borrowed_options = None
        awaiting_fresh_options = False
        fresh_options_deadline = None
        first_options_deadline = None
        mirror_wait_started = None
        resolving_since = None
        first_mana_wait = None
        visual_wait_since = None
        while time.monotonic() < deadline and (not max_actions or actions < max_actions):
            self.poll()
            outcome = self.game_result()
            if outcome:
                print(json.dumps({"game_result": outcome, "actions": actions}), flush=True)
                return
            try:
                status = mirror()
                mirror_wait_started = None
            except NotReady as error:
                if mirror_wait_started is None:
                    mirror_wait_started = time.monotonic()
                    print("waiting for observation: " + str(error), flush=True)
                if time.monotonic() - mirror_wait_started >= (initial_wait if self.owner is None else 8):
                    raise Stop("game observation did not recover: " + str(error)) from error
                time.sleep(.5)
                continue
            try:
                own = self.identify(status)
                choice = self.parser.pending_choice(own)
            except (NotReady, UnsafeState) as error:
                if time.monotonic() >= ready_deadline:
                    raise Stop("game state did not become ready: " + str(error)) from error
                time.sleep(.5)
                continue
            ready_deadline = time.monotonic() + 35
            if status.get("mulligan_waiting"):
                if not choice or choice["type"] != "MULLIGAN":
                    raise Stop("mulligan identity not confirmed")
                checked_geometry()
                click((735, 708))
                self.wait_until(lambda: self.parser.pending_choice(own) is None, 10)
                print("mulligan: kept starting hand", flush=True)
                actions += 1
                time.sleep(5)
                continue
            if choice and choice["type"] == "TARGET":
                target_state = self.parser.snapshot(own, allow_practice=True, extended=True, choice_id=choice["id"])
                advice = self.advisor.target(choice, target_state)
                before_options = self.parser.options_id
                time.sleep(.4)
                self.execute_target(own, choice, target_state, advice)
                self.advisor.confirm_target(advice)
                print("confirmed target choice", flush=True)
                actions += 1
                borrowed_options = before_options
                awaiting_fresh_options = True
                time.sleep(1.5)
                continue
            if choice and choice["type"] == "GENERAL":
                if not choice["shown"]:
                    choice_deadline = choice_deadline or (time.monotonic() + 12)
                    if time.monotonic() >= choice_deadline:
                        raise Stop("choice did not become visible")
                    time.sleep(.5)
                    continue
                choice_deadline = None
                choice_state = self.parser.snapshot(own, allow_practice=True, extended=True, choice_id=choice["id"])
                index = self.advisor.choose(choice, choice_state)
                self.poll()
                current_choice = self.parser.snapshot(own, allow_practice=True, extended=True, choice_id=choice["id"])
                if self.parser.pending_choice(own) != choice or signature(current_choice) != signature(choice_state):
                    raise Stop("choice or board changed during decision")
                fresh_status = mirror()
                if fresh_status.get("popup") or fresh_status.get("shop_open") or fresh_status.get("game_menu"):
                    raise Stop("choice is covered by a menu")
                xs = [370, 735, 1090] if len(choice["items"]) == 3 else [310, 595, 880, 1160]
                checked_geometry()
                before_options = self.parser.options_id
                click((xs[index], 455))
                before_id = choice["id"]
                self.wait_until(lambda: self.choice_confirmed(before_id, [choice["items"][index]["id"]]), 10)
                self.advisor.confirm_choice(choice, index)
                print("choice resolved", flush=True)
                actions += 1
                borrowed_options = before_options
                awaiting_fresh_options = True
                time.sleep(2)
                continue
            if waiting_for_first_mana(self.parser, own):
                first_mana_wait = first_mana_wait or time.monotonic()
                if time.monotonic() - first_mana_wait >= 90:
                    raise Stop("first-turn resource observation did not arrive")
                time.sleep(.7)
                continue
            first_mana_wait = None
            try:
                state = self.snapshot(own)
            except UnsafeState as error:
                if str(error) == "missing COST" and self.recover_observed_zero_cost(own):
                    previous = self.recovered_previous
                    continue
                if str(error) not in ("incomplete trailing log line", "game is resolving an action or choice", "mulligan is not supported"):
                    raise Stop("unsupported game state: " + str(error)) from error
                resolving_since = resolving_since or time.monotonic()
                if time.monotonic() - resolving_since >= 20:
                    raise Stop("game state remained unresolved for twenty seconds: " + str(error)) from error
                time.sleep(.5)
                continue
            resolving_since = None
            if not state["own_turn"]:
                visual_wait_since = None
                time.sleep(.7)
                continue
            if not visual_ready(state):
                visual_wait_since = visual_wait_since or time.monotonic()
                if time.monotonic() - visual_wait_since >= 20:
                    raise Stop("visual turn did not become actionable within twenty seconds")
                time.sleep(.5)
                continue
            visual_wait_since = None
            if state["options_id"] is None:
                if previous is not None and restore_observed_options(self.parser, previous, own):
                    state = self.snapshot(own)
                    borrowed_options = self.parser.options_id
                    refreshed_reconnect = True
                    print("reused observed options after same-game same-turn state check", flush=True)
                else:
                    first_options_deadline = first_options_deadline or time.monotonic() + 12
                    if time.monotonic() >= first_options_deadline:
                        raise Stop("legal options did not arrive")
                    time.sleep(.3)
                    continue
            first_options_deadline = None
            if awaiting_fresh_options:
                if self.parser.options_id == borrowed_options:
                    fresh_options_deadline = fresh_options_deadline or time.monotonic() + 10
                    if time.monotonic() >= fresh_options_deadline:
                        raise Stop("fresh legal options did not arrive after action")
                    time.sleep(.3)
                    continue
                awaiting_fresh_options = False
                borrowed_options = None
                fresh_options_deadline = None
            blank_options = any(o["error"] == "NONE" and o["id"] is None for o in self.parser.options)
            if not state["legal_options"] and blank_options and not refreshed_reconnect:
                if previous is not None:
                    if not restore_observed_options(self.parser, previous, own):
                        raise Stop("previous practice options do not match the reconnected state")
                    state = self.snapshot(own)
                    borrowed_options = self.parser.options_id
                    refreshed_reconnect = True
                    print("reused observed options after same-game same-turn state check", flush=True)
                else:
                    # End only this practice turn to obtain a fresh list; never
                    # guess missing entity IDs.
                    if RUN_MODE != "practice":
                        raise Stop("ranked reconnect requires complete legal options")
                    if not state.get("end_turn_allowed"):
                        raise Stop("reconnect options do not confirm end turn")
                    time.sleep(5)
                    checked_geometry()
                    print("reconnect options incomplete; ending one practice turn to refresh", flush=True)
                    for attempt in range(3):
                        self.poll()
                        current = self.snapshot(own)
                        if not current["own_turn"]:
                            break
                        if not visual_ready(current) or not current.get("end_turn_allowed"):
                            raise Stop("reconnect turn is no longer actionable")
                        click((1180, 435))
                        try:
                            self.wait_until(lambda: not self.snapshot(own)["own_turn"], 4)
                            break
                        except Stop:
                            if attempt == 2:
                                raise
                    refreshed_reconnect = True
                    actions += 1
                    continue
            prepared = state
            decision_started = time.monotonic()
            advice = self.advisor.ask(prepared)
            input_started = time.monotonic()
            self.revalidate(own, state, advice)
            plan = plan_inputs(state, advice)
            checked_geometry()
            kind, first, second = plan
            source_id = int(advice.get("source_id", 0))
            source_card = next((c["card_id"] for c in state["my_hand"] + state["my_board"] if c["id"] == source_id), None)
            print(json.dumps({"attempt": advice["kind"], "card": source_card, "source_id": source_id,
                              "from": first, "to": second, "hand_size": len(state["my_hand"])}), flush=True)
            if diagnose_hover and advice["kind"] == "play":
                run_checked(input_command("move", *first))
                time.sleep(.7)
                observed = json.loads(run_checked([str(ROOT / "mirror-status"), FRAMEWORK, "--hover"]))
                card = next(c for c in state["my_hand"] if c["id"] == source_id)
                print(json.dumps({"hover_diagnostic": observed.get("hover"), "expected_card": card["card_id"],
                                  "expected_position": card["position"]}), flush=True)
                raise Stop("hover diagnostic completed without playing a card")
            if advice["kind"] == "play":
                first = self.verify_hand_point(own, state, source_id, first)
                print(json.dumps({"verified_hand_point": first, "card": source_card, "source_id": source_id}), flush=True)
            self.revalidate(own, state, advice)
            before_attacks = board._tag_int(self.parser.game["entities"].get(source_id, {"tags": {}}), "NUM_ATTACKS_THIS_TURN")
            if kind == "click":
                self.execute_end_turn(own,state)
            elif kind == "click_power":
                click(first)
                self.wait_until(lambda: bool(self.game_result()) or self.snapshot(own)["options_id"] != state["options_id"], 10)
            elif kind == "drag":
                drag(first, second)
            elif kind == "battlecry":
                drag(first, (735, 510))
                time.sleep(2.5)
                # The target reticle is a client animation and can appear
                # after the drag. Retry only while the same source, target,
                # options and board layout are still confirmed in the log.
                for target_attempt in range(3):
                    self.poll()
                    current = self.snapshot(own)
                    if all(c["id"] != source_id for c in current["my_hand"]):
                        break
                    target_id = int(advice["target_id"])
                    option = next((o for o in current["legal_options"] if o["id"] == source_id), None)
                    if (not current["own_turn"] or current["options_id"] != state["options_id"]
                            or option is None or target_id not in option["targets"]
                            or target_point(current, target_id) != second):
                        raise Stop("battlecry target state changed")
                    click(second)
                    try:
                        self.wait_until(lambda: self.parser.game is not None and not self.parser.partial and
                                        all(c["id"] != source_id for c in board._in_zone(self.parser.game, own, "HAND")), 4)
                        break
                    except Stop as error:
                        if str(error) != "action was not confirmed in game log" or target_attempt == 2:
                            raise
            source = int(advice.get("source_id", 0))
            if advice["kind"] == "play":
                # A played card may immediately open Discover, in which case
                # snapshot() intentionally blocks until that choice resolves.
                self.wait_until(lambda: self.parser.game is not None and not self.parser.partial and
                                all(c["id"] != source for c in board._in_zone(self.parser.game, own, "HAND")), 10)
            elif advice["kind"] == "attack":
                def attacked():
                    if self.game_result():
                        return True
                    entity = self.parser.game["entities"].get(source)
                    return bool(entity and (entity.get("zone") != "PLAY" or
                                board._tag_int(entity, "NUM_ATTACKS_THIS_TURN") > before_attacks))
                self.wait_until(attacked, 10)
            print("confirmed", advice["kind"], flush=True)
            print(json.dumps({"action_timing": {"kind": advice["kind"],
                "decision_ms": round((input_started-decision_started)*1000),
                "input_confirmation_ms": round((time.monotonic()-input_started)*1000)}}), flush=True)
            self.advisor.confirm(advice, state)
            # Entity changes can arrive before the next legal-option packet.
            # Require a new option list after every action, not just reconnect
            # actions, before asking the model to decide again.
            borrowed_options = state["options_id"]
            awaiting_fresh_options = True
            actions += 1
            # Server confirmation can precede the end of a client animation.
            # Keep this settling window; hand verification itself now polls
            # readiness and avoids the old unconditional two-second delay.
            time.sleep(1.5)
        print("practice limit reached, actions=%d" % actions, flush=True)


def main():
    global RUN_MODE
    ap = argparse.ArgumentParser()
    logs = ap.add_mutually_exclusive_group(required=True)
    logs.add_argument("--log", type=Path)
    logs.add_argument("--new-session", action="store_true", help="arm before launching the next game")
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--watchdog-pid", type=int, required=True)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--max-actions", type=int, default=20)
    ap.add_argument("--diagnose-hover", action="store_true")
    ap.add_argument("--prior-evidence", type=Path)
    ap.add_argument("--deck-snapshot", type=Path)
    ap.add_argument("--reconnect-evidence", type=Path, action="append", default=[])
    ap.add_argument("--openai-config-stdin", action="store_true", help="read API settings privately from stdin")
    ap.add_argument("--mode", choices=("practice","ranked"), default="practice")
    args = ap.parse_args()
    RUN_MODE = args.mode
    if args.seconds < 0 or args.max_actions < 0:
        ap.error("game limits must be nonnegative; 0 means no quota")
    if not watchdog_alive(args.watchdog_pid):
        raise Stop("no armed game watchdog")
    if not args.openai_config_stdin:
        raise Stop("Start this driver through the standalone application")
    args.evidence.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    api_advisor = OpenAIAdvisor(json.loads(sys.stdin.readline(4097)),
                               audit_path=args.evidence.parent / "decisions.jsonl")
    path = args.log
    if args.new_session:
        previous = {p.name for p in LOG_ROOT.glob("Hearthstone_*")}
        end = time.monotonic() + 180
        print("waiting for a new practice game", flush=True)
        while time.monotonic() < end:
            if not watchdog_alive(args.watchdog_pid):
                raise Stop("game watchdog is not running")
            candidates = sorted(p / "Power.log" for p in LOG_ROOT.glob("Hearthstone_*")
                                if p.name not in previous and (p / "Power.log").is_file())
            if len(candidates) > 1:
                raise Stop("ambiguous new game sessions")
            if candidates:
                path = candidates[0]
                break
            time.sleep(.5)
        if path is None:
            raise Stop("new game log did not appear")
    else:
        end = time.monotonic() + 120
        print("waiting for the current practice log", flush=True)
        while not path.is_file() and time.monotonic() < end:
            if not watchdog_alive(args.watchdog_pid):
                raise Stop("game watchdog is not running")
            time.sleep(.5)
        if not path.is_file():
            raise Stop("current game log did not appear")
    previous = None
    if args.prior_evidence:
        previous = IncrementalBoard()
        reader = LogFollower(args.prior_evidence, previous)
        try:
            while reader.poll():
                pass
        finally:
            reader.close()
    driver = Driver(path, args.evidence, args.watchdog_pid, advisor=api_advisor, deck_snapshot=args.deck_snapshot,
                    reconnect_evidence=args.reconnect_evidence)
    try:
        driver.run(args.seconds, args.max_actions, diagnose_hover=args.diagnose_hover, previous=previous)
    except ClientFatal as error:
        raise Stop(client_error(path)) from error
    finally:
        driver.close()


if __name__ == "__main__":
    try:
        main()
    except (Stop, NotReady, UnsafeState, CaptureError, AdvisorError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print("practice driver stopped: " + str(exc), file=sys.stderr, flush=True)
        sys.exit(2)
