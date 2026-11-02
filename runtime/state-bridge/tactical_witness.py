"""Small arithmetic checker for FOUR fixed witness lines, not a game engine.

Supported effects are explicitly enumerated; anything else raises. It checks
mana, taunts, shields, attacks, Golem's two heals/Reborn, spell damage and auras.
It does not solve arbitrary boards, infer hidden cards or validate the client.
"""
import copy


def check(case):
    s=copy.deepcopy(case['state']); initial_tax=tax(s)
    for c in s['my_board']:
        c['remaining']=0 if c.get('exhausted') else 2 if c.get('windfury') else 1
    for c in s['my_hand']:
        c['untaxed_cost']=c['cost']-(initial_tax if c['card_type']=='SPELL' else 0)
    trace=[]
    def dead():
        for key in ('my_board','enemy_board'):
            for c in list(s[key]):
                if c['health']>0:continue
                s[key].remove(c)
                if c['card_id']=='BE_861':
                    assert key=='enemy_board'
                    s['enemy_hero']['health']=min(30,s['enemy_hero']['health']+3)
                    if c.get('reborn'):
                        s[key].append({'id':1000+c['id'],'card_id':'BE_861','attack':2,'health':1,'taunt':True,'reborn':False})
                elif c['card_id'] not in ('BE_729','BE_085t','CATA_210t','CORE_UNG_205','TIME_025','CAP_405','BE_728','BE_090'):
                    raise ValueError('Unmodeled witness death: '+c['card_id'])
    def damage(c,n):
        if c.get('divine_shield') and n>0:c['divine_shield']=False
        elif c.get('armor',0):
            absorbed=min(n,c['armor']);c['armor']-=absorbed;c['health']-=n-absorbed
        else:c['health']-=n
    for a in case['witness']:
        if a['kind']=='attack':
            c=next(x for x in s['my_board'] if x['id']==a['source_id'])
            assert c['remaining']>0 and not c.get('frozen')
            taunts={x['id'] for x in s['enemy_board'] if x.get('taunt')}
            assert not taunts or a['target_id'] in taunts
            c['remaining']-=1
            if a['target_id']==s['enemy_hero']['id']:damage(s['enemy_hero'],c['attack'])
            else:
                target=next(x for x in s['enemy_board'] if x['id']==a['target_id'])
                ca,ta=c['attack'],target['attack'];damage(target,ca);damage(c,ta);dead()
        else:
            c=next(x for x in s['my_hand'] if x['id']==a['source_id'])
            cost=cost_of(c,s);assert cost<=s['mana'];s['mana']-=cost
            repeat=c['card_id']=='BE_086' and all(cost>=cost_of(x,s) for x in s['my_hand'])
            s['my_hand'].remove(c)
            cid=c['card_id']
            if cid=='BE_094':
                target=next(x for x in s['my_board']+s['enemy_board'] if x['id']==a['target_id'])
                target['attack']+=2*cost;target['health']+=2*cost
                # Only one possible minion in hand in these witnesses.
                held=[x for x in s['my_hand'] if x['card_type']=='MINION']
                assert len(held)<=1
                for x in held:x['attack']+=2*cost;x['health']+=2*cost
            elif cid=='TIME_026':
                for x in s['my_board']:x['attack']+=1;x['health']+=1
            elif cid=='BE_086':
                for _ in range(2 if repeat else 1):
                    n=2+sum(x['card_id'] in ('EX1_012','CORE_EX1_012') and not x.get('silenced') for x in s['my_board'])
                    damage(s['enemy_hero'],n)
                    for x in s['enemy_board']:damage(x,n)
                    dead()
            elif cid=='BE_090':
                assert len(s['my_board'])<7
                c['remaining']=0;s['my_board'].append(c)
            elif cid=='GAME_005':s['mana']=min(10,s['mana']+1)
            else:raise ValueError('Unmodeled witness play: '+cid)
        trace.append({'action':a,'mana':s['mana'],'enemy_health':s['enemy_hero']['health'],
                      'enemy_armor':s['enemy_hero'].get('armor',0),'remaining_taunts':[x['id'] for x in s['enemy_board'] if x.get('taunt')]})
    assert s['enemy_hero']['health']<=0,case['name']
    return trace


def tax(s):
    return sum(c['card_id']=='BE_729' and not c.get('silenced') for c in s['my_board']+s['enemy_board'])


def cost_of(c,s):
    return c['untaxed_cost']+(tax(s) if c['card_type']=='SPELL' else 0)
