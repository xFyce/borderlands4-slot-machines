"""Fixed opaque lining behind the original cabinet's opening drawer."""
import unrealsdk

# Cabinet-local source units, behind the drawer's open back lip. The lining
# overlaps the surrounding shell; it never moves with the drawer or collides.
VERTICES=[(16,-28,53),(16,28,53),(16,28,75),(16,-28,75)]

def setup(ctx):
    material=ctx._call(ctx._component,'GetMaterial',ElementIndex=0)
    part=ctx._call(ctx._actor,'AddComponentByClass',
        Class=unrealsdk.find_class('/Script/ProceduralMeshComponent.ProceduralMeshComponent'),
        bManualAttachment=False,RelativeTransform=ctx._transform(),bDeferredFinish=False)
    if part is None:raise RuntimeError('Tray interior backing creation failed')
    scale=ctx._model_height/180
    vertices=[ctx._vec(*(n*scale for n in point)) for point in VERTICES]*2
    # Both windings cover the opening from either side without altering the
    # shared cabinet material's culling or transparency settings.
    ctx._call(part,'CreateMeshSection_LinearColor',SectionIndex=0,
        Vertices=vertices,Triangles=[0,1,2,0,2,3,4,6,5,4,7,6],
        Normals=[ctx._vec(1,0,0)]*4+[ctx._vec(-1,0,0)]*4,
        UV0=[unrealsdk.make_struct('Vector2D',X=.8,Y=.63)]*8,
        UV1=[],UV2=[],UV3=[],VertexColors=[],Tangents=[],
        bCreateCollision=False,bSRGBConversion=False,
        optional=('UV1','UV2','UV3','bSRGBConversion'))
    ctx._call(part,'SetCollisionEnabled',NewType=0)
    if material is not None:ctx._call(part,'SetMaterial',ElementIndex=0,Material=material)
    ctx._keepalive.append(part)
    ctx._report['tray_interior_backing']={'state':'fixed opaque lining','collision':False,'source_units':VERTICES}
