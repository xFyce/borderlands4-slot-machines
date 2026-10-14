"""Held jackpot guarantee, scattered extras, and gentle native Eridium spills.

World loot remains game-owned; no restraint, pickup hooks, wallet credit,
retained actor wrappers or native request retries are used here.
"""
import math
import random
import unrealsdk

class LootBurst:
    def __init__(self,ctx,loot):
        self.ctx=ctx;self.loot=loot;self.job=None

    def waiting(self,route=None):
        return self.job is not None and (route in (None,'front') or
            route=='side' and self.job['guarantee_pending'] and self.job['guarantee_route']=='side')

    def open(self,pc,specs,receipt,mode="jackpot"):
        if mode not in ("jackpot","eridium"):raise ValueError("Unknown native spill mode")
        if self.job:raise RuntimeError('Native burst already active')
        receipt.update(state='dispensing '+mode+' burst',dispensed=0,
                       note=('Small native Eridium spill; ground loot collects normally, no held display' if mode=='eridium' else
                             'Guaranteed epic/legendary held in tray; extras scatter as native world loot'))
        self.job={'player':self.ctx._path(pc),'pawn':self.ctx._path(pc.Pawn),
                  'specs':list(specs),'receipt':receipt,'next':self.ctx._clock(),
                  'observations':[],'machine':self.ctx._path(self.ctx._actor),
                  'mode':mode,'guarantee_pending':mode=='jackpot','guarantee_route':specs[0]['route']}

    def live(self):
        return {self.ctx._path(a):a for a in unrealsdk.find_all('InventoryPickup',exact=False)
                if not self.ctx._path(a).startswith('/Script/')}

    def arc(self,record,now):
        ctx=self.ctx
        if record.get('done'):return
        if now<record.get('next',0):return
        record['next']=now+(.02 if record.get('mode')=='eridium' else .1)
        target=record['receipt']['position']
        candidates=[]
        # Reacquire actors only in the narrow initialization window. Paths in
        # before include earlier burst prizes, so they are never tossed twice.
        if record.get('path'):
            actor=self.loot.display.lookup('InventoryPickup',record['path'])
            live={} if actor is None else {record['path']:actor}
        else:live=self.live()
        for path,actor in live.items():
            if path in record['before'] or bool(getattr(actor,'bHidden',False)):continue
            p=ctx._call(actor,'K2_GetActorLocation')
            if record.get('path')==path or sum((float(getattr(p,k))-target[k.lower()])**2 for k in ('X','Y','Z'))<90**2:candidates.append((path,actor))
        if len(candidates)==1:
            path,actor=candidates[0]
            record['path']=path
            try:
                root=getattr(actor,'RootPrimitiveComponent',None)
                if root is None:root=getattr(actor,'RootComponent',None)
                if root is None:
                    if now>=record['deadline']:
                        record['done']=True;record['receipt']['burst_arc']={'state':'native root not ready; loot unchanged'}
                    return
                if ctx._call(root,'IsSimulatingPhysics',BoneName='None'):
                    fn=root.SetPhysicsLinearVelocity
                    if {str(p.Name) for p in fn.func._properties()}!={'NewVel','bAddToCurrent','BoneName'}:
                        raise RuntimeError('Native linear-velocity signature differs')
                    yaw=float(ctx._call(ctx._actor,'K2_GetActorRotation').Yaw)
                    spill=record.get('mode')=='eridium'
                    heading=math.radians(yaw+random.SystemRandom().uniform(-60 if spill else -70,60 if spill else 70))
                    speed=random.SystemRandom().uniform(110,190) if spill else random.SystemRandom().uniform(200,650)
                    up=random.SystemRandom().uniform(340,420) if spill else random.SystemRandom().uniform(760,850)
                    velocity=[speed*math.cos(heading),speed*math.sin(heading),up]
                    record['done']=True  # One attempt, even if a native call raises.
                    ctx._call(root,'SetPhysicsLinearVelocity',NewVel=ctx._vec(*velocity),bAddToCurrent=False,BoneName='None')
                    record['receipt']['burst_arc']={'state':'scattered native physics toss','actor':path,'velocity':velocity}
                    return
            except Exception as error:
                record['done']=True;record['receipt']['burst_arc']={'state':'native loot left unchanged','error':str(error)}
        if now>=record['deadline']:
            record['done']=True
            record['receipt'].setdefault('burst_arc',{'state':'native pickup not ready/unique; no velocity write or respawn'})

    def tick(self,now):
        job=self.job;ctx=self.ctx
        if job is None:return
        pc=ctx.get_pc(possibly_loading=True)
        if pc is None or ctx._path(pc)!=job['player'] or ctx._path(getattr(pc,'Pawn',None))!=job['pawn'] or ctx._path(ctx._actor)!=job['machine']:
            self.clear();return
        if job['mode']=='eridium':
            tray=getattr(ctx,'_tray_state',None)
            if tray and tray['state']=='opening' and tray.get('open_fraction',0)<.2:return
        if job['guarantee_pending']:
            if job['receipt']['dispensed']==0:
                if job['guarantee_route']=='side':
                    state=ctx._weapon_state
                    if state is None or state['state']=='closed':
                        ctx._begin_weapon_door()
                        ctx._weapon_state['reward_cycle']=True
                        return
                    if state['state'] not in ('opening','open'):return
                spec=job['specs'].pop(0)
                receipt=job['receipt']['items'][0]
                receipt['state']='native request attempting'
                receipt['native_attempted']=None
                try:
                    # Use the proven held-prize backend, never toss the guarantee.
                    receipt.update(self.loot.spawn(spec['rarity'],spec['stage'],spec['pool'],display=True,category=spec['category'],route_override=spec['route'],attempt_receipt=receipt))
                    job['receipt']['dispensed']=1
                except Exception as error:
                    receipt.update(state='unavailable',error=str(error))
                    self.loot.refund_unattempted('Jackpot guarantee failed before any engine loot request')
                    self.clear();ctx._maybe_close_front_tray();return
            record=self.loot.display.record
            if record is not None and record['path'] is None:return
            tray=getattr(ctx,'_tray_state',None)
            if tray is not None and tray['state']=='opening':return
            door=getattr(ctx,'_weapon_state',None)
            if job['guarantee_route']=='side' and door is not None and door['state']=='opening':return
            if record is None and job['receipt']['items'][0].get('display',{}).get('state','').startswith('pickup not identified'):
                self.clear();ctx._maybe_close_front_tray();return
            job['guarantee_pending']=False
        # Resolve one native item at a time before requesting the next. This
        # preserves unique provenance even with asynchronous native spawning.
        for record in job['observations']:
            if not record.get('done'):self.arc(record,now)
        if job['specs'] and now>=job['next'] and all(r.get('done') for r in job['observations']):
            spec=job['specs'].pop(0)
            receipt=job['receipt']['items'][job['receipt']['dispensed']]
            try:
                receipt.update(state='native request attempting')
                receipt['native_attempted']=None
                receipt.update(self.loot.spawn(spec['rarity'],spec['stage'],spec['pool'],display=False,category=spec['category'],route_override='front',attempt_receipt=receipt))
                job['receipt']['dispensed']+=1
                observation={'receipt':receipt,'before':set(receipt.pop('_burst_before',[])),'deadline':now+.75,'mode':job['mode']}
                job['observations'].append(observation)
                self.arc(observation,now)
                job['next']=now+.08
            except Exception as error:
                receipt.update(state='unavailable',error=str(error),note='Burst stopped; uncertain request not retried')
                self.loot.refund_unattempted('Native burst failed before any engine loot request')
                job['specs']=[]
                for item in job['receipt']['items'][job['receipt']['dispensed']+1:]:item['state']='cancelled'
                job['receipt']['note']='Native request failed; remaining burst stopped without retry'
        if not job['specs'] and all(r.get('done') for r in job['observations']):
            failed=any(i.get('state')=='unavailable' for i in job['receipt']['items'])
            job['receipt']['state']=('payout failed; native request not retried' if failed else
                'eridium spill finished; collect native ground loot' if job['mode']=='eridium' else
                'jackpot burst finished; collect tray guarantee and native world loot')
            self.job=None
            ctx._maybe_close_front_tray();ctx._request_report()

    def clear(self):
        if self.job:
            for receipt in self.job['receipt']['items'][self.job['receipt']['dispensed']:]:
                if receipt.get('state')!='unavailable':receipt['state']='cancelled'
            self.job['receipt'].update(state='burst cancelled',note='Unspawned prizes cancelled; spawned native world loot preserved')
        self.job=None
