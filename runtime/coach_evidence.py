"""Complete compact public game transcripts for the optional in-app reviewer."""
import gzip
import json
import os
import tempfile
from pathlib import Path

from coach_policy import RUN, read_json

MIB = 1024*1024
FIELDS = ('game_type format phase game_turn mana mana_cap mana_spent temporary_mana overload_locked overload_owed '
          'my_fatigue enemy_fatigue my_deck_count enemy_deck_count enemy_hand_count my_corpses enemy_corpses '
          'enemy_mana_crystals my_hero enemy_hero my_hero_power enemy_hero_power my_weapon enemy_weapon '
          'my_hand my_board enemy_board my_locations enemy_locations my_objectives enemy_objectives '
          'my_secrets enemy_known_secrets enemy_secret_count enemy_known_hand opponent_public_cards '
          'my_player_effects enemy_player_effects active_rituals enemy_active_rituals choices source_card '
          'target_effect healing_as_damage healing_double turn_pace life_tap shred_battlecry unavailable_interactions '
          'secret_context own_known_deck enemy_secret_ids calculated_resources learned_matchup_notes turn_timeout_seconds').split()
EVENT_FIELDS = ('decision_id event for_decision action actions reason model model_action policy local_rule '
                'conditional_sequence conditional_attack evidence elapsed_ms requested_tier served_tier '
                'tactical_lesson sol_action restricted_menu candidate_scores sent card_id outcome game_turn '
                'phase basis offered planned_ids marked_ids submitted replaced_ids kept_ids model_reason '
                'proposed_index actual_index choice_id from_decision handoff_reason late_turn_effort').split()


def compact(value):
    if isinstance(value, list):
        return [compact(v) for v in value]
    if isinstance(value, dict):
        return {k:compact(v) for k,v in value.items() if v is not None and v is not False and v != [] and v != {}}
    return value


def public_event(row):
    # Preserve explicit false/zero for moves and receipts; compact only board fields.
    result = {k:row[k] for k in EVENT_FIELDS if k in row}
    obs = row.get('observation')
    if obs is not None:
        result['board'] = {k:compact(obs[k]) for k in FIELDS if k in obs}
        strategy = obs.get('strategy') or {}
        deck = strategy.get('deck') or {}
        result['board']['remaining_deck'] = {k:deck[k] for k in ('remaining_basis','reconciled_remaining_counts') if k in deck}
        matchup = strategy.get('matchup') or {}
        result['board']['opponent_estimate'] = {k:matchup[k] for k in ('enemy_class','stale','build_compatible','candidates') if k in matchup}
        result['definitions'] = {cid:{k:c[k] for k in ('name','text','type','cost') if k in c}
                                 for cid,c in obs.get('card_definitions',{}).items()}
    return result


def lines(path, limit):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Transcript exceeds evidence limit')
    with path.open('rb') as stream:
        size = 0
        for line in stream:
            size += len(line)
            if size > limit:
                raise ValueError('Transcript changed size')
            yield json.loads(line)


def transcript(dest, review):
    if (not RUN.fullmatch(dest.name) or review.get('run') != dest.name or review.get('game_type') != 'GT_RANKED'
            or review.get('outcome') not in ('WON','LOST','TIED')):
        return None
    summary = read_json(dest/'decision-summary.json', 32768, {})
    if not summary or not (dest/'decisions.jsonl').is_file():
        return None
    def events():
        yield from (public_event(row) for row in lines(dest/'decisions.jsonl', 2*MIB))
        if summary.get('diagnostics_capped') and (dest/'coach-overflow.jsonl').is_file():
            yield from lines(dest/'coach-overflow.jsonl', 2*MIB)
    sequence, definitions, prev, proposed = [], {}, {}, set()
    terminal = opened = False
    for row in events():
        if row['event']=='proposed':
            i = row['decision_id']
            board = row.pop('board')
            if (not isinstance(i,int) or i in proposed or board.get('game_type') != 'GT_RANKED'
                    or board.get('format') != 'FT_STANDARD' or not row.get('actions')):
                return None
            proposed.add(i)
            definitions.update(row.pop('definitions',{}))
            row['change'] = {k:board.get(k) for k in prev.keys() | board.keys() if prev.get(k) != board.get(k)}
            prev = board
        if row['event']=='mulligan_confirmed':
            opened = True
        if row['event']=='game_complete':
            terminal = row.get('outcome') == review['outcome']
        sequence.append(row)
    expected = sum(summary.get(k,0) for k in ('confirmed_decisions','expired_decisions','withdrawn_decisions',
                                              'external_choices')) + len(summary.get('unconfirmed_decisions',[]))
    if not (opened and terminal and proposed and len(proposed)==expected):
        return None
    build = read_json(dest/'run-version.json', 8192, {})
    return dict(run=dest.name, outcome=review['outcome'], version=build.get('version_id','unversioned'),
                policy=build.get('coach_revision','unrecorded'), actions=len(proposed), cards=definitions,
                terminal_receipts=review.get('turn_end_attacks',[]),
                events=sequence)


def pack(game):
    raw = json.dumps(game, ensure_ascii=False, separators=(',',':')).encode()
    if len(raw) > 2*MIB:
        raise ValueError('Complete game exceeds coaching archive limit')
    return gzip.compress(raw, compresslevel=1, mtime=0)


def archive(dest, review, learning):
    game = transcript(dest, review)
    if game is None:
        return
    folder = learning/'coach-replays'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(pack(game))
            f.flush()
            os.replace(tmp, folder/(dest.name+'.json.gz'))
        finally:
            tmp.unlink(missing_ok=True)
    for old in sorted(folder.glob('run-*.json.gz'))[:-24]:
        if RUN.fullmatch(old.name[:-8]):
            old.unlink(missing_ok=True)


def sources(data):
    folder = data/'learning/coach-replays'
    archived = {p.name[:-8]:p for p in folder.glob('run-*.json.gz') if RUN.fullmatch(p.name[:-8])}
    runs = {p.name:p for p in (data/'runs').glob('run-*') if RUN.fullmatch(p.name) and (p/'review.json').is_file()}
    return archived,runs


def recent(data, after, count):
    archived,runs = sources(data)
    batch, skipped = [], []
    for name in sorted(archived.keys() | runs.keys(), reverse=True):
        if name <= after or len(batch) == count:
            break
        try:
            if name in archived:
                path = archived[name]
                if path.is_symlink() or path.stat().st_size > 2*MIB:
                    raise ValueError('Invalid archive')
                with gzip.open(path,'rb') as f:
                    raw = f.read(2*MIB+1)
                if len(raw)>2*MIB:
                    raise ValueError('Excessive archive')
                game = json.loads(raw)
            else:
                dest = runs[name]
                game = transcript(dest,read_json(dest/'review.json',64*1024,{}))
            if game and game['run']==name:
                batch.append(game)
            else:
                skipped.append(name)
        except (OSError, ValueError, KeyError, TypeError):
            skipped.append(name)
    return list(reversed(batch)), skipped
