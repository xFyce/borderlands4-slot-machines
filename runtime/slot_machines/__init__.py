"""BL2 slot-machine preview with single-player cash spins for BL4."""
import json
import math
import os
import random
import copy
import sys
import importlib
import traceback
import time
from datetime import datetime
from pathlib import Path

from .crash_trace import CrashTrace
from . import outcomes, diagnostics
from .asset_cache import load_json
from . import asset_cache
from .streaming import resources as loading_resources, texture as shared_texture

import unrealsdk
from mods_base import Game, build_mod, command, get_pc, hook, keybind
from unrealsdk.hooks import Block, Type

_ROOT = Path(__file__).resolve().parent
_placements = None
_placement_editor = None
_editing = False
_compass = None
_fleet = globals().get('_fleet') if globals().get('_fleet_child',False) else None
_native_trace = CrashTrace(Path(os.environ.get('USERPROFILE',str(Path.home())))/'BL4SlotDiagnostics')
_building = False
_actor = None
_light = None
_component = None
_parts = {}
_part_poses = {}
_call_signatures = {}
_normal_sign = -1
_model_height = 300
_native_ui = None
_keepalive = []
_report = {}
_mesh_arrays = None
_animations = None
_spin = None
_reel_config = None
_settled_angles = {}
_texture_variant = "4x"
_timer_hook_active = False
_cash_pickups = None
_cash_mode = "pickup"
_tray_state = None
_weapon_state = None
_held_reel_poses = {}
_experience = None
_loot_rewards = None
_grenade_test = None
_cabinet_lights = None
_verification_index = 0
_report_pending = False
_report_quiet_since = None
_TIMER_FUNCTION = "/Script/Engine.Actor:ForceNetUpdate"
# This short interval is capped to one callback per engine frame below.
_FRAME_TIMER_INTERVAL = 0.001


def _path(obj):
    try:
        return obj._path_name()
    except Exception:
        return str(obj)


def _call(obj, name, optional=(), **values):
    """Build reflected params so newly added out/default params are zero-initialized."""
    _native_trace.record('lookup '+name)
    fn = getattr(obj, name)
    _native_trace.record('reflect '+name)
    signature_key=_path(fn.func)
    names=_call_signatures.get(signature_key)
    if names is None:
        names={str(p.Name).lower():str(p.Name) for p in fn.func._properties()}
        _call_signatures[signature_key]=names
        _report.setdefault('calls',{})[name]=list(names.values())
    params = unrealsdk.unreal.WrappedStruct(fn.func)
    for key, value in values.items():
        actual = names.get(key.lower())
        if actual is None:
            if key in optional:
                continue
            raise RuntimeError(f"{name}: missing parameter {key}; available: {list(names.values())}")
        setattr(params, actual, value)
    _native_trace.record('invoke '+name)
    measured=name in ('SpawnInventoryFromItemPool','BeginDeferredActorSpawnFromClass','FinishSpawningActor','AddComponentByClass','OpenFile')
    before_call=time.perf_counter() if measured else 0
    result = fn(params) if values else fn()
    if measured:
        elapsed_ms=(time.perf_counter()-before_call)*1000
        metric=_report.setdefault('performance',{}).setdefault(name,{'count':0,'total_ms':0,'max_ms':0})
        metric.update(count=metric['count']+1,total_ms=metric['total_ms']+elapsed_ms,
                      max_ms=max(metric['max_ms'],elapsed_ms),last_ms=elapsed_ms)
    _native_trace.record('returned '+name)
    return result[0] if isinstance(result, tuple) else result


def _vec(x=0, y=0, z=0):
    return unrealsdk.make_struct("Vector", X=x, Y=y, Z=z)


def _transform(x=0, y=0, z=0, yaw=0):
    angle = math.radians(yaw) / 2
    return unrealsdk.make_struct(
        "Transform", Translation=_vec(x, y, z), Scale3D=_vec(1, 1, 1),
        Rotation=unrealsdk.make_struct("Quat", X=0, Y=0, Z=math.sin(angle), W=math.cos(angle)),
    )


def _default(path):
    return unrealsdk.find_object("Object", path)


def _save_report(background=False, force=False):
    if not force and not diagnostics.enabled:return
    try:
        try:
            from .verification import status
            _report['payout_check']=status(sys.modules[__name__])
        except Exception as error:_report['payout_check_error']=str(error)
        folder = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "BL4SlotDiagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("slots_preview_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
        # Commands may be routed to a child machine; compass belongs to the fleet root.
        fleet = globals().get('_fleet')
        if fleet is not None:
            shared_compass = fleet.root._report.get('compass')
            if shared_compass is not None:
                _report['compass'] = dict(shared_compass)
        payload=json.dumps(_report, indent=2, default=str)
        if background:
            from .report_io import submit
            submit(path,payload)
        else:path.write_text(payload, encoding="utf-8")
        print(f"[Slots] Diagnostic: {path}")
    except Exception as error:
        print(f"[Slots] Could not save diagnostic: {error}")


def _request_report():
    global _report_pending, _report_quiet_since
    if not diagnostics.enabled:return
    _report_pending=True
    _report_quiet_since=None


def _flush_report_if_quiet(now):
    global _report_pending, _report_quiet_since
    if not diagnostics.enabled:
        _report_pending=False
        _report_quiet_since=None
        return
    if not _report_pending:return
    busy=(_spin is not None or
          _tray_state is not None and _tray_state['state'] in ('opening','closing') or
          _weapon_state is not None and _weapon_state['state'] in ('opening','closing') or
          _loot_rewards is not None and (_loot_rewards.pending is not None or _loot_rewards.burst.waiting()) or
          _grenade_test is not None and _grenade_test.waiting())
    if busy:
        _report_quiet_since=None
        return
    if _report_quiet_since is None:_report_quiet_since=now
    if now-_report_quiet_since<.5:return
    _report_pending=False
    _save_report(background=True)


def _abandon_world():
    """Forget an unloaded world without touching any of its native wrappers."""
    global _actor, _light, _component, _spin, _tray_state, _weapon_state
    global _experience, _native_ui, _loot_rewards, _grenade_test, _cabinet_lights
    global _cash_pickups, _report_pending, _report_quiet_since
    global _boot_pawn_path, _boot_ready_at, _building, _editing
    # Software audio owns OS handles/workers, which still need closing. Each
    # backend's abandon method explicitly avoids native Unreal calls.
    player=getattr(_experience,'player',None)
    if player is not None:
        try:player.abandon()
        except Exception as error:_report['audio_reset_note']=str(error)
    _building=False;_editing=False
    _actor=None;_light=None;_component=None;_spin=None
    _tray_state=None;_weapon_state=None
    _experience=None;_native_ui=None;_loot_rewards=None;_grenade_test=None
    _cabinet_lights=None;_cash_pickups=None
    _parts.clear();_part_poses.clear();_call_signatures.clear();_keepalive.clear()
    _settled_angles.clear();_held_reel_poses.clear()
    _report_pending=False;_report_quiet_since=None
    _boot_pawn_path=None;_boot_ready_at=0
    if _compass is not None:_compass.abandon()
    _report['world_reset']='Previous-world references discarded without native cleanup calls'


def _remove():
    global _actor, _light, _component, _tray_state, _weapon_state
    global _report_pending
    if _native_ui is not None:_native_ui.clear()
    _stop_spin(restore=False)
    if _grenade_test is not None:_grenade_test.clear()
    if _cabinet_lights is not None:_cabinet_lights.stop()
    _weapon_state = None
    if _loot_rewards is not None:_loot_rewards.clear()
    if _experience is not None:
        _experience.stop()
    if _cash_pickups is not None:
        _cash_pickups.clear()
    _light = None
    _component = None
    _parts.clear()
    _part_poses.clear()
    _call_signatures.clear()
    _settled_angles.clear()
    _held_reel_poses.clear()
    _tray_state = None
    if _actor is not None and _fleet is not None:
        compass = getattr(_fleet.root, '_compass', None)
        if compass is not None:
            try:compass.detach(sys.modules[__name__], _actor)
            except Exception as error:_report['compass_remove_error']=str(error)
    actor, _actor = _actor, None
    if actor is not None:
        try:
            _call(actor, "K2_DestroyActor")
        except Exception:
            # A previous map may already have destroyed the transient actor.
            pass
    _keepalive.clear()
    if _report_pending:
        _report_pending=False
        _save_report(background=True)


def _material_parameters(material):
    """Collect explicit overrides along the parent chain, nearest override first."""
    textures, scalars, vectors = {}, {}, {}
    current = material
    visited = set()
    for _ in range(12):
        if current is None or _path(current) in visited:
            break
        visited.add(_path(current))
        for attr, target in (("TextureParameterValues", textures),
                             ("ScalarParameterValues", scalars),
                             ("VectorParameterValues", vectors)):
            try:
                for value in getattr(current, attr):
                    target.setdefault(str(value.ParameterInfo.Name), value.ParameterValue)
            except Exception:
                pass
        try:
            current = current.Parent
        except Exception:
            break
    return textures, scalars, vectors


def _info_record(info):
    try:
        association = int(info.Association)
    except Exception:
        association = str(getattr(info, "Association", "unknown"))
    return {"name": str(info.Name), "association": association,
            "index": int(getattr(info, "Index", -1))}


def _parameter_infos(material, kind, name=None):
    current, visited, records = material, set(), {}
    for _ in range(12):
        if current is None or _path(current) in visited:
            break
        visited.add(_path(current))
        try:
            for param in getattr(current, kind + "ParameterValues"):
                info = param.ParameterInfo
                record = _info_record(info)
                if name is not None and record["name"] != name:
                    continue
                key = (record["name"], record["association"], record["index"])
                records.setdefault(key, (info, param.ParameterValue, _path(current)))
        except Exception:
            pass
        try:
            current = current.Parent
        except Exception:
            break
    return list(records.values())


def _set_parameter(dynamic, material, kind, name, value):
    """Target source layer/blend/global bindings, rather than inventing a global override."""
    infos = _parameter_infos(material, kind, name)
    method = "Set" + kind + "ParameterValueByInfo"
    bindings = []
    if infos and hasattr(dynamic, method):
        for info, previous, owner in infos:
            _call(dynamic, method, ParameterInfo=info, Value=value)
            record = _info_record(info)
            record.update({"method": method, "owner": owner})
            bindings.append(record)
    else:
        if any(_info_record(info)["association"] != 2 for info, previous, owner in infos):
            raise RuntimeError(f"{method} unavailable for a layer parameter: {name}")
        method = "Set" + kind + "ParameterValue"
        _call(dynamic, method, ParameterName=name, Value=value)
        bindings.append({"name": name, "method": method, "association": "global fallback"})
    _report.setdefault("parameter_writes", []).extend(bindings)
    return bindings


def _texture_metadata(texture):
    record = {"path": _path(texture)}
    for name in ("VirtualTextureStreaming", "bVirtualTextureStreaming", "SRGB", "NeverStream", "CompressionSettings"):
        try:
            record[name] = str(getattr(texture, name))
        except Exception:
            pass
    for axis in ("X", "Y"):
        try:
            record["size_" + axis.lower()] = int(_call(texture, "Blueprint_GetSize" + axis))
        except Exception:
            pass
    return record


def _neutral_surface(pc, rendering, dynamic, material):
    textures, scalars, vectors = _material_parameters(material)
    _report["source_texture_bindings"] = [
        {**_info_record(info), "owner": owner, "texture": _texture_metadata(value)}
        for info, value, owner in _parameter_infos(material, "Texture")
    ]
    _report["source_surface_parameters"] = {
        "textures": {name: _path(value) for name, value in textures.items()},
        "scalars": {name: str(value) for name, value in scalars.items()},
        "vectors": {name: str(value) for name, value in vectors.items()},
    }
    maps = {}
    changes = []
    errors = []
    def get_map(filename):
        if filename not in maps:
            # Prefer an existing cooked flat-normal texture if the game has one.
            result = None
            if filename == "Preview_FlatNormal.png":
                for candidate in unrealsdk.find_all("Texture2D"):
                    leaf = str(candidate.Name).lower()
                    if leaf in ("defaultnormal", "t_proxy_normal", "t_proxy_n", "t_proxy_flatnormal", "t_flatnormal"):
                        result = candidate
                        break
            if result is None:
                result = _call(rendering, "ImportFileAsTexture2D", WorldContextObject=pc,
                               Filename=str(_ROOT / filename))
                if result is None:
                    raise RuntimeError("Could not load " + filename)
                if filename != "Preview_White.png" and filename != "Preview_Black.png":
                    # Data maps use linear channels rather than diffuse sRGB sampling.
                    try:
                        result.SRGB = False
                    except Exception as error:
                        errors.append("Linear map flag: " + str(error))
            maps[filename] = result
            _keepalive.append(result)
        return maps[filename]
    for name in textures:
        key = name.lower().replace("_", "").replace(" ", "")
        filename = None
        if "normal" in key or key in ("nrm", "normalmap"):
            filename = "Preview_FlatNormal.png"
        elif any(word in key for word in ("aorm", "orm", "armmap", "occlusionroughnessmetal")):
            filename = "Preview_AORM.png"
        elif "metallic" in key or "metalness" in key:
            filename = "Preview_Black.png"
        elif "roughness" in key or "occlusion" in key or key in ("ao", "aomap"):
            filename = "Preview_White.png"
        elif "emissive" in key:
            filename = "Preview_Black.png"
        elif key in ("comp", "composite", "packedcomp"):
            # BL4's loaded neutral dielectric uses the game's own channel convention.
            try:
                neutral = _default("/Game/Common/Texture/Proxy/T_Proxy_Dielectric_C.T_Proxy_Dielectric_C")
                if neutral is not None:
                    _set_parameter(dynamic, material, "Texture", name, neutral)
                    changes.append({"parameter": name, "texture": _path(neutral)})
            except Exception as error:
                errors.append(name + ": " + str(error))
        if filename:
            try:
                _set_parameter(dynamic, material, "Texture", name, get_map(filename))
                changes.append({"parameter": name, "map": filename})
            except Exception as error:
                errors.append(name + ": " + str(error))
    for name in scalars:
        key = name.lower().replace("_", "").replace(" ", "")
        value = None
        if key in ("emissiveintensity", "emissivestrength", "flickerstrength"):
            value = 0.0
        elif key in ("metallic", "metalness", "metallicmultiplier", "metalnessmultiplier"):
            value = 0.0
        elif key in ("roughness", "roughnessmultiplier"):
            value = 0.85
        elif key in ("normalstrength", "normalintensity", "normalmultiplier"):
            value = 0.0
        if value is not None:
            try:
                _set_parameter(dynamic, material, "Scalar", name, value)
                changes.append({"parameter": name, "scalar": value})
            except Exception as error:
                errors.append(name + ": " + str(error))
    for name in vectors:
        if name.lower().replace("_", "").replace(" ", "") == "emissivecolor":
            try:
                _set_parameter(dynamic, material, "Vector", name,
                               unrealsdk.make_struct("LinearColor", R=0, G=0, B=0, A=1))
                changes.append({"parameter": name, "vector": "black"})
            except Exception as error:
                errors.append(name + ": " + str(error))
    _report["surface_changes"] = changes
    _report["surface_errors"] = errors


def _texture_material_steps(pc, component, selection=0, list_only=False):
    """Borrow a loaded material shader, editing only our own new instance."""
    candidates = []
    samples = []
    cache=loading_resources(sys.modules[__name__])
    cached=cache.body_material if selection==0 and not list_only else None
    if cached is not None:
        try:material=unrealsdk.find_object('MaterialInstanceConstant',cached[1])
        except ValueError:material=None
        if material is not None:candidates.append((*cached,cached[1]))
    # Only names survive a frame boundary; world streaming can collect borrowed
    # material objects between construction steps.
    material_paths=[] if candidates else [_path(m) for m in unrealsdk.find_all('MaterialInstanceConstant')]
    for material_path in material_paths:
        yield 'material selection' 
        try:
            material=unrealsdk.find_object('MaterialInstanceConstant',material_path)
            if material is None:continue
            path = _path(material)
            if "Default__" in path:
                continue
            texture_params, scalar_params, vector_params = _material_parameters(material)
            # Geometry/wear/colorization shaders depend on source-specific masks.
            if "gwc" in path.lower() or any("gwc" in key.lower() for key in texture_params):
                continue
            for name, source_texture in texture_params.items():
                texture_path = _path(source_texture)
                if len(samples) < 60:
                    samples.append({"material": path, "parameter": name, "texture": texture_path})
                text = name.lower().replace("_", "").replace(" ", "")
                if not any(word in text for word in ("basecolor", "diffuse", "albedo")):
                    continue
                lower = path.lower()
                score = sum(10 for word in ("prop", "metal") if word in lower)
                if "fireextinguisher" in lower:
                    score += 100
                elif "ricecooker" in lower:
                    score += 90
                if any(word in lower for word in ("/effects/", "/ai/", "skin", "hair", "eye", "water", "glass", "decal", "terrain", "landscape", "foliage", "emissive", "vending", "screen", "overlay", "uv1", "uv2", "grate", "coals", "windbob", "_anim")):
                    continue
                candidates.append((score, path, name, path))
        except Exception:
            continue
    _report["material_samples"] = samples
    _report["ranked_material_candidates"] = [
        {"source": path, "parameter": name, "score": score}
        for score, path, name, material in sorted(candidates, key=lambda x: (-x[0], x[1]))[:30]
    ]
    ranked = sorted(candidates, key=lambda x: (-x[0], x[1]))
    if list_only:
        for index, (score, path, name, material) in enumerate(ranked[:30]):
            print(f"[Slots] Material {index}: {path.rsplit('/', 1)[-1]}")
        if not ranked:
            print("[Slots] No compatible materials loaded.")
        return bool(ranked)
    if selection < 0 or selection >= len(ranked):
        if ranked:
            raise ValueError(f"Material index must be between 0 and {len(ranked)-1}")
    if not candidates:
        _report["texture_status"] = "No compatible loaded diffuse material; default gray preview."
        return False
    rendering = _default("/Script/Engine.Default__KismetRenderingLibrary")
    texture = shared_texture(sys.modules[__name__],pc,
                    {"4x": "SlotMachine_Dif_4x.png", "original": "SlotMachine_Dif.png"}[_texture_variant])
    if texture is None:
        raise RuntimeError("ImportFileAsTexture2D returned no texture")
    for score, path, name, material_path in (ranked[selection:] if selection==0 else ranked[selection:selection+1]):
        try:
            material=unrealsdk.find_object('MaterialInstanceConstant',material_path)
            if material is None:continue
            dynamic = _call(component, "CreateDynamicMaterialInstance", ElementIndex=0,
                            SourceMaterial=material, OptionalName="BL2SlotsPreview",
                            optional=("OptionalName",))
            if dynamic is None:
                continue
            _report["parameter_writes"] = []
            _set_parameter(dynamic, material, "Texture", name, texture)
            _keepalive.extend((texture, dynamic))
            if selection==0:cache.body_material=(score,path,name)
            yield 'body texture and material'
            _neutral_surface(pc, rendering, dynamic, material)
            for part in _parts.values():
                if part is not component:
                    _call(part, "SetMaterial", ElementIndex=0, Material=dynamic)
            _report["material"] = {"source": path, "diffuse_parameter": name, "score": score, "index": selection}
            try:
                _report["texture_variant"] = _texture_variant
                _report["imported_texture"] = _texture_metadata(texture)
                readbacks = []
                for info, previous, owner in _parameter_infos(material, "Texture", name):
                    effective = _call(dynamic, "K2_GetTextureParameterValueByInfo", ParameterInfo=info)
                    readbacks.append({**_info_record(info), "expected": _path(texture),
                                      "actual": _path(effective), "matches": effective == texture})
                _report["diffuse_layer_readbacks"] = readbacks
                effective = _call(dynamic, "K2_GetTextureParameterValue", ParameterName=name)
                _report["diffuse_readback"] = {"expected": _path(texture), "actual": _path(effective),
                                              "matches": effective == texture}
            except Exception as error:
                _report["diffuse_readback_error"] = str(error)
            _report["texture_status"] = "Original BL2 diffuse assigned to borrowed BL4 shader."
            return True
        except Exception as error:
            _report.setdefault("material_errors", []).append(str(error))
    return False


def _texture_material(pc,component,selection=0,list_only=False):
    steps=_texture_material_steps(pc,component,selection,list_only)
    while True:
        try:next(steps)
        except StopIteration as done:return done.value

def _build_mesh_steps(component,height):
    from .mesh_loading import build_steps
    yield from build_steps(sys.modules[__name__],component,height)

def _build_mesh(component,height):
    for _ in _build_mesh_steps(component,height):pass


@command("slots_spawn", description="Place a temporary BL2 slot-machine model in front of you.")
def slots_spawn(args):
    from .fleet import manager
    return manager(sys.modules[__name__]).spawn(args)


def _spawn_machine_steps(args):
    global _actor, _report, _component, _normal_sign, _model_height
    global _animations, _reel_config, _building
    if not math.isfinite(args.height) or not 100 <= args.height <= 400 or not math.isfinite(args.yaw):
        print("[Slots] Use a finite rotation and height between 100 and 400 cm.")
        return
    _report = {"version": "1.1.0", "stage": "player"}
    try:
        while not asset_cache.ready(_ROOT):yield 'waiting for authored data'
        pc = get_pc(possibly_loading=True)
        if pc is None or pc.Pawn is None:
            print("[Slots] Load into the game first, then run slots_spawn.")
            return
        pawn = pc.Pawn
        location = _call(pawn, "K2_GetActorLocation")
        rotation = _call(pawn, "K2_GetActorRotation")
        yaw = float(rotation.Yaw)
        height = 90.0
        try:
            height = float(_call(pawn.CapsuleComponent, "GetScaledCapsuleHalfHeight"))
        except Exception:
            pass
        radians = math.radians(yaw)
        transform = _transform(float(location.X) + 250 * math.cos(radians),
                               float(location.Y) + 250 * math.sin(radians),
                               float(location.Z) - height, yaw + 180 + args.yaw)
        fixed = getattr(args, 'placement', None)
        if fixed is not None:
            if len(fixed) != 4 or not all(math.isfinite(float(value)) for value in fixed):
                raise ValueError('Invalid permanent placement')
            transform = _transform(*fixed)
        _report["placement"] = {"pawn": _path(pawn), "yaw": yaw, "capsule_half_height": height, "model_height_cm": args.height,
                                "fixed_world_transform": list(fixed) if fixed is not None else None}
        _remove()
        _report["stage"] = "spawn actor"
        gameplay = _default("/Script/Engine.Default__GameplayStatics")
        _actor = _call(gameplay, "BeginDeferredActorSpawnFromClass", WorldContextObject=pc,
                       ActorClass=unrealsdk.find_class("/Script/Engine.Actor"),
                       SpawnTransform=transform, CollisionHandlingOverride=1, Owner=None,
                       TransformScaleMethod=0, optional=("TransformScaleMethod",))
        if _actor is None:
            raise RuntimeError("Deferred actor spawn returned None")
        finished = _call(gameplay, "FinishSpawningActor", Actor=_actor, SpawnTransform=transform,
                         TransformScaleMethod=0, optional=("TransformScaleMethod",))
        if finished is None:
            raise RuntimeError("FinishSpawningActor returned None")
        _actor = finished
        from .fleet import manager
        manager(sys.modules[__name__]).remember_actor(sys.modules[__name__])
        _report["actor"] = _path(_actor)
        placement_id=getattr(args,'placement_id',None)
        if placement_id:_actor.Tags=['BL4Slots.Placement.'+placement_id]
        try:
            _call(_actor, "SetReplicates", bInReplicates=False)
        except Exception as error:
            _report["replication_disable_note"] = str(error)
        _building=True
        _call(_actor,'SetActorHiddenInGame',bNewHidden=True)
        _call(_actor,'SetActorEnableCollision',bNewActorEnableCollision=False)
        yield 'hidden actor'
        _report["stage"] = "add procedural component"
        component = _call(_actor, "AddComponentByClass",
                          Class=unrealsdk.find_class("/Script/ProceduralMeshComponent.ProceduralMeshComponent"),
                          bManualAttachment=False, RelativeTransform=_transform(), bDeferredFinish=False)
        if component is None:
            raise RuntimeError("AddComponentByClass returned None")
        _keepalive.append(component)
        _component = component
        _normal_sign = -1
        _model_height = args.height
        _report["stage"] = "build mesh"
        yield from _build_mesh_steps(component, args.height)
        from . import cabinet_collision
        cabinet_collision.setup(sys.modules[__name__])
        yield 'cabinet collision'
        _report["stage"] = "place mesh"
        _call(_actor, "K2_SetActorTransform", NewTransform=transform, bSweep=False, bTeleport=True)
        _report["stage"] = "texture"
        textured = False
        try:
            textured = yield from _texture_material_steps(pc, component)
        except Exception:
            _report["texture_error"] = traceback.format_exc()
        from . import tray_backing
        tray_backing.setup(sys.modules[__name__])
        _report["stage"] = "complete"
        yield from _setup_cabinet_lights_steps(pc)
        try:
            if _animations is None:
                _animations=load_json(_ROOT/'animations.json.gz')
            if _reel_config is None:
                _reel_config=load_json(_ROOT/'reels.json')
            _experience_service().setup()
            yield 'interaction HUD'
            _pickup_service().prepare(persistent=True)
            yield 'cash display setup'
            # Warm assets at machine creation, rather than the first winning spin.
            try:
                _pickup_service().prewarm(pc)
            except Exception as error:_report['cash_asset_warmup_note']=str(error)
            yield 'cash asset warmup'
            _report['cash_timer'] = 'shared interaction dispatcher'
            _loot_service().catalog()
            slots_native_hud(type('NativeArgs',(),{'mode':'on'})())
        except Exception as error:
            _report['experience_setup_note'] = str(error)
        _call(_actor,'SetActorEnableCollision',bNewActorEnableCollision=True)
        _call(_actor,'SetActorHiddenInGame',bNewHidden=False)
        _building=False
        print("[Slots] Machine placed" + (" at its permanent location. " if fixed is not None else " 2.5 metres ahead. ") +
              ("BL2 diffuse assigned." if textured else "Default material; send the diagnostic for texture work."))
        print("[Slots] Use E to play or slots_test to list paid test spins. slots_remove removes the temporary cabinet.")
    except Exception:
        _report["error"] = traceback.format_exc()
        print(f"[Slots] Failed at {_report['stage']}: {_report['error'].splitlines()[-1]}")
        _remove()
    finally:
        _building=False


def _spawn_machine(args):
    try:
        for _ in _spawn_machine_steps(args):pass
    finally:_save_report()


slots_spawn.add_argument("yaw", nargs="?", type=float, default=0,
                         help="Extra model rotation in degrees, e.g. slots_spawn 90.")
slots_spawn.add_argument("height", nargs="?", type=float, default=300,
                         help="Machine height in cm, e.g. slots_spawn 0 260.")


@command("slots_remove", description="Remove the temporary slot-machine preview.")
def slots_remove(args):
    from .fleet import manager
    fleet=manager(sys.modules[__name__])
    editor=getattr(fleet.root,'_placement_editor',None)
    if editor is not None and editor.target is not None:
        print('[Slots] Save or cancel the placement edit before removing machines.');return
    if args.all:fleet.clear()
    else:fleet.remove(fleet.select())
    print('[Slots] Machine(s) removed.')


slots_remove.add_argument('--all',action='store_true',help='Remove every placed machine.')


@command('slots_placements', description='Show or pause automatic permanent world placements.')
def slots_placements(args):
    from .placements import service
    placed = service(sys.modules[__name__])
    if args.mode in ('on', 'off'):
        placed.enabled = args.mode == 'on'
    placed.status()


slots_placements.add_argument('mode', nargs='?', choices=('on', 'off', 'status'), default='status')


@command('slots_edit', description='Select/preview a cabinet, save its exact placement or cancel editing.')
def slots_edit(args):
    from .fleet import manager
    from .placement_editor import service
    owner=manager(sys.modules[__name__])
    service(owner.root).console(owner,args.mode,location=args.location)

slots_edit.add_argument('mode',nargs='?',default='status',choices=('status','select','preview','floor','save','cancel'))
slots_edit.add_argument('location',nargs='?',default=None)


@command('slots_move', description='Move the edited cabinet: forward/right/up centimetres, then yaw degrees.')
def slots_move(args):
    from .fleet import manager
    from .placement_editor import service
    owner=manager(sys.modules[__name__])
    service(owner.root).console(owner,'move',forward=args.forward,right=args.right,up=args.up,yaw=args.yaw)

slots_move.add_argument('forward',nargs='?',type=float,default=0)
slots_move.add_argument('right',nargs='?',type=float,default=0)
slots_move.add_argument('up',nargs='?',type=float,default=0)
slots_move.add_argument('yaw',nargs='?',type=float,default=0)


@command('slots_machines',description='List all placed independent slot machines.')
def slots_machines(args):
    from .fleet import manager
    manager(sys.modules[__name__]).describe()


def _clock():
    return float(_call(_default("/Script/Engine.Default__KismetSystemLibrary"),
                       "GetGameTimeInSeconds", WorldContextObject=_actor))


def _frame_pair(sequence, index):
    frames = sequence["bone_frames"]
    looping = sequence is _animations["sequences"]["Slots_SpinLoop"]
    count = sequence.get("playback_frames", len(frames)) if looping else len(frames)
    position = max(0.0, float(index))
    if looping:
        position %= count
    else:
        position = min(position, count-1)
    first = min(int(position), count-1)
    second = (first+1) % count if looping else min(first+1, count-1)
    return first, second, position-first


def _interpolate_bone(a, b, alpha):
    translation = [a[i] + (b[i]-a[i])*alpha for i in range(3)]
    sign = -1 if sum(a[i]*b[i] for i in range(3, 7)) < 0 else 1
    q = [a[i]*(1-alpha) + b[i]*sign*alpha for i in range(3, 7)]
    length = math.sqrt(sum(x*x for x in q))
    return translation + [x/length for x in q]


def _apply_animation_frame(sequence=None, index=0, bone_overrides=None):
    if _component is None or _mesh_arrays is None:
        raise RuntimeError("No spawned animation mesh")
    if _weapon_state is not None:
        bone_overrides = dict(bone_overrides or {})
        bone_overrides[6] = _weapon_state['bone_pose']
    pair = _frame_pair(sequence, index) if sequence is not None else None
    for bone, part in _parts.items():
        if bone <= 0:
            continue
        value = [0, 0, 0, 0, 0, 0, 1] if pair is None else _interpolate_bone(
            sequence["bone_frames"][pair[0]][bone], sequence["bone_frames"][pair[1]][bone], pair[2])
        if bone_overrides and bone in bone_overrides:
            value = bone_overrides[bone]
        pose_key=tuple(value)
        if _part_poses.get(bone)==pose_key:continue
        scale = _model_height / 180
        transform = unrealsdk.make_struct("Transform", Translation=_vec(*(x*scale for x in value[:3])),
            Scale3D=_vec(1, 1, 1), Rotation=unrealsdk.make_struct("Quat", X=value[3], Y=value[4], Z=value[5], W=value[6]))
        _call(part, "K2_SetRelativeTransform", NewTransform=transform, bSweep=False, bTeleport=True)
        _part_poses[bone]=pose_key
    # Only the twelve triangles crossing bone boundaries need vertex deformation.
    if -1 in _parts:
        arrays = _mesh_arrays["parts"][-1]
        vertices, normals, tangents = arrays["vertices"].copy(), arrays["normals"].copy(), arrays["tangents"].copy()
        if pair is not None:
            lookup = {w: j for j, w in enumerate(sequence["indices"])}
            for j, wedge in enumerate(arrays["indices"]):
                if wedge not in lookup:
                    continue
                a = sequence["frames"][pair[0]][lookup[wedge]]
                b = sequence["frames"][pair[1]][lookup[wedge]]
                value = [x+(y-x)*pair[2] for x,y in zip(a,b)]
                vertices[j] = _vec(*(x*_model_height/180 for x in value[:3]))
                normal = value[3:6]; tangent = value[6:9]
                normal_length = math.sqrt(sum(x*x for x in normal)) or 1
                tangent_length = math.sqrt(sum(x*x for x in tangent)) or 1
                normals[j] = _vec(*(x*_normal_sign/normal_length for x in normal))
                flip = _mesh_arrays["geometry"]["tangents"][wedge][3]
                tangents[j] = unrealsdk.make_struct("ProcMeshTangent", TangentX=_vec(*(x/tangent_length for x in tangent)),
                    bFlipTangentY=(not flip) if _normal_sign < 0 else flip)
        if bone_overrides:
            geometry = _mesh_arrays["geometry"]
            for j, wedge in enumerate(arrays["indices"]):
                bone = geometry["bone_for_vertex"][wedge]
                if bone not in bone_overrides:
                    continue
                value = bone_overrides[bone]
                position = _rotate_vector(value[3:], geometry["vertices"][wedge])
                vertices[j] = _vec(*((position[i]+value[i])*_model_height/180 for i in range(3)))
                normal = _rotate_vector(value[3:], geometry["normals"][wedge])
                tangent = _rotate_vector(value[3:], geometry["tangents"][wedge][:3])
                normals[j] = _vec(*(x*_normal_sign for x in normal))
                flip = geometry["tangents"][wedge][3]
                tangents[j] = unrealsdk.make_struct("ProcMeshTangent", TangentX=_vec(*tangent),
                    bFlipTangentY=(not flip) if _normal_sign < 0 else flip)
        _call(_parts[-1], "UpdateMeshSection_LinearColor", SectionIndex=0,
              Vertices=vertices, Normals=normals, UV0=arrays["uv"], UV1=[], UV2=[], UV3=[],
              VertexColors=arrays["colors"], Tangents=tangents, bSRGBConversion=False,
              optional=("UV1", "UV2", "UV3", "bSRGBConversion"))


def _apply_payout_part(bone,pose):
    # The imported mesh's only cross-bone seams belong to LeverArm/ArmDangle
    # (bones 1/2). Door 6 and tray 5 are independent rigid sections.
    part=_parts.get(bone)
    if part is None:raise RuntimeError('Missing payout mesh part '+str(bone))
    pose_key=tuple(pose)
    if _part_poses.get(bone)==pose_key:return
    scale=_model_height/180
    transform=unrealsdk.make_struct('Transform',Translation=_vec(*(v*scale for v in pose[:3])),
        Scale3D=_vec(1,1,1),Rotation=unrealsdk.make_struct('Quat',X=pose[3],Y=pose[4],Z=pose[5],W=pose[6]))
    _call(part,'K2_SetRelativeTransform',NewTransform=transform,bSweep=False,bTeleport=True)
    _part_poses[bone]=pose_key


def _rotate_vector(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = 2*(y*vz-z*vy), 2*(z*vx-x*vz), 2*(x*vy-y*vx)
    return [vx+w*tx+y*tz-z*ty, vy+w*ty+z*tx-x*tz, vz+w*tz+x*ty-y*tx]


def _quat_multiply(a, b):
    x,y,z,w = a; X,Y,Z,W = b
    return [w*X+x*W+y*Z-z*Y, w*Y-x*Z+y*W+z*X,
            w*Z+x*Y-y*X+z*W, w*W-x*X-y*Y-z*Z]


def _offset_pose(base, angle, reel):
    axis = _reel_config["axis"]
    q = [x*math.sin(angle/2) for x in axis] + [math.cos(angle/2)]
    pivot = reel["pivot"]
    rotated = _rotate_vector(q, [base[i]-pivot[i] for i in range(3)])
    return [rotated[i]+pivot[i] for i in range(3)] + _quat_multiply(q, base[3:])


def _make_stop_plans(choices):
    plans = []
    minimum = math.tau
    speed = _reel_config["angular_speed"]
    for reel, choice in zip(_reel_config["reels"], choices):
        target = reel["stops"][choice]
        # The verified loop enters the brake half a revolution from bind/rest.
        travel = (target["angle"] + math.pi) % math.tau
        while travel < minimum:
            travel += math.tau
        duration = 2*travel/speed
        plans.append(dict(reel=reel, target=target, travel=travel, duration=duration))
        minimum = travel + math.pi
    return plans


def _reel_overrides(name, frame):
    overrides = {}
    if _spin is None:
        return overrides
    if name == "Slots_SpinEnd":
        seconds = frame / _animations["sequences"][name]["rate"]
        for plan in _spin["stop_plans"]:
            u = min(1.0, max(0.0, seconds/plan["duration"]))
            angle = plan["travel"] * (2*u-u*u)
            reel = plan["reel"]
            overrides[reel["bone"]] = _offset_pose(reel["start_pose"], angle, reel)
    else:
        sequence = _animations["sequences"][name]
        first, second, alpha = _frame_pair(sequence, frame)
        fade = 1.0
        if name == "Slots_SpinLoop":
            # Use total loop time, not wrapped frame index, for a one-time fade.
            elapsed = max(0.0, _clock()-_spin["started"]-_spin["pull_length"])
            fade = (1+math.cos(math.pi*min(elapsed,1.0)))/2
        for reel in _reel_config["reels"]:
            bone = reel["bone"]
            angle = _spin["previous_angles"].get(bone,0)*fade
            if angle:
                base = _interpolate_bone(sequence["bone_frames"][first][bone], sequence["bone_frames"][second][bone], alpha)
                overrides[bone] = _offset_pose(base, angle, reel)
    if name == 'Pull' and _spin.get('close_tray'):
        close = _animations['sequences']['FrontDroor_Close']
        elapsed = max(0.0, _clock()-_spin['started'])
        a,b,t = _frame_pair(close,elapsed*close['rate'])
        overrides[5] = _interpolate_bone(close['bone_frames'][a][5],close['bone_frames'][b][5],t)
        if elapsed*close['rate'] >= len(close['bone_frames'])-1:
            overrides[5] = [0,0,0,0,0,0,1]
            _spin['close_tray'] = False
            if _tray_state is not None:
                _tray_state['state'] = 'closed'
    return overrides


def _stop_spin(restore=True):
    global _spin, _timer_hook_active
    active, _spin = _spin, None
    if _grenade_test is not None and _grenade_test.job and _grenade_test.job["state"]=="armed":_grenade_test.clear()
    if active is not None:
        try:
            _refund_cash_spin(active)
        except Exception:
            _report["cash_refund_error"] = traceback.format_exc()
            print("[Slots] Cash refund failed; send the newest diagnostic.")
            _save_report()
    if active is not None and _actor is not None:
        try:
            _call(_default("/Script/Engine.Default__KismetSystemLibrary"), "K2_ClearTimer",
                  Object=_actor, FunctionName="ForceNetUpdate")
        except Exception as error:
            _report["animation_clear_error"] = str(error)
    if restore:
        try:
            player=getattr(_experience,'player',None)
            if player:player.stop()
        except Exception:pass
        _settled_angles.clear()
        if active is not None and _component is not None:
            _apply_animation_frame()


def _spin_pose(elapsed):
    sequences = _animations["sequences"]
    pull, loop, end = (sequences[name] for name in ("Pull", "Slots_SpinLoop", "Slots_SpinEnd"))
    pull_tail = pull.get("last_interval_frames", 1.0)
    pull_tail_start = len(pull["frames"])-2
    pull_length = (pull_tail_start + pull_tail) / pull["rate"]
    loop_length = 2.0
    end_length = max(p["duration"] for p in _spin["stop_plans"]) if _spin and "stop_plans" in _spin else (len(end["frames"])-1) / end["rate"]
    if elapsed < pull_length:
        position = elapsed * pull["rate"]
        if position > pull_tail_start:
            position = pull_tail_start + (position-pull_tail_start) / pull_tail
        return "Pull", min(position, len(pull["frames"])-1)
    if elapsed < pull_length + loop_length:
        return "Slots_SpinLoop", ((elapsed-pull_length) * loop["rate"]) % loop.get("playback_frames", len(loop["frames"]))
    if elapsed < pull_length + loop_length + end_length:
        return "Slots_SpinEnd", (elapsed-pull_length-loop_length) * end["rate"]
    return None


def _animation_tick(obj, args, ret, func):
    if _actor is None or _spin is None:
        return None
    if obj != _actor and _path(obj) != _path(_actor):
        return None
    try:
        elapsed = max(0.0, _clock() - _spin["started"])
        pose = _spin_pose(elapsed)
        stats = _report["animation"]
        stats["ticks"] += 1
        stats["elapsed"] = elapsed
        if elapsed > 0:
            stats["average_updates_per_second"] = (stats["ticks"]-1) / elapsed
        if pose is None:
            plans = _spin["stop_plans"]
            _apply_animation_frame(_animations["sequences"]["Slots_SpinEnd"],
                                   max(p["duration"] for p in plans)*_animations["sequences"]["Slots_SpinEnd"]["rate"],
                                   _reel_overrides("Slots_SpinEnd", max(p["duration"] for p in plans)*_animations["sequences"]["Slots_SpinEnd"]["rate"]))
            _settled_angles.update({p["reel"]["bone"]:p["target"]["angle"] for p in plans})
            _held_reel_poses.clear()
            _held_reel_poses.update(_reel_overrides("Slots_SpinEnd", max(p["duration"] for p in plans)*_animations["sequences"]["Slots_SpinEnd"]["rate"]))
            settlement_started=time.perf_counter()
            receipt = _settle_cash_spin(_spin)
            _report.setdefault('performance',{})['last_settlement_ms']=(time.perf_counter()-settlement_started)*1000
            _report["cash_result"] = receipt
            _stop_spin(restore=False)
            stats["state"] = "complete"
            print("[Slots] Result: " + " | ".join(stats["symbols"]) +
                  (f". Wager {receipt['wager']:,}; payout {receipt['payout']:,}; net {receipt['net']:+,} Cash."
                   if receipt["state"]=="settled" else ". Free test; no cash changes."))
            _request_report()
        elif pose != _spin.get("last_pose"):
            name, frame = pose
            _apply_animation_frame(_animations["sequences"][name], frame, _reel_overrides(name, frame))
            _spin["last_pose"] = pose
            stats.update(sequence=name, frame=frame)
    except Exception:
        _report["animation_error"] = traceback.format_exc()
        _report.setdefault("animation", {})["state"] = "failed"
        try:
            _stop_spin(restore=True)
        except Exception:
            pass
        print("[Slots] Animation failed: " + _report["animation_error"].splitlines()[-1])
        _save_report()
    return Block


def _near_machine():
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Pawn is None or _actor is None:
        return False
    # Native use event already identifies our owned target and controller.
    # Both action checks share this scope; keep their existing spin watchdog.
    if _native_ui is not None and _native_ui.enabled and _native_ui.native_machine_use:
        return True
    pos = _call(pc.Pawn, "K2_GetActorLocation")
    machine = _call(_actor, "K2_GetActorLocation")
    dx, dy = float(machine.X-pos.X), float(machine.Y-pos.Y)
    distance = math.hypot(dx, dy)
    if distance > 225 or abs(float(machine.Z-pos.Z)) > 260:
        return False
    try:
        view = _call(pc, "GetControlRotation")
    except Exception:
        view = _call(pc.Pawn, "K2_GetActorRotation")
    yaw = math.radians(float(view.Yaw))
    return distance < 30 or (math.cos(yaw)*dx + math.sin(yaw)*dy) / distance > 0.65


def _cash_entry(pc):
    if pc is None:
        raise RuntimeError("No current player controller")
    manager = pc.CurrencyManager
    entries = manager.currencies
    matches = [(i, entry) for i,entry in enumerate(entries) if str(entry.type.Name).lower()=="cash"]
    if len(matches)!=1:
        raise RuntimeError("Expected exactly one Cash entry in this player's currency manager")
    index, entry = matches[0]
    amount = int(entry.Amount)
    if not 0 <= amount <= 2**64-1:
        raise RuntimeError("Cash balance outside valid range")
    return manager, entries, index, entry, amount


def _outcome(active):
    return outcomes.classify([p['target']['symbol'] for p in active['stop_plans']])


def _change_cash(pc, delta, stage):
    if not _call(pc,"HasAuthority"):
        raise RuntimeError("Cash spins currently require single-player or host authority")
    manager,entries,index,entry,before = _cash_entry(pc)
    after = before + delta
    if not 0 <= after <= 2**64-1:
        raise RuntimeError("Insufficient Cash or currency range exceeded")
    old_currencies = [copy.copy(value) for value in entries]
    if delta:
        original = copy.copy(entry)
        updated = copy.copy(entry)
        updated.Amount = after
        entries[index] = updated
        if int(manager.currencies[index].Amount)!=after:
            entries[index] = original
            raise RuntimeError("Cash write readback failed; original entry restored")
    receipt = {"stage":stage,"delta":delta,"before":before,"after":after,"currency":"Cash"}
    _report["wallet"] = {"balance":after,"currency":"Cash","wager_policy":"character-level curve",
                         "authority":True,"stage":stage}
    if delta:
        try:
            _call(manager,"OnRep_Currencies",OldCurrencies=old_currencies)
            receipt["ui_notification"] = "called"
        except Exception as error:
            receipt["ui_notification_note"] = str(error)
    return receipt


def _charge_cash_spin(active):
    wager = active.get("wager",0)
    if not wager or active.get("charge") is not None:
        return
    pc = get_pc(possibly_loading=True)
    if pc is None or pc!=active["player"]:
        raise RuntimeError("Player changed before the wager")
    active["charge"] = _change_cash(pc,-wager,"wager charged")
    _report["cash_result"] = {"state":"charged","wager":wager,"charge":active["charge"]}


def _refund_cash_spin(active):
    if not active.get("charge") or active.get("cash_receipt") is not None:
        return
    refund = _change_cash(active["player"],active["wager"],"cancelled spin refund")
    active["cash_receipt"] = {"state":"refunded","wager":active["wager"],"refund":refund}
    _report["cash_result"] = active["cash_receipt"]
    print(f"[Slots] Cancelled spin: refunded {active['wager']:,} Cash.")
    _save_report()


def _settle_cash_spin(active):
    wager = active.get("wager",0)
    if wager==0:
        if active.get('cash_receipt') is not None:return active['cash_receipt']
        if active.get('grenade_test'):_grenade_service().open()
        active['cash_receipt']={"state":"free", "wager":0, "payout":0,
                               "grenade_test":bool(active.get('grenade_test'))}
        return active['cash_receipt']
    if active.get("cash_receipt") is not None:
        return active["cash_receipt"]
    pc = get_pc(possibly_loading=True)
    if pc is None or pc!=active["player"]:
        raise RuntimeError("Player changed during the spin")
    if active.get("charge") is None:
        raise RuntimeError("Spin has no verified wager charge")
    symbols = [p["target"]["symbol"] for p in active["stop_plans"]]
    outcome = _outcome(active)
    multiplier,match = outcome['cash_multiplier'],outcome['name']
    # Excluded from random paid rolls. Protect wagers if an old/diagnostic
    # spin ever reaches an unsupported result after a file update.
    if outcome['kind']=='reserved':
        _refund_cash_spin(active)
        active['cash_receipt'].update(outcome=outcome,payout=0,net=0,
                                     note='Unsupported native reward; wager refunded')
        return active['cash_receipt']
    if outcome['kind']=='grenade':
        try:_grenade_service().open()
        except Exception:
            _grenade_service().clear()
            _refund_cash_spin(active)
            active['cash_receipt'].update(outcome=outcome,payout=0,net=0,note='Grenade could not be opened; wager refunded before native spawn')
            return active['cash_receipt']
    gear = _loot_service().reward(active) if outcome['kind'] in ('weapon','jackpot','eridium') else {'state':'no gear match'}
    if outcome['kind'] in ('weapon','jackpot','eridium') and gear['state'] in ('disabled','unavailable'):
        _refund_cash_spin(active)
        active['cash_receipt'].update(outcome=outcome,gear=gear,payout=0,net=0,
                                     note='Weapon could not be queued; wager refunded')
        return active['cash_receipt']
    if 'cash_award' not in active:
        active['cash_award'] = outcomes.cash_award(match,wager)
    award = active['cash_award']
    payout = award['payout']
    multiplier = award['percent']/100
    credit = None
    if _cash_mode == "wallet" or not payout:
        credit = _change_cash(pc,payout,"spin payout")
    receipt = {"state":"settled","wager":wager,"payout":payout,"net":payout-wager,
               "multiplier":multiplier,"match":match,"outcome":outcome,"before":active["charge"]["before"],
               "after":credit["after"] if credit else _cash_entry(pc)[4],
               "charge":active["charge"],"credit":credit,"level_sync":active.get("level_sync"),
               "collection":"collected" if credit else "waiting",
               "currency":"Cash","backend":"local CurrencyManager Cash entry"}
    if payout and _cash_mode == "pickup":
        _pickup_service().spawn(pc,payout,receipt)
        print(f"[Slots] {payout:,} Cash won. Tray opening; " +
              ('walk close to collect.' if _experience_service().settings['auto_cash'] else 'aim at the cash stack and press E to collect.'))
    active["cash_receipt"] = receipt
    if outcome['name'] in ('legendary','jackpot','pearl'):
        _experience_service().sound('jackpot')
    if outcome['kind']=='grenade':
        receipt['grenade']=_grenade_service().job
        _grenade_service().paid_active=active
    receipt['gear'] = gear
    if receipt['gear']['state']=='native spawn queued':
        _begin_gear_payout()
        print('[Slots] Native prize: '+receipt['gear']['rarity']+'; '+str(receipt['gear'].get('item_count',1))+' item(s). Collect from payout opening.')
    elif receipt['gear']['state']=='unavailable':
        print('[Slots] Gear prize unavailable; check slots_status.')
    return receipt


def _pickup_service():
    global _cash_pickups
    if _cash_pickups is None:
        from .cash_pickups import CashPickups
        _cash_pickups = CashPickups(sys.modules[__name__], shared=True)
    return _cash_pickups


def _begin_gear_payout():
    global _animations
    job=_loot_service().pending
    if job is None:return
    if _animations is None:
        _animations=load_json(_ROOT/'animations.json.gz')
    if job['route']=='front':
        if _tray_state is None or _tray_state['state']=='closed':_begin_payout_tray()
    else:
        _begin_weapon_door()
        _weapon_state['reward_cycle']=True


def _finish_gear_payout(route):
    if route=='front':_maybe_close_front_tray()
    # Side door checks the live pickup state on the next frame.


def _maybe_close_front_tray():
    if _grenade_test is not None and _grenade_test.waiting():return
    if _loot_rewards is not None and _loot_rewards.waiting('front'):return
    if _cash_pickups is not None and any(not r.get('collected') for r in _cash_pickups.records):return
    if _tray_state is not None and _tray_state['state'] in ('open','opening'):
        _begin_payout_tray_close()


def _begin_weapon_door():
    global _weapon_state, _animations
    if _animations is None:
        _animations=load_json(_ROOT/'animations.json.gz')
    _weapon_state={'started':_clock(),'state':'opening','bone_pose':[0,0,0,0,0,0,1],'last_frame':None}
    _report['weapon_door']=_weapon_state
    _experience_service().sound('weapon_open')


def _advance_weapon_door():
    global _weapon_state
    if _weapon_state is None or _spin is not None:return
    state=_weapon_state
    if state['state']=='open':
        if state.get('reward_cycle'):
            if _loot_rewards is not None and _loot_rewards.waiting('side'):return
        elif _clock()-state['opened_at']<5:return
        state.update(state='closing',started=_clock(),last_frame=None)
        _experience_service().sound('weapon_close')
    closing=state['state']=='closing'
    seq=_animations['sequences']['SideDoor_Close' if closing else 'SideDoor_Open']
    frame=min(max(0,_clock()-state['started'])*seq['rate'],len(seq['bone_frames'])-1)
    if frame!=state['last_frame']:
        a,b,t=_frame_pair(seq,frame)
        state['open_fraction']=min(1.0,frame/max(1,len(seq['bone_frames'])-1))
        state['bone_pose']=_interpolate_bone(seq['bone_frames'][a][6],seq['bone_frames'][b][6],t)
        if closing and frame>=len(seq['bone_frames'])-1:state['bone_pose']=[0,0,0,0,0,0,1]
        _apply_payout_part(6,state['bone_pose'])
        state['last_frame']=frame
    if not closing and _loot_rewards is not None and _loot_rewards.pending is not None and _loot_rewards.pending['route']=='side':
        result=_loot_rewards.release_pending()
        if result and result['state']=='unavailable':print('[Slots] Weapon payout unavailable; check slots_status. Native request will not be retried.')
        _request_report()
    if frame>=len(seq['bone_frames'])-1:
        state.update(state='closed' if closing else 'open',opened_at=_clock())
        if closing:_weapon_state=None


def _begin_payout_tray(sound='win'):
    global _tray_state
    _tray_state = {'started':_clock(),'state':'opening','last_frame':None,'bone_pose':[0,0,0,0,0,0,1]}
    _report['tray'] = _tray_state
    _experience_service().sound(sound)


def _begin_payout_tray_close():
    global _tray_state
    _tray_state = {'started':_clock(),'state':'closing','last_frame':None}
    _report['tray'] = _tray_state
    _experience_service().sound('drawer_close')


def _advance_payout_tray():
    if _tray_state is None or _spin is not None:
        return False
    if _tray_state['state'] in ('open','closed'):
        return True
    closing = _tray_state['state'] == 'closing'
    seq = _animations['sequences']['FrontDroor_Close' if closing else 'FrontDroor_Open']
    frame = min(max(0.0,_clock()-_tray_state['started'])*seq['rate'],len(seq['bone_frames'])-1)
    if frame != _tray_state['last_frame']:
        a,b,t = _frame_pair(seq,frame)
        pose=_interpolate_bone(seq['bone_frames'][a][5],seq['bone_frames'][b][5],t)
        if closing and frame>=len(seq['bone_frames'])-1:pose=[0,0,0,0,0,0,1]
        _tray_state['open_fraction']=min(1.0,frame/max(1,len(seq['bone_frames'])-1))
        _tray_state['bone_pose']=list(pose)
        _apply_payout_part(5,pose)
        _tray_state['last_frame'] = frame
    if frame >= len(seq['bone_frames'])-1:
        _tray_state['state'] = 'closed' if closing else 'open'
        _tray_state['opened_at'] = _clock()
        return True
    return False


def _collect_cash_nearby():
    if _cash_pickups is None or not _cash_pickups.records:
        print("[Slots] No cash winnings waiting.")
        return
    if all(r.get('collected') for r in _cash_pickups.records):
        print('[Slots] Wait for the tray to finish closing.');return
    try:
        collected = False
        record = _cash_pickups.targeted()
        if record is not None:
            collected = _cash_pickups.collect(record)
        if not collected:
            print("[Slots] Wait for the tray to open, then aim at the cash stack and press E (or F9).")
    except Exception:
        _report["cash_pickup_error"] = traceback.format_exc()
        print("[Slots] Cash collection failed; send newest JSON.")
    _save_report()

@keybind("Collect slot winnings", "F9", description="Aim at the money stack in the open payout tray and press F9 to collect it.")
def collect_slot_winnings():
    from .fleet import manager
    manager(sys.modules[__name__]).select()._collect_cash_nearby()


def _experience_service():
    global _experience
    if _experience is None:
        from .experience import Experience
        _experience = Experience(sys.modules[__name__])
    return _experience


def _loot_service():
    global _loot_rewards
    if _loot_rewards is None:
        from .loot_rewards import LootRewards
        _loot_rewards = LootRewards(sys.modules[__name__])
    return _loot_rewards


@keybind('Play slot machine','E',description='Face the slot machine and press E to pay the displayed price. Collect waiting cash first.')
def interact_slot_machine():
    from .fleet import manager
    ctx=manager(sys.modules[__name__]).select()
    if ctx._native_ui is not None and ctx._native_ui.enabled:return None
    return ctx._interact_machine('SDK keybind')


def _interact_machine(source):
    if _editing:return
    service=_experience_service()
    now=_clock()
    info=_report.setdefault('interaction_input',{})
    info.update(source=source,attempts=info.get('attempts',0)+1,key=str(getattr(interact_slot_machine,'key','E')))
    if hasattr(service,'menu_open') and service.menu_open(get_pc(possibly_loading=True)):
        info['state']='menu or console open';return None
    if _cash_pickups and any(not r.get('collected') for r in _cash_pickups.records):
        target=_cash_pickups.targeted()
        if target is None:
            info['state']='cash waiting; crosshair is not on cash';return None
        if now-getattr(service,'last_use',-100)<.35:return Block
        service.last_use=now
        info['state']='cash stack targeted'
        _collect_cash_nearby()
        return Block
    if not _near_machine():
        info['state']='outside interaction range or view';return None
    if _loot_rewards is not None and _loot_rewards.waiting():
        # Let BL4 receive its normal pickup key; never consume E as a new spin.
        info['state']='native weapon collection'
        if _cash_pickups and _cash_pickups.records and now-getattr(service,'last_use',-100)>=.35:
            service.last_use=now
            _collect_cash_nearby()
        return None
    if now-getattr(service,'last_use',-100)<.35:return Block
    service.last_use=now
    info['state']='accepted near machine'
    if _cash_pickups and _cash_pickups.records:
        _collect_cash_nearby()
    else:
        _start_spin(require_near=True,wager=None)
    return Block






@command('slots_auto_cash',description='Enable normal proximity cash collection, or use E/F9 manually.')
def slots_auto_cash(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_auto_cash(args)
    service=_experience_service();service.settings['auto_cash']=args.mode=='on';service.save()
    print('[Slots] Automatic cash collection: '+args.mode)


slots_auto_cash.add_argument('mode',choices=('on','off'))


@command('slots_sounds',description='Enable/disable original BL2 audio once installed.')
def slots_sounds(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_sounds(args)
    service=_experience_service();service.settings['sounds']=args.mode=='on';service.save()
    if args.mode=='on':service.sound('lever')
    else:
        try:
            if service.player:service.player.stop()
        except Exception:pass
    print('[Slots] Prepared slot-machine audio not installed yet; silent.' if args.mode=='on' and not any((_ROOT/'audio'/name).is_file() for name in ('ASSET_SOURCE.txt','BL2_ORIGINAL.txt')) else '[Slots] Sounds: '+args.mode)


slots_sounds.add_argument('mode',choices=('on','off'))


def slots_native_hud(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_native_hud(args)
    global _native_ui
    try:
        if args.mode=='off':
            if _native_ui is not None:_native_ui.clear()
            print('[Slots] Custom HUD restored.')
        else:
            from .native_usability import NativeUsability
            if _native_ui is not None:_native_ui.clear()
            _native_ui=NativeUsability(sys.modules[__name__]);_native_ui.start()
            print('[Slots] Native target prototype on. Aim at the machine and check for the game prompt. Native Use/Take labels; price/amount labels pending. E uses the existing paid spin logic.')
    except Exception as error:
        _report.setdefault('native_usability',{}).update(state='unavailable; custom prompt retained',error=str(error))
        print('[Slots] Native HUD target unavailable: '+str(error))
    _save_report()


def _setup_cabinet_lights_steps(pc):
    global _cabinet_lights
    from .cabinet_glow import CabinetGlow
    if _cabinet_lights is None:_cabinet_lights=CabinetGlow(sys.modules[__name__])
    return (yield from _cabinet_lights.setup_steps(pc))

def _setup_cabinet_lights(pc):
    steps=_setup_cabinet_lights_steps(pc)
    while True:
        try:next(steps)
        except StopIteration as done:return done.value


@command('slots_lights',description='Toggle retained BL2 masked rainbow/chase lights on the cabinet.')
def slots_lights(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_lights(args)
    if _actor is None:
        print('[Slots] Run slots_spawn first.');return
    if args.mode=='on' and (_cabinet_lights is None or not _cabinet_lights.available):
        _setup_cabinet_lights(get_pc(possibly_loading=True))
    if _cabinet_lights is not None:_cabinet_lights.set_enabled(args.mode=='on')
    state=_report.get('cabinet_lights',{}).get('state','not available')
    print('[Slots] Cabinet lights '+args.mode+': '+state)
    _save_report()


slots_lights.add_argument('mode',choices=('on','off'))


@command('slots_audio_test',description='Play one original BL2 sound without wagering or spawning rewards.')
def slots_audio_test(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_audio_test(args)
    service=_experience_service()
    if _spin is not None:
        print('[Slots] Wait for the spin to finish before a sound test.');return
    if not service.settings['sounds']:
        print('[Slots] Enable slots_sounds on first.');return
    service.sound(args.event)
    print('[Slots] Sound-only test: '+args.event+'; no reward or charge.')


slots_audio_test.add_argument('event',choices=('lever','rolling','win','drawer_close','weapon_open','weapon_close','jackpot','cash_drop','cash_pickup','grenade_warning','grenade_explosion'))


@command('slots_rewards',description='Enable native weapons at your current character level; updates automatically.')
def slots_rewards(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_rewards(args)
    prefs=_experience_service();prefs.settings['loot_enabled']=True;prefs.save()
    print('[Slots] Native weapon prizes enabled with automatic character-level sync. Legacy level argument ignored.')

slots_rewards.add_argument('level',type=int,nargs='?',default=None)


@command('slots_level',description='Read native character level and show the synchronized price/reward profile.')
def slots_level(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_level(args)
    try:
        from .level_sync import snapshot
        profile=snapshot(sys.modules[__name__],get_pc(possibly_loading=True))
        print(f"[Slots] Level {profile['level']}: spin ${profile['price']:,}; cash 30-60%/100%/300%; weapons level {profile['weapon_stage']}; grenade damage {profile['grenade_damage']:,}. Eridium stacks/gear levels synced; paid mixed rewards active.")
    except Exception as error:
        _report['level_sync_error']=str(error)
        print('[Slots] '+str(error))
    _save_report()


@command('slots_loot_enable',description='Enable native gear prizes using the verified item-pool backend.')
def slots_loot_enable(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_loot_enable(args)
    service=_loot_service();prefs=_experience_service()
    if args.mode=='on' and not service.tested:
        print('[Slots] Verified gear pools are unavailable; run slots_check.');return
    prefs.settings['loot_enabled']=args.mode=='on';prefs.save()
    print('[Slots] Native gear prizes: '+args.mode)


slots_loot_enable.add_argument('mode',choices=('on','off'))


@command("slots_collect", description="Aim at the money stack in the open payout tray to collect slot winnings.")
def slots_collect(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_collect(args)
    _collect_cash_nearby()


@command("slots_wallet", description="Read the current Cash balance and test spin price.")
def slots_wallet(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_wallet(args)
    try:
        pc = get_pc(possibly_loading=True)
        manager,entries,index,entry,amount = _cash_entry(pc)
        _report["wallet"] = {"balance":amount,"currency":"Cash","wager_policy":"character-level curve",
                             "authority":bool(_call(pc,"HasAuthority"))}
        from .level_sync import snapshot
        profile=snapshot(sys.modules[__name__],pc)
        print(f"[Slots] Cash: {amount:,}. Level {profile['level']} spin: {profile['price']:,} Cash.")
    except Exception:
        _report["wallet_error"] = traceback.format_exc()
        print("[Slots] Could not read wallet; send the newest diagnostic.")
    _save_report()


def _grenade_service():
    global _grenade_test
    if _grenade_test is None:
        from .grenade_test import GrenadeTest
        _grenade_test=GrenadeTest(sys.modules[__name__])
    return _grenade_test


@command('slots_help', description='Show release commands and paid test-spin examples.')
def slots_help(args):
    print('[Slots] E: paid spin. slots_play: paid spin at the selected/nearest machine.')
    print('[Slots] slots_test or slots_test list: list all paid test outcomes.')
    print('[Slots] Examples: slots_test grenade | slots_test jackpot | slots_test legendary --type shield')
    print('[Slots] slots_status | slots_machines | slots_level | slots_wallet | slots_odds | slots_check | slots_verify')
    print('[Slots] Settings: slots_sounds on/off | slots_lights on/off | slots_auto_cash on/off | slots_loot_enable on/off | slots_pearl_enable on/off')
    print('[Slots] Placement: slots_placements status | slots_edit | slots_move (use each command --help for arguments).')
    print('[Slots] Troubleshooting: slots_diagnostics on/off. Saved preferences and placements are kept in BL4SlotDiagnostics under your user folder.')


@command('slots_test', description='Controlled PAID spin; charges wager and awards the named supported outcome.')
def slots_test(args):
    if args.outcome == 'list':
        print('[Slots] Paid test outcomes: ' + ', '.join(outcomes.SUPPORTED))
        print('[Slots] slots_test <outcome> [wager] [--type weapon|classmod|shield|enhancement|repkit|ordnance|heavy]')
        print('[Slots] Tests charge the normal wager and award real rewards. Grenades cause damage.')
        return
    if getattr(args,'category',None) and args.outcome not in ('common','uncommon','rare','epic','legendary','pearl'):
        print('[Slots] --type applies to gear rarity tests only. Jackpot keeps its normal equipment and burst rewards.')
        return
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_test(args)
    if args.wager is not None and args.wager<=0:
        print('[Slots] Use a positive wager.');return
    _start_spin(wager=args.wager,forced_outcome=args.outcome,forced_category=getattr(args,'category',None))

slots_test.add_argument('outcome',nargs='?',default='list',choices=('list',)+outcomes.SUPPORTED)
slots_test.add_argument('wager',nargs='?',type=int,default=None)
slots_test.add_argument('--type',dest='category',choices=('weapon','classmod','shield','enhancement','repkit','ordnance','heavy'),default=None)


@command('slots_check',description='Read-only payout readiness and latest reward request summary; no prizes or charges.')
def slots_check(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_check(args)
    from .verification import status
    info=status(sys.modules[__name__])
    _report['payout_check']=info
    print('[Slots] '+('Ready for next spin.' if info['ready'] else 'Waiting: '+', '.join(info['blockers'])))
    print('[Slots] Latest payout: '+str(info['outcome'])+' / '+str(info['last_paid_state'])+
          '; native requests '+str(info['native_attempts'])+'/'+str(info['native_planned'])+
          '; failed '+str(info['native_failed'])+'; unidentified '+str(info['native_unidentified']))
    _save_report()


@command('slots_verify',description='Reward checklist: list/reset are free; next starts ONE $100 paid test. Collect prize before next.')
def slots_verify(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_verify(args)
    global _verification_index
    from .verification import CASES,status
    if args.mode=='list':
        for i,(name,category,note) in enumerate(CASES,1):
            print(f'[Slots] {i}/{len(CASES)} {name}'+(' --type '+category if category else '')+' - '+note)
        print('[Slots] Run slots_verify next for ONE $100 test. No automatic spins.');return
    if args.mode=='reset':
        if not status(sys.modules[__name__])['ready']:
            print('[Slots] Finish the current payout before resetting the checklist.');return
        _verification_index=0;print('[Slots] Checklist reset.');return
    if _verification_index>=len(CASES):
        print('[Slots] All checklist tests started. Finish the last payout, then use slots_check and normal E spins.');return
    outcome,category,note=CASES[_verification_index]
    print(f'[Slots] Test {_verification_index+1}/{len(CASES)} - '+note+' Wager: $100.')
    before=_spin
    _start_spin(wager=100,forced_outcome=outcome,forced_category=category)
    if _spin is not None and _spin is not before:
        _verification_index+=1
        _report['verification']={'started':_verification_index,'total':len(CASES),'outcome':outcome,'category':category,'expectation':note,'wager':100}
        _save_report()
    else:print('[Slots] Checklist has not advanced; resolve the start error and retry next.')

slots_verify.add_argument('mode',nargs='?',choices=('list','next','reset'),default='list')


@command('slots_pearl_enable',description='Enable/disable native pearlescent paid prizes (enabled by default).')
def slots_pearl_enable(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_pearl_enable(args)
    enabled=args.mode=='on'
    if enabled and not _loot_service().pearl_available():
        print('[Slots] Authored pearl pools unavailable; random pearl rewards remain disabled.');return
    service=_experience_service();service.settings['pearl_enabled']=enabled;service.save()
    print('[Slots] Random pearlescent prizes '+('enabled' if enabled else 'disabled')+'. Use slots_odds for current chances.')

slots_pearl_enable.add_argument('mode',choices=('on','off'))


@command('slots_odds',description='Show actual paid-spin chances and payout rules.')
def slots_odds(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_odds(args)
    enabled=_experience_service().settings['loot_enabled']
    grenades=not bool(_grenade_service().blocked)
    try:
        from .native_grenade import validate
        validate(sys.modules[__name__])
    except Exception:grenades=False
    eridium=_loot_service().eridium_available()
    pearls=_experience_service().settings.get('pearl_enabled',False) and _loot_service().pearl_available()
    table=outcomes.odds(enabled,grenades,eridium,pearls)
    _report['payout_profile']={'chances_percent':table,'source':outcomes.SOURCE,
        'cash_multipliers':{'cash_small_range':[0.30,0.60],'cash_medium':1,'cash_large':3},
        'excluded':['customization']+([] if grenades else ['grenade'])+([] if eridium else ['eridium'])+([] if pearls else ['pearl']),
        'gear_type_weights':outcomes.TYPE_WEIGHTS,'jackpot_rarities':outcomes.JACKPOT_RARITIES,
        'jackpot_equipment_weights':outcomes.JACKPOT_EQUIPMENT_WEIGHTS,
        'note':'Custom v95 odds; unavailable/disabled categories renormalized.'}
    print('[Slots] Paid chances: '+', '.join(f'{name} {chance:.4f}%' for name,chance in table.items()))
    print('[Slots] Cash 30-60%/100%/300%; mixed native gear50/10/10/10/10/5/5; single/triple purple stacks Eridium; three cyan pearls native pearl weapon; sevens epic/legendary equipment held guarantee +20 scattered items. Free spins award nothing.')
    _save_report()


@command("slots_play", description="Play a cash spin; defaults to your level-synced price, charged at the start.")
def slots_play(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_play(args)
    if args.wager is not None and not 1 <= args.wager <= 10_000_000:
        print("[Slots] Use a wager from 1 to 10000000 Cash.")
        return
    _start_spin(wager=args.wager)


slots_play.add_argument("wager",nargs="?",type=int,default=None)


def _start_spin(require_near=False, wager=0, forced_outcome=None, grenade_test=False, forced_category=None):
    if _editing:
        print('[Slots] Save or cancel the placement edit before spinning.');return
    attempt={'wager':wager,'state':'requested'}
    _report['last_start_attempt']=attempt
    if wager:_report['last_paid_attempt']=attempt
    global _animations, _spin, _timer_hook_active, _reel_config
    if _actor is None or _component is None:
        attempt["state"]="no machine"
        print("[Slots] Run slots_spawn first.")
        return
    if _grenade_test is not None and _grenade_test.waiting():
        attempt['state']='grenade test active'
        print('[Slots] Wait for the grenade test and tray to finish.');return
    if _weapon_state is not None:
        attempt["state"]="weapon door active"
        print("[Slots] Wait for the weapon door to close.");return
    if (_loot_rewards is not None and _loot_rewards.waiting()) or (_tray_state is not None and _tray_state['state']!='closed'):
        attempt['state']='waiting payout or tray closing'
        print('[Slots] Collect the reward and wait for the tray to close.');return
    if _spin is not None:
        attempt["state"]="already spinning"
        print("[Slots] Machine is already spinning.")
        return
    if _cash_pickups is not None and _cash_pickups.records:
        attempt["state"]="waiting cash or tray closing"
        print("[Slots] Wait for the tray to finish closing." if all(r.get('collected') for r in _cash_pickups.records)
              else "[Slots] Aim at the waiting cash stack and press E before the next spin.")
        return
    try:
        if require_near and not _near_machine():
            print("[Slots] Stand within 2.25 metres and face the machine to use it.")
            return
        if wager is not None and (not isinstance(wager,int) or not 0 <= wager <= 10_000_000):
            raise ValueError("Use a Cash wager from 1 to 10000000")
        if grenade_test and wager:raise ValueError("Grenade tests must be free")
        player = get_pc(possibly_loading=True)
        profile=None
        if wager is None or wager:
            from .level_sync import snapshot
            profile=snapshot(sys.modules[__name__],player)
            if wager is None:wager=profile['price']
            attempt.update(wager=wager,level_sync=profile)
            _report['last_paid_attempt']=attempt
        if wager:
            if player is None or player.Pawn is None or not _call(player,"HasAuthority"):
                raise RuntimeError("Cash spins currently require an active single-player/host controller")
            if _cash_entry(player)[4] < wager:
                attempt.update(state="insufficient cash",balance=_cash_entry(player)[4])
                print("[Slots] Not enough Cash for this wager.")
                return
        if _animations is None:
            _animations=load_json(_ROOT/'animations.json.gz')
        if _reel_config is None:
            _reel_config = load_json(_ROOT/'reels.json')
        if wager and _cash_mode == 'pickup':
            _pickup_service().prepare()
        # Native no-argument callback on our own nonreplicated preview actor.
        # The pre-hook blocks its native body for this actor only.
        # Blueprint-only K2_OnReset produced no callbacks in v0.7 runtime testing.
        callback_function = unrealsdk.find_object("Function", _TIMER_FUNCTION)
        callback_flags = int(callback_function.FunctionFlags)
        if not callback_flags & 0x400:
            raise RuntimeError("Timer callback is not a native UFunction")
        _report.pop("animation_error", None)
        prepare_started = _clock()
        grenade_enabled=False
        if wager:
            try:
                from .native_grenade import validate
                if _grenade_service().blocked:raise RuntimeError(_grenade_service().blocked)
                validate(sys.modules[__name__]);grenade_enabled=True
            except Exception as error:_report['paid_grenade_unavailable']=str(error)
        eridium_enabled=_loot_service().eridium_available() if wager else False
        choices = [3,3,3] if grenade_test else (outcomes.choose(_experience_service().settings['loot_enabled'],forced_outcome,grenade_enabled=grenade_enabled,eridium_enabled=eridium_enabled,
                       pearl_enabled=(_experience_service().settings.get('pearl_enabled',False) or forced_outcome=='pearl') and _loot_service().pearl_available())
                   if wager else [random.SystemRandom().randrange(10) for _ in range(3)])
        if wager and outcomes.classify([outcomes.SYMBOLS[i] for i in choices])['kind']=='grenade':
            _grenade_service().prepare(native=True,profile=profile)
        reward_plan=None
        selected=outcomes.classify([outcomes.SYMBOLS[i] for i in choices])
        if wager and selected['kind']=='cash' and _cash_mode=='pickup':
            _pickup_service().resolve_mesh()  # Load/check the exact cash asset before charging.
        if wager and selected['kind'] in ('weapon','jackpot','eridium'):
            reward_plan=_loot_service().plan(selected,profile,forced_category)
        elif forced_category is not None:raise ValueError('--type requires a rarity gear outcome')
        plans = _make_stop_plans(choices)
        pull = _animations["sequences"]["Pull"]
        pull_length = (len(pull["frames"])-2+pull.get("last_interval_frames",1.0))/pull["rate"]
        _report["cash_result"] = {"state":"pending" if wager else "free", "wager":wager}
        started = _clock()
        _report["spin_preparation_ms"] = (started-prepare_started)*1000
        _spin = {"started": started, "last_pose": None, "stop_plans": plans,
                 "previous_angles": _settled_angles.copy(), "pull_length": pull_length,
                 "wager":wager, "player":player,"grenade_test":grenade_test,"level_sync":profile,"reward_plan":reward_plan,
                 "close_tray":bool(_tray_state and _tray_state['state']=='open')}
        _report["animation"] = {"state": "running", "ticks": 0, "elapsed": 0,
                                "timer_event": _TIMER_FUNCTION, "interval": _FRAME_TIMER_INTERVAL, "playback": "frame-paced interpolated rigid parts",
                                "max_once_per_frame": True,
                                "cost": wager, "payouts_enabled": bool(wager), "settlement": "charge at start, " + ("cash stack at completion, wallet credit on collection" if _cash_mode == "pickup" else "wallet credit at completion"), "callback_function_flags": callback_flags,
                                "stop_indices": choices, "symbols": [p["target"]["symbol"] for p in plans],
                                "stop_durations": [p["duration"] for p in plans],
                                "odds": "Custom v95 mixed native rewards; prevalidated pool plan and spin-start level" if wager else "Uniform free visual test; no prizes",
                                "forced_outcome":forced_outcome}
        if not _timer_hook_active:
            raise RuntimeError("Slot animation dispatcher is not enabled")
        _apply_animation_frame(_animations["sequences"]["Pull"], 0, _reel_overrides("Pull",0))
        # Check interception immediately rather than leave a silently stalled timer.
        before = _report["animation"]["ticks"]
        _call(_actor, "ForceNetUpdate")
        if _report["animation"]["ticks"] == before:
            if require_near or getattr(_experience_service(),'in_poll',False):
                _report["animation"]["hook_self_test"] = "awaiting real timer callback; 0.6-second refund watchdog"
            else:
                raise RuntimeError("Native callback hook self-test received no callback")
        else:
            _report["animation"]["hook_self_test"] = "passed"
        system = _default("/Script/Engine.Default__KismetSystemLibrary")
        timer_params = {str(p.Name).lower() for p in system.K2_SetTimer.func._properties()}
        if "bmaxonceperframe" not in timer_params:
            raise RuntimeError("Frame-paced timer requires bMaxOncePerFrame support")
        handle = _call(system, "K2_SetTimer",
                       Object=_actor, FunctionName="ForceNetUpdate", Time=_FRAME_TIMER_INTERVAL, bLooping=True,
                       bMaxOncePerFrame=True, InitialStartDelay=_FRAME_TIMER_INTERVAL, InitialStartDelayVariance=0.0,
                       optional=("InitialStartDelay", "InitialStartDelayVariance"))
        _report["animation"]["timer_handle"] = str(handle)
        _charge_cash_spin(_spin)
        attempt["state"]="started"
        _experience_service().spin_sound(_spin)
        if wager:
            destination = "collect winnings from the open tray." if _cash_mode == "pickup" else "winnings paid when reels stop."
            print(f"[Slots] Cash spin: {wager:,} Cash charged now; {destination}")
        else:
            print("[Slots] Free randomized spin: reels stop left, middle, then right.")
    except Exception:
        _report["animation_error"] = traceback.format_exc()
        attempt.update(state="failed",error=_report["animation_error"])
        _report.setdefault("animation", {})["state"] = "failed"
        try:
            _stop_spin(restore=True)
        except Exception:
            pass
        print("[Slots] Could not start spin: " + _report["animation_error"].splitlines()[-1])
    _request_report()




def _disable():
    _native_trace.close()
    pc=get_pc(possibly_loading=True)
    if pc is not None and _boot_pc_path is not None and _path(pc)==_boot_pc_path:
        _call(_default('/Script/Engine.Default__KismetSystemLibrary'),'K2_ClearTimer',Object=pc,FunctionName='ForceNetUpdate')
    from .fleet import manager
    manager(sys.modules[__name__]).clear()
    if _compass is not None:
        _compass.stop()
    from .timer_bus import unsubscribe
    unsubscribe("slots.animation")
    unsubscribe("slots.startup")
    global _timer_hook_active
    _timer_hook_active=False


@command("slots_status", description="Save the current model/animation diagnostics.")
def slots_status(args):
    if not globals().get('_fleet_child',False) and not globals().get('_fleet_local',False):
        from .fleet import manager
        target=manager(sys.modules[__name__]).select()
        if target is not sys.modules[__name__]:return target.slots_status(args)
    if _loot_rewards is not None:_report['loot_display_snapshot']=_loot_rewards.display.snapshot()
    print("[Slots] " + str(_report.get("animation", "No animation started.")))
    _save_report(force=True)


@command("slots_diagnostics", description="Enable or disable automatic diagnostic files for all machines this session.")
def slots_diagnostics(args):
    if args.mode is not None:
        diagnostics.enabled = args.mode == 'on'
        if not diagnostics.enabled:
            fleet = globals().get('_fleet')
            for ctx in fleet.instances if fleet is not None else [sys.modules[__name__]]:
                ctx._report_pending = False
                ctx._report_quiet_since = None
            _native_trace.close()
    print('[Slots] Automatic diagnostics: ' + ('ON' if diagnostics.enabled else 'OFF') +
          '. Applies to all machines; defaults to OFF after restarting the game. slots_status saves one manual report.')


slots_diagnostics.add_argument('mode', nargs='?', choices=('on','off'), default=None)


_boot_pc_path=None
_boot_pawn_path=None
_boot_ready_at=0

def _arm_startup():
    global _boot_pc_path,_boot_poll_at
    _boot_poll_at=0
    pc=get_pc(possibly_loading=True)
    if pc is None:return
    _boot_pc_path=_path(pc)
    _call(_default('/Script/Engine.Default__KismetSystemLibrary'),'K2_SetTimer',
          Object=pc,FunctionName='ForceNetUpdate',Time=.01,bLooping=True,
          bMaxOncePerFrame=True,InitialStartDelay=.25,InitialStartDelayVariance=0)

def _startup_tick(obj,args,ret,fn):
    global _boot_pawn_path,_boot_ready_at
    if _boot_pc_path is None or _path(obj)!=_boot_pc_path:return
    from .fleet import manager
    fleet=manager(sys.modules[__name__])
    global _boot_poll_at
    now=time.monotonic()
    placements=globals().get('_placements')
    searching=placements is not None and placements.search is not None
    if fleet.loading is None and not searching and now<globals().get('_boot_poll_at',0):return Block
    _boot_poll_at=now+.25
    pc=get_pc(possibly_loading=True)
    fleet.sync_session()
    if pc is None or pc.Pawn is None:return Block
    pawn_path=_path(pc.Pawn)
    if pawn_path!=_boot_pawn_path:
        _boot_pawn_path=pawn_path;_boot_ready_at=time.monotonic()+1.5
    if _boot_ready_at and time.monotonic()>=_boot_ready_at:
        _boot_ready_at=0
    if not _boot_ready_at:
        fleet.advance_loading()
        from .placement_editor import service as editor_service
        try:editor_service(sys.modules[__name__]).tick(fleet)
        except Exception as error:_report['placement_editor_error']=str(error)
        from .placements import service
        service(sys.modules[__name__]).tick()
        from .compass import service as compass_service
        compass_service(sys.modules[__name__]).tick(manager(sys.modules[__name__]))
    return Block

@hook('/Script/Engine.PlayerController:ClientRestart',Type.POST)
def _character_loaded(obj,args,ret,fn):
    pc=get_pc(possibly_loading=True)
    if pc is not None and _path(obj)==_path(pc):
        try:
            from .fleet import manager
            manager(sys.modules[__name__]).sync_session()
            _arm_startup()
        except Exception as error:_report['startup_error']=str(error)

@hook('/Script/Engine.Actor:ReceiveEndPlay',Type.PRE)
def _world_end_play(obj,args,ret,fn):
    # EndPlay reasons: 0 Destroyed (normal remove), 1 LevelTransition,
    # 2 EndPlayInEditor, 3 RemovedFromWorld, 4 Quit.
    reason=getattr(args,'EndPlayReason',None)
    try:reason=int(getattr(reason,'value',reason))
    except (TypeError,ValueError):return
    fleet=globals().get('_fleet')
    if reason in (1,3,4) and fleet is not None:
        fleet.end_world(_path(obj))


def _enable():
    global _timer_hook_active
    from .timer_bus import subscribe
    from .fleet import manager
    subscribe('slots.animation',manager(sys.modules[__name__]).animate)
    subscribe('slots.startup',_startup_tick)
    _timer_hook_active=True
    asset_cache.preload(_ROOT)
    manager(sys.modules[__name__]).prepare_template()
    try:_arm_startup()
    except Exception as error:_report['startup_error']=str(error)


_mod = build_mod(name="Slot Machines", author="Fyce / Codex", version="1.1.0",
          description="Physical slot machines with paid spins, native BL4 prizes, animated lighting, positional sound and permanent placements.",
          supported_games=Game.BL4, commands=[slots_audio_test, slots_auto_cash, slots_check, slots_collect, slots_diagnostics, slots_edit, slots_help, slots_level, slots_lights, slots_loot_enable, slots_machines, slots_move, slots_odds, slots_pearl_enable, slots_placements, slots_play, slots_remove, slots_rewards, slots_sounds, slots_spawn, slots_status, slots_test, slots_verify, slots_wallet],
          keybinds=[interact_slot_machine,collect_slot_winnings], hooks=[_character_loaded,_world_end_play], on_enable=_enable, on_disable=_disable)
