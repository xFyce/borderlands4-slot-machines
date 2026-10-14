"""Opt-in native dynamic GbxUseTrigger targets; never modifies authored definitions."""
import math
import unrealsdk
from unrealsdk.hooks import Block

FUNCTION='/Script/OakGame.OakPlayerController:ServerUseObject'

class NativeUsability:
    def __init__(self,ctx):
        self.ctx=ctx;self.nodes={};self.session=None;self.enabled=False
        self.hooked=False;self.next_tick=0;self.native_machine_use=False
    def report(self,**values):
        self.ctx._report.setdefault('native_usability',{}).update(values)
    def resolve(self,path):
        try:return unrealsdk.find_object('Actor',path)
        except Exception:return None
    def clear(self):
        self.enabled=False;self.native_machine_use=False
        if self.hooked:
            from .fleet import manager
            manager(self.ctx).detach_native(self);self.hooked=False
        for node in self.nodes.values():
            actor=self.resolve(node['path'])
            if actor is not None:
                try:self.ctx._call(actor,'K2_DestroyActor')
                except Exception:pass
        self.nodes={};self.session=None
        self.report(state='off; custom prompt restored')
    def configure(self,actor,kind):
        ctx=self.ctx
        from .native_grenade import initialize_null_handles
        schema=unrealsdk.find_object('ScriptStruct','/Script/GbxGame.RegisteredDynamicUsabilityInteractionInfo')
        info_schema=unrealsdk.find_object('ScriptStruct','/Script/GbxGame.DynamicUsabilityInteractionInfo')
        props={str(p.Name):p for p in info_schema._properties()}
        prop=props.get('Prompt')
        if prop is None or type(prop).__name__!='ZGameDataHandleProperty':
            raise RuntimeError('Dynamic prompt field is not a typed game-data handle')
        type_id=int(prop.TypeHandle)
        if type_id<=0:raise RuntimeError('Native prompt type ID unavailable')
        entry=unrealsdk.make_struct('RegisteredDynamicUsabilityInteractionInfo')
        trace={'typed_null_handles':[]}
        initialize_null_handles(entry,schema,trace)
        data=entry.UsabilityParams.data
        current=data.UseType
        members=getattr(type(current),'__members__',{})
        primary=[v for n,v in members.items() if n.lower() in ('primary','primaryuse')]
        if not primary:raise RuntimeError('Native primary use enum unavailable: '+str(list(members)))
        data.UseType=primary[0]
        data.bHoldToUse=False
        data.HoldToUseTime=0.0
        prompt='UsePrompt_Use' if kind=='machine' else 'UsePrompt_Take'
        data.Prompt=unrealsdk.unreal.FGameDataHandle(type_id,prompt)
        data.CostRequirements.bUseAttributeCost=False
        entry.priority=1
        actor.UsableActorState.ReplicatedDynamicInteractions=[entry]
        actor.UsableActorState.bInteractabilityLockedReplicated=False
        actor.bPrimaryUsabilityEnabled=False
        actor.bSecondaryUsabilityEnabled=False
        actor.bLoadReferencedNexusContent=False
        self.report(prompt_definitions={**ctx._report.get('native_usability',{}).get('prompt_definitions',{}),kind:prompt},prompt_type_id=type_id,dynamic_primary_use=str(primary[0]))
    def refresh(self,actor):
        # Public reflected replication refresh, only when its signature is empty.
        fn=getattr(actor,'OnRep_UsableActorState',None)
        if fn is None:
            self.report(refresh='No reflected callback; dynamic state populated');return
        params=list(fn.func._properties())
        if params:
            self.report(refresh='Callback has parameters; not invoked');return
        self.ctx._call(actor,'OnRep_UsableActorState')
        self.report(refresh='Native replicated usability state refreshed')
    def create(self,kind,position,radius):
        ctx=self.ctx;pc=ctx.get_pc(possibly_loading=True)
        gs=ctx._default('/Script/Engine.Default__GameplayStatics')
        transform=ctx._transform(*position)
        actor=ctx._call(gs,'BeginDeferredActorSpawnFromClass',WorldContextObject=pc,
                       ActorClass=unrealsdk.find_class('/Script/GbxGame.GbxUseTrigger'),
                       SpawnTransform=transform,CollisionHandlingOverride=1,Owner=pc,
                       TransformScaleMethod=0,optional=('TransformScaleMethod',))
        if actor is None:raise RuntimeError('Native usability actor spawn returned None')
        self.nodes[kind]={'path':ctx._path(actor),'active':None,'position':tuple(position)}
        self.configure(actor,kind)
        root=actor.RootComponent
        if root is None:raise RuntimeError('Native use trigger sphere root missing')
        ctx._call(root,'SetMobility',NewMobility=2)
        ctx._call(root,'SetSphereRadius',InSphereRadius=radius,bUpdateOverlaps=True)
        ctx._call(root,'SetHiddenInGame',NewHidden=True,bPropagateToChildren=True)
        # Query-only: native focus trace can hit it, but it has no physical body.
        ctx._call(root,'SetCollisionEnabled',NewType=1)
        ctx._call(root,'SetCollisionResponseToAllChannels',NewResponse=2)
        ctx._call(root,'SetCollisionResponseToChannel',Channel=2,NewResponse=1)
        ctx._call(root,'SetGenerateOverlapEvents',bInGenerateOverlapEvents=True)
        ctx._call(root,'SetCollisionResponseToChannel',Channel=4,NewResponse=0)
        ctx._call(actor,'SetActorEnableCollision',bNewActorEnableCollision=False)
        actor=ctx._call(gs,'FinishSpawningActor',Actor=actor,SpawnTransform=transform,
                        TransformScaleMethod=0,optional=('TransformScaleMethod',))
        if actor is None:raise RuntimeError('Native usability actor finish returned None')
        self.nodes[kind]['path']=ctx._path(actor)
        self.refresh(actor)
        ctx._call(actor,'SetReplicates',bInReplicates=False)
        self.nodes[kind]['active']=False
        return actor
    def machine_position(self):
        ctx=self.ctx;p=ctx._call(ctx._actor,'K2_GetActorLocation')
        yaw=float(ctx._call(ctx._actor,'K2_GetActorRotation').Yaw)
        scale=ctx._model_height/180;angle=math.radians(yaw)
        return (float(p.X)+25*scale*math.cos(angle),float(p.Y)+25*scale*math.sin(angle),float(p.Z)+85*scale)
    def start(self):
        self.clear();ctx=self.ctx;pc=ctx.get_pc(possibly_loading=True)
        if ctx._actor is None or pc is None or pc.Pawn is None:raise RuntimeError('Run slots_spawn in-game first')
        cls=unrealsdk.find_class('/Script/GbxGame.GbxUseTrigger')
        fields={str(p.Name):type(p).__name__ for p in cls._properties()}
        required={'UsableActorState','bPrimaryUsabilityEnabled','bSecondaryUsabilityEnabled','PrimaryUsability','bLoadReferencedNexusContent'}
        self.report(trigger_schema=fields)
        if not required.issubset(fields):raise RuntimeError('Native use trigger schema is incomplete')
        # The controller's reflected parameters were captured from the live game.
        fn=unrealsdk.find_object('Function',FUNCTION)
        params={str(p.Name):type(p).__name__ for p in fn._properties()}
        if params.get('Usable')!='ZObjectProperty':raise RuntimeError('Native use hook parameter schema changed')
        try:
            self.session=(ctx._path(ctx._actor),ctx._path(pc),ctx._path(pc.Pawn))
            pos=self.machine_position()
            self.create('machine',pos,70)
            self.create('cash',pos,12)
            from .fleet import manager
            manager(ctx).attach_native(self)
            self.hooked=True;self.enabled=True;self.next_tick=0
            self.report(state='native target prototype active',label='Native authored Use/Take prompts; custom price/amount labels pending',paths={k:n['path'] for k,n in self.nodes.items()})
            self.tick(force=True)
        except Exception:
            self.clear();raise
    def set_active(self,kind,active,position=None):
        node=self.nodes[kind];actor=self.resolve(node['path'])
        if actor is None:raise RuntimeError('Owned native use target no longer exists')
        ctx=self.ctx
        if position is not None and tuple(position)!=node['position']:
            moved=ctx._call(actor,'K2_SetActorLocation',NewLocation=ctx._vec(*position),bSweep=False,bTeleport=True)
            if moved is False:raise RuntimeError('Native cash target could not move to the stack')
            node['position']=tuple(position)
        if node['active']!=active:
            actor.bPrimaryUsabilityEnabled=bool(active)
            ctx._call(actor,'SetActorEnableCollision',bNewActorEnableCollision=bool(active))
            node['active']=bool(active)
            self.report(target_states={k:n['active'] for k,n in self.nodes.items()})
    def tick(self,force=False):
        if not self.enabled:return
        ctx=self.ctx;now=ctx._clock()
        if not force and now<self.next_tick:return
        self.next_tick=now+.1
        pc=ctx.get_pc(possibly_loading=True)
        current=(ctx._path(ctx._actor),ctx._path(pc),ctx._path(getattr(pc,'Pawn',None)))
        if current!=self.session:
            self.clear();self.report(state='cancelled after machine/player change');return
        try:
            if getattr(ctx,'_editing',False):
                self.set_active('cash',False)
                self.set_active('machine',False)
                return
            service=ctx._experience_service();menu=service.menu_open(pc)
            cash=getattr(ctx,'_cash_pickups',None)
            waiting=[r for r in cash.records if not r.get('collected')] if cash else []
            rewards=getattr(ctx,'_loot_rewards',None)
            gear=bool(rewards is not None and rewards.waiting())
            grenade=getattr(ctx,'_grenade_test',None)
            busy=ctx._spin is not None or gear or bool(grenade is not None and grenade.waiting())
            tray=getattr(ctx,'_tray_state',None)
            cash_ready=not menu and not busy and len(waiting)==1 and waiting[0].get('visible') and tray is not None and tray['state']=='open'
            pos=tuple(waiting[0]['position']) if cash_ready else None
            if pos is not None:pos=(pos[0],pos[1],pos[2]+2)
            self.set_active('cash',bool(cash_ready),pos)
            door=getattr(ctx,'_weapon_state',None)
            machine_ready=not menu and not busy and not waiting and (tray is None or tray['state']=='closed') and (door is None or door['state']=='closed')
            self.set_active('machine',bool(machine_ready))
        except Exception as error:
            self.clear();self.report(state='unavailable; custom prompt restored',error=str(error))
    def used(self,obj,args,ret,fn):
        # Never intercept a native weapon, vendor, NPC, or another player's use.
        if not self.enabled:return None
        try:
            path=self.ctx._path(args.Usable)
            match=next((k for k,n in self.nodes.items() if n['path']==path),None)
            if match is None or self.ctx._path(obj)!=self.session[1]:return None
            node=self.nodes[match]
            if not node['active']:return Block
            self.report(last_native_use=match)
            self.native_machine_use=match=='machine'
            try:self.ctx._interact_machine('native BL4 use target: '+match)
            finally:self.native_machine_use=False
            return Block
        except Exception as error:
            # Suppress our owned target only; no native effects or duplicate charge.
            self.report(use_error=str(error))
            return Block if 'match' in locals() and match is not None else None
