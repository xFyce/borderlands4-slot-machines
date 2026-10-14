"""Equipment-independent native grenade payout and explicit free tests.

SpawnGrenade and SpawnGrenadeOptions signatures were read from the user's
20261010_063620 probe. Source/offset semantics require in-game verification.
No shared definitions, gadget state or charge values are changed. One
launch adjustment applies only to the discovered grenade; native damage,
behaviors and explosion remain native. The dedicated source is a separate
test path and never consults the equipped gadget or inventory identity.
"""
import math
import random
import unrealsdk

RELEASE_OPEN_FRACTION=0.20
ARC_HEIGHT_SCALE=3.3
ARC_SPEED_SCALE=math.sqrt(ARC_HEIGHT_SCALE)

OPTION_FIELDS={'SpawnSocket','SpawnFuseTime','bUseThrowAtLocation',
               'ThrowAtLocation','GrenadeOwner','GrenadeSource','SpawnOffset'}


class GrenadeTest:
    def __init__(self,ctx):
        self.ctx=ctx;self.job=None;self.blocked=None;self.paid_active=None

    def waiting(self):
        return self.job is not None and self.job['state'] in ('opening','observing')

    def _owned_gadget(self,pc):
        matches=[]
        for gadget in unrealsdk.find_all('GrenadeGadget',exact=False):
            path=self.ctx._path(gadget)
            if path.startswith('/Script/'):continue
            # OwningCharacter was confirmed by runtime reflection. Never
            # choose an enemy gadget or use the default object.
            if self.ctx._path(gadget.OwningCharacter)==self.ctx._path(pc.Pawn):matches.append(gadget)
        if len(matches)!=1:raise RuntimeError('Expected one grenade gadget owned by your current character; found '+str(len(matches)))
        return matches[0]

    def prepare(self,native=False,profile=None):
        ctx=self.ctx
        if self.blocked:raise RuntimeError(self.blocked)
        if self.waiting():raise RuntimeError('A grenade test is already active')
        pc=ctx.get_pc(possibly_loading=True)
        if pc is None or pc.Pawn is None or not ctx._call(pc,'HasAuthority'):
            raise RuntimeError('Load into single-player or host before this test')
        if native:
            from .native_grenade import validate,DEFINITION_NAME,DEFINITION_TYPE
            validate(ctx)
            self.job={'state':'armed','backend':'dedicated_native','pawn':ctx._path(pc.Pawn),
                'machine':ctx._path(ctx._actor),'native_calls':0,'samples':[],'projectiles':[],
                'scope':'Explicit free native-source test; no equipped-ordnance reads; paid outcomes unchanged',
                'native_source':{'definition':DEFINITION_NAME,'type_handle':DEFINITION_TYPE,
                                 'function':'SpawnProjectile_Const'},
                'offset_semantics':'Explicit world-space tray origin',
                'level_sync':dict(profile) if profile else None,
                'damage':profile['grenade_damage'] if profile else 100.0}
            if profile:self.job['scope']='Paid three-psycho payout; character-level damage; no equipped-ordnance reads'
            ctx._report['grenade_test']=self.job
            return self.job
        gadget=self._owned_gadget(pc)
        params={str(p.Name) for p in gadget.SpawnGrenade.func._properties()}
        if params!={'SpawnGrenadeOptions'}:raise RuntimeError('SpawnGrenade signature differs from the inspected build')
        schema=unrealsdk.find_object('ScriptStruct','/Script/OakGame.SpawnGrenadeOptions')
        if schema is None or {str(p.Name) for p in schema._properties()}!=OPTION_FIELDS:
            raise RuntimeError('SpawnGrenadeOptions differs from the inspected build')
        charges=int(gadget.NumberOfCharges)
        if charges<=0:raise RuntimeError('Wait until your equipped grenade has at least one charge')
        self.job={'state':'armed','gadget':ctx._path(gadget),'pawn':ctx._path(pc.Pawn),
                  'machine':ctx._path(ctx._actor),'charges_before':charges,
                  'scope':'Explicit free test using equipped grenade behavior; paid outcomes unchanged',
                  'native_calls':0,'samples':[],'projectiles':[],
                  'offset_semantics':'Assumed source-local offset; needs visual validation'}
        ctx._report['grenade_test']=self.job
        return self.job

    def _refund_unattempted(self):
        active=self.paid_active;job=self.job
        if not active or not job or job.get('native_calls') or active.get('grenade_refund'):return
        if self.ctx.get_pc(possibly_loading=True)!=active['player']:return
        receipt=active.get('cash_receipt')
        if receipt is None or receipt.get('state')!='settled':return
        # Known zero native attempts only. Never refund an uncertain request.
        refund=self.ctx._change_cash(active['player'],active['wager'],'unattempted grenade refund')
        active['grenade_refund']=refund
        receipt.update(state='refunded',refund=refund,net=0,note='Grenade failed before native attempt; wager refunded once')

    def open(self):
        if self.job is None or self.job['state']!='armed':raise RuntimeError('No armed grenade test')
        rng=random.SystemRandom()
        self.job.update(state='opening',started=self.ctx._clock(),
                        launch_heading_degrees=rng.uniform(-65,65),
                        launch_horizontal_speed=rng.uniform(75,110),
                        launch_upward_speed=rng.uniform(480,600)*ARC_SPEED_SCALE,
                        arc_height_scale=ARC_HEIGHT_SCALE)
        self.ctx._begin_payout_tray(sound='grenade_warning')

    def _points(self):
        ctx=self.ctx
        pose=(ctx._tray_state or {}).get('bone_pose',[0,0,0,0,0,0,1])
        p=ctx._rotate_vector(pose[3:],[10,0,65])
        scale=ctx._model_height/180
        # Start slightly above the opened tray's payout anchor.
        local=[(p[i]+pose[i])*scale for i in range(3)];local[2]+=12
        location=ctx._call(ctx._actor,'K2_GetActorLocation')
        yaw=math.radians(float(ctx._call(ctx._actor,'K2_GetActorRotation').Yaw))
        world=[float(location.X)+local[0]*math.cos(yaw)-local[1]*math.sin(yaw),
               float(location.Y)+local[0]*math.sin(yaw)+local[1]*math.cos(yaw),float(location.Z)+local[2]]
        heading=yaw+math.radians(self.job['launch_heading_degrees'])
        target=[world[0]+180*math.cos(heading),world[1]+180*math.sin(heading),world[2]+220*ARC_HEIGHT_SCALE]
        speed=self.job['launch_horizontal_speed']
        self.job['launch_velocity']=[speed*math.cos(heading),speed*math.sin(heading),self.job['launch_upward_speed']]
        return local,world,target

    def arm_native(self,obj,location):
        """Arm the owned, initialized projectile independently of arc adjustment."""
        ctx=self.ctx;job=self.job
        if job.get('backend')!='dedicated_native' or job.get('native_fuse') is not None:return
        if len(job['projectiles'])!=1 or ctx._clock()-job['released_at']>1.0:return
        point=[float(location.X),float(location.Y),float(location.Z)]
        if point==[0.,0.,0.] or math.dist(point,job['expected_origin'])>600:return
        from .native_grenade import initialized,start_fuse
        if not initialized(ctx,obj,job):return
        start_fuse(ctx,obj,job)
        if job.get('native_fuse',{}).get('state')!='started':
            self.blocked='Dedicated grenade did not arm its native fuse; send the newest report before another test'

    def launch(self,obj,location):
        """One bounded write to our newly discovered projectile, not gadget.

        Velocity and gravity are documented UProjectileMovementComponent
        properties. Do not assume Gearbox's custom component inherits it;
        GetComponentsByClass performs that check in the running game.
        """
        ctx=self.ctx;job=self.job
        if job.get('launch_adjustment') is not None or len(job['projectiles'])!=1:return
        if ctx._clock()-job['released_at']>.5:return
        point=[float(location.X),float(location.Y),float(location.Z)]
        # Native projectile initialization is asynchronous: the report showed
        # a pooled actor at world zero before its real transform was assigned.
        # Never launch while its reported transform is still uninitialized.
        if point==[0.0,0.0,0.0] or math.dist(point,job['expected_origin'])>600:return
        if job.get('backend')=='dedicated_native':
            from .native_grenade import initialized
            if not initialized(ctx,obj,job):return
        job['launch_adjustment']={'state':'attempting','actor':ctx._path(obj),'position':point}
        record=job['launch_adjustment']
        try:
            cls=unrealsdk.find_class('/Script/Engine.ProjectileMovementComponent')
            method='K2_GetComponentsByClass' if hasattr(obj,'K2_GetComponentsByClass') else 'GetComponentsByClass'
            record['component_query']=method
            record['projectile_class']=ctx._path(obj.Class)
            for name in ('BodyData',):
                try:record[name]=ctx._path(getattr(obj,name))
                except Exception:pass
            components=ctx._call(obj,method,ComponentClass=cls)
            if len(components)!=1:raise RuntimeError('Expected one standard projectile movement component; found '+str(len(components)))
            movement=components[0]
            record['component']=ctx._path(movement)
            record['class']=ctx._path(movement.Class)
            fields={str(p.Name) for p in movement.Class._properties()}
            if not {'Velocity','ProjectileGravityScale','bIsHomingProjectile'}<=fields:
                raise RuntimeError('Movement component does not expose the documented launch fields')
            record['before']={'gravity':float(movement.ProjectileGravityScale),
                              'homing':bool(movement.bIsHomingProjectile)}
            movement.ProjectileGravityScale=1.0
            movement.bIsHomingProjectile=False
            movement.Velocity=ctx._vec(*job['launch_velocity'])
            record.update(state='applied',velocity=job['launch_velocity'],elapsed=ctx._clock()-job['released_at'])
        except Exception as error:record.update(state='unavailable',error=str(error))
        from . import diagnostics
        if diagnostics.enabled and record.get('state')=='applied' and not job.get('source_capture_attempted'):
            # Capture configured native source data once, after the accepted
            # launch. This is read-only and never changes the spawn backend.
            job['source_capture_attempted']=True
            try:
                from .grenade_probe import collect
                gadget=unrealsdk.find_object('GrenadeGadget',job['gadget']) if job.get('gadget') else None
                ctx._report['dedicated_grenade_probe']=collect(ctx,projectile=obj,gadget=gadget)
                job['source_capture']='captured'
            except Exception as error:
                job['source_capture']='unavailable'
                job['source_capture_error']=str(error)

    def _projectile_class(self):
        if self.job and self.job.get('backend')=='dedicated_native':
            from .native_grenade import PROJECTILE_CLASS
            return PROJECTILE_CLASS
        return 'ModularGrenade'

    def _scan(self):
        try:return list(unrealsdk.find_all(self._projectile_class(),exact=False))
        except Exception as error:
            self.job['projectile_observation_note']=str(error);return []

    def _native_active_before(self):
        """Pooled NPC actors persist at world zero after native recycling.

        Exclude already active projectiles, but allow a pooled path to become
        our new spawn. Ownership, definition and valid tray position are still
        checked before any write. No UObject wrapper survives this snapshot.
        """
        active=set();pooled=[]
        for obj in self._scan():
            path=self.ctx._path(obj)
            try:
                loc=self.ctx._call(obj,'K2_GetActorLocation')
                point=[float(loc.X),float(loc.Y),float(loc.Z)]
                if point==[0.,0.,0.]:pooled.append(path)
                else:active.add(path)
            except Exception:active.add(path)
        self.job['pooled_before']=sorted(pooled)[:16]
        return active

    def release(self):
        ctx=self.ctx;job=self.job
        if job is None or job['state']!='opening':return
        try:
            pc=ctx.get_pc(possibly_loading=True)
            if pc is None or pc.Pawn is None or ctx._path(pc.Pawn)!=job['pawn'] or ctx._path(ctx._actor)!=job['machine']:
                raise RuntimeError('Character or machine changed before grenade release')
            if not ctx._call(pc,'HasAuthority'):raise RuntimeError('Host authority lost')
            if job.get('backend')=='dedicated_native':
                from .native_grenade import request,FUSE_SECONDS
                local,world,target=self._points()
                before=self._native_active_before()
                job.update(state='native request attempting',expected_origin=world,target=target,
                           source_local_offset=local,fuse_seconds=FUSE_SECONDS)
                request(ctx,pc,job,world)
                job.update(state='observing',released_at=ctx._clock(),next_sample=ctx._clock(),before=sorted(before))
                self.sample(discover=True)
                return
            gadget=self._owned_gadget(pc)
            if ctx._path(gadget)!=job['gadget']:raise RuntimeError('Equipped grenade gadget changed')
            local,world,target=self._points()
            before={ctx._path(p) for p in self._scan()}
            options=unrealsdk.make_struct('SpawnGrenadeOptions',SpawnSocket='None',SpawnFuseTime=3.0,
                bUseThrowAtLocation=True,ThrowAtLocation=ctx._vec(*target),
                GrenadeOwner=pc.Pawn,GrenadeSource=ctx._actor,SpawnOffset=ctx._vec(*local))
            job.update(state='native request attempting',expected_origin=world,target=target,
                       source_local_offset=local,fuse_seconds=3.0,charges_at_release=int(gadget.NumberOfCharges))
            # Mark before entering native code; an uncertain request must never
            # be retried, even when the function raises or no actor is observed.
            job['native_calls']=1
            ctx._save_report()  # Preserve attempted options even if native code crashes.
            ctx._call(gadget,'SpawnGrenade',SpawnGrenadeOptions=options)
            job.update(state='observing',released_at=ctx._clock(),next_sample=ctx._clock(),before=sorted(before))
            self.sample(discover=True)
        except Exception as error:
            job.update(state='failed',error=str(error),note='No automatic native retry')
            self.blocked='Previous grenade test failed; inspect its report before another test'
            self._refund_unattempted()
            ctx._maybe_close_front_tray()
            ctx._save_report()

    def sample(self,discover=False):
        ctx=self.ctx;job=self.job;now=ctx._clock();row={'elapsed':now-job['released_at']}
        try:
            pc=ctx.get_pc(possibly_loading=True)
            if pc is None or pc.Pawn is None or ctx._path(pc.Pawn)!=job['pawn']:
                raise RuntimeError('Character changed during grenade observation')
            gadget=unrealsdk.find_object('GrenadeGadget',job['gadget']) if job.get('gadget') else None
            if gadget is not None:
                row['charges']=int(gadget.NumberOfCharges)
                if row['charges']<job['charges_at_release']:
                    job['charge_decrease_observed']=True
                    self.blocked='Native grenade test decreased charges; further tests blocked until reviewed'
            if discover:
                allowed={job['pawn'],job['machine']}
                if job.get('gadget'):allowed.add(job['gadget'])
                candidates=[]
                for obj in self._scan():
                    path=ctx._path(obj)
                    if path in job['before'] or path.startswith('/Script/'):continue
                    owner=ctx._path(getattr(obj,'owner',None));instigator=ctx._path(getattr(obj,'instigator',None))
                    if job.get('backend')=='dedicated_native':
                        from .native_grenade import PROJECTILE_CLASS_PATH
                        # Broad OakProjectile discovery includes unrelated projectiles.
                        # Require both our owner/instigator and the authored class.
                        # Definition/root readiness is checked before any launch write.
                        if owner!=job['machine'] or instigator!='None':continue
                        if ctx._path(obj.Class)!=PROJECTILE_CLASS_PATH:continue
                        candidates.append(path)
                    elif owner in allowed or instigator in allowed:candidates.append(path)
                job['projectiles']=sorted(set(job['projectiles'])|set(candidates))[:16]
            row['projectiles']=[]
            for path in job['projectiles']:
                obj=unrealsdk.find_object(self._projectile_class(),path)
                if obj is None:row['projectiles'].append({'path':path,'present':False});continue
                loc=ctx._call(obj,'K2_GetActorLocation')
                self.arm_native(obj,loc)
                self.launch(obj,loc)
                vel=ctx._call(obj,'GetVelocity')
                row['projectiles'].append({'path':path,'present':True,
                    'class':ctx._path(obj.Class),'body':ctx._path(getattr(obj,'BodyData',None)),
                    'position':[float(loc.X),float(loc.Y),float(loc.Z)],
                    'velocity':[float(vel.X),float(vel.Y),float(vel.Z)]})
        except Exception as error:row['error']=str(error)
        job['samples'].append(row)
        # Brief 100 Hz observation catches asynchronous native initialization.
        # After the one launch adjustment (or .5 s), return to 4 Hz.
        job['next_sample']=now+(.01 if job.get('launch_adjustment') is None and now-job['released_at']<.5 else .25)

    def tick(self,now):
        job=self.job;ctx=self.ctx
        if job is None:return
        if job['state']=='opening':
            if ctx._spin is None and ctx._tray_state:
                seq=ctx._animations['sequences']['FrontDroor_Open']
                duration=(len(seq['bone_frames'])-1)/seq['rate']
                fraction=min(1,(now-ctx._tray_state['started'])/duration)
                if ctx._tray_state['state']=='open' or (ctx._tray_state['state']=='opening' and fraction>=RELEASE_OPEN_FRACTION):
                    job['release_open_fraction']=fraction
                    self.release()
        elif job['state']=='observing':
            elapsed=now-job['released_at']
            if now>=job['next_sample']:self.sample(discover=not job['projectiles'] and elapsed<=.5)
            if elapsed>=4.5:
                job.update(state='test complete',note='Native request returned; visible spawn, fuse and damage need player confirmation')
                if job.get('backend')=='dedicated_native' and not job.get('projectiles'):
                    self.blocked='No owned native grenade was observed; send the report before another source test'
                    job['native_source']['observation']='No owned projectile observed; no automatic fallback or retry'
                elif job.get('backend')=='dedicated_native' and job.get('native_fuse',{}).get('state')!='started':
                    self.blocked='Dedicated source did not initialize and arm; send the report before another source test'
                    job['native_source']['observation']='Projectile discovered but native fuse not armed; no retry'
                job.pop('before',None)
                self.paid_active=None
                ctx._maybe_close_front_tray();ctx._save_report(force=bool(self.blocked))
                print('[Slots] '+(self.blocked if self.blocked else 'Grenade payout complete.'))

    def clear(self):
        self._refund_unattempted()
        self.paid_active=None
        if self.job and self.job['state'] not in ('test complete','failed'):
            self.job.update(state='cancelled',note='Pending test cancelled; any already spawned native grenade remains game-owned')
        self.job=None
