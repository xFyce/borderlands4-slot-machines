"""Read-only dedicated grenade activation/definition investigation."""
import unrealsdk

FUNCTIONS=(
    '/Script/GbxGame.ProjectileStatics:SpawnProjectile',
    '/Script/GbxGame.ProjectileStatics:SpawnProjectile_Source',
    '/Script/GbxGame.ProjectileStatics:SpawnProjectile_ThrowAtLocation',
    '/Script/GbxGame.ProjectileStatics:SpawnProjectile_Const',
    '/Script/GbxGame.ProjectileStatics:SpawnProjectile_Source_Const',
    '/Script/GbxGame.ProjectileStatics:SpawnProjectile_ThrowAtLocation_Const',
    '/Script/GbxSpawn.SpawnBlueprintLibrary:GbxSpawnActorAtTransform',
    '/Script/GbxGame.projectile:StartFuse',
    '/Script/GbxGame.projectile:SetVelocity',
    '/Script/GbxGame.projectile:SetCustomGravityScale',
    '/Script/OakGame.ModularGrenade:ActivateGrenade',
    '/Script/OakGame.ModularGrenade:SpawnChildGrenade',
    '/Script/OakGame.ModularGrenade:PayloadBeginExplodeCallback',
    '/Script/OakGame.ModularGrenade:PayloadExplodeCallback',
    '/Script/GbxGame.GbxProjectileMovementComponent:ScaleVelocity',
    '/Script/Engine.Actor:K2_GetComponentsByClass')
CLASSES=('/Script/OakGame.ModularGrenade','/Script/GbxGame.GbxProjectileMovementComponent',
         '/Script/Engine.ProjectileMovementComponent','/Script/OakGame.GrenadeGadget',
         '/Script/GbxGame.GbxBodyData','/Script/GbxGame.InventoryBodyData')
STRUCTS=('/Script/GbxGame.ProjectileDef','/Script/OakGame.ModularGrenadeDef',
         '/Script/GbxSpawn.GbxActorDef',
         '/Script/OakGame.OakProjectileDef','/Script/GbxSpawn.GbxActorData',
         '/Script/GbxGame.GbxActorScriptOwnerState','/Script/GbxGame.InventoryIdentity',
         '/Script/GbxGame.GbxItem')


def snapshot(ctx,obj,schema=None,depth=0,budget=None):
    """Bounded ordinary reflected values; never dereference custom def pointers.

    Handle names/type IDs identify a source even when not resolved. Addresses
    are used only as a boolean resolution check, never stored or dereferenced.
    No generic _get_field, repr(handle), native function or property write.
    """
    if budget is None:budget=[256]
    if obj is None:return None
    if schema is None:schema=obj.Class
    data={}
    for index,prop in enumerate(schema._properties()):
        if index>=96 or budget[0]<=0:
            data['_truncated']=True;break
        name=str(prop.Name);kind=type(prop).__name__
        # Inline definitions can require a custom struct cast. Leave them out.
        # Def pointers below expose ONLY SDK wrapper metadata, not their data.
        if kind=='ZGbxInlineStructProperty':
            data[name]={'skipped':kind};continue
        scalar=kind in ('ZBoolProperty','ZByteProperty','ZEnumProperty','ZFloatProperty',
            'ZDoubleProperty','ZIntProperty','ZInt64Property','ZUInt32Property',
            'ZUInt64Property','ZUInt16Property','ZInt16Property','ZNameProperty','ZStrProperty')
        supported=scalar or kind in ('ZObjectProperty','ZSoftObjectProperty','ZGameDataHandleProperty','ZGbxDefPtrProperty')
        struct=getattr(prop,'Struct',None) if kind=='ZStructProperty' else None
        if struct is not None and depth<2:supported=True
        if not supported:continue
        budget[0]-=1
        try:
            value=getattr(obj,name)
            if kind=='ZGbxDefPtrProperty':
                data[name]={'kind':kind,'name':str(value._name),
                            'declared_type':ctx._path(getattr(prop,'Struct',None)),
                            'actual_type':ctx._path(value._type),
                            'resolved':bool(value._get_address()),
                            'definition_fields_read':False}
            elif kind=='ZGameDataHandleProperty':
                expected=int(prop.TypeHandle)
                actual=int(value._type_handle)
                record={'kind':kind,'name':str(value._name),'type_handle':actual,
                        'property_type_handle':expected,'type_matches':actual==expected}
                # A valid SDK handle has _get_address, not an _instance API.
                record['resolved']=bool(value._get_address()) if actual==expected else False
                data[name]=record
            elif scalar:
                if kind=='ZBoolProperty':data[name]=bool(value)
                elif kind in ('ZNameProperty','ZStrProperty','ZEnumProperty'):data[name]=str(value)[:256]
                elif kind in ('ZFloatProperty','ZDoubleProperty'):data[name]=float(value)
                else:data[name]=int(value)
            elif kind=='ZStructProperty':
                data[name]={'struct':ctx._path(struct),'values':snapshot(ctx,value,struct,depth+1,budget)}
            else:data[name]=ctx._path(value)
        except Exception as error:data[name]={'error':str(error)[:300]}
    return data


def collect(ctx,projectile=None,gadget=None):
    result={'scope':'Read-only reflection and object paths; no gameplay calls, actor creation, hooks or property writes.',
            'definitions':[],'objects':[],'errors':[]}
    queue=[];seen=set()

    def inspect(kind,path):
        if path in seen:return
        seen.add(path)
        try:
            obj=unrealsdk.find_object(kind,path)
            if obj is None:raise LookupError('Not loaded')
            data={'kind':kind,'path':path,'properties':[],'fields':[]}
            for index,prop in enumerate(obj._properties()):
                if index>=180:break
                row={'name':str(prop.Name),'type':type(prop).__name__}
                for key in ('PropertyFlags','TypeHandle'):
                    try:row[key]=int(getattr(prop,key))
                    except (AttributeError,TypeError,ValueError):pass
                for key in ('Struct','PropertyClass','Enum'):
                    try:
                        value=getattr(prop,key)
                        if value is not None:
                            ref=ctx._path(value);row[key]=ref
                            if key=='Struct' and ref.startswith('/Script/'):queue.append(('ScriptStruct',ref))
                    except (AttributeError,TypeError):pass
                data['properties'].append(row)
            try:
                for index,field in enumerate(obj._fields()):
                    if index>=180:break
                    data['fields'].append(str(field.Name))
            except AttributeError:pass
            result['definitions'].append(data)
        except Exception as error:result['errors'].append({'path':path,'error':str(error)})

    for path in FUNCTIONS:inspect('Function',path)
    for path in CLASSES:inspect('Class',path)
    for path in STRUCTS:inspect('ScriptStruct',path)
    # Capture initialized source instances while they are alive. No wrappers
    # escape this call or persist across travel; the report is JSON primitives.
    result['source_snapshots']=[]
    for role,obj in (('initialized_projectile',projectile),('owned_gadget',gadget)):
        if obj is None:continue
        try:
            record={'role':role,'path':ctx._path(obj),'class':ctx._path(obj.Class),
                    'values':snapshot(ctx,obj)}
            body=getattr(obj,'BodyData',None)
            if body is not None:
                record['body_data']={'path':ctx._path(body),'class':ctx._path(body.Class),
                                     'values':snapshot(ctx,body)}
                inspect('Class',ctx._path(body.Class))
            result['source_snapshots'].append(record)
        except Exception as error:result['errors'].append({'role':role,'error':str(error)})
    result['live_initialization']=[]
    # Include loaded authored projectile classes, not just the native base.
    for kind in ('ModularGrenade','GbxProjectileMovementComponent'):
        try:
            values=unrealsdk.find_all(kind,exact=False)
            for obj in values[:32]:
                path=ctx._path(obj);cls_path=ctx._path(obj.Class)
                result['objects'].append({'path':path,'class':cls_path})
                inspect('Class',cls_path)
                if kind=='ModularGrenade' and not path.startswith('/Script/') and len(result['live_initialization'])<8:
                    # Also inspect stalled actors: source capture used to wait
                    # for a valid launch position and therefore omitted them.
                    record={'path':path,'class':cls_path,'values':snapshot(ctx,obj)}
                    root=getattr(obj,'RootComponent',None)
                    record['root_component']=ctx._path(root)
                    body=getattr(obj,'BodyData',None)
                    record['body_data']=ctx._path(body)
                    if root is not None:
                        record['root_values']=snapshot(ctx,root,budget=[128])
                    if body is not None:
                        record['body_values']=snapshot(ctx,body,budget=[128])
                    # Read only the base schema exposed by this resolved SDK
                    # wrapper. No casting to ProjectileDef/ModularGrenadeDef
                    # and no child-specific fields or pointer dereferences.
                    try:
                        ref=obj.GbxActorData.GbxActorDef
                        declared=ctx._path(ref._type)
                        record['actor_definition']={'name':str(ref._name),'type':declared,
                                                    'resolved':bool(ref._get_address())}
                        if declared=='/Script/GbxSpawn.GbxActorDef' and ref._get_address():
                            schema=unrealsdk.find_object('ScriptStruct',declared)
                            if schema is not None:
                                record['actor_definition']['base_values']=snapshot(ctx,ref,schema,budget=[128])
                    except Exception as error:record['definition_read_error']=str(error)
                    result['live_initialization'].append(record)
            result[kind+'_count']=len(values)
        except Exception as error:result['errors'].append({'class':kind,'error':str(error)})
    # Native spawning may depend on authored default-object references. Read
    # only reflected UObject fields, avoiding typed definition-pointer casts.
    for record in list(result['objects']):
        if 'Default__' not in record['path']:continue
        try:
            obj=unrealsdk.find_object('Object',record['path'])
            refs={}
            for prop in obj.Class._properties():
                name=str(prop.Name)
                if type(prop).__name__ not in ('ZObjectProperty','ZSoftObjectProperty'):continue
                if not any(term in name.lower() for term in ('grenade','projectile','movement','delivery','payload','body','mesh')):continue
                try:refs[name]=ctx._path(getattr(obj,name))
                except Exception as error:refs[name]={'error':str(error)}
            record['authored_references']=refs
        except Exception as error:record['read_error']=str(error)
    for kind,path in list(dict.fromkeys(queue))[:32]:inspect(kind,path)
    return result
