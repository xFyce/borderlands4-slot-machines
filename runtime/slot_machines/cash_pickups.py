"""Transient slot winnings: BL4 money mesh, collected through verified wallet adapter."""
import math
import traceback
from types import SimpleNamespace

import unrealsdk
from unrealsdk.hooks import Block, Type, add_hook, remove_hook

MESH = "/Game/World/Interactives/Pickups/Money/SM_Money_Stack_Single.SM_Money_Stack_Single"
# Captured from a real money pickup in slots_preview_20261010_075014_241102.
GLOW = '/Game/Pickups/_Shared/Effects/Systems/NS_FX_Item_Loot_Beams.NS_FX_Item_Loot_Beams'
FUNCTION = "/Script/Engine.Actor:FlushNetDormancy"
HOOK = "BL2SlotMachinePreview.cash_pickups"


class CashPickups:
    def __init__(self, context, shared=False):
        self.ctx = context
        self.shared = shared
        self.records = []
        self.hooked = False
        self.persistent = False
        self.idle_resource = None

    def prepare(self, persistent=False):
        # Spawn pins the observer for the machine lifetime. Interaction and
        # payout callbacks then never need to modify the active SDK hook list.
        self.persistent = self.persistent or persistent
        if self.shared:
            if not self.ctx._experience_service().hooked:
                raise RuntimeError('Shared interaction/cash timer is not registered')
            self.hooked = True
            return
        if not self.hooked:
            remove_hook(FUNCTION, Type.PRE, HOOK)
            if add_hook(FUNCTION, Type.PRE, HOOK, self.tick) is False:
                raise RuntimeError('Cash observer hook rejected')
            self.hooked = True

    def _destroy(self, record):
        recycled=False
        if self.persistent and record.get('collected') and self.idle_resource is None and record.get('resource') is not None:
            try:
                ctx=self.ctx
                system=ctx._default('/Script/Engine.Default__KismetSystemLibrary')
                ctx._call(system,'K2_ClearTimer',Object=record['actor'],FunctionName='FlushNetDormancy')
                ctx._call(record['actor'],'SetActorHiddenInGame',bNewHidden=True)
                if record.get('glow') is not None:ctx._call(record['glow'],'Deactivate')
                self.idle_resource=record['resource']
                recycled=True
            except Exception:pass
        if not recycled:
            try:self.ctx._call(record['actor'],'K2_DestroyActor')
            except Exception:pass
        if record in self.records:self.records.remove(record)
        if not self.records and self.hooked and not self.persistent:
            if not self.shared:remove_hook(FUNCTION,Type.PRE,HOOK)
            self.hooked=False

    def collect(self, record, stage="slot cash collected"):
        if record.get('collected'):
            return False
        # Commit wallet first; failed writes leave the collectible available.
        credit = self.ctx._change_cash(record['player'], record['amount'], stage)
        record['collected'] = True
        receipt = record['receipt']
        receipt.update(credit=credit, after=credit['after'], collection='collected')
        receipt['pickup']['state'] = 'collected'
        if stage == 'slot cash collected':
            # Keep the actor's timer alive (mesh hidden) until the tray closes.
            try:
                self.ctx._call(record['actor'], 'SetActorHiddenInGame', bNewHidden=True)
            except Exception as error:
                receipt['pickup']['hide_note'] = str(error)
            self.ctx._experience_service().sound('cash_pickup')
            self.ctx._maybe_close_front_tray()
        else:
            self._destroy(record)
        print(f"[Slots Preview] Collected {record['amount']:,} Cash.")
        self.ctx._report['cash_collection'] = receipt
        self.ctx._request_report()
        return True

    def clear(self):
        self.persistent = False
        resource,self.idle_resource=self.idle_resource,None
        if resource is not None:
            try:self.ctx._call(resource['actor'],'K2_DestroyActor')
            except Exception:pass
        # Earned winnings survive explicit machine removal/respawn/mod disable.
        # If wallet credit fails, keep the object and observer for another attempt.
        for record in self.records[:]:
            if not record.get('collected'):
                self.collect(record, "uncollected slot cash redeemed on cleanup")
            self._destroy(record)
        if not self.records and self.hooked:
            if not self.shared:remove_hook(FUNCTION, Type.PRE, HOOK)
            self.hooked = False

    def nearby(self, record, manual=False):
        if record.get('collected'):
            return False
        ctx = self.ctx
        pc = ctx.get_pc(possibly_loading=True)
        if pc is None or pc != record['player'] or pc.Pawn is None:
            return False
        if not manual and ctx._clock() - record['born'] < 1.0:
            return False
        if not record.get('visible'):
            return False
        if self.ctx._tray_state is None or self.ctx._tray_state['state'] != 'open':
            return False
        if manual:
            return self._aim_distance(pc,record) is not None
        p = ctx._call(pc.Pawn, 'K2_GetActorLocation')
        x, y, z = record['position']
        return math.hypot(float(p.X)-x, float(p.Y)-y) <= 65 and abs(float(p.Z)-z) <= 130

    def _aim_distance(self, pc, record):
        """Small camera-ray target on the cash, independent of cabinet facing."""
        ctx=self.ctx
        camera=getattr(pc,'PlayerCameraManager',None)
        if camera is not None:
            origin=ctx._call(camera,'GetCameraLocation')
            rotation=ctx._call(camera,'GetCameraRotation')
            origin=[float(getattr(origin,k)) for k in ('X','Y','Z')]
        else:
            origin=ctx._call(pc.Pawn,'K2_GetActorLocation')
            origin=[float(getattr(origin,k)) for k in ('X','Y','Z')]
            origin[2]+=float(getattr(pc.Pawn,'BaseEyeHeight',0))
            rotation=ctx._call(pc,'GetControlRotation')
        yaw=math.radians(float(rotation.Yaw));pitch=math.radians(float(rotation.Pitch))
        direction=(math.cos(pitch)*math.cos(yaw),math.cos(pitch)*math.sin(yaw),math.sin(pitch))
        # The mesh is scaled to 0.8, independent of the cabinet size. Give the
        # stack a forgiving 18 cm target, rather than the whole machine cone.
        center=list(record['position']);center[2]+=2.
        delta=[center[i]-origin[i] for i in range(3)]
        if not all(math.isfinite(v) for v in origin+delta+list(direction)):return None
        distance2=sum(v*v for v in delta)
        if distance2>225**2:return None
        along=sum(delta[i]*direction[i] for i in range(3))
        if along<0:return None
        if max(0.,distance2-along*along)>18**2:return None
        return along

    def targeted(self):
        return next((r for r in self.records if self.nearby(r,manual=True)),None)

    def tick(self, obj, args, ret, fn):
        record = next((r for r in self.records if r['actor'] == obj), None)
        if record is None:
            return None
        record['ticks'] += 1
        try:
            if record.get('collected'):
                if self.ctx._advance_payout_tray():
                    self._destroy(record)
                    self.ctx._request_report()
                return Block
            if not record.get('error'):
                record['receipt']['pickup']['callback_confirmed'] = True
                ready = self.ctx._advance_payout_tray()
                pose = self.ctx._tray_state.get('bone_pose',[0,0,0,0,0,0,1])
                if record.get('pose') != pose:
                    transform = self.transform(record['origin'],record['yaw'],record['scale'],pose)
                    self.ctx._call(record['actor'],'K2_SetActorTransform',NewTransform=transform,bSweep=False,bTeleport=True)
                    record['pose'] = list(pose)
                    p = transform.Translation
                    record['position'] = (float(p.X),float(p.Y),float(p.Z))
                    record['receipt']['pickup']['position'] = list(record['position'])
                prefs=self.ctx._experience_service().settings
                if ready and not record.get('ready_notified'):
                    record['ready_notified']=True
                    record['receipt']['pickup']['state'] = 'waiting for E or proximity' if prefs['auto_cash'] else 'waiting for E/F9'
                    self.ctx._report['cash_result'] = record['receipt']
                    self.ctx._request_report()
                    print('[Slots Preview] Tray open. Aim at the cash stack and press E to pick it up.'+
                          (' Walking close to the tray also collects it.' if prefs['auto_cash'] else ''))
                opened=self.ctx._tray_state.get('opened_at',self.ctx._clock())
                if ready and prefs['auto_cash'] and self.ctx._clock()-opened>=.75 and self.nearby(record):
                    self.collect(record)
        except Exception:
            # Avoid repeating a failed credit every 100 ms; explicit collect can retry.
            if not record.get('error'):
                record['error'] = traceback.format_exc()
                record['receipt']['pickup']['error'] = record['error']
                self.ctx._report['cash_pickup_error'] = record['error']
                self.ctx._request_report()
        return Block

    def transform(self, origin, yaw, scale, pose):
        ctx = self.ctx
        local = ctx._rotate_vector(pose[3:], [10,0,64])
        local = [(local[i]+pose[i])*scale for i in range(3)]
        angle = math.radians(yaw)
        x = origin[0]+local[0]*math.cos(angle)-local[1]*math.sin(angle)
        y = origin[1]+local[0]*math.sin(angle)+local[1]*math.cos(angle)
        z = origin[2]+local[2]
        result = ctx._transform(x,y,z,yaw)
        q = ctx._quat_multiply([0,0,math.sin(angle/2),math.cos(angle/2)],pose[3:])
        result.Rotation = unrealsdk.make_struct('Quat',X=q[0],Y=q[1],Z=q[2],W=q[3])
        return result

    def _glow_asset(self, info):
        try:asset=unrealsdk.find_object('NiagaraSystem',GLOW)
        except ValueError:asset=None
        if asset is not None:return asset
        load_info=info.setdefault('asset_load',{'asset':GLOW})
        loader=getattr(unrealsdk,'load_package',None)
        if callable(loader):
            try:loader(GLOW.split('.',1)[0])
            except Exception as error:load_info['package_note']=str(error)
        try:asset=unrealsdk.find_object('NiagaraSystem',GLOW)
        except ValueError:asset=None
        if asset is not None:return asset
        return self._registry_mesh(load_info,GLOW,'/Script/Niagara.NiagaraSystem')

    def _glow_parameters(self, fx, info):
        """Restore captured cash-beam settings using public typed setters."""
        ctx=self.ctx
        settings={'User.Color':('LinearColor',(1.,1.,0.,1.)),
                  'User.Color2':('LinearColor',(.43415364623069763,.005964858457446098,0.,1.)),
                  'User.Height':('Float',85.),'User.ScanlineTile':('Float',1.),
                  'User.HasAnointment':('Bool',False),'User.HasFirmware':('Bool',False),
                  'User.IsLegendary':('Bool',False)}
        info['parameters']={}
        for variable,(kind,value) in settings.items():
            name=next((n for n in ('SetVariable'+kind,'SetNiagaraVariable'+kind)
                       if callable(getattr(fx,n,None))),None)
            if name is None:raise RuntimeError('Native glow setter unavailable: '+kind)
            props={str(p.Name):p for p in getattr(fx,name).func._properties()}
            if 'InVariableName' not in props or type(props['InVariableName']).__name__ not in ('ZNameProperty','ZStrProperty'):
                raise RuntimeError('Unexpected native glow variable-name signature: '+name)
            p=props.get('InValue')
            if p is None or type(p).__name__!={'LinearColor':'ZStructProperty','Float':'ZFloatProperty','Bool':'ZBoolProperty'}[kind]:
                raise RuntimeError('Unexpected native glow value signature: '+name)
            if kind=='LinearColor':
                if ctx._path(p.Struct)!='/Script/CoreUObject.LinearColor':
                    raise RuntimeError('Unexpected native glow color structure')
                value=unrealsdk.make_struct('LinearColor',R=value[0],G=value[1],B=value[2],A=value[3])
            ctx._call(fx,name,InVariableName=variable,InValue=value)
            info['parameters'][variable]=settings[variable][1]

    def add_glow(self, actor, mesh, receipt):
        """Use the captured cash effect directly, with no world-wide donor scans."""
        ctx = self.ctx
        info = {'state':'unavailable','note':'No loaded money pickup effect found'}
        receipt['pickup']['glow'] = info
        try:
            # Captured exact native cash beam and typed parameters are sufficient;
            # finding a live donor scanned every pickup and mesh in the map.
            info['source']='captured native money pickup asset/settings'
            asset=self._glow_asset(info)
            if asset is None:raise RuntimeError('Captured cash glow asset could not be loaded')
            info['asset']=ctx._path(asset)
            fx = ctx._call(actor,'AddComponentByClass',
                           Class=unrealsdk.find_class('/Script/Niagara.NiagaraComponent'),
                           bManualAttachment=False,RelativeTransform=ctx._transform(),bDeferredFinish=False)
            if fx is None:
                raise RuntimeError('Niagara component creation returned None')
            ctx._call(fx,'SetAutoActivate',bNewAutoActivate=False)
            names = [str(p.Name) for p in fx.SetAsset.func._properties()]
            asset_name = next((n for n in names if n.lower() in ('inasset','newasset','asset')),None)
            if asset_name is None:
                raise RuntimeError('No reflected Niagara SetAsset asset parameter: '+str(names))
            ctx._call(fx,'SetAsset',**{asset_name:asset},bResetExistingOverrideParameters=True,
                      optional=('bResetExistingOverrideParameters',))
            self._glow_parameters(fx,info)
            ctx._call(fx,'Activate',bReset=True)
            info.pop('note',None)
            info['state'] = 'native asset active'
            return fx
        except Exception:
            info.update(state='failed',error=traceback.format_exc())
            return None

    def _registry_queries(self, registry, info):
        """Expose interface functions omitted from normal SDK class lookup."""
        names=('GetAssetByObjectPath','K2_GetAssetByObjectPath')
        methods={name:getattr(registry,name,None) for name in names}
        if any(callable(fn) for fn in methods.values()):return registry
        ctx=self.ctx
        info['registry_object']=ctx._path(registry)
        cls=registry.Class
        info['registry_class']=ctx._path(cls)
        interface=unrealsdk.find_class('/Script/AssetRegistry.AssetRegistry')
        implements=getattr(cls,'_implements',None)
        binder=getattr(unrealsdk.unreal,'BoundFunction',None)
        if interface is None or not callable(implements) or not callable(binder):
            # An available normal query can still work without interface binding.
            if any(callable(fn) for fn in methods.values()):return registry
            raise RuntimeError('SDK cash registry interface binding unavailable')
        if not implements(interface):
            raise RuntimeError('Returned cash registry does not implement AssetRegistry')
        info['registry_interface_verified']=True
        bindings=info.setdefault('registry_interface_bindings',{})
        for name in names:
            if callable(methods[name]):continue
            function=None
            # UE function paths use ':'; some SDK builds accept the manifest '.' form.
            for separator in (':','.'):
                path='/Script/AssetRegistry.AssetRegistry'+separator+name
                try:function=unrealsdk.find_object('Function',path)
                except ValueError:continue
                if function is not None:break
            if function is None:
                bindings[name]='reflected interface function unavailable'
                continue
            if ctx._path(function.Outer)!='/Script/AssetRegistry.AssetRegistry':
                raise RuntimeError('Unexpected cash query function owner: '+name)
            # Public SDK binding, on the verified implementing UObject. This
            # avoids casting the implementation or editing any engine objects.
            methods[name]=binder(function,registry)
            bindings[name]=ctx._path(function)
        return SimpleNamespace(**methods)

    def _registry_mesh(self, info, asset_path=MESH, expected_class='/Script/Engine.StaticMesh'):
        """Load the individual cooked asset through reflected AssetRegistry APIs."""
        ctx=self.ctx
        helpers=ctx._default('/Script/AssetRegistry.Default__AssetRegistryHelpers')
        if helpers is None:raise RuntimeError('AssetRegistryHelpers unavailable')
        registry=ctx._call(helpers,'GetAssetRegistry')
        if registry is None:raise RuntimeError('Native asset registry unavailable')
        registry=self._registry_queries(registry,info)
        signatures=info.setdefault('registry_signatures',{})
        def props(obj, name):
            method=getattr(obj,name,None)
            if not callable(method):raise RuntimeError(name+' unavailable')
            result={str(p.Name):p for p in method.func._properties()}
            signatures[name]={n:type(p).__name__ for n,p in result.items()}
            return result
        def require(parameters, name, kind, struct=None):
            p=parameters.get(name)
            if p is None or type(p).__name__!=kind:
                raise RuntimeError('Unexpected reflected cash-loader parameter: '+name)
            if struct is not None and ctx._path(p.Struct)!=struct:
                raise RuntimeError('Unexpected reflected cash-loader structure: '+name)
        # BL4 ships both the FName query and the newer FSoftObjectPath query.
        # Never put a Python string into a structure/soft object property.
        query=getattr(registry,'GetAssetByObjectPath',None)
        parameters=props(registry,'GetAssetByObjectPath') if callable(query) else {}
        if 'ObjectPath' in parameters and type(parameters['ObjectPath']).__name__=='ZNameProperty':
            require(parameters,'bIncludeOnlyOnDiskAssets','ZBoolProperty')
            data=ctx._call(registry,'GetAssetByObjectPath',ObjectPath=asset_path,bIncludeOnlyOnDiskAssets=True)
            info['registry_query']='GetAssetByObjectPath (FName)'
        else:
            parameters=props(registry,'K2_GetAssetByObjectPath')
            require(parameters,'ObjectPath','ZStructProperty','/Script/CoreUObject.SoftObjectPath')
            require(parameters,'bIncludeOnlyOnDiskAssets','ZBoolProperty')
            require(parameters,'bSkipARFilteredAssets','ZBoolProperty')
            system=ctx._default('/Script/Engine.Default__KismetSystemLibrary')
            make=props(system,'MakeSoftObjectPath')
            require(make,'PathString','ZStrProperty')
            path=ctx._call(system,'MakeSoftObjectPath',PathString=asset_path)
            data=ctx._call(registry,'K2_GetAssetByObjectPath',ObjectPath=path,
                           bIncludeOnlyOnDiskAssets=True,bSkipARFilteredAssets=False)
            info['registry_query']='K2_GetAssetByObjectPath (native FSoftObjectPath)'
        valid=props(helpers,'IsValid')
        require(valid,'InAssetData','ZStructProperty','/Script/CoreUObject.AssetData')
        if not ctx._call(helpers,'IsValid',InAssetData=data):
            raise RuntimeError('Cooked asset registry has no entry for the exact requested asset')
        info['registered_package']=str(data.PackageName)
        info['registered_asset']=str(data.AssetName)
        load=props(helpers,'GetAsset')
        require(load,'InAssetData','ZStructProperty','/Script/CoreUObject.AssetData')
        # Unlike load_package, GetAsset resolves/loads the asset itself.
        mesh=ctx._call(helpers,'GetAsset',InAssetData=data)
        if mesh is None:raise RuntimeError('Asset registry found requested asset but GetAsset returned None')
        if ctx._path(mesh.Class)!=expected_class:
            raise RuntimeError('Asset registry returned a non-'+expected_class.rsplit('.',1)[-1]+' asset')
        info['resolved_asset']=ctx._path(mesh)
        return mesh

    def resolve_mesh(self):
        """Load the exact authored asset when absent; do not depend on drops."""
        info={'asset':MESH,'package':MESH.split('.',1)[0],'state':'checking loaded cash mesh'}
        self.ctx._report['cash_asset']=info
        def find():
            try:return unrealsdk.find_object('StaticMesh',MESH)
            except ValueError:return None  # Missing objects raise on the real SDK.
        mesh=find()
        if mesh is not None:
            info['state']='cash mesh already loaded'
            return mesh
        loader=getattr(unrealsdk,'load_package',None)
        notes=[]
        try:
            if callable(loader):
                info['state']='loading exact cash mesh package'
                try:loader(info['package'])
                except Exception as error:notes.append('package load: '+str(error))
            else:notes.append('SDK package loader unavailable')
            mesh=find()
            if mesh is not None:
                info['state']='cash mesh package loaded and verified'
                return mesh
            info['package_notes']=notes or ['Package load returned without resolving StaticMesh']
            info['state']='loading individual cash asset through native registry'
            mesh=self._registry_mesh(info)
            info['state']='cash mesh asset registry loaded and verified'
            return mesh
        except Exception as error:
            message='; '.join(notes+[str(error)])
            info.update(state='cash mesh load failed',error=message)
            raise RuntimeError('Cannot load physical cash mesh: '+message) from error

    def _build_resource(self,pc,transform):
        ctx=self.ctx
        mesh=self.resolve_mesh()
        actor=None
        try:
            gs = ctx._default('/Script/Engine.Default__GameplayStatics')
            actor = ctx._call(gs, 'BeginDeferredActorSpawnFromClass', WorldContextObject=pc,
                              ActorClass=unrealsdk.find_class('/Script/Engine.Actor'),
                              SpawnTransform=transform, CollisionHandlingOverride=1, Owner=None,
                              TransformScaleMethod=0, optional=('TransformScaleMethod',))
            if actor is None:
                raise RuntimeError('Cash actor spawn returned None')
            finished = ctx._call(gs, 'FinishSpawningActor', Actor=actor, SpawnTransform=transform,
                                 TransformScaleMethod=0, optional=('TransformScaleMethod',))
            if finished is None:
                raise RuntimeError('Cash actor finish returned None')
            actor = finished
            ctx._call(actor, 'SetReplicates', bInReplicates=False)
            ctx._call(actor, 'SetActorHiddenInGame', bNewHidden=True)
            component = ctx._call(actor, 'AddComponentByClass',
                                  Class=unrealsdk.find_class('/Script/Engine.StaticMeshComponent'),
                                  bManualAttachment=False, RelativeTransform=ctx._transform(), bDeferredFinish=False)
            if component is None:
                raise RuntimeError('Money mesh component returned None')
            if ctx._call(component, 'SetStaticMesh', NewMesh=mesh) is False:
                raise RuntimeError('Engine rejected money mesh')
            ctx._call(component, 'SetCollisionEnabled', NewType=0)
            ctx._call(component, 'SetRelativeScale3D', NewScale3D=ctx._vec(.8, .8, .8))
            ctx._call(actor, 'K2_SetActorTransform', NewTransform=transform, bSweep=False, bTeleport=True)
            info={'pickup':{}}
            glow=self.add_glow(actor,mesh,info)
            if glow is not None:ctx._call(glow,'Deactivate')
            return {'actor':actor,'component':component,'mesh':mesh,'glow':glow,
                    'glow_info':info['pickup']['glow']}
        except Exception:
            if actor is not None:
                try:ctx._call(actor,'K2_DestroyActor')
                except Exception:pass
            raise

    def prewarm(self,pc):
        if self.idle_resource is None:
            self.idle_resource=self._build_resource(pc,self.ctx._transform())
        self.ctx._report['cash_display_pool']='one hidden reusable actor per machine; no winnings/timer while parked'

    def spawn(self, pc, amount, receipt):
        ctx = self.ctx
        origin = ctx._call(ctx._actor, 'K2_GetActorLocation')
        rotation = ctx._call(ctx._actor, 'K2_GetActorRotation')
        yaw = float(rotation.Yaw)
        scale = ctx._model_height / 180
        origin = (float(origin.X),float(origin.Y),float(origin.Z))
        pose = [0,0,0,0,0,0,1]
        transform = self.transform(origin,yaw,scale,pose)
        x,y,z = transform.Translation.X,transform.Translation.Y,transform.Translation.Z
        actor = None
        record = None
        try:
            resource,self.idle_resource=self.idle_resource,None
            if resource is None:resource=self._build_resource(pc,transform)
            actor=resource['actor'];component=resource['component'];mesh=resource['mesh']
            ctx._call(actor,'K2_SetActorTransform',NewTransform=transform,bSweep=False,bTeleport=True)
            ctx._call(actor,'SetActorHiddenInGame',bNewHidden=False)
            self.prepare()
            record = {'actor':actor, 'component':component, 'mesh':mesh, 'player':pc,
                      'amount':amount, 'position':(x,y,z), 'born':ctx._clock(),
                      'receipt':receipt, 'ticks':0, 'collected':False, 'visible':True,
                      'origin':origin,'yaw':yaw,'scale':scale,'pose':pose}
            self.records.append(record)
            # Do not synchronously invoke a second hook while completing a reel
            # callback. Confirm this separate timer on its first real engine tick.
            receipt['pickup'] = {'actor':ctx._path(actor), 'mesh':MESH, 'position':[x,y,z],
                                 'amount':amount, 'state':'opening tray', 'collection_radius_cm':65,
                                 'manual_collection':'camera ray targets cash stack; fully open tray only',
                                 'manual_target_radius_cm':18,'manual_range_cm':225,
                                 'callback_confirmed':False, 'timer_function':FUNCTION,
                                 'motion':'follows drawer bone from opening start'}
            record['resource']=resource
            record['glow']=resource['glow']
            receipt['pickup']['glow']=dict(resource['glow_info'])
            if record['glow'] is not None:ctx._call(record['glow'],'Activate',bReset=True)
            ctx._begin_payout_tray()
            ctx._experience_service().sound('cash_drop')
            system = ctx._default('/Script/Engine.Default__KismetSystemLibrary')
            ctx._call(system, 'K2_SetTimer', Object=actor, FunctionName='FlushNetDormancy',
                      Time=.001, bLooping=True, bMaxOncePerFrame=True,
                      InitialStartDelay=.001, InitialStartDelayVariance=0)
            return record
        except Exception:
            if record is not None:
                self._destroy(record)
            else:
                if actor is not None:
                    try:
                        ctx._call(actor, 'K2_DestroyActor')
                    except Exception:
                        pass
                if not self.records and self.hooked and not self.persistent:
                    if not self.shared:remove_hook(FUNCTION, Type.PRE, HOOK)
                    self.hooked = False
            raise
