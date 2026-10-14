"""Bake original BL2 mask/panner inputs onto the existing cabinet UV surfaces."""
import ast,gzip,hashlib,json,math,shutil
from pathlib import Path
import numpy as np
from PIL import Image,ImageFilter,ImageDraw

def prepare_original_lighting(root,textures):
    
    HERE=Path(__file__).parent
    STAGE=Path(root)
    GAME=STAGE
    TEX=Path(textures)
    LIGHTS=STAGE/'lights143';LIGHTS.mkdir(parents=True,exist_ok=True)
    g=json.loads((GAME/'geometry.json').read_text(encoding='utf-8'))
    groups={}
    for off in range(0,len(g['triangles']),3):
     tri=g['triangles'][off:off+3];bones={g['bone_for_vertex'][i] for i in tri}
     bone=next(iter(bones)) if len(bones)==1 else -1
     groups.setdefault(str(bone),[]).extend(tri)
    old=dict(geometry_sha=hashlib.sha256(json.dumps(g,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
     source_file_sha=hashlib.sha256((GAME/'geometry.json').read_bytes()).hexdigest(),body_groups=groups)
    tree=ast.parse((HERE/'cabinet_glow_template.py').read_text(encoding='utf-8'))
    scope={};exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('clip_convex','clipped_mesh')],type_ignores=[]),'UV interpolation helpers','exec'),scope)
    # Coordinates remain in the original 1024-square BL2 UV atlas.
    regions=[
     ('crown_left',(243,18,278,38),4,'glass'),('crown_middle',(292,18,327,38),4,'glass'),('crown_right',(341,18,376,38),4,'glass'),
     ('lower_left',(243,243,280,261),4,'glass'),('lower_right',(339,243,377,261),4,'glass'),
     ('below_reels',(173,595,305,612),2,'strip'),
     ('above_reels_left',(399,591,438,604),4,'strip'),('above_reels_middle',(443,591,486,604),4,'strip'),('above_reels_right',(491,591,531,604),4,'strip'),
     ('tray_front_lamp',(417,629,455,639),4,'tray')]
    recipe=json.loads((HERE/'bl2_lighting_recipe.json').read_text(encoding='utf-8'))
    bulbs=[]
    for index,(x,y) in enumerate(recipe['SIGN_BULBS']):bulbs.append((f'sign_{index:03}',x,y,1.7,index))
    for index,(x,y) in enumerate([(870,24.5),(864,40),(866,60),(871.5,77.5),(884,93),(905,94.5),(905,108),(885.5,109)]):bulbs.append((f'arrow_{index:02}',x,y,2.5,index+100))
    for row,(y,xs) in enumerate(recipe['BULB_ROWS']):
     for index,x in enumerate(xs):bulbs.append((f'row_{row}_{index:02}',x,y,2.1,index+row*30+200))
    bulb_info={}
    for name,x,y,r,index in bulbs:
     box=(x-r-0.5,y-r-0.5,x+r+0.5,y+r+0.5)
     regions.append((name,box,4,'bulbs'))
     # Each actual source bulb gets its own repeatable phase and pulse rate.
     # Uniform masks no longer make an entire letter or sign pulse together.
     rng=np.random.default_rng(14300+index)
     bulb_info[name]=dict(center=(x,y),radius=r,phase=float(rng.random()),cycles=int(rng.integers(3,7)),duty=float(rng.uniform(.16,.32)))
    layout={};shelves=[]
    for name,box,scale,kind in sorted(regions,key=lambda r:(r[1][3]-r[1][1])*r[2],reverse=True):
     w=math.ceil((box[2]-box[0])*scale);h=math.ceil((box[3]-box[1])*scale)
     shelf=next((s for s in shelves if s['height']>=h+4 and s['x']+w+4<=1024),None)
     if shelf is None:
      shelf=dict(x=0,y=sum(s['height'] for s in shelves),height=h+4);shelves.append(shelf)
     layout[name]=dict(box=box,scale=scale,kind=kind,x=shelf['x']+2,y=shelf['y']+2,w=w,h=h)
     shelf['x']+=w+4
    height=2**math.ceil(math.log2(max(s['y']+s['height'] for s in shelves)))
    assert height<=256,height
    SIZE=(1024,height)
    dif=Image.open(GAME/'SlotMachine_Dif.png').convert('RGB')
    mask=Image.open(TEX/'SlotMachine_Mask.png').convert('RGB')
    maskb=Image.open(TEX/'SlotMachineMaskB.png').convert('RGB')
    noise=np.asarray(Image.open(TEX/'Noise2_LiquidDev_Dif.png').convert('RGB')).astype(float)/255
    original_maskb=np.asarray(maskb)
    
    # Measured PS4 grille frequencies and phases; no footage is embedded.
    yy,xx=np.meshgrid(np.arange(80),np.arange(140),indexing='ij')
    lattice=[np.cos(2*np.pi*(fx*xx+fy*yy)+phase) for fx,fy,phase in [(0.150390625, 0.14453125, 1.6521698834574412), (0.1484375, -0.14453125, 0.8753921674320502)]]
    grille=np.clip(.60+.30*(lattice[0]+lattice[1]),.02,1)
    
    base=Image.new('RGB',SIZE)
    inputs={}
    for name,row in layout.items():
     box=row['box'];size=(row['w'],row['h'])
     patch=dif.transform(size,Image.Transform.EXTENT,box,Image.Resampling.BICUBIC)
     if row['kind']=='glass':
      grate=np.asarray(Image.fromarray(np.uint8(grille*255)).resize(size,Image.Resampling.BICUBIC)).astype(float)/255
      patch=Image.fromarray(np.uint8(np.asarray(patch).astype(float)*np.sqrt(grate[:,:,None])))
     base.paste(patch,(row['x'],row['y']))
     mb=np.asarray(maskb.resize((1024,1024),Image.Resampling.BILINEAR).transform(size,Image.Transform.EXTENT,box,Image.Resampling.BILINEAR)).astype(float)/255
     phase=np.asarray(mask.transform(size,Image.Transform.EXTENT,box,Image.Resampling.BILINEAR)).astype(float)/255
     uvx,uvy=np.meshgrid((np.arange(size[0])+.5)/size[0]*(box[2]-box[0])+box[0],(np.arange(size[1])+.5)/size[1]*(box[3]-box[1])+box[1])
     inputs[name]=(mb,phase,uvx/1024,uvy/1024,np.asarray(patch).astype(float)/255)
    base.save(LIGHTS/'base.png')
    
    def sample_wrap(image,u,v):
     h,w=image.shape[:2];xx=(u%1)*w;yy=(v%1)*h;x0=np.floor(xx).astype(int)%w;y0=np.floor(yy).astype(int)%h
     dx=(xx-np.floor(xx))[...,None];dy=(yy-np.floor(yy))[...,None]
     return (image[y0,x0]*(1-dx)+image[y0,(x0+1)%w]*dx)*(1-dy)+(image[(y0+1)%h,x0]*(1-dx)+image[(y0+1)%h,(x0+1)%w]*dx)*dy
    
    # Fit one common lookup transform to the three visible panel colors at t=0.
    # Colors still come from the shipped BL2 liquid texture, not an HSV rainbow.
    phases=np.array([inputs[n][1][:,:,1].mean() for n in ['crown_left','crown_middle','crown_right']])
    targets=np.array([[1,.9841,.7421],[1,.9755,.7755],[1,.7582,.8811]])
    best=None
    for scale in np.linspace(.3,1.5,25):
     for yoff in np.linspace(0,1,32,endpoint=False):
      offsets=np.linspace(0,1,64,endpoint=False)[:,None]
      colors=.12+.88*sample_wrap(noise,offsets+phases*scale,np.full((64,3),yoff))
      colors/=colors.max(2,keepdims=True)
      errors=((colors-targets)**2).mean((1,2));i=errors.argmin()
      if best is None or errors[i]<best[0]:best=(float(errors[i]),float(offsets[i,0]),float(yoff),float(scale))
    color_fit=dict(error=best[0],x_offset=best[1],y_offset=best[2],phase_scale=best[3])
    
    for frame in range(120):
     t=frame/120
     atlas=Image.new('RGB',SIZE)
     for name,row in layout.items():
      mb,phase,u,v,albedo=inputs[name]
      if row['kind']=='bulbs':
       info=bulb_info[name]
       cycle=(t*info['cycles']+info['phase'])%1
       distance=min(cycle,1-cycle)
       pulse=math.exp(-.5*(distance/(info['duty']*.40))**2)
       chase=.025+.975*pulse
       x,y=info['center'];radius=info['radius']
       # Smooth analytic lamp falloff at the original measured bulb centre avoids
       # magnifying the 256px mask's blurry block-shaped dots.
       radial=((u*1024-x)**2+(v*1024-y)**2)/radius**2
       strength=np.exp(-radial*2.8)*np.clip((1-radial)*4,0,1)*chase
       color=np.broadcast_to(np.array([1,.70,.22]),albedo.shape)
      elif row['kind']=='tray':
       strength=np.clip(mb[:,:,0]/.7,0,1)
       color=np.broadcast_to(np.array([1,.94,.62]),albedo.shape)
       # Original diffuse scratch/grate lines remain visible in the small lamp.
       strength*=np.clip(albedo.mean(2)/.55,.2,1)
      else:
       # Reuse BL2's multicolor liquid texture and moving blue-blob mask, rather
       # than HSV color changes and independently clipped diagonal triangles.
       color_noise=sample_wrap(noise,best[1]+phase[:,:,1]*best[3]+t,best[2]+t)
       color=.02+.98*color_noise
       color/=color.max(2,keepdims=True)
       grey=color.mean(2,keepdims=True)
       color=np.clip(grey+(color-grey)*1.45,0,1)
       highlight=sample_wrap(np.asarray(mask).astype(float)/255,u+t,v+t)[:,:,2]
       # White shapes add brightness as well as removing color. The previous
       # white lerp had almost the same luminance as the background and disappeared.
       color=color*.45+highlight[:,:,None]**1.25*.90
       strength=np.clip(mb[:,:,0]/.78,0,1)
       strength*=np.clip(albedo.mean(2)/.55,.25,1)
       if row['kind']=='glass':
        grate=np.asarray(Image.fromarray(np.uint8(grille*255)).resize((row['w'],row['h']),Image.Resampling.BICUBIC)).astype(float)/255
        strength*=grate
      rgb=np.clip(color*strength[:,:,None]*255,0,255).astype(np.uint8)
      atlas.paste(Image.fromarray(rgb),(row['x'],row['y']))
     atlas.save(LIGHTS/f'frame_{frame:02}.png')
    
    def combine(meshes):
     out={k:[] for k in ('vertices','normals','uv','tangents','triangles','face_normals','source_uv')}
     for mesh in meshes:
      offset=len(out['vertices'])
      for key in out:
       if key=='triangles':out[key].extend(i+offset for i in mesh[key])
       else:out[key].extend(mesh[key])
     return out
    def chunks(mesh,limit=512):
     result=[];triangles=[];used=set()
     def finish():
      if not triangles:return
      ids=sorted(set(triangles));remap={v:i for i,v in enumerate(ids)}
      r={k:[values[i] for i in ids] for k,values in mesh.items() if k!='triangles'}
      r['triangles']=[remap[i] for i in triangles];result.append(r)
     for off in range(0,len(mesh['triangles']),3):
      tri=mesh['triangles'][off:off+3]
      if len(used|set(tri))>limit:finish();triangles=[];used=set()
      triangles+=tri;used.update(tri)
     finish();return result
    
    new={k:old[k] for k in ('geometry_sha','source_file_sha','body_groups')}
    new['emission_chunks']={};counts={};max_error=0
    for bone in sorted(set(g['bone_for_vertex'])):
     if bone<0:continue
     indices=[i for i,b in enumerate(g['bone_for_vertex']) if b==bone];meshes=[]
     for name,row in layout.items():
      mesh=scope['clipped_mesh'](g,indices,[row['box']])
      if not mesh['triangles']:continue
      mesh['source_uv']=[uv[:] for uv in mesh['uv']]
      x0,y0,x1,y1=row['box']
      scalex=row['w']/(x1-x0);scaley=row['h']/(y1-y0)
      for uv in mesh['uv']:
       sx,sy=uv[0]*1024,uv[1]*1024
       uv[:]=[(row['x']+(sx-x0)*scalex)/SIZE[0],(row['y']+(sy-y0)*scaley)/SIZE[1]]
       recovered=((uv[0]*SIZE[0]-row['x'])/scalex+x0,(uv[1]*SIZE[1]-row['y'])/scaley+y0)
       max_error=max(max_error,abs(recovered[0]-sx),abs(recovered[1]-sy))
      counts.setdefault(name,{})[str(bone)]=len(mesh['triangles'])//3
      meshes.append(mesh)
     if meshes:new['emission_chunks'][str(bone)]=chunks(combine(meshes))
    assert 'tray_front_lamp' in counts,counts
    assert max_error<1e-9
    new['light_profile']={'frames':120,'fps':30,'intensity':6.0,'surface_offset_cm':.02,'atlas_size':SIZE,'layout':layout,'source':'Original BL2 masks/diffuse/liquid texture; PS4 reference grille. Reconstructed panning and independent bulb pulses, not original compiled UE3 shader.'}
    (STAGE/'glow_geometry.json.gz').write_bytes(gzip.compress(json.dumps(new,separators=(',',':')).encode(),mtime=0))
    return new
