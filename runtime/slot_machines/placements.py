"""Curated, streamed-in world placements shared by all cabinet editions."""
import json
import math
import time
from types import SimpleNamespace

import unrealsdk


def service(ctx):
    current = getattr(ctx, '_placements', None)
    if current is None:
        current = Placements(ctx)
        ctx._placements = current
    return current


def trace(ctx, pc, name, **values):
    # Keep the HitResult out parameter: the normal runtime call adapter returns
    # only the function's return value. Initialize every other field to zero.
    fn = getattr(ctx._default('/Script/Engine.Default__KismetSystemLibrary'), name)
    names = {str(p.Name).lower(): str(p.Name) for p in fn.func._properties()}
    params = unrealsdk.unreal.WrappedStruct(fn.func)
    for key, value in dict(WorldContextObject=pc, **values).items():
        setattr(params, names[key.lower()], value)
    result = fn(params)
    if not isinstance(result, tuple) or len(result) < 2:
        raise RuntimeError(name + ' did not return its hit result')
    # Const reference input arrays can also appear in the SDK's out tuple.
    # Find the reflected HitResult by its type instead of assuming index 1.
    for value in result[1:]:
        if type(value).__name__ != 'WrappedStruct':
            continue
        native_type = getattr(value, '_type', None)
        if str(getattr(native_type, 'Name', '')).lower() == 'hitresult':
            return bool(result[0]), value
    raise RuntimeError(name + ' returned no HitResult; outputs: ' +
        ', '.join(type(value).__name__ for value in result[1:]))


def fit(ctx, pc, candidate, height):
    x, y, z, yaw = candidate
    hit, floor = trace(ctx, pc, 'LineTraceSingle',
        Start=ctx._vec(x, y, z + 120), End=ctx._vec(x, y, z - 400),
        TraceChannel=0, bTraceComplex=False, ActorsToIgnore=[pc.Pawn],
        DrawDebugType=0, bIgnoreSelf=True)
    if not hit or float(floor.ImpactNormal.Z) < .85:
        return None
    z = float(floor.ImpactPoint.Z) + 1
    angle = math.radians(yaw)
    for forward, side in ((-55, -60), (-55, 60), (55, -60), (55, 60)):
        corner_x = x + forward * math.cos(angle) - side * math.sin(angle)
        corner_y = y + forward * math.sin(angle) + side * math.cos(angle)
        supported, corner = trace(ctx, pc, 'LineTraceSingle',
            Start=ctx._vec(corner_x, corner_y, z + 20),
            End=ctx._vec(corner_x, corner_y, z - 20),
            TraceChannel=0, bTraceComplex=False, ActorsToIgnore=[pc.Pawn],
            DrawDebugType=0, bIgnoreSelf=True)
        if not supported or abs(float(corner.ImpactPoint.Z) - (z - 1)) > 6:
            return None
    # Leave the bottom centimetres out of the clearance test so a flat floor
    # doesn't count as an obstruction. This queries existing scenery only;
    # the cabinet's collision setup is completely unchanged.
    centre = ctx._vec(x, y, z + height / 2 + 3)
    blocked, _ = trace(ctx, pc, 'BoxTraceSingle', Start=centre, End=centre,
        HalfSize=ctx._vec(height * .24, height * .26, height / 2 - 4),
        Orientation=unrealsdk.make_struct('Rotator', Pitch=0, Yaw=yaw, Roll=0),
        TraceChannel=0, bTraceComplex=False, ActorsToIgnore=[pc.Pawn],
        DrawDebugType=0, bIgnoreSelf=True)
    if blocked:
        return None
    # Space in front for the open cash tray and the person using it.
    front = ctx._vec(x + 150 * math.cos(angle), y + 150 * math.sin(angle), z + 90)
    blocked, _ = trace(ctx, pc, 'BoxTraceSingle', Start=front, End=front,
        HalfSize=ctx._vec(55, 65, 80),
        Orientation=unrealsdk.make_struct('Rotator', Pitch=0, Yaw=yaw, Roll=0),
        TraceChannel=0, bTraceComplex=False, ActorsToIgnore=[pc.Pawn],
        DrawDebugType=0, bIgnoreSelf=True)
    return None if blocked else (x, y, z, yaw)


class Placements:
    def __init__(self, ctx):
        self.ctx = ctx
        self.entries = json.loads((ctx._ROOT / 'placements.json').read_text(encoding='utf-8'))['machines']
        from .placement_editor import read_saved, apply_saved
        try:apply_saved(self.entries, read_saved(self.entries))
        except (OSError, ValueError, TypeError) as error:ctx._report['saved_placement_error']=str(error)
        self.live = {}
        self.old_paths = {}
        self.session = None
        self.next_check = 0
        self.retry_at = {}
        self.enabled = True
        self.search = None  # Compatibility with the staged startup scheduler.

    def cancel_search(self):
        self.search = None

    def loading_finished(self,target,error=None):
        for key,value in tuple(self.live.items()):
            if value['ctx'] is not target:continue
            if error:
                self.live.pop(key,None)
                self.retry_at[key]=time.monotonic()+30
                self.ctx._report.setdefault('permanent_placements',{})[key]=str(error)
            else:
                value['path']=self.ctx._path(target._actor)
                target._report['permanent_placement']=key
                self.ctx._report.setdefault('permanent_placements',{})[key]='Placed'

    def tick(self):
        ctx = self.ctx
        now = time.monotonic()
        if not self.enabled or now < self.next_check:
            return
        self.next_check = now + 1
        from .fleet import manager
        fleet = manager(ctx)
        if not fleet.sync_session():
            return
        if any(target._spin is not None for target in fleet.instances):
            return
        if fleet.session != self.session:
            # These are Python strings, never native wrappers from an old world.
            self.old_paths.update({key: path for key, value in self.live.items()
                if (path := value['path'] or value['ctx']._report.get('actor'))})
            self.live.clear()
            self.retry_at.clear()
            self.session = fleet.session
        pc = ctx.get_pc(possibly_loading=True)
        world = ctx._path(pc.Pawn).split(':', 1)[0]
        pos = ctx._call(pc.Pawn, 'K2_GetActorLocation')
        if fleet.loading is not None:return
        entries=sorted(self.entries,key=lambda e:math.dist((float(pos.X),float(pos.Y),float(pos.Z)),e['candidates'][0][:3]))
        for entry in entries:
            key = entry['id']
            if world != entry['world'] or now < self.retry_at.get(key, 0):
                continue
            current = self.live.get(key)
            if current is not None and current['ctx']._actor is not None:
                if current['path'] is None:
                    current['path']=ctx._path(current['ctx']._actor)
                    current['ctx']._actor.Tags=['BL4Slots.Placement.'+key]
                    current['ctx']._report['permanent_placement']=key
                    ctx._report.setdefault('permanent_placements',{})[key]='Placed'
                continue
            x, y, z, _ = entry['candidates'][0]
            if (float(pos.X)-x)**2 + (float(pos.Y)-y)**2 + (float(pos.Z)-z)**2 > 12000**2:
                continue
            try:
                # World-partition anchors live in generated streaming packages,
                # and editor prefab containers may not exist at runtime. Use
                # the current world's actual floor/clearance queries instead.
                # Respawning a pawn can leave the actor alive in the same world.
                # Reacquire it through the CURRENT object registry before cleanup;
                # never call an actor wrapper retained across a world transition.
                old_path = self.old_paths.pop(key, None)
                if old_path is not None:
                    try: old = unrealsdk.find_object('Actor', old_path)
                    except ValueError: old = None
                    if old is not None and 'BL4Slots.Placement.' + key in [str(t) for t in old.Tags]:
                        ctx._call(old, 'K2_DestroyActor')
                # Explicitly saved, visually checked positions are exact. They
                # must not pass through the automatic guesses/wall solver again.
                placement = tuple(entry['candidates'][0]) if entry.get('saved_position') else next(
                    (value for candidate in entry['candidates']
                     if (value := fit(ctx, pc, candidate, entry['height'])) is not None), None)
                if placement is None:
                    self.retry_at[key] = now + 2
                    ctx._report.setdefault('permanent_placements', {})[key] = 'Waiting for clear floor and space'
                    continue
                target = fleet.start_loading(SimpleNamespace(height=entry['height'], yaw=0, placement=placement, placement_id=key))
                if target is None:
                    self.retry_at[key]=now+2
                    continue
                self.live[key]={'ctx':target,'path':None}
                ctx._report.setdefault('permanent_placements',{})[key]='Loading in small frame steps'
            except Exception as error:
                self.retry_at[key] = now + 30
                ctx._report.setdefault('permanent_placements', {})[key] = str(error)
            # Only one asset-heavy creation attempt per check, outside a spin.
            return

    def status(self):
        print('[Slots] Permanent placements ' + ('on' if self.enabled else 'off') + '.')
        pc = self.ctx.get_pc(possibly_loading=True)
        if pc is None or pc.Pawn is None:
            print('[Slots] Waiting for a loaded player.')
            return
        world = self.ctx._path(pc.Pawn).split(':', 1)[0]
        pos = self.ctx._call(pc.Pawn, 'K2_GetActorLocation')
        nearby = sorted((math.dist((float(pos.X), float(pos.Y), float(pos.Z)), e['candidates'][0][:3]), e['id'])
            for e in self.entries if e['world'] == world)
        if nearby:
            distance, key = nearby[0]
            print('[Slots] Nearest location: ' + key + ', ' + str(round(distance / 100)) + ' metres away.')
        else:
            print('[Slots] No configured locations in the current world.')
        compass = self.ctx._report.get('compass', {})
        if compass.get('error'):
            print('[Slots] Compass: ' + compass['error'])
        elif 'registered' in compass:
            print('[Slots] Compass: ' + str(len(compass['registered'])) + ' nearby machine marker(s), ' + 'native compass display; 40 metre range; triple-reel icon.')
        if self.session is None:
            print('[Slots] Automatic placement has not checked the loaded world yet.')
        for key, value in self.live.items():
            if value['ctx']._actor is not None:
                print('[Slots] ' + key + ': placed')
        for key, note in self.ctx._report.get('permanent_placements', {}).items():
            if note != 'Placed':
                print('[Slots] ' + key + ': ' + note)
