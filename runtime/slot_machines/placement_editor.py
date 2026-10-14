"""Live cabinet placement only; background workers never access Unreal objects."""
import json
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

VERSION = '0.141.0'
_io = ThreadPoolExecutor(max_workers=1, thread_name_prefix='BL4-slot-placement')


def saved_file():
    return Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'BL4SlotDiagnostics' / 'placements_saved.json'


def checked_pose(values):
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError('A position needs x, y, z and yaw')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('Position values must be finite numbers')
    if any(abs(v) > 10000000 for v in values[:3]):
        raise ValueError('Position is outside the supported world bounds')
    x, y, z, yaw = map(float, values)
    return (x, y, z, (yaw + 180) % 360 - 180)


def read_saved(entries):
    path = saved_file()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('machines'), dict):
        raise ValueError('Saved placement file has an unsupported format')
    known = {entry['id']: entry for entry in entries}
    result = {}
    for key, item in data['machines'].items():
        if key not in known:
            continue
        if not isinstance(item, dict) or item.get('world') != known[key]['world']:
            raise ValueError('Saved world does not match location ' + key)
        height = item.get('height')
        if type(height) not in (int, float) or not math.isfinite(height) or not 100 <= height <= 400:
            raise ValueError('Invalid saved cabinet height')
        result[key] = dict(world=item['world'], height=float(height), placement=list(checked_pose(item.get('placement'))))
    return result


def apply_saved(entries, values):
    for entry in entries:
        if entry['id'] in values:
            item = values[entry['id']]
            entry.update(candidates=[item['placement'][:]], height=item['height'], saved_position=True)


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(payload, encoding='utf-8')
    os.replace(temporary, path)


def read_request(path):
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise ValueError('Placement request exceeds 16 KB')
    return json.loads(path.read_text(encoding='utf-8'))


def service(ctx):
    current = getattr(ctx, '_placement_editor', None)
    if current is None:
        current = Editor(ctx)
        ctx._placement_editor = current
    return current


class Editor:
    def __init__(self, root):
        self.root = root
        self.session = None
        self.token = uuid.uuid4().hex
        self.target = None
        self.key = None
        self.original = None
        self.created = False
        self.waiting = False
        self.restart_ui = False
        self.was_enabled = None
        self.last_id = None
        self.reply = dict(state='ready', message='Placement editor ready')
        self.next_poll = 0
        self.next_status = 0
        self.read_future = None
        self.write_future = None
        self.channel = None
        config = root._ROOT / 'placement_control.json'
        if config.is_file():
            data = json.loads(config.read_text(encoding='utf-8'))
            if data.get('enabled') is True:
                self.channel = Path(data['directory'])
                if not self.channel.is_absolute():
                    raise ValueError('Placement channel must use an absolute directory')

    def placements(self):
        from .placements import service as placements_service
        return placements_service(self.root)

    def abandon(self):
        # Pure Python reset: never touch wrappers belonging to the old world.
        if self.was_enabled is not None:
            self.placements().enabled = self.was_enabled
        self.target = None
        self.key = None
        self.original = None
        self.created = False
        self.waiting = False
        self.restart_ui = False
        self.was_enabled = None
        self.session = None
        self.token = uuid.uuid4().hex
        self.reply = dict(state='ready', message='Previous edit discarded after world/player change')
        self.next_status = 0

    def pc(self, fleet):
        if not fleet.sync_session() or fleet.session != self.session:
            raise ValueError('Player/world changed; use the current session token')
        return self.root.get_pc(possibly_loading=True)

    def location(self, fleet, key=None):
        pc = self.pc(fleet)
        world = self.root._path(pc.Pawn).split(':', 1)[0]
        entries = [e for e in self.placements().entries if e['world'] == world]
        if not entries:
            raise ValueError('No configured cabinet locations in this world')
        p = self.root._call(pc.Pawn, 'K2_GetActorLocation')
        point = (float(p.X), float(p.Y), float(p.Z))
        if key is None:
            entry = min(entries, key=lambda e: math.dist(point, e['candidates'][0][:3]))
        else:
            entry = next((e for e in entries if e['id'] == key), None)
        if entry is None:
            raise ValueError('Location is unknown or belongs to another world')
        if math.dist(point, entry['candidates'][0][:3]) > 15000:
            raise ValueError('Go within 150 metres of the location first')
        return entry

    def pose(self, target):
        p = target._call(target._actor, 'K2_GetActorLocation')
        yaw = target._call(target._actor, 'K2_GetActorRotation').Yaw
        return checked_pose((float(p.X), float(p.Y), float(p.Z), float(yaw)))

    def idle(self, target):
        if getattr(target, '_building', False):
            raise ValueError('Wait for the cabinet to finish loading')
        from .verification import status
        state = status(target)
        if not state['ready']:
            raise ValueError('Finish the current spin/payout before editing: ' + ', '.join(state['blockers']))

    def pause(self):
        placed = self.placements()
        self.was_enabled = placed.enabled
        placed.enabled = False

    def resume(self):
        if self.was_enabled is not None:
            self.placements().enabled = self.was_enabled
        self.was_enabled = None

    def relocate_use_targets(self, target):
        ui = getattr(target, '_native_ui', None)
        if ui is not None and ui.enabled:
            try:
                position = ui.machine_position()
                for kind in tuple(ui.nodes):
                    ui.set_active(kind, False, position)
            except Exception as error:
                target._report['placement_ui_error'] = str(error)
                ui.clear()
                self.restart_ui = True

    def activate(self):
        target = self.target
        self.idle(target)
        target._editing = True
        target._call(target._actor, 'SetActorEnableCollision', bNewActorEnableCollision=False)
        self.relocate_use_targets(target)
        self.waiting = False

    def begin(self, fleet, key, preview=False):
        if self.target is not None:
            raise ValueError('Save or cancel the current edit first')
        if fleet.loading is not None:
            raise ValueError('A cabinet is loading; wait a moment and retry')
        entry = self.location(fleet, key)
        key = entry['id']
        current = self.placements().live.get(key)
        target = current['ctx'] if current is not None and current['ctx'] in fleet.instances and current['ctx']._actor is not None and current['ctx']._report.get('permanent_placement') == key else None
        if target is None:
            target = next((t for t in fleet.instances if t._actor is not None
                           and t._report.get('permanent_placement') == key), None)
        if target is None and not preview:
            raise ValueError('No machine is spawned here yet; use preview for this location')
        if target is not None:
            self.idle(target)
            self.original = self.pose(target)
            self.pause()
            self.target, self.key = target, key
            try:
                self.activate()
            except Exception:
                target._editing = False
                try:
                    target._call(target._actor, 'SetActorEnableCollision', bNewActorEnableCollision=True)
                finally:
                    self.target = None
                    self.key = None
                    self.original = None
                    self.resume()
                raise
            return
        pc = self.pc(fleet)
        p = self.root._call(pc.Pawn, 'K2_GetActorLocation')
        rotation = self.root._call(pc, 'GetControlRotation')
        yaw = float(rotation.Yaw)
        angle = math.radians(yaw)
        x, y = float(p.X) + 250 * math.cos(angle), float(p.Y) + 250 * math.sin(angle)
        from .placements import trace
        hit, floor = trace(self.root, pc, 'LineTraceSingle', Start=self.root._vec(x, y, float(p.Z)+50),
                           End=self.root._vec(x, y, float(p.Z)-600), TraceChannel=0, bTraceComplex=False,
                           ActorsToIgnore=[pc.Pawn], DrawDebugType=0, bIgnoreSelf=True)
        if not hit or float(floor.ImpactNormal.Z) < .7:
            raise ValueError('No usable floor 2.5 metres ahead; face a clear patch of floor')
        pose = checked_pose((x, y, float(floor.ImpactPoint.Z)+1, yaw+180))
        self.pause()
        try:
            target = fleet.start_loading(SimpleNamespace(height=entry['height'], yaw=0,
                                         placement=pose, placement_id=key))
            if target is None:
                raise ValueError('Player is not ready to create a preview')
            target._editing = True
            self.target, self.key = target, key
            self.original = pose
            self.created = True
            self.waiting = True
            self.placements().live[key] = dict(ctx=target, path=None)
        except Exception:
            self.resume()
            raise

    def ready_target(self, fleet):
        self.pc(fleet)
        if self.target is None:
            raise ValueError('Select or preview a cabinet first')
        if self.waiting or getattr(self.target, '_building', False):
            raise ValueError('Preview is still loading')
        if self.target._actor is None:
            raise ValueError('The selected cabinet no longer exists')
        self.idle(self.target)
        return self.target

    def apply(self, pose):
        target = self.target
        pose = checked_pose(pose)
        # Bound editing to the local room. No cross-map cabinet teleporting.
        if math.dist(pose[:3], self.original[:3]) > 3000:
            raise ValueError('Keep the cabinet within 30 metres of its starting position')
        moved = target._call(target._actor, 'K2_SetActorTransform', NewTransform=target._transform(*pose),
                            bSweep=False, bTeleport=True)
        if moved is False:
            raise ValueError('The engine rejected the cabinet transform')
        self.relocate_use_targets(target)
        target._report['placement_edit'] = dict(location=self.key, position=list(pose))

    def finish(self):
        target = self.target
        target._editing = False
        target._call(target._actor, 'SetActorEnableCollision', bNewActorEnableCollision=True)
        ui = getattr(target, '_native_ui', None)
        if ui is not None and self.restart_ui:
            try:ui.start()
            except Exception as error:target._report['placement_ui_restart_error']=str(error)
        elif ui is not None and ui.enabled:
            ui.tick(force=True)
        self.target = None
        self.key = None
        self.original = None
        self.created = False
        self.waiting = False
        self.restart_ui = False
        self.resume()

    def execute(self, fleet, request):
        if not isinstance(request, dict):
            raise ValueError('Placement request must be an object')
        op = request.get('op')
        if op not in ('status', 'select', 'preview', 'move', 'set', 'floor', 'save', 'cancel'):
            raise ValueError('Unknown placement operation')
        if request.get('session') != self.token:
            raise ValueError('Stale session; read status.json and use its current session token')
        self.pc(fleet)
        if op == 'status':
            return 'Placement channel is ready'
        if op in ('select', 'preview'):
            self.begin(fleet, request.get('location'), preview=op == 'preview')
            return 'Loading preview' if self.waiting else 'Editing ' + self.key
        if op == 'cancel':
            if self.target is None:
                return 'No edit is active'
            key = self.key
            if self.created:
                self.target._editing = False
                fleet.remove(self.target)
                self.placements().live.pop(key, None)
                self.target = None
                self.key = None
                self.original = None
                self.waiting = False
                self.created = False
                self.restart_ui = False
                self.resume()
            else:
                self.apply(self.original)
                self.finish()
            return 'Cancelled ' + key
        target = self.ready_target(fleet)
        pose = self.pose(target)
        if op == 'move':
            changes = [request.get(name, 0) for name in ('forward', 'right', 'up', 'yaw')]
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in changes):
                raise ValueError('Movement must use finite numeric values')
            forward, side, up, turn = changes
            if any(abs(v) > 500 for v in changes[:3]) or abs(turn) > 180:
                raise ValueError('Each move is limited to 500 cm and 180 degrees')
            angle = math.radians(pose[3])
            self.apply((pose[0]+forward*math.cos(angle)-side*math.sin(angle),
                        pose[1]+forward*math.sin(angle)+side*math.cos(angle), pose[2]+up, pose[3]+turn))
            return 'Moved ' + self.key
        if op == 'set':
            self.apply(request.get('position'))
            return 'Positioned ' + self.key
        if op == 'floor':
            from .placements import trace
            pc = self.pc(fleet)
            hit, floor = trace(target, pc, 'LineTraceSingle',
                               Start=target._vec(pose[0], pose[1], pose[2]+50),
                               End=target._vec(pose[0], pose[1], pose[2]-400),
                               TraceChannel=0, bTraceComplex=False,
                               ActorsToIgnore=[pc.Pawn, target._actor], DrawDebugType=0, bIgnoreSelf=True)
            if not hit or float(floor.ImpactNormal.Z) < .7:
                raise ValueError('No usable floor below the cabinet')
            self.apply((pose[0], pose[1], float(floor.ImpactPoint.Z)+1, pose[3]))
            return 'Set ' + self.key + ' on the floor'
        if op == 'save':
            entries = self.placements().entries
            values = read_saved(entries)
            entry = next(e for e in entries if e['id'] == self.key)
            key = self.key
            values[key] = dict(world=entry['world'], height=float(target._model_height), placement=list(pose))
            # This small user-requested config write is atomic and synchronous:
            # report success only after the complete save has reached the file.
            atomic_write(saved_file(), json.dumps(dict(schema=1, machines=values), indent=2))
            apply_saved(entries, values)
            target._report['permanent_placement'] = key
            self.placements().live[key] = dict(ctx=target, path=target._path(target._actor))
            self.finish()
            return 'Saved exact placement for ' + key

    def snapshot(self, fleet):
        value = dict(version=VERSION, session=self.token, world=None, player=None,
                     editing=self.key, loading=self.waiting, result=dict(self.reply),
                     saved_file=str(saved_file()), locations=[])
        pc = self.root.get_pc(possibly_loading=True)
        if pc is not None and pc.Pawn is not None and fleet.session == self.session:
            world = self.root._path(pc.Pawn).split(':', 1)[0]
            p = self.root._call(pc.Pawn, 'K2_GetActorLocation')
            r = self.root._call(pc, 'GetControlRotation')
            point = [float(p.X), float(p.Y), float(p.Z)]
            value.update(world=world, player=dict(position=point, yaw=float(r.Yaw), pitch=float(r.Pitch)))
            value['locations'] = [dict(id=e['id'], metres=round(math.dist(point, e['candidates'][0][:3])/100, 1),
                 saved=bool(e.get('saved_position'))) for e in self.placements().entries if e['world'] == world]
            value['locations'].sort(key=lambda e: e['metres'])
            if self.target is not None and self.target._actor is not None and not self.waiting:
                value['position'] = list(self.pose(self.target))
                value['height'] = self.target._model_height
        return value

    def console(self, fleet, op, **values):
        fleet.sync_session()
        if fleet.session != self.session:
            self.abandon()
            self.session = fleet.session
        try:
            message = self.execute(fleet, dict(op=op, session=self.token, **values))
            self.reply = dict(state='ok', message=message)
            print('[Slots edit] ' + message)
            if op == 'status':print(json.dumps(self.snapshot(fleet), indent=2))
        except Exception as error:
            self.reply = dict(state='error', message=str(error))
            print('[Slots edit] ' + str(error))
        self.next_status = 0

    def tick(self, fleet):
        now = time.monotonic()
        if fleet.session != self.session:
            self.abandon()
            self.session = fleet.session
        if self.waiting and fleet.loading is None:
            if self.target is None or self.target._actor is None:
                self.target = None
                self.key = None
                self.waiting = False
                self.created = False
                self.restart_ui = False
                self.resume()
                self.reply = dict(state='error', message='Preview could not finish loading')
            else:
                try:
                    self.activate()
                    self.reply.update(state='ok', message='Preview ready: ' + self.key)
                except Exception as error:
                    self.waiting = False
                    self.reply.update(state='error', message=str(error) + '; cancel this preview and retry')
            self.next_status = 0
        if self.channel is None:
            return
        if self.read_future is not None and self.read_future.done():
            future, self.read_future = self.read_future, None
            request = None
            try:
                request = future.result()
                if request is not None:
                    if not isinstance(request, dict) or not isinstance(request.get('id'), str) or not 1 <= len(request['id']) <= 128:
                        raise ValueError('Placement request needs a short unique string id')
                    if request['id'] != self.last_id:
                        self.last_id = request['id']  # Consume failures too; never repeat an edit.
                        message = self.execute(fleet, request)
                        self.reply = dict(id=request['id'], op=request.get('op'), state='ok', message=message)
                        self.next_status = 0
            except Exception as error:
                self.reply = dict(id=request.get('id') if isinstance(request, dict) else None,
                                  state='error', message=str(error))
                self.next_status = 0
        if now >= self.next_poll and self.read_future is None:
            self.next_poll = now + .25
            self.read_future = _io.submit(read_request, self.channel / 'request.json')
        if now >= self.next_status and (self.write_future is None or self.write_future.done()):
            try:
                if self.write_future is not None:
                    self.write_future.result()
                payload = json.dumps(self.snapshot(fleet), indent=2)
                self.write_future = _io.submit(atomic_write, self.channel / 'status.json', payload)
                self.next_status = now + (1 if self.target is not None else 3)
            except Exception as error:
                self.root._report['placement_channel_error'] = str(error)
                self.next_status = now + 5
