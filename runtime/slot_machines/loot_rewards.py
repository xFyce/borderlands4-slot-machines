"""Native weapon prizes using installed NCS pools and the user-verified spawning path."""
import json
import gzip
import random
import math
import re
from pathlib import Path

import unrealsdk
from . import outcomes
from .loot_burst import LootBurst
from . import prize_ownership

RARITIES=('common','uncommon','rare','epic','legendary')
WEAPON_RARITIES=RARITIES+('pearl',)
_POOL_ROWS={}

def pool_rows(path):
    # Authored JSON is immutable for a loaded mod; retain no native wrappers here.
    key=str(path)
    if key not in _POOL_ROWS:_POOL_ROWS[key]=json.loads(path.read_text(encoding='utf-8'))
    return _POOL_ROWS[key]

PEARL_POOLS=tuple('itempool_'+kind+'_06_pearl' for kind in ('ar','ps','sg','sm','sr'))


class LootRewards:
    def __init__(self,ctx):
        self.ctx=ctx
        self.pools={}
        self.report={}
        self.tested=True  # All five rarities confirmed in the user's v46 tests.
        self.pending=None;self.backlog=[];self.batch_receipt=None
        self.extra_rows={};self.currency_receipt=None;self.currency_next=0
        self.display=NativePrizeDisplay(ctx)
        self.display.on_finish=self._display_finished
        self.display.placement=self.payout_transform
        self.burst=LootBurst(ctx,self)
        self.catalog_key=None
        self.side_open_pose=None
        self.paid_active=None

    def catalog(self):
        errors=[];self.pools={};self.extra_rows={};stores=[];snapshots=[]
        for store in unrealsdk.find_all('NexusConfigStoreItemPool',exact=False):
            try:
                if self.ctx._path(store).startswith('/Script/') or store.OwningLoader is None:continue
                stores.append(store)
                snapshots.append({'path':self.ctx._path(store),'owning_loader':self.ctx._path(store.OwningLoader)})
            except Exception as error:errors.append(str(error))
        # ElementScriptStructs contains schemas, not the authored definition rows.
        # Use exact names decoded from this user's installed NCS files.
        if len(stores)==1:
            for row in pool_rows(self.ctx._ROOT/'weapon_pools.json'):
                self.pools[row['itempool']]=stores[0]
            extra=self.ctx._ROOT/'extra_pools.json'
            self.extra_rows={row['itempool']:row for row in pool_rows(extra)} if extra.exists() else {}
            for name in self.extra_rows:self.pools[name]=stores[0]
        elif stores:errors.append('Multiple live item-pool stores; no arbitrary store selected')
        self.report={'store_snapshots':snapshots,'catalog_source':'user installed NCS pakchunk0-Windows_20_P',
                     'pool_names':sorted(self.pools),'count':len(self.pools),'errors':errors,
                     'verified_spawn_pool':'itempool_ps_01_common','extra_pool_count':len(self.extra_rows),
                     'extra_pool_scope':'Native gear/Eridium, grenade/knife ordnance and heavy pools user-confirmed through v76',
                     'scope':'Names confirmed on disk; all five rarities visually confirmed in v46 free tests'}
        pc=self.ctx.get_pc(possibly_loading=True)
        self.catalog_key=(self.ctx._path(self.ctx._actor),self.ctx._path(getattr(pc,'Pawn',None)))
        self.ctx._report['loot_catalog']=self.report
        return self.report

    def ensure_catalog(self):
        pc=self.ctx.get_pc(possibly_loading=True)
        key=(self.ctx._path(self.ctx._actor),self.ctx._path(getattr(pc,'Pawn',None)))
        if key!=self.catalog_key or not self.pools:self.catalog()

    def pearl_available(self):
        self.ensure_catalog()
        return all(name in self.pools for name in PEARL_POOLS)

    def choose(self,rarity,stage=None):
        stage=stage if stage is not None else self.game_stage(self.ctx.get_pc(possibly_loading=True))
        if rarity=='pearl':
            selected=[name for name in PEARL_POOLS if name in self.pools and stage>=outcomes.pool_minimum('weapon',name)]
            if not selected:raise RuntimeError('No authored pearlescent weapon pools loaded')
            return random.SystemRandom().choice(selected)
        configured=self.ctx._experience.settings['loot_pools'].get(rarity)
        if configured:
            if configured in self.extra_rows:raise RuntimeError('Weapon overrides must name weapon pools; use slots_test RARITY --type CATEGORY for gear tests')
            if configured not in self.pools:raise RuntimeError('Configured pool is not currently loaded: '+configured)
            outcomes.require_pool_level('weapon',configured,stage)
            return configured
        selected=[]
        for name in self.pools:
            text=name.lower()
            words=set(re.split('[^a-z0-9]+',text))
            if rarity not in words:continue
            if any(t in text for t in ('mission','quest','boss','dedicated','named','currency','money','ammo','eridium')):continue
            if not any(t in text for t in ('weapon','gun')) and not re.fullmatch(r'itempool_(ar|ps|sg|sm|sr)_\d+_(common|uncommon|rare|epic|legendary)',text):continue
            if stage<outcomes.pool_minimum('weapon',name):continue
            score=sum(t in text for t in ('world','random','all','generic'))
            selected.append((-score,name))
        if not selected:raise RuntimeError('No unambiguous generic '+rarity+' weapon pool found; check slots_loot_catalog')
        best=min(score for score,name in selected)
        return random.SystemRandom().choice([name for score,name in selected if score==best])

    def game_stage(self,pc):
        from .level_sync import snapshot
        return snapshot(self.ctx,pc)['weapon_stage']

    def spawn(self,rarity,stage=None,name=None,display=False,category=None,route_override=None,attempt_receipt=None):
        ctx=self.ctx
        if attempt_receipt is not None:attempt_receipt['native_attempted']=False
        self.catalog()  # Reacquire live store after map changes; no cached native object used.
        if not self.pools:raise RuntimeError('No item-pool rows are loaded; catalog report saved')
        pc=ctx.get_pc(possibly_loading=True)
        if pc is None or pc.Pawn is None or not ctx._call(pc,'HasAuthority'):raise RuntimeError('Native loot tests require active single-player/host authority')
        stage=stage if stage is not None else self.game_stage(pc)
        if type(stage) is not int or not 1<=stage<=200:raise ValueError('Use an integer item level from 1 to 200')
        name=name or self.choose(rarity,stage)
        if name not in self.pools:raise RuntimeError('Pool is not a catalog row: '+name)
        category=category or self.extra_rows.get(name,{}).get('category','weapon')
        outcomes.require_pool_level(category,name,stage)
        if ctx._actor is None:raise RuntimeError('Spawn the machine first')
        location=ctx._call(ctx._actor,'K2_GetActorLocation')
        yaw=float(ctx._call(ctx._actor,'K2_GetActorRotation').Yaw)
        route=route_override or self.route(category,name)
        transform,position=self.payout_transform(location,yaw,ctx._model_height,route,category)
        currency_before=self.currency_balance(pc) if category=='eridium' else None
        before={ctx._path(p) for p in unrealsdk.find_all('InventoryPickup',exact=False)}
        if attempt_receipt is not None:attempt_receipt['native_attempted']=True
        ctx._call(self.pools[name],'SpawnInventoryFromItemPool',WorldContextObject=pc.Pawn,
                  Transform=transform,GameStage=stage,ItemPoolName=name)
        after_live={ctx._path(p):p for p in unrealsdk.find_all('InventoryPickup',exact=False)}
        after=set(after_live)
        receipt={'state':'native spawn requested','native_attempted':True,'rarity':rarity,'pool':name,'game_stage':stage,'category':category,
                 'new_pickups':sorted(after-before),'context':'player pawn',
                 'position':position,'exit':route+' opening',
                 'note':'Native API returned; asynchronous appearance/rarity must be checked in game'}
        ctx._report['loot_result']=receipt
        if not display:receipt['_burst_before']=list(before)
        if category=='eridium':
            receipt['currency']={'name':'eridium','before':currency_before,'after':self.currency_balance(pc),'delta':None,'credit_backend':'native game pickup; mod never credits Eridium'}
            self.currency_receipt={'receipt':receipt,'player_path':ctx._path(pc),'pawn_path':ctx._path(pc.Pawn),'born':ctx._clock()}
        if display:self.display.begin(before,receipt,transform,route,pc,initial_live=after_live)
        self.tested=True
        return receipt

    def payout_transform(self,location,yaw,height,route='side',category=None):
        # Source wall coordinates belong to the CLOSED mesh. Transform them
        # through SideDoor_Open's final bone pose before using a world anchor.
        x,y,z=-12.0,34.32,58.0
        side_rotation=None
        if route=='side':
            if self.side_open_pose is None:
                animations=getattr(self.ctx,'_animations',None)
                if animations is None:
                    with gzip.open(Path(__file__).with_name('animations.json.gz'),'rt') as file:animations=json.load(file)
                self.side_open_pose=tuple(animations['sequences']['SideDoor_Open']['bone_frames'][-1][6])
            final=self.side_open_pose
            wall=self.ctx._rotate_vector(final[3:],[-10.0,23.5,66.0])
            x,y=wall[0]+final[0],wall[1]+final[1]
            # Preserve approved ordinary/heavy pickup heights.
            z=82.0 if category=='heavy' else 66.0
            state=getattr(self.ctx,'_weapon_state',None) or {}
            if state.get('state')=='opening':
                final=self.side_open_pose
                current=state.get('bone_pose',[0,0,0,0,0,0,1])
                norm=sum(v*v for v in final[3:])
                inverse=[-final[3]/norm,-final[4]/norm,-final[5]/norm,final[6]/norm]
                relative=self.ctx._rotate_vector(inverse,[x-final[0],y-final[1],z-final[2]])
                rotated=self.ctx._rotate_vector(current[3:],relative)
                x,y,z=[rotated[i]+current[i] for i in range(3)]
                side_rotation=self.ctx._quat_multiply(current[3:],inverse)
        if route=='front':
            pose=(self.ctx._tray_state or {}).get('bone_pose',[0,0,0,0,0,0,1])
            # Currency spills start at the moving drawer lip, away from
            # the cabinet; held equipment keeps its approved anchor.
            anchor=[26,0,68] if category=='eridium' else [10,0,65]
            local=self.ctx._rotate_vector(pose[3:],anchor)
            x,y,z=[local[i]+pose[i] for i in range(3)]
        scale=height/180
        a=math.radians(yaw)
        position={'x':float(location.X)+scale*(x*math.cos(a)-y*math.sin(a)),
                  'y':float(location.Y)+scale*(x*math.sin(a)+y*math.cos(a)),
                  'z':float(location.Z)+scale*z}
        # Weapon forward is +X (confirmed by v48 screenshots). Large guns
        # stand muzzle-up with their broad side along the door wall; pistols lie across the tray.
        heading=yaw+(-90 if route=='front' else 270)
        transform=self.ctx._transform(position['x'],position['y'],position['z'],heading)
        if category=='eridium':
            # Native pool launch follows the spawn rotation. The sideways,
            # rolled held-gear orientation sends currency right before our
            # physics toss; use the cabinet's upright forward yaw instead.
            transform=self.ctx._transform(position['x'],position['y'],position['z'],yaw)
            return transform,position
        half=math.sqrt(.5)
        local=[half,0,0,half] if route=='front' else [0,-half,0,half]
        cabinet_q=self.ctx._quat_multiply([0,0,math.sin(math.radians(heading-yaw)/2),math.cos(math.radians(heading-yaw)/2)],local)
        if side_rotation is not None:cabinet_q=self.ctx._quat_multiply(side_rotation,cabinet_q)
        world_angle=math.radians(yaw)/2
        q=self.ctx._quat_multiply([0,0,math.sin(world_angle),math.cos(world_angle)],cabinet_q)
        transform.Rotation=unrealsdk.make_struct('Quat',X=q[0],Y=q[1],Z=q[2],W=q[3])
        return transform,position

    def queue_test(self,rarity,stage=None,name=None,category='weapon'):
        ctx=self.ctx
        if self.waiting():raise RuntimeError('A weapon payout is already queued or waiting for pickup')
        if ctx._actor is None or ctx._spin is not None or ctx._weapon_state is not None or (ctx._tray_state and ctx._tray_state['state']!='closed') or self.display.waiting():
            raise RuntimeError('Spawn a machine and wait for the spin/weapon door to finish')
        pc=ctx.get_pc(possibly_loading=True)
        if pc is None or pc.Pawn is None or not ctx._call(pc,'HasAuthority'):
            raise RuntimeError('Native loot tests require active single-player/host authority')
        self.catalog()
        stage=stage if stage is not None else self.game_stage(pc)
        if type(stage) is not int or not 1<=stage<=200:raise ValueError('Use an integer item level from 1 to 200')
        name=name or self.choose(rarity,stage)
        if name not in self.pools:raise RuntimeError('Pool is not a catalog row: '+name)
        category=self.extra_rows.get(name,{}).get('category',category)
        outcomes.require_pool_level(category,name,stage)
        receipt={'state':'native spawn queued','rarity':rarity,'pool':name,'game_stage':stage,'category':category,
                 'note':'One free native item request after its payout opening is ready'}
        self.pending={'receipt':receipt,'player':pc,'stage':stage,'pool':name,'rarity':rarity,'category':category,'route':self.route(category,name)}
        if category=='eridium':
            child=dict(receipt,native_attempted=False)
            spec={'category':category,'rarity':rarity,'pool':name,'stage':stage,'route':'front'}
            receipt.update(outcome='eridium_test',items=[child],item_count=1,
                           note='One free native Eridium stack spilled from tray; normal ground pickup')
            self.pending={'receipt':receipt,'player':pc,'route':'front','burst_specs':[spec],'burst_mode':'eridium'}
        ctx._report['loot_result']=receipt
        return receipt

    def route(self,category,name):
        return 'side' if category=='heavy' or category=='weapon' and not name.startswith('itempool_ps_') else 'front'

    def extra_pool(self,category,rarity='rare',variant='multi'):
        if category=='eridium':
            wanted={'normal':'itempool_eridium_normal','rich':'itempool_eridium_rich','multi':'itempool_event_loot_eridium'}[variant]
        else:
            prefix={'classmod':'class_mods','enhancement':'enhancements','shield':'shields','repkit':'repkit','ordnance':'grenade_gadgets','heavy':'hw'}[category]
            wanted=f'itempool_{prefix}_{RARITIES.index(rarity)+1:02}_{rarity}'
        matches=[name for name,row in self.extra_rows.items() if row['key']==wanted and name in self.pools]
        if len(matches)!=1:raise RuntimeError('Expected one authored native reward pool: '+wanted)
        return matches[0]

    def eridium_available(self,stage=None):
        self.ensure_catalog()
        try:
            stage=stage if stage is not None else self.game_stage(self.ctx.get_pc(possibly_loading=True))
            if stage<outcomes.EQUIPMENT_MIN_LEVEL['eridium']:return False
            self.extra_pool('eridium');return True
        except Exception:return False

    def plan(self,outcome,profile,category=None,rng=None):
        rng=rng or random.SystemRandom()
        self.ensure_catalog()
        specs=[]
        level=profile['level']
        if category is not None:
            if outcome['kind']!='weapon':raise ValueError('--type requires a rarity gear result')
            outcomes.require_equipment_level(category,level)
        if outcome['kind']=='weapon':
            if outcome['rarity']=='pearl':
                if category not in (None,'weapon'):raise ValueError('Pearlescent reward supports native weapons only')
                category='weapon'
            else:category=category or outcomes.weighted_choice(outcomes.equipment_weights(level),rng)
            specs=[(category,outcome['rarity'])]
        elif outcome['kind']=='jackpot':
            guaranteed=outcomes.weighted_choice(outcomes.JACKPOT_RARITIES,rng)
            count=20
            equipment=outcomes.equipment_weights(level,jackpot=True)
            mixed=outcomes.equipment_weights(level)
            specs=[(outcomes.weighted_choice(equipment,rng),guaranteed)]+[(outcomes.weighted_choice(mixed,rng),outcomes.weighted_choice({'common':35,'uncommon':35,'rare':20,'epic':9,'legendary':1},rng)) for _ in range(count)]
        elif outcome['kind']=='eridium':
            quantities=profile['eridium_quantities']
            count=quantities[0 if outcome['name']=='eridium_small' else 2]
            if type(count) is not int or not 1<=count<=64:raise RuntimeError('Eridium stack count outside safe batch size')
            specs=[('eridium','common')]*count
        result=[]
        for index,(kind,rarity) in enumerate(specs):
            name=self.choose(rarity,profile['weapon_stage']) if kind=='weapon' else self.extra_pool(kind,rarity)
            outcomes.require_pool_level(kind,name,profile['weapon_stage'])
            if name not in self.pools:raise RuntimeError('Prize pool unavailable before wager')
            result.append({'category':kind,'rarity':rarity,'pool':name,'stage':profile['weapon_stage'],
                           'route':'front' if outcome['kind']=='jackpot' and index>0 else self.route(kind,name)})
        return result

    def reward(self,active):
        outcome=self.ctx._outcome(active)
        if outcome['kind'] not in ('weapon','jackpot','eridium'):return {'state':'no gear match'}
        if outcome['kind']!='eridium' and not self.ctx._experience.settings['loot_enabled']:return {'state':'disabled'}
        try:
            if self.waiting():raise RuntimeError('A native payout is already waiting')
            specs=active.get('reward_plan')
            if specs is None:
                # Legacy diagnostics constructed without a spin-start plan keep
                # their verified weapon path. Real paid v95 spins always preplan.
                if outcome['kind']!='weapon':raise RuntimeError('Extra reward has no validated spin-start plan')
                self.catalog()
                stage=active.get('level_sync',{}).get('weapon_stage') or self.game_stage(active['player'])
                name=self.choose(outcome['rarity'],stage)
                specs=[{'category':'weapon','rarity':outcome['rarity'],'pool':name,'stage':stage,
                        'route':'front' if name.startswith('itempool_ps_') else 'side'}]
            if not specs:raise RuntimeError('Empty native prize plan')
            parent={'state':'native spawn queued','rarity':outcome['rarity'] or outcome['name'],
                    'outcome':outcome['name'],'items':[],'item_count':len(specs),
                    'note':'Native items dispensed sequentially; collect each before next appears'}
            jobs=[]
            for index,spec in enumerate(specs):
                receipt={'state':'native spawn queued','index':index+1,'category':spec['category'],
                         'native_attempted':False,
                         'rarity':spec['rarity'],'pool':spec['pool'],'game_stage':spec['stage']}
                parent['items'].append(receipt)
                jobs.append(dict(spec,player=active['player'],receipt=receipt))
            if outcome['kind'] in ('jackpot','eridium'):
                parent['note']=('Small native Eridium spill; normal ground pickup, no held stacks' if outcome['kind']=='eridium' else
                                'Guaranteed epic/legendary held in tray; 20 random native items scattered upward')
                self.pending={'player':active['player'],'receipt':parent,'route':'front','burst_specs':specs,'burst_mode':outcome['kind']}
                self.backlog=[]
            else:
                if len(jobs)==1:parent.update(parent['items'][0])
                self.pending=jobs[0];self.backlog=jobs[1:]
            self.batch_receipt=parent
            self.paid_active=active if active.get('wager',0)>0 and active.get('cash_receipt') is None else None
            self.ctx._report['native_reward_batch']=parent
            return parent
        except Exception as error:return {'state':'unavailable','error':str(error),'note':'Reward could not be queued; paid-spin controller refunds the wager'}

    def release_pending(self):
        job,self.pending=self.pending,None  # Clear BEFORE calling native; never retry an uncertain request.
        if job is None:return None
        receipt=job['receipt']
        if job.get('burst_specs') is not None:
            self.burst.open(job['player'],job['burst_specs'],receipt,mode=job.get('burst_mode','jackpot'))
            return receipt
        receipt['state']='native spawn attempting'
        receipt['native_attempted']=None
        try:
            if self.ctx.get_pc(possibly_loading=True)!=job['player']:
                receipt['native_attempted']=False
                raise RuntimeError('Player changed before weapon payout')
            receipt.update(self.spawn(job['rarity'],job['stage'],job['pool'],display=True,category=job.get('category'),attempt_receipt=receipt))
        except Exception as error:
            receipt.update(state='unavailable',error=str(error),note='Native payout not retried; an uncertain engine request is never duplicated')
            self.refund_unattempted('Native reward failed before any engine loot request')
        if self.batch_receipt:
            if len(self.batch_receipt['items'])==1:self.batch_receipt.update(receipt)
            else:self.batch_receipt['state']=receipt['state']
            if receipt['state']=='unavailable':
                for queued in self.backlog:queued['receipt'].update(state='cancelled',note='Prior request failed; remaining batch stopped')
                self.backlog=[]
        self.ctx._report['loot_result']=receipt
        return receipt

    def refund_unattempted(self,reason):
        """Refund only a positively known zero-request paid payout, once."""
        active=self.paid_active
        if not active or active.get('gear_refund') or not self.batch_receipt:return False
        items=self.batch_receipt.get('items',[])
        if not items or any(item.get('native_attempted') is not False for item in items):return False
        receipt=active.get('cash_receipt')
        if receipt is None or receipt.get('state')!='settled':return False
        if self.ctx.get_pc(possibly_loading=True)!=active['player']:return False
        try:
            refund=self.ctx._change_cash(active['player'],active['wager'],'unattempted native reward refund')
        except Exception as error:
            self.ctx._report['gear_refund_error']=str(error);return False
        active['gear_refund']=refund
        receipt.update(state='refunded',refund=refund,after=refund['after'],net=0,note=reason+'; wager refunded once')
        self.batch_receipt['refund']=refund
        self.ctx._report['cash_result']=receipt
        return True

    def _display_finished(self,state):
        if state.startswith('pickup not identified'):
            for job in self.backlog:job['receipt'].update(state='cancelled',note='Prior pickup not identified; remaining batch stopped without retry')
            self.backlog=[]
            if self.batch_receipt:self.batch_receipt['note']='Prior pickup not identified; remaining batch stopped'

    def currency_balance(self,pc):
        try:
            matches=[row for row in pc.CurrencyManager.currencies if str(row.type.Name).casefold()=='eridium']
            if len(matches)==1 and type(matches[0].Amount) is int:return matches[0].Amount
        except Exception:pass
        return None

    def queue_item_test(self,category,rarity='rare',variant='normal'):
        self.catalog()
        if category=='eridium':
            wanted={'normal':'itempool_eridium_normal','rich':'itempool_eridium_rich','multi':'itempool_event_loot_eridium'}[variant]
        else:
            prefix={'classmod':'class_mods','enhancement':'enhancements','shield':'shields','repkit':'repkit','ordnance':'grenade_gadgets','heavy':'hw'}[category]
            wanted=f'itempool_{prefix}_{RARITIES.index(rarity)+1:02}_{rarity}'
        matches=[name for name,row in self.extra_rows.items() if row['key']==wanted]
        if len(matches)!=1:raise RuntimeError('Expected one authored extra reward pool: '+wanted)
        # Always automatic stage; the same resolver as paid weapon rewards.
        receipt=self.queue_test(rarity,name=matches[0],category=category)
        receipt['scope']='ONE free native item-pool test; no wager'
        if category=='eridium':receipt['amount_policy']='One native stack; paid purple results use level-scaled stack counts'
        return receipt

    def observe_currency(self,now):
        job=self.currency_receipt
        if job is None or now<self.currency_next:return
        self.currency_next=now+.25
        pc=self.ctx.get_pc(possibly_loading=True)
        if pc is None or self.ctx._path(pc)!=job['player_path'] or self.ctx._path(getattr(pc,'Pawn',None))!=job['pawn_path']:
            self.currency_receipt=None;return
        info=job['receipt']['currency'];after=self.currency_balance(pc)
        info['after']=after
        if info['before'] is not None and after is not None:info['delta']=after-info['before']
        self.ctx._report['eridium_test']=job['receipt']
        # Keep observing while a displayed item is waiting; native collection
        # may hide/recycle the actor before its wallet replication is visible.
        if not self.waiting() and now-job['born']>=5:
            self.currency_receipt=None;self.ctx._request_report()

    def waiting(self,route=None):
        return (self.pending is not None and (route is None or self.pending['route']==route)) or any(route is None or job['route']==route for job in self.backlog) or self.display.waiting(route) or self.burst.waiting(route)

    def tick(self,now=None):
        self.burst.tick(self.ctx._clock() if now is None else now)
        self.display.tick(now)
        if self.pending is None and self.backlog and not self.display.waiting():
            self.pending=self.backlog.pop(0)
            route=self.pending['route']
            state=self.ctx._tray_state if route=='front' else self.ctx._weapon_state
            if state and state['state']=='open':self.release_pending()
            else:self.ctx._begin_gear_payout()
        if self.batch_receipt and self.batch_receipt.get('outcome') not in ('jackpot','eridium_small','eridium_large') and not self.waiting():
            items=self.batch_receipt.get('items',[])
            if any(item.get('state')=='unavailable' for item in items):
                self.batch_receipt['state']='payout failed; native request not retried'
            elif any(item.get('display',{}).get('state','').startswith('pickup not identified') for item in items):
                self.batch_receipt['state']='native pickup not identified; native request not retried'
            elif any(item.get('state')=='cancelled' for item in items):
                self.batch_receipt['state']='cancelled'
            else:self.batch_receipt['state']='payout requests finished; native pickups collected or no longer tracked'
        if self.paid_active is not None and not self.waiting():self.paid_active=None
        self.observe_currency(self.ctx._clock() if now is None else now)

    def clear(self):
        self.catalog_key=None
        self.pools.clear()
        self.refund_unattempted('Queued reward cancelled before any engine loot request')
        self.burst.clear()
        self.currency_receipt=None
        self.cancel_pending()
        self.display.clear()
        self.paid_active=None

    def cancel_pending(self):
        if self.batch_receipt and (self.pending is not None or self.backlog):self.batch_receipt['state']='cancelled'
        job,self.pending=self.pending,None
        for pending in ([job] if job is not None else [])+self.backlog:
            pending['receipt'].update(state='cancelled',note='Machine removed before native item request')
        self.backlog=[]


class NativePrizeDisplay:
    """Hold ONE newly spawned native pickup; collection stays with BL4.

    Store paths instead of long-lived native actor/component wrappers. Each
    update reacquires live actors before reading/setting native properties.
    Ambiguous or distant pickups are never selected, modified or destroyed.
    """
    def __init__(self,ctx):
        self.ctx=ctx
        self.record=None
        self.next_poll=0

    def waiting(self,route=None):
        return self.record is not None and (route is None or self.record['route']==route)

    def begin(self,before,receipt,transform,route,player,initial_live=None):
        if self.record is not None:raise RuntimeError('A native weapon pickup is still waiting')
        info={'state':'waiting for native pickup','route':route,'collection':'native game interaction'}
        receipt['display']=info
        self.record={'before':before,'receipt':receipt,'info':info,'transform':transform,
                     'route':route,'player':player,'born':self.ctx._clock(),'path':None,
                     'physics':None,'holding':False,'visuals':[],'collection_flags':{},
                     'metrics':{'global_scans':0,'direct_lookups':0,'corrections':0,'ownership_checks':0}}
        from .fleet import manager
        manager(self.ctx).attach_pickup(self)
        self.next_poll=0
        self.tick(initial_live=initial_live)

    def item_id(self,actor):
        return prize_ownership.instance_id(prize_ownership.item(actor))

    def native_equipped(self,pawn,weapon):
        r=self.record
        if r is None or r.get('item_id') is None:return
        if pawn!=getattr(r['player'],'Pawn',None):return
        if self.item_id(weapon)!=r['item_id']:return
        # WeaponEquipped is a positive native success signal. Defer cleanup
        # until our tick, outside the engine callback and shared hook dispatch.
        r['holding']=False
        r['equipped_confirmed']=True
        r['info']['ownership']='native equip callback'
        r['info']['state']='native equip confirmed; display restraint released'

    def ownership_changed(self,actor,r):
        original=r.get('item_id')
        if original is None:return False
        current=self.item_id(actor)
        # A swapped-out weapon can reuse a world pickup. It is not our reward.
        if current is not None and current!=original:return True
        return False

    def retire_equipped(self,r):
        self.retire_collected(r,'native equip confirmed; prize collected')

    def retire_collected(self,r,state):
        # The item is positively owned by the player, or the claimed pickup is
        # now empty. Retire only our old world actor; a replacement native swap
        # item must keep its mesh, interaction and normal physics.
        actor=self.lookup('InventoryPickup',r['path']) if r['path'] else None
        current=self.item_id(actor) if actor is not None else None
        if current is not None and current!=r.get('item_id'):
            self.release_replacement(actor,r)
            self.finish(state+'; replacement world item released');return
        cleanup={'hidden_visuals':0,'errors':[]}
        r['info']['cleanup']=cleanup
        # Native collection may remove the actor before a detached visual goes
        # away. A mesh transferred to the equipped weapon has a different owner
        # and must never be hidden, even if its component path is unchanged.
        for visual in r['visuals']:
            try:
                component=self.lookup('Object',visual['path'])
                if component is None:continue
                owner=self.ctx._call(component,'GetOwner')
                if owner is None or self.ctx._path(owner)!=r['path']:continue
                self.ctx._call(component,'SetHiddenInGame',NewHidden=True,bPropagateToChildren=False)
                cleanup['hidden_visuals']+=1
            except Exception as error:cleanup['errors'].append(str(error))
        if actor is not None and getattr(actor,'bActorIsBeingDestroyed',False) is not True:
            for method,params in (
                    ('SetActorHiddenInGame',{'bNewHidden':True}),
                    ('SetActorEnableCollision',{'bNewActorEnableCollision':False}),
                    ('K2_DestroyActor',{})):
                try:self.ctx._call(actor,method,**params)
                except Exception as error:cleanup['errors'].append(method+': '+str(error))
        r['holding']=False
        self.finish(state)

    def release_replacement(self,actor,r):
        r['holding']=False
        if r['physics'] is not None:
            self.ctx._call(self.root(actor),'SetSimulatePhysics',bSimulate=r['physics'])

    def observe_ownership(self,r,now):
        if r.get('item_id') is None or now<r.get('next_ownership_check',0):return False
        r['next_ownership_check']=now+.25
        source,reads=prize_ownership.owned(r['player'],r['item_id'])
        r['metrics']['ownership_checks']+=1
        r['info']['ownership_reads']=reads
        # A world reward appearing in committed player inventory is a success
        # signal even when BL4 misses the equip callback. Ignore identities that
        # were already owned when this specific pickup was first claimed.
        if source and not r.get('owned_at_claim'):
            r['info']['ownership']=source
            self.retire_collected(r,'native inventory confirmed; prize collected')
            return True
        return False

    def native_use(self,player,path):
        r=self.record
        if r is None or player!=r['player'] or path!=r['path']:return
        r['native_attempt']=self.ctx._clock()
        r['report_attempt']=True
        r['holding']=False
        r.pop('attempt_recovered',None)
        r['info']['state']='native pickup attempt; display restraint suspended'

    def detach(self):
        from .fleet import manager
        manager(self.ctx).detach_pickup(self)

    def live(self):
        return {self.ctx._path(a):a for a in unrealsdk.find_all('InventoryPickup',exact=False)
                if not self.ctx._path(a).startswith('/Script/')}

    def lookup(self,kind,path):
        # BL4 SDK uses StaticFindObjectSafe; no full GUObjectArray iteration.
        if self.record is not None:self.record['metrics']['direct_lookups']+=1
        try:return unrealsdk.find_object(kind,path)
        except ValueError:return None

    def visual_state(self,component):
        state={}
        for name in ('bVisible','bHiddenInGame'):
            value=getattr(component,name,None)
            if type(value) is bool:state[name]=value
        for name in ('StaticMesh','SkeletalMeshAsset','SkeletalMesh'):
            try:value=getattr(component,name)
            except AttributeError:continue
            state[name]=self.ctx._path(value) if value is not None else None
        return state

    def capture_visuals(self,actor):
        components=[]
        try:
            result=self.ctx._call(actor,'K2_GetComponentsByClass',ComponentClass=unrealsdk.find_class('/Script/Engine.MeshComponent'))
            components=list(result)
        except (AttributeError,TypeError,ValueError,RuntimeError):pass
        if not components:components=[self.root(actor)]
        visuals=[]
        for component in components:
            state=self.visual_state(component)
            if state.get('bVisible') is False or state.get('bHiddenInGame') is True:continue
            # Track only initially visible meshes; pre-hidden components do not
            # prevent closure when the displayed weapon is removed.
            if state:
                visuals.append({'path':self.ctx._path(component),'initial':state})
        return visuals

    def visuals_removed(self,record):
        visuals=record['visuals']
        if not visuals:return False
        for visual in visuals:
            component=self.lookup('Object',visual['path'])
            if component is None:continue
            state=self.visual_state(component);initial=visual['initial']
            if state.get('bVisible') is False or state.get('bHiddenInGame') is True:continue
            assets=[name for name in ('StaticMesh','SkeletalMeshAsset','SkeletalMesh') if initial.get(name) is not None]
            if assets and all(state.get(name) is None for name in assets):continue
            return False
        return True

    def collection_flags(self,actor):
        # Optional positive collection-state flags, used only when actually
        # exposed as booleans by this installed runtime. No guessed function calls.
        names=('bIsBeingPickedUp','bIsBeingCollected','bPickedUp','bWasPickedUp','bIsCollected','bCollected')
        result={}
        for name in names:
            value=getattr(actor,name,None)
            if type(value) is bool:result[name]=value
        return result

    def removed(self,actor):
        if bool(getattr(actor,'bHidden',False)):return True
        try:getattr(actor,'IsActorBeingDestroyed')
        except AttributeError:return False
        return bool(self.ctx._call(actor,'IsActorBeingDestroyed'))

    def root(self,actor):
        root=getattr(actor,'RootPrimitiveComponent',None)
        if root is None:root=getattr(actor,'RootComponent',None)
        if root is None:raise RuntimeError('Native pickup has no primitive root')
        return root

    def place(self,actor,record):
        ctx=self.ctx
        ctx._call(actor,'K2_SetActorTransform',NewTransform=record['transform'],bSweep=False,bTeleport=True)
        # Some pickup visuals simulate separately from the actor root.
        root=self.root(actor)
        if root!=getattr(actor,'RootComponent',root):
            ctx._call(root,'K2_SetWorldTransform',NewTransform=record['transform'],bSweep=False,bTeleport=True)

    def opening(self,record):
        state=getattr(self.ctx,'_tray_state' if record['route']=='front' else '_weapon_state',None)
        return state is not None and state.get('state')=='opening'

    def gate(self,actor,record,blocked):
        """Temporarily remove pickup interaction; restore captured state only."""
        if blocked and 'opening_gate' not in record:
            saved={'flags':{},'collision':None}
            record['opening_gate']=saved
            # Optional native booleans are used only when actually exposed.
            for name in ('bPickupEnabled','bCanBePickedUp','bCanBeCollected','bInteractionEnabled'):
                value=getattr(actor,name,None)
                if type(value) is bool:
                    saved['flags'][name]=value
                    setattr(actor,name,False)
            try:
                value=self.ctx._call(actor,'GetActorEnableCollision')
                if type(value) is not bool:raise RuntimeError('Collision getter did not return a bool')
                setter=actor.SetActorEnableCollision
                if {str(p.Name) for p in setter.func._properties()}!={'bNewActorEnableCollision'}:
                    raise RuntimeError('Collision setter signature differs')
                saved['collision']=value
                self.ctx._call(actor,'SetActorEnableCollision',bNewActorEnableCollision=False)
            except Exception as error:record['info']['opening_gate_note']=str(error)
            record['info']['opening_gate']='pickup disabled during opening' if saved['flags'] or saved['collision'] is not None else 'native pickup gate unavailable; check report'
        elif not blocked and record.get('opening_gate') is not None:
            saved=record['opening_gate']
            for name,value in list(saved['flags'].items()):
                setattr(actor,name,value);del saved['flags'][name]
            if saved['collision'] is not None:
                self.ctx._call(actor,'SetActorEnableCollision',bNewActorEnableCollision=saved['collision'])
                saved['collision']=None
            record['opening_gate']=None
            record['info']['opening_gate']='original pickup interaction restored'

    def animate_opening(self,record):
        # Motion runs on every game frame while opening, not the slower pickup
        # poll. Only direct path lookups are used after the one-time claim.
        if record['path'] is None:return
        if not self.opening(record) and not record.get('opening_gate'):return
        actor=self.lookup('InventoryPickup',record['path'])
        if actor is None:return
        try:
            ctx=self.ctx
            if not self.opening(record):self.gate(actor,record,False)
            transform,position=self.placement(
                ctx._call(ctx._actor,'K2_GetActorLocation'),
                float(ctx._call(ctx._actor,'K2_GetActorRotation').Yaw),
                ctx._model_height,record['route'],record['receipt'].get('category'))
            record['transform']=transform
            if record['holding']:self.place(actor,record)
            record['receipt']['position']=position
        except Exception as error:record['info']['opening_motion_error']=str(error)

    def finish(self,state):
        record,self.record=self.record,None
        self.detach()
        if record is None:return
        record['info']['state']=state
        prize_ownership.report(self.ctx,record,'finished')
        callback=getattr(self,'on_finish',None)
        if callback:callback(state)
        self.ctx._report['loot_result']=record['receipt']
        self.ctx._finish_gear_payout(record['route'])
        self.ctx._request_report()

    def tick(self,now=None,initial_live=None):
        r=self.record
        if r is None:return
        ctx=self.ctx
        now=ctx._clock() if now is None else now
        if ctx.get_pc(possibly_loading=True)!=r['player']:
            self.record=None  # Travel/controller change: no stale native reads.
            self.detach()
            return
        if r.get('equipped_confirmed'):
            self.retire_equipped(r);return
        # Check identity before any opening-animation or restraint write.
        if r['path'] is not None:
            actor=self.lookup('InventoryPickup',r['path'])
            if actor is not None and self.ownership_changed(actor,r):
                self.release_replacement(actor,r)
                self.finish('native pickup item changed; replacement released');return
            if actor is not None:
                r['info']['current_item_id']=self.item_id(actor)
                r['info']['quantity']=prize_ownership.quantity(actor)
                if r['info']['quantity']==0:
                    r['info']['ownership']='native pickup emptied'
                    self.retire_collected(r,'native pickup emptied; old display cleared');return
            if self.observe_ownership(r,now):return
            if r.pop('report_attempt',False):prize_ownership.report(ctx,r,'native interaction')
        self.animate_opening(r)
        if now<self.next_poll:return
        self.next_poll=now+.1
        try:
            if r['path'] is None:
                if initial_live is None:
                    r['metrics']['global_scans']+=1
                    live=self.live()
                else:live=initial_live
                target=r['transform'].Translation
                candidates=[]
                for path,a in live.items():
                    if path in r['before'] or self.removed(a):continue
                    p=ctx._call(a,'K2_GetActorLocation')
                    if sum((float(getattr(p,k))-float(getattr(target,k)))**2 for k in ('X','Y','Z'))<=90**2:
                        candidates.append((path,a))
                if len(candidates)>1:
                    r['info']['note']='Multiple new nearby pickups; none claimed'
                elif len(candidates)==1:
                    path,actor=candidates[0]
                    r['path']=path;r['info']['actor']=path
                    r['item_id']=self.item_id(actor);r['info']['item_id']=r['item_id']
                    r['owned_at_claim'],reads=prize_ownership.owned(player=r['player'],wanted=r['item_id'])
                    r['info']['ownership_reads']=reads
                    r['next_ownership_check']=now+.25
                    if self.opening(r):self.gate(actor,r,True)
                    self.next_poll=now+.1
                    r['collection_flags']=self.collection_flags(actor)
                    r['info']['collection_flags']=r['collection_flags']
                    r['visuals']=self.capture_visuals(actor)
                    r['info']['visuals']=r['visuals']
                    r['info']['metrics']=r['metrics']
                    r['info']['state']='waiting for pickup'
                    try:
                        root=self.root(actor)
                        r['physics']=bool(ctx._call(root,'IsSimulatingPhysics',BoneName='None'))
                        ctx._call(root,'SetSimulatePhysics',bSimulate=False)
                        self.place(actor,r)
                        r['holding']=True
                        r['info']['holding']='physics stopped; native pickup preserved'
                    except Exception as error:
                        # Keep collection tracking even if display restraint fails.
                        r['info']['holding_error']=str(error)
                        try:
                            if r['physics'] is not None:ctx._call(self.root(actor),'SetSimulatePhysics',bSimulate=r['physics'])
                        except Exception:pass
                    ctx._report['loot_result']=r['receipt'];ctx._request_report()
                    prize_ownership.report(ctx,r,'display claimed')
                if r['path'] is None and now-r['born']>=3:
                    self.finish('pickup not identified; native request not repeated')
                return
            actor=self.lookup('InventoryPickup',r['path'])
            if actor is None:
                self.retire_collected(r,'pickup removed by game');return
            if getattr(actor,'bActorIsBeingDestroyed',False) is True:
                self.retire_collected(r,'native pickup being destroyed');return
            # A failed/full-backpack attempt may briefly hide or animate the
            # pickup. That transient state is not proof of collection.
            invisible=self.removed(actor) or self.visuals_removed(r)
            if invisible:
                r.setdefault('hidden_since',now)
                if now-r['hidden_since']>=1.5:self.finish('pickup removed by game')
                return
            r.pop('hidden_since',None)
            progress=any(not initial and getattr(actor,name,False) is True for name,initial in r['collection_flags'].items())
            if progress and 'native_attempt' not in r and not r.get('attempt_recovered'):
                r['native_attempt']=now
                r['holding']=False
            if 'native_attempt' in r:
                # A surviving, visible pickup is still a world reward. Do not
                # wait for a latched native flag or a physics bounce to settle.
                if now-r['native_attempt']<.6:return
                # Never pull a surviving actor back into the tray after a
                # native swap. The engine may be transferring its item/mesh.
                # Hand movement back to BL4 and close the now-released display.
                if r['physics'] is not None:
                    ctx._call(self.root(actor),'SetSimulatePhysics',bSimulate=r['physics'])
                r['holding']=False;r.pop('native_attempt',None)
                r['attempt_recovered']=True
                r['info']['state']='native interaction handed back; world item preserved'
                prize_ownership.report(ctx,r,'native interaction handoff')
                self.finish('native interaction released; world item preserved')
                return
            if not self.opening(r):
                root=self.root(actor)
                physics=bool(ctx._call(root,'IsSimulatingPhysics',BoneName='None'))
                p=ctx._call(actor,'K2_GetActorLocation');q=r['transform'].Translation
                moved=sum((float(getattr(p,k))-float(getattr(q,k)))**2 for k in ('X','Y','Z'))>4
                if physics or moved:
                    # Swaps need not pass through ServerUseObject. Observe
                    # native movement/physics without fighting it or treating
                    # it as proof that the player received the item.
                    r['holding']=False
                    r['info']['state']='native movement resumed; display restraint released'
                    if r['physics'] is not None and not physics:
                        ctx._call(root,'SetSimulatePhysics',bSimulate=r['physics'])
                    prize_ownership.report(ctx,r,'native movement handoff')
                    self.finish('pickup left display; world item preserved')
                    return
        except Exception as error:
            # No uncertain native request retries or tight-loop failed writes.
            r['info']['tracking_error']=str(error)
            r['holding']=False
            self.next_poll=now+.5

    def snapshot(self):
        r=self.record
        if r is None:return {'state':'idle'}
        result={'state':r['info']['state'],'route':r['route'],'actor':r['path'],
                'metrics':dict(r['metrics']),'visuals':[]}
        try:
            actor=self.lookup('InventoryPickup',r['path']) if r['path'] else None
            result['actor_missing']=actor is None
            if actor is not None:
                result['actor_removed']=self.removed(actor)
                result['collection_flags']=self.collection_flags(actor)
            for visual in r['visuals']:
                component=self.lookup('Object',visual['path'])
                result['visuals'].append({'path':visual['path'],'initial':visual['initial'],
                    'current':self.visual_state(component) if component is not None else None})
        except Exception as error:result['error']=str(error)
        return result

    def clear(self):
        r,self.record=self.record,None
        self.detach()
        if r is None:return
        try:
            actor=self.lookup('InventoryPickup',r['path']) if r['path'] else None
            if actor is not None and not self.removed(actor):self.gate(actor,r,False)
            if actor is not None and not self.removed(actor) and r['physics'] is not None:
                self.ctx._call(self.root(actor),'SetSimulatePhysics',bSimulate=r['physics'])
            r['info']['state']='released on machine cleanup; native item preserved'
        except Exception as error:r['info']['cleanup_error']=str(error)
