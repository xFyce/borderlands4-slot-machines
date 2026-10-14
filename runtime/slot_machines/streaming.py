"""Budgeted game-thread construction and world-scoped, native-owned assets."""
import json
import os
import time
from pathlib import Path

BUDGET_SECONDS = .002
MAX_STEPS = 8
RESOURCE_TAG = 'BL4Slots.ResourceAnchor'


class Resources:
    def __init__(self):
        self.textures = {}
        self.meshes = {}
        self.body_material = None
        self.imports = 0
        self.texture_hits = 0
        self.mesh_hits = 0
        self.world = None
        self.anchor_actor = None
        self.anchor_component = None
        self.retained_slots = {}
        self.retained_paths = {}
        self.retention_retry_at = 0


def resources(ctx):
    from .fleet import manager
    return manager(ctx).resources


def texture(ctx, pc, filename):
    # Store names, never cached texture wrappers. A live cabinet's MID retains
    # the texture in Unreal; find_object checks the current registry on reuse.
    import unrealsdk
    cache = resources(ctx)
    key = str(ctx._ROOT/filename)
    path = cache.textures.get(key)
    if path:
        try:
            existing = unrealsdk.find_object('Texture2D', path)
        except ValueError:
            existing = None
        if existing is not None:
            cache.texture_hits += 1
            return existing
    rendering = ctx._default('/Script/Engine.Default__KismetRenderingLibrary')
    result = ctx._call(rendering, 'ImportFileAsTexture2D',
                       WorldContextObject=pc, Filename=key)
    if result is None:
        raise RuntimeError('Texture import failed: '+filename)
    cache.textures[key] = ctx._path(result)
    cache.imports += 1
    return result


def retain_material(ctx, pc, material, key):
    """Native material references keep imports alive across same-world travel.

    Python caches contain only names and value arrays. A hidden, non-colliding
    world actor owns the references; it renders no mesh and owns no Use target.
    Failure is optional: ordinary cabinet-owned materials still render normally.
    """
    import unrealsdk
    cache=resources(ctx)
    if time.monotonic()<cache.retention_retry_at:return False
    try:
        part=unrealsdk.find_object('Object',cache.anchor_component) if cache.anchor_component else None
    except ValueError:part=None
    try:
        if part is None:
            release_retained_materials(ctx)
            gs=ctx._default('/Script/Engine.Default__GameplayStatics')
            transform=ctx._transform()
            actor=ctx._call(gs,'BeginDeferredActorSpawnFromClass',WorldContextObject=pc,
                ActorClass=unrealsdk.find_class('/Script/Engine.Actor'),SpawnTransform=transform,
                CollisionHandlingOverride=1,Owner=pc,TransformScaleMethod=0,optional=('TransformScaleMethod',))
            if actor is None:raise RuntimeError('Resource anchor spawn failed')
            cache.anchor_actor=ctx._path(actor)
            actor.Tags=[RESOURCE_TAG]
            ctx._call(actor,'SetActorHiddenInGame',bNewHidden=True)
            ctx._call(actor,'SetActorEnableCollision',bNewActorEnableCollision=False)
            actor=ctx._call(gs,'FinishSpawningActor',Actor=actor,SpawnTransform=transform,
                           TransformScaleMethod=0,optional=('TransformScaleMethod',))
            if actor is None:raise RuntimeError('Resource anchor finish failed')
            part=ctx._call(actor,'AddComponentByClass',
                Class=unrealsdk.find_class('/Script/ProceduralMeshComponent.ProceduralMeshComponent'),
                bManualAttachment=False,RelativeTransform=transform,bDeferredFinish=False)
            if part is None:raise RuntimeError('Resource anchor component failed')
            cache.anchor_component=ctx._path(part)
            ctx._call(part,'SetCollisionEnabled',NewType=0)
            ctx._call(part,'SetVisibility',bNewVisibility=False,bPropagateToChildren=False)
        path=ctx._path(material)
        if cache.retained_paths.get(key)!=path:
            slot=cache.retained_slots.setdefault(key,len(cache.retained_slots))
            ctx._call(part,'SetMaterial',ElementIndex=slot,Material=material)
            if ctx._call(part,'GetMaterial',ElementIndex=slot)!=material:
                raise RuntimeError('Resource material reference was not retained')
            cache.retained_paths[key]=path
        return True
    except Exception as error:
        cache.retention_retry_at=time.monotonic()+5
        ctx._report['resource_retention_note']=str(error)
        return False


def release_retained_materials(ctx):
    import unrealsdk
    cache=resources(ctx)
    path,cache.anchor_actor=cache.anchor_actor,None
    cache.anchor_component=None;cache.retained_slots.clear();cache.retained_paths.clear()
    if not path:return
    try:
        actor=unrealsdk.find_object('Actor',path)
        if actor is not None and RESOURCE_TAG in [str(t) for t in actor.Tags]:
            ctx._call(actor,'K2_DestroyActor')
    except (ValueError,RuntimeError):pass


def section_steps(ctx, part, index, mesh, offset, cache_key=None, label='light mesh section'):
    import unrealsdk
    cache = resources(ctx)
    arrays = cache.meshes.get(cache_key) if cache_key is not None else None
    if arrays is None:
        vertices=[];normals=[];uvs=[];tangents=[]
        scale=ctx._model_height/180
        white=unrealsdk.make_struct('LinearColor',R=1,G=1,B=1,A=1)
        for start in range(0,len(mesh['vertices']),128):
            for i in range(start,min(start+128,len(mesh['vertices']))):
                position=mesh['vertices'][i];face=mesh['face_normals'][i]
                vertices.append(ctx._vec(*(position[a]*scale+face[a]*ctx._normal_sign*offset for a in range(3))))
                normals.append(ctx._vec(*(x*ctx._normal_sign for x in mesh['normals'][i])))
                u=mesh['uv'][i];uvs.append(unrealsdk.make_struct('Vector2D',X=u[0],Y=u[1]))
                tangents.append(unrealsdk.make_struct('ProcMeshTangent',TangentX=ctx._vec(*mesh['tangents'][i]),bFlipTangentY=False))
            yield 'light vertex batch'
        arrays=dict(Vertices=vertices,Triangles=mesh['triangles'],Normals=normals,UV0=uvs,
                    UV1=[],UV2=[],UV3=[],VertexColors=[white]*len(vertices),Tangents=tangents)
        if cache_key is not None:cache.meshes[cache_key]=arrays
    else:
        cache.mesh_hits+=1
    ctx._call(part,'CreateMeshSection_LinearColor',SectionIndex=index,**arrays,
              bCreateCollision=False,bSRGBConversion=False,optional=('UV1','UV2','UV3','bSRGBConversion'))
    yield label


class LoadJob:
    def __init__(self, target, iterator, clock=time.perf_counter):
        self.target = target
        self.iterator = iterator
        self.clock = clock
        self.started = clock()
        self.frames = 0
        self.steps = 0
        self.max_frame_ms = 0
        self.max_step_ms = 0
        self.longest_step = ''
        self.stage = 'waiting for data'
        self.done = False
        self.result = None

    def advance(self):
        start = self.clock()
        self.frames += 1
        for _ in range(MAX_STEPS):
            before = self.clock()
            try:
                stage = next(self.iterator)
            except StopIteration as finished:
                self.done = True
                self.result = finished.value
                stage = 'finished'
            elapsed = (self.clock()-before)*1000
            self.steps += 1
            if elapsed > self.max_step_ms:
                self.max_step_ms = elapsed
                self.longest_step = str(stage)
            self.stage = str(stage)
            if self.done or self.clock()-start >= BUDGET_SECONDS:
                break
        self.max_frame_ms = max(self.max_frame_ms, (self.clock()-start)*1000)
        return self.done

    def cancel(self):
        # Generator finalizers contain Python bookkeeping only. World teardown
        # must not call into actors/components retained by a suspended iterator.
        self.iterator.close()
        self.done = True

    def metrics(self):
        return dict(frames=self.frames, steps=self.steps,
                    elapsed_ms=round((self.clock()-self.started)*1000, 2),
                    max_frame_ms=round(self.max_frame_ms, 2),
                    max_step_ms=round(self.max_step_ms, 2),
                    longest_step=self.longest_step, stage=self.stage,
                    target_budget_ms=BUDGET_SECONDS*1000)


def report(fleet, job, error=None):
    metrics = job.metrics()
    metrics['state'] = 'failed' if error else 'ready'
    if error:
        metrics['error'] = str(error)
    job.target._report['streaming_load'] = metrics
    cache = fleet.resources
    payload = dict(version='1.3.0', machine=job.target._path(job.target._actor),
                   loading=metrics, texture_imports=cache.imports,
                   shared_texture_reuses=cache.texture_hits,
                   shared_mesh_reuses=cache.mesh_hits,retained_materials=len(cache.retained_paths),
                   visual_and_stutter_confirmation='requires in-game check')
    fleet.root._report['streaming_load'] = payload
    from . import diagnostics
    if not diagnostics.enabled:return
    try:
        folder = Path(os.environ.get('USERPROFILE', str(Path.home())))/'BL4SlotDiagnostics'
        folder.mkdir(parents=True, exist_ok=True)
        from .report_io import submit
        submit(folder/'slots_loading_v141.json', json.dumps(payload, indent=2))
    except OSError as failure:
        fleet.root._report['loading_report_error'] = str(failure)
