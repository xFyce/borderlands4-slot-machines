"""Dedicated native source test, based on authored NPC grenade configuration.

The collected NCS records define Proj_NPC_Gestalt_Grenade as an OakProjectile
with an explicit body and no item-identity appearance/body cloning. This avoids
the modular Torgue source used in v58. The same reflected spawn API is retained;
initialization must still be demonstrated in-game before enabling paid outcomes.
No equipment, identity, pointer cast or shared definition write is used.
"""
import copy
import unrealsdk

# BL2 reference: visible ejection at ~8.40 s, explosion at ~11.57 s.
FUSE_SECONDS=3.2
DEFINITION_NAME='Proj_NPC_Gestalt_Grenade'
PROJECTILE_CLASS='OakProjectile'
PROJECTILE_CLASS_PATH='/Script/OakGame.OakProjectile'
DEFINITION_TYPE=16384
NORMAL_DAMAGE_TYPE=12290
BODY='/Game/AI/ArmyBandit/_Shared/_Design/Projectiles/Body_Proj_Bandit_Grenade.Body_Proj_Bandit_Grenade'
STATICS='/Script/GbxGame.Default__ProjectileStatics'
PARAMS={'definition':'ZGameDataHandleProperty','owner':'ZObjectProperty',
        'bOwnerIsInstigator':'ZBoolProperty','instigator':'ZObjectProperty',
        'Location':'ZStructProperty','Velocity':'ZStructProperty',
        'GravityScale':'ZFloatProperty','DamageOverrides':'ZStructProperty'}


def validate(ctx):
    obj=unrealsdk.find_object('Object',STATICS)
    if obj is None:raise RuntimeError('Native ProjectileStatics is not loaded')
    props={str(p.Name):p for p in obj.SpawnProjectile_Const.func._properties()}
    if {n:type(p).__name__ for n,p in props.items()}!=PARAMS:
        raise RuntimeError('Native direct-spawn signature differs from the v56 capture')
    if int(props['definition'].TypeHandle)!=DEFINITION_TYPE:
        raise RuntimeError('Native projectile definition type differs from the slot GrenadeDef')
    for name,path in (('Location','/Script/CoreUObject.Vector'),
                      ('Velocity','/Script/CoreUObject.Vector'),
                      ('DamageOverrides','/Script/GbxGame.ProjectileDamageOverrideData')):
        if ctx._path(props[name].Struct)!=path:
            raise RuntimeError('Native direct-spawn structure differs: '+name)
    damage=unrealsdk.find_object('ScriptStruct','/Script/GbxGame.ProjectileDamageOverrideData')
    if damage is None:raise RuntimeError('Native damage override schema is not loaded')
    fields={str(p.Name):p for p in damage._properties()}
    if not {'damage','damageradius','DamageType'}<=fields.keys():
        raise RuntimeError('Native damage override schema differs')
    if (type(fields['damage']).__name__!='ZFloatProperty'
        or type(fields['damageradius']).__name__!='ZFloatProperty'
        or type(fields['DamageType']).__name__!='ZGameDataHandleProperty'
        or int(fields['DamageType'].TypeHandle)!=NORMAL_DAMAGE_TYPE):
        raise RuntimeError('Native normal-damage override types differ')
    return obj


def initialize_null_handles(value,schema,record,path='',depth=0,budget=None):
    """Type the null handles in our own zeroed parameter buffer before copying.

    SDK WrappedStruct copies validate every nested handle, including inactive
    fields of ForceSelection. A zeroed handle has type 0, not its declared
    type. Only ordinary reflected structs are visited; no definition data,
    UObject or custom inline/def pointer is followed or written.
    """
    if budget is None:budget=[256]
    if depth>8:raise RuntimeError('Native parameter schema exceeds bounded depth')
    for prop in schema._properties():
        budget[0]-=1
        if budget[0]<0:raise RuntimeError('Native parameter schema exceeds bounded size')
        name=str(prop.Name);field_path=path+'.'+name if path else name
        kind=type(prop).__name__
        if kind=='ZGameDataHandleProperty':
            type_handle=int(prop.TypeHandle)
            record['stage']='initialize null handle '+field_path
            setattr(value,name,unrealsdk.unreal.FGameDataHandle(type_handle,'None'))
            record['typed_null_handles'].append({'field':field_path,'type_handle':type_handle})
        elif kind=='ZStructProperty':
            struct=prop.Struct
            if struct is None:raise RuntimeError('Native parameter structure is untyped: '+field_path)
            initialize_null_handles(getattr(value,name),struct,record,field_path,depth+1,budget)


def request(ctx,pc,job,world):
    obj=validate(ctx)
    # Construct ordinary SDK handles by verified name and native type ID.
    # Their data is resolved by the GAME; the SDK constructor does not resolve.
    definition=unrealsdk.unreal.FGameDataHandle(DEFINITION_TYPE,DEFINITION_NAME)
    record=job['native_source']
    record.update(definition_resolved_before_call=bool(definition._get_address()),
        test_damage=float(job.get('damage',100.0)),test_damage_radius=400.0,damage_type='authored source; no unresolved Normal override',
        ownership='Machine-owned neutral hazard; no player instigator',typed_null_handles=[],
        stage='allocate parameter buffer')
    fn=obj.SpawnProjectile_Const
    params=unrealsdk.unreal.WrappedStruct(fn.func)
    initialize_null_handles(params,fn.func,record)
    record['stage']='set definition'
    params.definition=definition
    record['stage']='set ownership and launch'
    params.owner=ctx._actor;params.bOwnerIsInstigator=False;params.instigator=None
    params.Location=ctx._vec(*world);params.Velocity=ctx._vec(*job['launch_velocity'])
    params.GravityScale=1.0
    record['stage']='set damage overrides in place'
    # Preserve typed null handles in all inactive nested selection fields.
    # Assigning a separate zeroed DamageOverrides struct would copy them back.
    overrides=params.DamageOverrides
    overrides.damage=float(job.get('damage',100.0));overrides.damageradius=400.0
    # A typed null means no damage-type override. The old literal Normal was
    # unresolved in the live report and replaced the authored projectile type.
    # Let the source definition provide its valid native damage type instead.
    record['stage']='validate SDK parameter copy'
    params=copy.copy(params)
    record['sdk_parameter_copy']='passed'
    record['stage']='native function attempting'
    job['native_calls']=1
    ctx._save_report()
    # _Const has no latent context/output parameter in the observed build.
    # Do not invent an actor return; bounded ownership-filtered observation
    # below discovers the result, just as for the established gadget path.
    # The usual adapter assigns structs wholesale. This one already owns a
    # fully initialized parameter buffer; pass it directly through the SDK.
    ctx._report.setdefault('calls',{})['SpawnProjectile_Const']=list(PARAMS)
    trace=getattr(ctx,'_native_trace',None)
    if trace is not None:trace.record('invoke SpawnProjectile_Const dedicated typed params')
    fn(params)
    if trace is not None:trace.record('returned SpawnProjectile_Const dedicated typed params')
    record['stage']='native function returned'


def initialized(ctx,obj,job):
    """Validate this live owned source using SDK metadata, not BodyData.

    v60's native NPC actor launched with BodyData=None. Definition metadata
    and a live root distinguish a constructed projectile from a pooled shell.
    No definition payload is read or modified, and no wrapper is retained.
    """
    record=job.setdefault('native_initialization',{})
    try:
        record.update(actor=ctx._path(obj),projectile_class=ctx._path(obj.Class),
                      owner=ctx._path(getattr(obj,'owner',None)),
                      instigator=ctx._path(getattr(obj,'instigator',None)),
                      root=ctx._path(getattr(obj,'RootComponent',None)),
                      body=ctx._path(getattr(obj,'BodyData',None)))
        if record['projectile_class']!=PROJECTILE_CLASS_PATH:
            raise RuntimeError('Source is not the authored native NPC projectile class')
        if record['owner']!=job['machine'] or record['instigator']!='None':
            raise RuntimeError('Source ownership does not match this machine and character')
        if getattr(obj,'RootComponent',None) is None:
            raise RuntimeError('Source has no live root component yet')
        ref=obj.GbxActorData.GbxActorDef
        record.update(definition=str(ref._name),definition_type=ctx._path(ref._type),
                      definition_resolved=bool(ref._get_address()))
        if record['definition'].casefold()!=DEFINITION_NAME.casefold():
            raise RuntimeError('Source has a different actor definition')
        if record['definition_type'] not in ('/Script/GbxSpawn.GbxActorDef','/Script/OakGame.OakProjectileDef'):
            raise RuntimeError('Source definition schema differs from the native projectile schema')
        if not record['definition_resolved']:
            raise RuntimeError('Source definition is not resolved yet')
        native_type=getattr(obj,'DamageType',None)
        record['damage_type']={'name':str(native_type._name),'resolved':bool(native_type._get_address())} if native_type is not None else None
        record['damage']=float(getattr(obj,'damage',0.0))
        record['damage_on_explode']=bool(getattr(obj,'bDamageOnExplode',False))
        record.update(state='ready',reason='Matching resolved definition, ownership, class and live root')
        return True
    except Exception as error:
        record.update(state='not ready',reason=str(error))
        return False


def start_fuse(ctx,obj,job):
    if job.get('native_fuse') is not None:return
    record=job['native_fuse']={'state':'validating','calls':0}
    try:
        if not initialized(ctx,obj,job):
            raise RuntimeError(job['native_initialization']['reason'])
        props={str(p.Name):type(p).__name__ for p in obj.StartFuse.func._properties()}
        if props!={'FuseTime':'ZFloatProperty'}:
            raise RuntimeError('Native StartFuse signature differs from the v56 capture')
        # Never invoke ActivateGrenade on an empty inventory identity. Use
        # the standard projectile fuse; no equipment-derived payload is needed.
        record.update(state='attempting',calls=1,seconds=FUSE_SECONDS)
        ctx._call(obj,'StartFuse',FuseTime=FUSE_SECONDS)
        record['state']='started'
    except Exception as error:
        record.update(state='unavailable',error=str(error),note='No automatic retry')
