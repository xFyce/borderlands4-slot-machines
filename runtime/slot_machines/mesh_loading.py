"""Same cabinet mesh, with value preparation split into bounded batches."""
import unrealsdk
from .asset_cache import load_json
from .streaming import resources


def build_steps(ctx, component, height):
    ctx._part_poses.clear()
    geometry=load_json(ctx._ROOT/'geometry.json')
    groups=load_json(ctx._ROOT/'glow_geometry.json.gz')['body_groups']
    cache=resources(ctx)
    key=('body',str(ctx._ROOT),height,ctx._normal_sign)
    prepared=cache.meshes.get(key)
    arrays={}
    for bone_text,triangles in sorted(groups.items(),key=lambda item:int(item[0])):
        bone=int(bone_text)
        if bone not in ctx._parts:
            part=component if bone==0 else ctx._call(ctx._actor,'AddComponentByClass',
                Class=unrealsdk.find_class('/Script/ProceduralMeshComponent.ProceduralMeshComponent'),
                bManualAttachment=False,RelativeTransform=ctx._transform(),bDeferredFinish=False)
            if part is None:raise RuntimeError('Animation component creation returned None')
            ctx._parts[bone]=part;ctx._keepalive.append(part)
        part=ctx._parts[bone]
        if prepared is not None:
            data=prepared[bone]
        else:
            indices=sorted(set(triangles));remap={old:new for new,old in enumerate(indices)}
            vertices=[];normals=[];uv=[];tangents=[]
            white=unrealsdk.make_struct('LinearColor',R=1,G=1,B=1,A=1)
            for start in range(0,len(indices),128):
                for i in indices[start:start+128]:
                    vertices.append(ctx._vec(*(x*height/180 for x in geometry['vertices'][i])))
                    normals.append(ctx._vec(*(x*ctx._normal_sign for x in geometry['normals'][i])))
                    uv.append(unrealsdk.make_struct('Vector2D',X=geometry['uv'][i][0],Y=geometry['uv'][i][1]))
                    flip=geometry['tangents'][i][3]
                    tangents.append(unrealsdk.make_struct('ProcMeshTangent',TangentX=ctx._vec(*geometry['tangents'][i][:3]),
                        bFlipTangentY=(not flip) if ctx._normal_sign<0 else flip))
                yield 'body vertex batch'
            data=dict(indices=indices,vertices=vertices,normals=normals,uv=uv,
                      colors=[white]*len(indices),tangents=tangents,triangles=[remap[i] for i in triangles])
        ctx._call(part,'CreateMeshSection_LinearColor',SectionIndex=0,
            Vertices=data['vertices'],Triangles=data['triangles'],Normals=data['normals'],
            UV0=data['uv'],UV1=[],UV2=[],UV3=[],VertexColors=data['colors'],Tangents=data['tangents'],
            bCreateCollision=False,bSRGBConversion=False,optional=('UV1','UV2','UV3','bSRGBConversion'))
        arrays[bone]=dict(data)
        yield 'body mesh section'
    if prepared is None:cache.meshes[key]=arrays
    else:cache.mesh_hits+=1
    ctx._mesh_arrays=dict(geometry=geometry,parts=arrays)
    ctx._report['payline']=dict(correction_degrees=-18,markers='removed',result_row='middle')
    ctx._report['mesh']=dict(vertices=len(geometry['vertices']),triangles=len(geometry['triangles'])//3,
        parts=len(groups),flexible_triangles=len(groups.get('-1',[]))//3,
        normal_direction='corrected' if ctx._normal_sign<0 else 'original')
