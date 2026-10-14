"""Static cabinet shapes for player collision; moving reward parts stay clear."""
import unrealsdk

def shapes(geometry,height):
    scale=height/180
    result=[]
    for lower,upper in ((0,53),(53,130),(130,180)):
        points=[v for v,bone in zip(geometry['vertices'],geometry['bone_for_vertex']) if bone==0 and lower<=v[2]<=upper]
        if not points:continue
        # A small inset avoids snagging on decorative edge geometry. Only the
        # fixed body is represented; neither reel, handle, drawer nor side door.
        lo=[min(v[k] for v in points)+1.5 for k in (0,1)]+[lower]
        hi=[max(v[k] for v in points)-1.5 for k in (0,1)]+[upper]
        result.append((tuple((a+b)*.5*scale for a,b in zip(lo,hi)),tuple((b-a)*.5*scale for a,b in zip(lo,hi))))
    return result

def setup(ctx):
    report={'state':'setting up','boxes':[],'scope':'fixed cabinet; physical blocking; moving reward parts stay clear'}
    ctx._report['cabinet_collision']=report;owned=[]
    try:
        pc=ctx.get_pc(possibly_loading=True)
        capsule=getattr(getattr(pc,'Pawn',None),'CapsuleComponent',None)
        pawn_channel=int(ctx._call(capsule,'GetCollisionObjectType')) if capsule is not None else 2
        report['player_channel']=pawn_channel
        for position,extent in shapes(ctx._mesh_arrays['geometry'],ctx._model_height):
            box=ctx._call(ctx._actor,'AddComponentByClass',Class=unrealsdk.find_class('/Script/Engine.BoxComponent'),
                          bManualAttachment=True,RelativeTransform=ctx._transform(*position),bDeferredFinish=False)
            if box is None:raise RuntimeError('Cabinet collision shape creation failed')
            owned.append(box)
            ctx._call(box,'SetMobility',NewMobility=2)
            attached=ctx._call(box,'K2_AttachToComponent',Parent=ctx._component,SocketName='None',LocationRule=0,RotationRule=0,ScaleRule=0,bWeldSimulatedBodies=False)
            if attached is False:raise RuntimeError('Cabinet collision box attachment failed')
            ctx._call(box,'K2_SetRelativeTransform',NewTransform=ctx._transform(*position),bSweep=False,bTeleport=True)
            ctx._call(box,'SetBoxExtent',InBoxExtent=ctx._vec(*extent),bUpdateOverlaps=False)
            ctx._call(box,'SetGenerateOverlapEvents',bInGenerateOverlapEvents=False)
            ctx._call(box,'SetCollisionProfileName',InCollisionProfileName='BlockAll',bUpdateOverlaps=False,optional=('bUpdateOverlaps',))
            ctx._call(box,'SetCollisionObjectType',Channel=0)
            ctx._call(box,'SetCollisionResponseToAllChannels',NewResponse=2)
            ctx._call(box,'SetCollisionResponseToChannel',Channel=pawn_channel,NewResponse=2)
            ctx._call(box,'SetCollisionEnabled',NewType=3)
            ctx._keepalive.append(box)
            ctx._call(box,'SetHiddenInGame',NewHidden=True,bPropagateToChildren=False)
            report['boxes'].append({'path':ctx._path(box),'center':position,'extent':extent})
        if not owned:raise RuntimeError('No fixed cabinet geometry')
        ctx._call(ctx._actor,'SetActorEnableCollision',bNewActorEnableCollision=True)
        report['state']='physical cabinet blocking configured'
        report['readbacks']=[]
        for box in owned:
            row={'path':ctx._path(box),'enabled':int(ctx._call(box,'GetCollisionEnabled')),
                 'player_response':int(ctx._call(box,'GetCollisionResponseToChannel',Channel=pawn_channel)),
                 'world_position':str(ctx._call(box,'K2_GetComponentLocation'))}
            report['readbacks'].append(row)
            if row['enabled']!=3 or row['player_response']!=2:raise RuntimeError('Collision configuration did not stick')
        return True
    except Exception as error:
        for box in owned:
            try:ctx._call(box,'K2_DestroyComponent',Object=ctx._actor,optional=('Object',))
            except Exception:pass
        report.update(state='unavailable',error=str(error));return False
