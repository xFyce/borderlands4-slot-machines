"""Slot markers owned and laid out by BL4's native discovery/compass system."""
import json
import math
import os
import time
from pathlib import Path

import unrealsdk

RANGE_CM = 4000
LOCATION = 'DiscoveryLocation_LuckyCircuit_Slots'
LOCATION_PREFIX = LOCATION + '_'
MARKER_SLOTS = 64
LOCATION_CLASS = '/Script/OakGame.OakDiscoveryLocationTypeDef'
COMPONENT_CLASS = '/Script/OakGame.OakDiscoveryLocationComponent'
ICON = '/Game/uiresources/_shared/assets/ico_ui_art_discovery/ico_misc_slots777_icon.ico_misc_slots777_icon'
PAK_NAME = 'zLuckyCircuit_Compass_10000300_P.pak'


def service(ctx):
    if getattr(ctx, '_compass', None) is None:
        ctx._compass = Compass(ctx)
    return ctx._compass


class Compass:
    def __init__(self, ctx):
        self.ctx = ctx
        self.package = ctx._ROOT.parent.parent/'OakGame'/'Content'/'Paks'/PAK_NAME
        self.output = Path(os.environ.get('USERPROFILE', str(Path.home())))/'BL4SlotDiagnostics'/'slots_native_compass_v137.json'
        self.last_report = None
        self.abandon()

    def abandon(self):
        # World teardown must never touch actors/components from the previous world.
        self.session = None
        self.registered = set()
        self.entries = {}
        self.failures = {}
        self.identities = {}
        self.next_check = 0
        self.next_icon_check = 0
        self.icon_info = {'path': ICON, 'resolved': False}

    def library(self):
        return self.ctx._default('/Script/GbxGame.Default__GbxDiscoveryFunctionLibrary')

    def stop(self):
        for target, actor, component in tuple(self.entries.values()):
            try:
                target._call(self.library(), 'RemoveActorDiscoverability', InActor=actor)
            except Exception:
                pass
        self.abandon()

    def detach(self, target, actor):
        path = target._path(actor)
        if path in self.entries:
            target._call(self.library(), 'RemoveActorDiscoverability', InActor=actor)
            self.entries.pop(path, None)
        self.failures.pop(path, None)
        self.identities.pop(path, None)
        self.registered.discard(path)

    def register(self, target, actor):
        cls = unrealsdk.find_class(COMPONENT_CLASS)
        path = target._path(actor)
        used = set(self.identities.values())
        index = next((i for i in range(MARKER_SLOTS) if i not in used), None)
        if index is None:
            raise RuntimeError('All 64 nearby native compass identities are in use')
        name = LOCATION_PREFIX + f'{index:03d}'
        ref = unrealsdk.unreal.FGbxDefPtr(name, LOCATION_CLASS)
        # Use the same native constructor as v134, which produced real compass
        # markers in game. It initializes and resolves both location pointers.
        target._call(self.library(), 'MakeActorDiscoverable',
            InActor=actor, InLocationType=ref, InComponentType=cls)
        component = target._call(actor, 'GetComponentByClass', ComponentClass=cls)
        if component is None:
            target._call(self.library(), 'RemoveActorDiscoverability', InActor=actor)
            raise RuntimeError('Native discovery did not create its component')
        self.identities[path] = index
        self.entries[path] = (target, actor, component)
        self.registered.add(path)

    def load_icon(self):
        # A soft property getter only resolves an already loaded UObject. Load
        # the cooked package explicitly before native discovery consumes it.
        info = {'path': ICON, 'resolved': False}
        self.icon_info = info
        def find():
            try:
                return unrealsdk.find_object('Texture2D', ICON)
            except ValueError:
                return None
        try:
            texture = find()
            if texture is None:
                package = unrealsdk.load_package(ICON.rsplit('.', 1)[0])
                info['package_returned'] = package is not None
                texture = find()
            if texture is None:
                raise RuntimeError('777 Texture2D was not found after loading its package')
            info.update(resolved=True, object=self.ctx._path(texture))
        except Exception as error:
            info['error'] = str(error)

    def snapshot(self, path, metres):
        target, actor, component = self.entries[path]
        row = {'actor': path, 'metres': round(metres, 1),
               'component': target._path(component), 'state': 'native component created'}
        try:
            row['discovery_id'] = str(component.EditorGeneratedDiscoveryLocationGuidString)
        except Exception as error:
            row['discovery_id_error'] = str(error)
        row['definition_slot'] = self.identities.get(path)
        for field in ('BaseLocationType', 'ReplicatedLocationType'):
            try:
                ref = getattr(component, field)
                resolved = bool(ref._get_address())
                item = {'name': str(ref._name), 'resolved': resolved}
                if resolved:
                    item['tracks_live'] = bool(ref.bTrackLive)
                    item['dynamic'] = bool(ref.bIsDynamic)
                    item['no_metadata'] = bool(ref.bDoNotGenerateMetaData)
                    if field == 'BaseLocationType' and str(ref._name).lower().startswith(LOCATION_PREFIX.lower()):
                        row['state'] = 'slot definition resolved by native game code'
                    try:
                        item['presentations'] = []
                        for presentation in ref.DiscoveryStatePresentations:
                            pres = {'name':str(presentation._name),'resolved':bool(presentation._get_address())}
                            if pres['resolved']:
                                texture = presentation.DiscoveryIconTexture
                                pres['icon_texture'] = str(texture)
                                pres['icon_loaded'] = texture is not None
                                try:
                                    pres['icon_soft_path'] = unrealsdk.unreal.ZSoftObjectProperty._get_identifier_from(presentation, 'DiscoveryIconTexture')
                                except Exception:
                                    pass
                                pres['compass_state'] = str(presentation.CompassState)
                            item['presentations'].append(pres)
                    except Exception as error:
                        item['presentation_error'] = str(error)
                row[field] = item
            except Exception as error:
                row[field] = {'error': str(error)}
        return row

    def report(self, rows, error=None):
        report = {'version': '0.137.0', 'mode': 'native discovery component',
                  'range_metres': 40, 'icon': '777 triple reels',
                  'definition': LOCATION, 'package_present': self.package.is_file(),
                  'native_texture_containers_present': all(self.package.with_suffix(ext).is_file() for ext in ('.utoc','.ucas')),
                  'texture_load': dict(self.icon_info),
                  'registered': sorted(self.registered), 'machines': rows,
                  'visual_confirmation': 'requires in-game check'}
        if error:
            report['error'] = error
        self.ctx._report['compass'] = report
        from . import diagnostics
        if not diagnostics.enabled:return
        # Distances change constantly; save only registration/resolution/error changes.
        signature = json.dumps({**report, 'machines':[
            {k:v for k,v in r.items() if k != 'metres'} for r in rows]}, sort_keys=True)
        if signature == self.last_report:
            return
        try:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            from .report_io import submit
            submit(self.output, json.dumps(report, indent=2))
            self.last_report = signature
        except OSError as error:
            report['diagnostic_write_error'] = str(error)

    def tick(self, fleet):
        now = time.monotonic()
        if fleet.session != self.session:
            self.abandon()
            self.session = fleet.session
        if now < self.next_check:
            return
        self.next_check = now + 1
        if not self.package.is_file() or not all(self.package.with_suffix(ext).is_file() for ext in ('.utoc','.ucas')):
            self.report([], 'Native compass package missing; install all three v137 native package files and restart BL4')
            return
        ctx = self.ctx
        rows = []
        try:
            pc = ctx.get_pc(possibly_loading=True)
            if pc is None or pc.Pawn is None:
                return
            p = ctx._call(pc.Pawn, 'K2_GetActorLocation')
            player = (float(p.X), float(p.Y), float(p.Z))
            nearby = {}
            for target in tuple(fleet.instances):
                actor = target._actor
                if actor is None or getattr(target,'_building',False):
                    continue
                p = target._call(actor, 'K2_GetActorLocation')
                distance = math.dist(player, (float(p.X), float(p.Y), float(p.Z)))
                if distance <= RANGE_CM:
                    nearby[target._path(actor)] = (target, actor, distance/100)
            if nearby and now >= self.next_icon_check:
                self.load_icon()
                self.next_icon_check = now + 10
            for path, (target, actor, component) in tuple(self.entries.items()):
                if path not in nearby:
                    self.detach(target, actor)
            for path, (target, actor, metres) in nearby.items():
                if path not in self.entries and path not in self.failures:
                    try:
                        self.register(target, actor)
                    except Exception as error:
                        self.failures[path] = str(error)
                if path in self.entries:
                    rows.append(self.snapshot(path, metres))
                else:
                    rows.append({'actor':path, 'metres':round(metres,1), 'error':self.failures[path]})
            for path in tuple(self.failures):
                if path not in nearby:
                    self.failures.pop(path)
            self.report(rows)
        except Exception as error:
            self.report(rows, str(error))
            self.next_check = now + 10
