"""BL2 light masks on exact source UV surfaces, with native-retained frames.

The UE3 shader cannot execute in BL4. Mask/panner frames are prepared offline;
game callbacks only select an already retained material. Drawer lights attach
to their original bone, and imported assets are shared across cabinets.
"""
import time
from pathlib import Path
import unrealsdk
from .asset_cache import load_json
from .streaming import resources, section_steps, texture as shared_texture

SOURCE='/Game/World/Order/Architecture/Pipeline/MI_Pipeline_Emissive.MI_Pipeline_Emissive'
FRAME_COUNT=120
FPS=30
DETAIL_GEOMETRY_SHA=load_json(Path(__file__).with_name('asset_manifest.json'))['geometry_sha']


class CabinetGlowBase:
    def __init__(self,ctx):
        self.ctx=ctx
        self.paths=[]
        self.owner=None
        self.session=None
        self.available=False
        self.enabled=True
        self.last_frame=-1

    def resolve(self,path):
        if not path:return None
        try:return unrealsdk.find_object('Object',path)
        except ValueError:return None

    def report(self,**values):
        self.ctx._report.setdefault('cabinet_lights',{}).update(values)

    def valid(self,pc):
        return bool(self.owner and self.session and pc is not None and pc.Pawn is not None
                    and self.resolve(self.owner)==self.ctx._actor
                    and self.session==(self.ctx._path(pc),self.ctx._path(pc.Pawn)))

    def stop(self):
        self.available=False
        paths,self.paths=self.paths,[]
        for path in paths:
            try:
                part=self.resolve(path)
                if part is not None:
                    self.ctx._call(part,'K2_DestroyComponent',Object=self.resolve(self.owner),optional=('Object',))
            except Exception:pass
        self.owner=None;self.session=None;self.last_frame=-1

    def setup(self,pc):
        steps=self.setup_steps(pc)
        while True:
            try:next(steps)
            except StopIteration as done:return done.value

    def setup_steps(self,pc):
        ctx=self.ctx;self.stop();self.enabled=True
        if ctx._actor is None or pc is None or pc.Pawn is None:return False
        self.owner=ctx._path(ctx._actor);self.session=(ctx._path(pc),ctx._path(pc.Pawn))
        self.report(state='building original-mask light test',enabled=True,backend='retained source-UV mask/panner frames')
        try:
            baked=load_json(ctx._ROOT/'glow_geometry.json.gz')
            if baked['geometry_sha']!=DETAIL_GEOMETRY_SHA:
                raise RuntimeError('Original-mask light geometry does not match the cabinet')
            profile=baked['light_profile']
            if profile['frames']!=FRAME_COUNT or profile['fps']!=FPS:
                raise RuntimeError('Prepared light timing does not match the light renderer')
            source=self.resolve(SOURCE)
            if source is None:
                source=ctx._pickup_service()._registry_mesh({'asset':SOURCE},asset_path=SOURCE,expected_class='/Script/Engine.MaterialInstanceConstant')
            if source is None:raise RuntimeError('Verified cabinet emissive material could not be loaded')
            # Root before the first staged yield. Python wrappers are not GC roots.
            ctx._call(ctx._component,'SetMaterial',ElementIndex=1,Material=source)
            textures,scalars,vectors=ctx._material_parameters(source)
            if not {'BaseColor','Mask','T_Emissive'}<=set(textures) or 'EmissiveIntensity' not in scalars or 'EmissiveColor' not in vectors:
                raise RuntimeError('World emissive material profile does not match')
            white=self.resolve('/Game/Common/Texture/Proxy/T_Proxy_White_D.T_Proxy_White_D')
            if white is None:raise RuntimeError('Native white mask is unavailable')
            count=0
            for bone,chunks in baked['emission_chunks'].items():
                parent=ctx._parts.get(int(bone))
                if parent is None:raise RuntimeError('Original light bone is missing: '+bone)
                for number,chunk in enumerate(chunks):
                    part=ctx._call(ctx._actor,'AddComponentByClass',Class=unrealsdk.find_class('/Script/ProceduralMeshComponent.ProceduralMeshComponent'),
                                   bManualAttachment=False,RelativeTransform=ctx._transform(),bDeferredFinish=False)
                    if part is None:raise RuntimeError('Light surface creation failed')
                    self.paths.append(ctx._path(part))
                    ctx._call(part,'SetVisibility',bNewVisibility=False,bPropagateToChildren=False)
                    ctx._call(part,'K2_AttachToComponent',Parent=parent,SocketName='None',LocationRule=0,RotationRule=0,ScaleRule=0,bWeldSimulatedBodies=False)
                    ctx._call(part,'SetCollisionEnabled',NewType=0)
                    ctx._call(part,'SetCastShadow',NewCastShadow=False)
                    yield 'original light surface'
                    yield from section_steps(ctx,part,0,chunk,profile['surface_offset_cm'],
                        ('original-mask143',str(ctx._ROOT),ctx._model_height,ctx._normal_sign,bone,number),label='original light surface upload')
                    count+=len(chunk['triangles'])//3
            if not self.paths:raise RuntimeError('Prepared original light surfaces are empty')
            anchor=self.resolve(self.paths[0])
            cache=resources(ctx)
            if not hasattr(cache,'mask_light_materials'):cache.mask_light_materials={}
            cache_key=str(ctx._ROOT.resolve())+'|original-mask143'
            previous=cache.mask_light_materials.get(cache_key,[])
            retained=[]
            from .cabinet_lights import key
            for frame in range(FRAME_COUNT):
                mid=self.resolve(previous[frame]) if frame<len(previous) else None
                if mid is None:
                    mid=ctx._call(anchor,'CreateDynamicMaterialInstance',ElementIndex=frame+1,SourceMaterial=source,
                                  OptionalName='BL2OriginalMask143Frame'+str(frame),optional=('OptionalName',))
                    if mid is None:raise RuntimeError('Prepared light material creation failed')
                    ctx._call(anchor,'SetMaterial',ElementIndex=frame+1,Material=mid)
                    # Each texture is bound/retained before the next staged yield.
                    body=shared_texture(ctx,pc,'lights143/base.png')
                    ctx._set_parameter(mid,source,'Texture','BaseColor',body)
                    ctx._set_parameter(mid,source,'Texture','Mask',white)
                    emission=shared_texture(ctx,pc,'lights143/frame_'+f'{frame:02}.png')
                    ctx._set_parameter(mid,source,'Texture','T_Emissive',emission)
                    ctx._set_parameter(mid,source,'Vector','EmissiveColor',unrealsdk.make_struct('LinearColor',R=1,G=1,B=1,A=1))
                    ctx._set_parameter(mid,source,'Scalar','UVCoordinates',0.0)
                    controls={'emissiveintensity':profile['intensity'],'eyeadaptationcontrolamount':0.0,
                              'geonormalinfluence':0.0,'cellnoisestrength':0.0}
                    for name in scalars:
                        if key(name) in controls:ctx._set_parameter(mid,source,'Scalar',name,controls[key(name)])
                    for name in ('Comp','Normal'):
                        value=textures.get(name)
                        if value is not None and '/Game/Common/Texture/Proxy/' in ctx._path(value):
                            ctx._set_parameter(mid,source,'Texture',name,value)
                ctx._call(anchor,'SetMaterial',ElementIndex=frame+1,Material=mid)
                if ctx._call(anchor,'GetMaterial',ElementIndex=frame+1)!=mid:
                    raise RuntimeError('Native light-frame material was not retained')
                retained.append(ctx._path(mid))
                yield 'retained original-mask frame'
            cache.mask_light_materials[cache_key]=retained
            self.available=True
            self.tick(pc)
            if not self.available:raise RuntimeError('Initial light frame failed')
            self.set_enabled(True)
            self.report(state='original-mask lighting test ready',parts=len(self.paths),triangles=count,
                        frames=FRAME_COUNT,fps=FPS,surface_offset_cm=profile['surface_offset_cm'],
                        tray_light='original source UV, attached to source bone',
                        grating='sampled from supplied BL2 PS4 reference',
                        animation='original BL2 noise/blue-mask inputs; reconstructed periodic panning',
                        original_shader_direct_port=False,texture_swaps_per_tick=0,
                        retention='native component -> frame MID -> packed original-light texture')
            return True
        except Exception as error:
            self.stop();self.report(state='unavailable',enabled=False,error=str(error));return False

    def tick(self,pc=None):
        if not self.available or not self.enabled:return
        ctx=self.ctx
        if pc is None:pc=ctx.get_pc(possibly_loading=True)
        if not self.valid(pc):
            self.available=False;self.report(state='stopped: cabinet/player changed',enabled=False);return
        frame=int(ctx._clock()*FPS)%FRAME_COUNT
        if frame==self.last_frame:return
        try:
            parts=[self.resolve(path) for path in self.paths]
            if any(part is None for part in parts):raise RuntimeError('Light surface no longer exists')
            mid=ctx._call(parts[0],'GetMaterial',ElementIndex=frame+1)
            if mid is None:raise RuntimeError('Native light frame is missing')
            for part in parts:ctx._call(part,'SetMaterial',ElementIndex=0,Material=mid)
            self.last_frame=frame;self.report(frame=frame)
        except Exception as error:
            self.available=False;self.report(state='stopped after light update failure',enabled=False,error=str(error))

    def set_enabled(self,enabled):
        self.enabled=bool(enabled)
        if not self.available:return
        for path in self.paths:
            part=self.resolve(path)
            if part is not None:self.ctx._call(part,'SetVisibility',bNewVisibility=self.enabled,bPropagateToChildren=False)
        self.report(enabled=self.enabled)


class CabinetGlow(CabinetGlowBase):
    def __init__(self,ctx):
        super().__init__(ctx)
        self.next_distance_check=0
        self.near=True

    def stop(self):
        self.next_distance_check=0;self.near=True
        super().stop()

    def tick(self,pc=None):
        if self.available and self.enabled and self.last_frame>=0:
            now=time.monotonic()
            if now>=self.next_distance_check:
                self.next_distance_check=now+1
                if pc is None:pc=self.ctx.get_pc(possibly_loading=True)
                if pc is None or pc.Pawn is None:return
                actor=self.resolve(self.owner)
                if actor is None:return
                p=self.ctx._call(pc.Pawn,'K2_GetActorLocation')
                a=self.ctx._call(actor,'K2_GetActorLocation')
                self.near=sum((float(getattr(p,k))-float(getattr(a,k)))**2 for k in ('X','Y','Z'))<=8000**2
            if not self.near:return
        super().tick(pc)
