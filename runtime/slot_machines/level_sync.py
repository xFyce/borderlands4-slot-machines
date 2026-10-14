"""Character-level snapshots. Economy curves are mod policy, not native odds.

Read the native Character ExperienceState row directly. The real getter
requires ExperienceDef; no generic definition pointer is constructed or called.
No native wrappers are retained here, including across travel/character swaps.
"""
import math
import re

MAX_WAGER=10_000_000

def _level(value):
    if type(value) is int and 1<=value<=200:return value
    raise RuntimeError('Character level must be an integer from 1 to 200')

def _state_level(ctx,state):
    try:rows=state.ExperienceState
    except AttributeError:return None
    if rows is None:return None
    count=len(rows)
    if not 0<=count<=32:raise RuntimeError('Unexpected experience track count; no wager taken')
    snapshot=[];matches=[]
    for row in rows:
        token=None
        identity=None
        for field in ('ExperienceId','ExperienceDef'):
            try:identity=getattr(row,field)
            except AttributeError:continue
            if identity is not None:break
        if identity is not None:
            for attr in ('Name','name','_name'):
                try:value=getattr(identity,attr)
                except AttributeError:continue
                text=str(value)
                if text and text!='None':token=text;break
            if token is None:
                match=re.search(r'''Name:\s*['"]([^'"]+)['"]''',str(identity))
                if match:token=match.group(1)
        # Only safe identity metadata is read, never pointed-to definition data.
        try:value=row.ExperienceLevel
        except AttributeError:value=None
        snapshot.append({'token':token,'level':value if type(value) is int else None})
        if token and token.rsplit('/',1)[-1].casefold()=='character':matches.append(value)
    ctx._report['native_experience_tracks']=snapshot
    if len(matches)>1:raise RuntimeError('Multiple Character experience tracks; no wager taken')
    if len(matches)==1:return _level(matches[0]),'PlayerState.ExperienceState[Character].ExperienceLevel'
    # BL4's ordered experience array starts with Character. Optional tracks
    # (specialization and Vault Cards) follow it; their count must not be used
    # to infer ownership or reject a base-game character. Prefer identities
    # above whenever present, and only use index 0 for an entirely unnamed
    # array. Never choose the largest level or a later DLC/card track.
    if 1<=count<=32 and all(r['token'] is None for r in snapshot):
        result=_level(snapshot[0]['level'])
        ctx._report['experience_track_resolution']='Ordered unnamed tracks; Character index 0; optional track count ignored'
        return result,'PlayerState.ExperienceState[0].ExperienceLevel (ordered unnamed tracks)'
    raise RuntimeError('Native Character experience track unavailable; no wager taken')


def character_level(ctx,pc):
    if pc is None or getattr(pc,'Pawn',None) is None:
        raise RuntimeError('Load a character before using level-synced slots')
    state=getattr(pc,'PlayerState',None)
    if state is not None:
        result=_state_level(ctx,state)
        if result is not None:return result
        try:fn=state.BP_GetExperienceLevel
        except AttributeError:fn=None
        if fn is not None:
            props=list(fn.func._properties())
            ctx._report['level_getter_signature']=[{'name':str(p.Name),'type':type(p).__name__} for p in props]
            if len(props)!=1 or str(props[0].Name)!='ReturnValue' or type(props[0]).__name__ not in ('ZIntProperty','IntProperty'):
                ctx._report['level_getter_skipped']='Requires ExperienceDef; no definition pointer constructed or native call attempted'
            else:
                return _level(ctx._call(state,'BP_GetExperienceLevel')), 'PlayerState.BP_GetExperienceLevel'
    # Safe reflected scalar fallback for builds exposing the level directly.
    # GameStage is deliberately excluded: difficulty can raise it above level.
    for obj,label in ((state,'PlayerState'),(pc.Pawn,'Pawn')):
        if obj is None:continue
        try:props={str(p.Name):p for p in obj.Class._properties()}
        except Exception:continue
        for name in ('ExperienceLevel','CharacterLevel'):
            prop=props.get(name)
            if prop is not None and type(prop).__name__ in ('ZIntProperty','IntProperty'):
                return _level(getattr(obj,name)),label+'.'+name
    raise RuntimeError('Character level unavailable; run slots_level and send the newest JSON. No wager taken')

def economy(level):
    level=_level(level)
    # Spin price grows 11% per level, anchored at $860 at level 15.
    # Rounded to $10; existing wager cap retained.
    price=min(MAX_WAGER,int(round(860*1.11**(level-15)/10))*10)
    damage=min(10_000_000.0,round(100*1.09**(level-1),2))
    return {'level':level,'price':price,'weapon_stage':level,'mod_stage':level,
            'grenade_damage':damage,'cash_multipliers':{'small_range':[0.30,0.60],'medium':1,'large':3},
            'eridium_quantities':[max(1,math.ceil(level/10))*n for n in (1,2,3)],
            'pending_backends':['customization'],
            'balance':'Custom mod curves; native Eridium quantities expressed as multistack counts'}

def snapshot(ctx,pc):
    level,source=character_level(ctx,pc)
    result=economy(level);result['source']=source
    ctx._report['level_sync']=result
    return result
