"""Independent cabinet runtimes; only the parent registers a mod and input bindings."""
import ast
import math
import sys
import types

from unrealsdk.hooks import Type, add_hook, remove_hook

FLUSH = '/Script/Engine.Actor:FlushNetDormancy'
FLUSH_HOOK = 'BL4Slots.FleetInteraction'
USE = '/Script/OakGame.OakPlayerController:ServerUseObject'
USE_HOOK = 'BL4Slots.FleetNativeUse'
EQUIP = '/Script/OakGame.OakCharacter:WeaponEquipped'
EQUIP_HOOK = 'BL4Slots.FleetPrizeEquipped'


def manager(ctx):
    fleet = getattr(ctx, '_fleet', None)
    if fleet is None:
        fleet = Fleet(ctx)
        ctx._fleet = fleet
    return fleet


class Fleet:
    def __init__(self, root):
        self.root = root
        self.instances = [root]
        self.serial = 1
        self.flush_registered = False
        self.experiences = []
        self.native_targets = []
        self.pickup_displays = []
        self.equip_registered = False
        self.use_registered = False
        self.template = None
        self.session = None
        self.actor_paths = set()
        from .streaming import Resources
        self.resources = Resources()
        self.loading = None
        self.next_interaction_check = 0
        self.retired_use_paths = set()

    def sync_session(self):
        # Only inspect the CURRENT controller and pawn supplied by mods_base.
        # Old actors/components are never dereferenced to check their validity.
        pc=self.root.get_pc(possibly_loading=True)
        pawn=getattr(pc,'Pawn',None) if pc is not None else None
        current=(self.root._path(pc),self.root._path(pawn)) if pawn is not None else None
        if self.session is not None and current!=self.session:
            self.abandon_world()
        self.session=current
        return current is not None

    def remember_actor(self,ctx):
        self.actor_paths.add(ctx._path(ctx._actor))

    def end_world(self,path):
        if path in self.actor_paths or self.session is not None and path in self.session:
            self.abandon_world();self.session=None

    def abandon_world(self):
        # Unregister Python dispatchers, then discard wrappers. Calling normal
        # remove/clear here can enter freed native memory; try/except cannot
        # protect against that access violation.
        # Keep only owned path STRINGS for deferred cleanup. A pawn reset in
        # the same persistent world can leave its separate Use actors alive.
        for ctx in tuple(self.instances):
            target=getattr(ctx,'_native_ui',None)
            if target is not None:
                self.retired_use_paths.update(node['path'] for node in target.nodes.values())
        self.next_interaction_check=0
        editor=getattr(self.root,'_placement_editor',None)
        if editor is not None:editor.abandon()
        placements=getattr(self.root,'_placements',None)
        if placements is not None:
            placements.cancel_search()
            # Streaming EndPlay can reset cabinets without replacing the pawn.
            # Force placement bookkeeping to retire its old actor paths too.
            placements.session=None
        if self.loading is not None:
            self.loading.cancel()
            self.loading.target._fleet_local=False
            self.loading=None
        from .streaming import Resources
        self.resources=Resources()
        for function,registered,kind,name in (
                (FLUSH,self.flush_registered,Type.PRE,FLUSH_HOOK),
                (USE,self.use_registered,Type.PRE,USE_HOOK),
                (EQUIP,self.equip_registered,Type.POST,EQUIP_HOOK)):
            if registered:remove_hook(function,kind,name)
        self.flush_registered=False;self.use_registered=False;self.equip_registered=False
        self.experiences.clear();self.native_targets.clear();self.pickup_displays.clear()
        for ctx in tuple(self.instances):
            ctx._abandon_world()
            if ctx is not self.root:sys.modules.pop(ctx.__name__,None)
        self.instances[:]=[self.root];self.actor_paths.clear()

    def prepare_template(self):
        if self.template is not None:return
        path=self.root._ROOT/'__init__.py'
        tree=ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
        class StripParser(ast.NodeTransformer):
            def visit_Expr(self,node):
                if isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute) and node.value.func.attr=='add_argument':
                    return ast.copy_location(ast.Pass(),node)
                return self.generic_visit(node)
        body=[]
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id in ('_mod','_native_trace') for target in node.targets):continue
            if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute) and node.value.func.attr=='add_argument':continue
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):node.decorator_list=[]
            body.append(StripParser().visit(node))
        tree.body=body
        self.template=compile(tree,str(path),'exec')

    def new_runtime(self):
        self.prepare_template()
        self.serial += 1
        name = self.root.__name__ + '._machine_' + str(self.serial)
        child = types.ModuleType(name)
        child.__file__ = self.root.__file__
        child.__package__ = self.root.__package__
        child._fleet_child = True
        child._instance_id = str(self.serial)
        child._fleet = self
        child._native_trace = self.root._native_trace
        sys.modules[name] = child
        try:
            exec(self.template, child.__dict__)
            child._native_trace = self.root._native_trace
            child._timer_hook_active = self.root._timer_hook_active
            self.instances.append(child)
            return child
        except Exception:
            sys.modules.pop(name, None)
            raise

    def start_loading(self,args):
        if self.loading is not None:return self.loading.target
        if not self.sync_session():return None
        from .streaming import LoadJob
        target=self.root if self.root._actor is None else self.new_runtime()
        target._fleet_local=True;target._building=True
        self.loading=LoadJob(target,target._spawn_machine_steps(args))
        return target

    def advance_loading(self):
        if not self.sync_session() or self.loading is None:return
        if any(ctx._spin is not None for ctx in self.instances):return
        job=self.loading
        try:
            done=job.advance()
            if self.loading is not job:return
            if not done:return
            self.loading=None
            job.target._building=False;job.target._fleet_local=False
            from .streaming import report
            error=job.target._report.get('error') if job.target._actor is None else None
            error=error or ('Machine creation returned no actor' if job.target._actor is None else None)
            report(self,job,error)
            placements=getattr(self.root,'_placements',None)
            if placements is not None:placements.loading_finished(job.target,error)
            if job.target._actor is None and job.target is not self.root:self.remove(job.target)
        except Exception as error:
            if self.loading is not job:return
            self.loading=None;job.cancel()
            job.target._building=False;job.target._fleet_local=False
            from .streaming import report
            report(self,job,error)
            placements=getattr(self.root,'_placements',None)
            if placements is not None:placements.loading_finished(job.target,error)
            self.remove(job.target)

    def spawn(self, args):
        editor=getattr(self.root,'_placement_editor',None)
        if editor is not None and editor.target is not None:
            print('[Slots] Save or cancel the placement edit before spawning another machine.')
            return editor.target
        if self.loading is not None:
            print('[Slots] A cabinet is still loading; please wait a moment.')
            return self.loading.target
        if not self.sync_session():
            print("[Slots Preview] Load into the game first, then run slots_spawn.")
            return self.root
        target = self.root if self.root._actor is None else self.new_runtime()
        try:
            target._fleet_local = True
            target._spawn_machine(args)
        except Exception:
            self.remove(target)
            raise
        finally:
            target._fleet_local = False
        if target._actor is None and target is not self.root:
            self.remove(target)
        count = sum(ctx._actor is not None for ctx in self.instances)
        print('[Slots] ' + str(count) + ' machine(s) placed. Existing machines keep their own spins and prizes.')
        return target

    def select(self):
        self.sync_session()
        pc = self.root.get_pc(possibly_loading=True)
        live = [ctx for ctx in self.instances if ctx._actor is not None and not getattr(ctx,'_building',False)]
        if not live or pc is None or pc.Pawn is None:
            return self.root
        pos = self.root._call(pc.Pawn, 'K2_GetActorLocation')
        try:
            view = self.root._call(pc, 'GetControlRotation')
        except Exception:
            view = self.root._call(pc.Pawn, 'K2_GetActorRotation')
        angle = math.radians(float(view.Yaw))
        def score(ctx):
            try:
                if ctx._cash_pickups is not None and ctx._cash_pickups.targeted() is not None:
                    return (-1, 0)
                point = ctx._call(ctx._actor, 'K2_GetActorLocation')
                dx, dy = float(point.X-pos.X), float(point.Y-pos.Y)
                distance = math.hypot(dx, dy)
                facing = distance < 30 or (dx*math.cos(angle)+dy*math.sin(angle))/distance > .65
                return (0 if facing and distance <= 225 else 1, distance)
            except Exception:
                return (2, float('inf'))
        return min(live, key=score)

    def attach_experience(self, experience):
        if not self.flush_registered:
            if add_hook(FLUSH, Type.PRE, FLUSH_HOOK, self.flush) is False:
                raise RuntimeError('Fleet interaction dispatcher rejected')
            self.flush_registered = True
        if experience not in self.experiences:
            self.experiences.append(experience)

    def detach_experience(self, experience):
        if experience in self.experiences:
            self.experiences.remove(experience)
        if self.flush_registered and not self.experiences:
            remove_hook(FLUSH, Type.PRE, FLUSH_HOOK)
            self.flush_registered = False

    def attach_native(self, target):
        if not self.use_registered:
            if add_hook(USE, Type.PRE, USE_HOOK, self.use) is False:
                raise RuntimeError('Shared native use dispatcher rejected')
            self.use_registered = True
        if target not in self.native_targets:
            self.native_targets.append(target)

    def detach_native(self, target):
        if target in self.native_targets:
            self.native_targets.remove(target)
        if self.use_registered and not self.native_targets and not self.pickup_displays:
            remove_hook(USE, Type.PRE, USE_HOOK)
            self.use_registered = False

    def attach_pickup(self, display):
        if not self.equip_registered:
            if add_hook(EQUIP, Type.POST, EQUIP_HOOK, self.equipped) is False:
                raise RuntimeError('Shared prize equip observer rejected')
            self.equip_registered = True
        if not self.use_registered:
            if add_hook(USE, Type.PRE, USE_HOOK, self.use) is False:
                raise RuntimeError('Shared pickup observer rejected')
            self.use_registered = True
        if display not in self.pickup_displays:self.pickup_displays.append(display)

    def detach_pickup(self, display):
        if display in self.pickup_displays:self.pickup_displays.remove(display)
        if self.equip_registered and not self.pickup_displays:
            remove_hook(EQUIP, Type.POST, EQUIP_HOOK);self.equip_registered=False
        if self.use_registered and not self.native_targets and not self.pickup_displays:
            remove_hook(USE, Type.PRE, USE_HOOK);self.use_registered=False

    def equipped(self, obj, args, ret, fn):
        if not self.sync_session():return None
        weapon=getattr(args,'Weapon',None)
        if weapon is not None:
            for display in tuple(self.pickup_displays):
                display.native_equipped(obj,weapon)
        return None

    def use(self, obj, args, ret, fn):
        if not self.sync_session():return None
        # Each trigger is owned by exactly one cabinet. Never consume a vendor,
        # NPC, weapon pickup, or another controller's native interaction.
        try:
            path = self.root._path(args.Usable)
        except Exception:
            return None
        for display in tuple(self.pickup_displays):
            display.native_use(obj,path)
        for target in tuple(self.native_targets):
            if target.enabled and any(node['path'] == path for node in target.nodes.values()):
                return target.used(obj, args, ret, fn)
        return None

    def flush(self, obj, args, ret, fn):
        if not self.sync_session():return None
        for experience in tuple(self.experiences):
            ctx = experience.ctx
            if getattr(ctx,'_building',False):continue
            pickups = ctx._cash_pickups
            if obj == ctx._actor or (pickups is not None and any(r['actor'] == obj for r in pickups.records)):
                return experience.tick(obj, args, ret, fn)
        return None

    def recover_interactions(self,now):
        # Runs from the controller's independent startup timer, so a lost
        # cabinet timer cannot also prevent its own repair. No actor scans.
        if now<self.next_interaction_check:return
        self.next_interaction_check=now+1
        self.cleanup_retired_interactions()
        for ctx in tuple(self.instances):
            if ctx._actor is None or getattr(ctx,'_building',False):continue
            # Service deadlines use Unreal's game clock; `now` here is the
            # startup dispatcher's monotonic clock and only throttles this loop.
            try:ctx._experience_service().recover(ctx._clock())
            except Exception as error:ctx._report['interaction_recovery_error']=str(error)

    def cleanup_retired_interactions(self):
        if not self.retired_use_paths:return
        pc=self.root.get_pc(possibly_loading=True)
        pawn=getattr(pc,'Pawn',None) if pc is not None else None
        if pawn is None:return
        world=self.root._path(pawn).split(':',1)[0]
        from .native_usability import OWNED_TAG
        import unrealsdk
        info=self.root._report.setdefault('retired_use_cleanup',{'removed':0})
        for path in tuple(self.retired_use_paths):
            # Another map's actors are gone; never look them up or dereference
            # retained wrappers. Only touch freshly resolved, tagged mod actors.
            if path.split(':',1)[0]!=world:
                self.retired_use_paths.discard(path);continue
            try:
                try:actor=unrealsdk.find_object('Actor',path)
                except ValueError:actor=None
                if actor is not None and OWNED_TAG in [str(t) for t in actor.Tags]:
                    actor.UsableActorState.ReplicatedDynamicInteractions=[]
                    actor.bPrimaryUsabilityEnabled=False
                    self.root._call(actor,'SetActorEnableCollision',bNewActorEnableCollision=False)
                    if self.root._call(actor,'K2_DestroyActor') is False:
                        raise RuntimeError('Retired interaction target could not be removed')
                    info['removed']+=1
                self.retired_use_paths.discard(path)
            except Exception as error:
                info['error']=str(error)
        info['pending']=len(self.retired_use_paths)

    def animate(self, obj, args, ret, fn):
        if not self.sync_session():return None
        for ctx in tuple(self.instances):
            if not getattr(ctx,'_building',False) and ctx._actor is not None and obj == ctx._actor:
                compass = getattr(self.root, '_compass', None)
                if compass is not None:
                    compass.tick(self)
                return ctx._animation_tick(obj, args, ret, fn)
        return None

    def remove(self, target):
        if self.loading is not None and self.loading.target is target:
            self.loading.cancel();self.loading=None
            target._building=False;target._fleet_local=False
        target._remove()
        if target is not self.root:
            if target in self.instances:
                self.instances.remove(target)
            sys.modules.pop(target.__name__, None)

    def clear(self):
        self.sync_session()
        for target in tuple(self.instances):
            self.remove(target)

    def describe(self):
        for target in self.instances:
            if target._actor is not None:
                print('[Slots] Machine ' + getattr(target, '_instance_id', '1') + ': ' + target._path(target._actor)
                      + (' [spinning]' if target._spin is not None else ''))
