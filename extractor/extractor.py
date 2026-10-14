"""Build local edition assets from an installed BL2 copy. Never changes game files."""
import argparse, json, math, os, struct, subprocess, sys, tempfile, threading, wave
from pathlib import Path
from array import array
import numpy as np
from PIL import Image
from assets import save_json, prebuild_audio, write_wave, SYMBOLS
from lighting_assets import prepare_original_lighting

HERE=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))

from pearl_symbol import apply_pearl_symbol




def native_working_path(path):
    """Old native tools use ANSI paths; prefer Windows' ASCII short alias."""
    path=str(Path(path).resolve())
    if path.isascii():return path
    if os.name=='nt':
        import ctypes
        buffer=ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path,buffer,len(buffer)) and buffer.value.isascii():
            return buffer.value
    raise RuntimeError('The bundled BL2 tools cannot use this folder name because Windows did not provide an ASCII short-path alias: '+path+'. Move the extracted package to a path using simple English letters, such as C:/BL4Mods/Slots. If the affected folder is your BL2 installation, choose an ASCII-only BL2 installation path too.')


def run(args,cwd=None):
    result=subprocess.run([str(a) for a in args],capture_output=True,text=True,encoding='utf-8',errors='replace',cwd=native_working_path(cwd) if cwd is not None else None,timeout=120,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError(result.stdout[-1800:]+result.stderr[-1800:])
    return result.stdout

def gltf_data(path):
    doc=json.loads(path.read_text(encoding='utf-8'));buffers=[(path.parent/b['uri']).read_bytes() for b in doc['buffers']]
    def access(index):
        a=doc['accessors'][index];view=doc['bufferViews'][a['bufferView']]
        dtype={5121:'u1',5123:'<u2',5125:'<u4',5126:'<f4'}[a['componentType']]
        width={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT4':16}[a['type']]
        value=np.frombuffer(buffers[view['buffer']],dtype=dtype,count=a['count']*width,
            offset=view.get('byteOffset',0)+a.get('byteOffset',0)).reshape(-1,width).copy()
        if a.get('normalized'):value=value.astype(float)/(255 if a['componentType']==5121 else 65535)
        return value
    return doc,access

def matrix(position,q):
    x,y,z,w=q;norm=math.sqrt(sum(v*v for v in q)) or 1;x,y,z,w=[v/norm for v in q]
    m=np.array([[1-2*y*y-2*z*z,2*x*y-2*z*w,2*x*z+2*y*w,position[0]],
        [2*x*y+2*z*w,1-2*x*x-2*z*z,2*y*z-2*x*w,position[1]],
        [2*x*z-2*y*w,2*y*z+2*x*w,1-2*x*x-2*y*y,position[2]],[0,0,0,1]],float)
    return m

def rotation_quat(m):
    # Stable conversion even for the reel's 180-degree rotations.
    r=m[:3,:3];trace=np.trace(r)
    if trace>0:
        s=math.sqrt(trace+1)*2;q=[(r[2,1]-r[1,2])/s,(r[0,2]-r[2,0])/s,(r[1,0]-r[0,1])/s,s/4]
    else:
        i=int(np.argmax(np.diag(r)));j=(i+1)%3;k=(i+2)%3;s=math.sqrt(max(0,1+r[i,i]-r[j,j]-r[k,k]))*2
        q=[0.,0.,0.,0.];q[i]=s/4;q[j]=(r[j,i]+r[i,j])/s;q[k]=(r[k,i]+r[i,k])/s;q[3]=(r[k,j]-r[j,k])/s
    if q[3]<0:q=[-x for x in q]
    return q

def convert_mesh_and_motion(folder,out):
    path=next(folder.rglob('Skel_SlotMachine.gltf'));doc,access=gltf_data(path)
    prim=doc['meshes'][0]['primitives'][0];attrs=prim['attributes']
    convert=np.array([[1,0,0],[0,0,-1],[0,1,0]],float)
    raw=access(attrs['POSITION'])@convert.T
    scale=180/(raw[:,2].max()-raw[:,2].min());centre=(raw.max(axis=0)+raw.min(axis=0))/2;centre[2]=raw[:,2].min()
    positions=(raw-centre)*scale;positions[:,1]*=-1
    normals=access(attrs['NORMAL'])@convert.T;normals[:,1]*=-1;tangents=access(attrs['TANGENT'])
    output_tangents=(tangents[:,:3]@convert.T)*[1,-1,1]
    uv=access(attrs['TEXCOORD_0']);joints=access(attrs['JOINTS_0']).astype(int);weights=access(attrs['WEIGHTS_0'])
    bones=joints[np.arange(len(joints)),np.argmax(weights,axis=1)]
    triangles=access(prim['indices']).reshape(-1).astype(int)
    # UModel's glTF has outward normals; the existing runtime reverses stored normals.
    g={'vertices':positions.tolist(),'uv':uv.tolist(),'triangles':triangles.tolist(),'normals':(-normals).tolist(),
        'tangents':[[*v,bool(t[3]<0)] for v,t in zip(output_tangents,tangents)],
        'bone_for_vertex':bones.tolist(),'source':'Extracted locally with UE Viewer from the player BL2 installation'}
    save_json(out/'geometry.json',g)
    nodes=doc['nodes'];skin_nodes=doc['skins'][0]['joints'];parents={child:i for i,n in enumerate(nodes) for child in n.get('children',[])}
    bind=[];parent_bones=[];world={}
    for i,index in enumerate(skin_nodes):
        node=nodes[index];p=convert@np.array(node.get('translation',[0,0,0]))*100
        q0=node.get('rotation',[0,0,0,1]);q=[*(convert@np.array(q0[:3])),q0[3]]
        m=matrix(p,q);parent=parents.get(index);m=world[parent]@m if parent in world else m
        world[index]=m;bind.append(m);parent_bones.append(skin_nodes.index(parent) if parent in skin_nodes else -1)
    data=next(folder.rglob('Anim_SlotMachine.psa')).read_bytes();offset=0;chunks={}
    while offset<len(data):
        name,_,size,count=struct.unpack_from('<20s3i',data,offset);offset+=32
        chunks[name.split(b'\0')[0].decode()]=(size,count,data[offset:offset+size*count]);offset+=size*count
    infos=chunks['ANIMINFO'];keys=np.frombuffer(chunks['ANIMKEYS'][2],dtype='<f4').reshape(-1,8)
    centre_cm=centre*100;C=np.eye(4);C[:3,:3]*=scale/100;C[:3,3]=-centre*scale;inverseC=np.linalg.inv(C)
    sequences={}
    for i in range(infos[1]):
        info=struct.unpack_from('<64s64s4i3f3i',infos[2],i*infos[0]);name=info[0].split(b'\0')[0].decode();count=info[-1];first=info[-2];rate=info[8]
        frames=[];vertex_frames=[];affected=set()
        for frame in range(count):
            poses=[];world=[]
            for bone in range(len(bind)):
                key=keys[(first+frame)*len(bind)+bone];q=[-float(x) for x in key[3:6]]+[float(key[6])]
                m=matrix(key[:3],q);parent=parent_bones[bone]
                if parent>=0:m=world[parent]@m
                world.append(m);reflection=np.diag([1,-1,1,1]);skin=reflection@C@m@np.linalg.inv(bind[bone])@inverseC@reflection
                pose=[*skin[:3,3],*rotation_quat(skin[:3,:3])];poses.append(pose)
                if np.max(np.abs(skin-np.eye(4)))>1e-5:affected.add(bone)
            frames.append(poses)
        # Flexible triangles are rebuilt from rigid dominant-bone source vertices.
        indices=sorted({int(v) for triangle in triangles.reshape(-1,3) if len(set(bones[triangle]))>1 for v in triangle})
        for poses in frames:
            transformed=[]
            for vertex in indices:
                pose=poses[int(bones[vertex])];m=matrix(pose[:3],pose[3:]);p=m[:3,:3]@positions[vertex]+m[:3,3]
                n=m[:3,:3]@(-normals[vertex]);t=m[:3,:3]@output_tangents[vertex];transformed.append([*p,*n,*t])
            vertex_frames.append(transformed)
        sequences[name]={'rate':float(rate),'indices':indices,'frames':vertex_frames,'bone_frames':frames,'animated_bones':sorted(affected)}
        if name=='Slots_SpinLoop':sequences[name]['playback_frames']=count-1
    save_json(out/'animations.json.gz',{'source':'Player installation; PSA skin transforms','bones':[nodes[n]['name'] for n in skin_nodes],'sequences':sequences})
    reels=[]
    for bone in (4,3,7):
        pivot=(bind[bone][:3,3]/100-centre)*scale;pivot[1]*=-1
        reels.append({'bone':bone,'pivot':pivot.tolist(),'start_pose':sequences['Slots_SpinEnd']['bone_frames'][0][bone],
          'stops':[{'index':i,'symbol':s,'angle':(6-i)*math.tau/10,'uv_center':[0,0],'area':1} for i,s in enumerate(SYMBOLS)]})
    save_json(out/'reels.json',{'axis':[0,1,0],'angular_speed':math.tau*2,'reels':reels})

def pck_entries(path,table):
    with path.open('rb') as f:
        header=f.read(28)
        if header[:4]!=b'AKPK':raise ValueError('Unexpected BL2 sound package format')
        _,version,langs,banks,streams,external=struct.unpack_from('<6I',header,4)
        if version!=1:raise ValueError('Unsupported sound package version')
        f.seek(28+langs+(banks if table=='streams' else 0));count=struct.unpack('<I',f.read(4))[0]
        if count>1000000:raise ValueError('Invalid package table')
        for _ in range(count):
            ident,block,size,offset,language=struct.unpack('<5I',f.read(20))
            yield ident,offset*block,size

def bank_media(path,offset,size,wanted):
    with path.open('rb') as f:f.seek(offset);data=f.read(size)
    pos=0;index=None;payload=None
    while pos+8<=len(data):
        kind=data[pos:pos+4];length=struct.unpack_from('<I',data,pos+4)[0];pos+=8
        if kind==b'DIDX':index=data[pos:pos+length]
        if kind==b'DATA':payload=data[pos:pos+length]
        pos+=length
    if index is None or payload is None:return {}
    return {ident:payload[start:start+length] for ident,start,length in struct.iter_unpack('<3I',index) if ident in wanted}

def convert_audio(game,out,temp,tools,log):
    recipe=json.loads((HERE/'audio_recipe.json').read_text(encoding='utf-8'))
    required={row['source_id'] for row in recipe.values()};wanted=required | {11772802}
    media={};banks=game/'WillowGame/CookedPCConsole/Audio_Banks.pck'
    for ident,offset,size in pck_entries(banks,'banks'):
        media.update(bank_media(banks,offset,size,wanted))
    for stream in (game/'WillowGame/CookedPCConsole/Audio_Streaming.pck',
                   game/'WillowGame/CookedPCConsole/English(US)/Audio_Streaming.pck'):
        if not stream.exists():continue
        for ident,offset,size in pck_entries(stream,'streams'):
            if ident in wanted:
                with stream.open('rb') as f:f.seek(offset);media[ident]=f.read(size)
    missing=required-set(media)
    if missing:raise RuntimeError('Required sound media not found: '+str(sorted(missing)))
    audio=out/'audio';audio.mkdir(exist_ok=True);decoded={}
    for ident,raw in media.items():
        wem=temp/f'{ident}.wem';wav=temp/f'{ident}.wav';wem.write_bytes(raw)
        run([tools/'vgmstream/vgmstream-cli.exe','-o',wav.name,wem.name],cwd=temp)
        with wave.open(str(wav),'rb') as f:
            rate=f.getframerate();channels=f.getnchannels();width=f.getsampwidth();pcm=f.readframes(f.getnframes())
        if width!=2:raise RuntimeError('Unexpected decoded sample format')
        values=np.frombuffer(pcm,dtype='<i2').astype(float).reshape(-1,channels).mean(axis=1)
        if rate!=22050:values=np.interp(np.arange(round(len(values)*22050/rate))*rate/22050,np.arange(len(values)),values)
        decoded[ident]=values
    for name,row in recipe.items():
        write_wave(audio/(name+'.wav'),array('h',np.clip(np.rint(decoded[row['source_id']]),-32768,32767).astype('<i2').tolist()))
    def mix(names,gains):
        sources=[decoded[recipe[n]['source_id']] for n in names];result=np.zeros(max(map(len,sources)))
        for values,gain in zip(sources,gains):result[:len(values)]+=values*gain
        peak=max(abs(result));result*=min(1,31000/peak) if peak else 1
        return array('h',result.astype('<i2').tolist())
    write_wave(audio/'win.wav',mix(['drawer_open_mechanical','drawer_open_accent'],[1,10**(-3/20)]))
    write_wave(audio/'weapon_open.wav',mix(['weapon_open_mechanical','drawer_open_accent'],[1,10**(-3/20)]))
    # English BOOM TIME is optional on localized installations. Keep the
    # original warning beeps if that voice media is absent.
    beep=decoded[recipe['grenade_warning']['source_id']]
    if 11772802 in decoded:
        voice=decoded[11772802];delay=round(1.32235827664399*22050)
        warning=np.zeros(max(len(voice),delay+len(beep)))
        warning[:len(voice)]+=voice
        warning[delay:delay+len(beep)]+=beep*.1282436
    else:
        warning=beep
        log('BOOM TIME voice unavailable in this installation; original warning beeps retained.')
    write_wave(audio/'grenade_warning.wav',array('h',np.clip(np.rint(warning),-32768,32767).astype('<i2').tolist()))
    prebuild_audio(audio)
    (audio/'ASSET_SOURCE.txt').write_text('Extracted locally from this player Borderlands 2 installation. Do not redistribute generated game assets.\n')
    log('Original sound banks decoded.')

def extract(game,destination,tools=None,log=print):
    game=Path(game).resolve();destination=Path(destination).resolve();tools=Path(tools or HERE/'tools').resolve()
    if destination.is_relative_to(game):raise ValueError('Generated assets must be saved outside your BL2 installation.')
    cooked=game/'WillowGame/CookedPCConsole'
    if not (cooked/'SanctuaryAir_Dynamic.upk').is_file() or not (game/'Binaries/Win32/Borderlands2.exe').is_file():
        raise ValueError('Choose the Borderlands 2 game folder containing Binaries and WillowGame.')
    native_cooked=native_working_path(cooked)
    destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bl2-assets-',dir=destination.parent) as staging:
        temp=Path(staging);exports=temp/'exports';out=temp/'assets';out.mkdir()
        # Keep external texture caches available in the original installation;
        # use Windows' ASCII alias and relative output names for the native tools.
        for name in ('Skel_SlotMachine','Anim_SlotMachine','SlotMachine_Dif','SlotMachineMaskB','SlotMachine_Mask','Noise2_LiquidDev_Dif'):
            log('Extracting '+name+'...')
            run([tools/'umodel/umodel.exe','-export',*(['-gltf'] if name=='Skel_SlotMachine' else []),'-png','-path='+native_cooked,'-out=exports','SanctuaryAir_Dynamic',name],cwd=temp)
        for required_file in ('Skel_SlotMachine.gltf','Anim_SlotMachine.psa','SlotMachine_Dif.png','SlotMachineMaskB.png','SlotMachine_Mask.png','Noise2_LiquidDev_Dif.png'):
            if not any(exports.rglob(required_file)):
                raise RuntimeError('The asset exporter did not produce '+required_file+'. Check the chosen BL2 installation and output folder.')
        convert_mesh_and_motion(exports,out)
        image=apply_pearl_symbol(Image.open(next(exports.rglob('SlotMachine_Dif.png'))).convert('RGB'))
        image.save(out/'SlotMachine_Dif.png');image.resize((4096,4096),Image.Resampling.LANCZOS).save(out/'SlotMachine_Dif_4x.png')
        for name,color in [('Preview_Black.png',(0,0,0)),('Preview_White.png',(255,255,255)),('Preview_FlatNormal.png',(128,128,255)),('Preview_AORM.png',(255,255,0))]:Image.new('RGB',(8,8),color).save(out/name)
        log('Building local animation and 30 FPS original-mask lighting…')
        prepare_original_lighting(out,next(exports.rglob('SlotMachineMaskB.png')).parent)
        convert_audio(game,out,temp,tools,log)
        save_json(out/'asset_manifest.json',{'edition':'bl2','schema':1,'asset_revision':153,'geometry_sha':__import__('hashlib').sha256(json.dumps(json.loads((out/'geometry.json').read_text(encoding='utf-8')),sort_keys=True,separators=(',',':')).encode()).hexdigest(),'pearl_symbol':'Compact 34px original ink pearl; old purple and compression fringe erased before compositing; paper rebuilt from local samples','source':'Local player installation; do not redistribute generated assets'})
        # Commit only after the complete extraction succeeds; manifest is last.
        for path in sorted(out.rglob('*'),key=lambda p:p.name=='asset_manifest.json'):
            if path.is_file():
                target=destination/path.relative_to(out);target.parent.mkdir(parents=True,exist_ok=True);os.replace(path,target)
    log('Done. Follow README.txt to install sdk_mods and OakGame into Borderlands 4, with the game closed.')

def gui():
    import tkinter as tk
    from tkinter import filedialog,messagebox
    import queue
    window=tk.Tk();window.title('Slot Machines - Prepare BL2 Assets');window.geometry('640x340')
    tk.Label(window,text='Choose your own Borderlands 2 installation.\nGame files are read only. Generated assets stay on your computer.',pady=15).pack()
    candidates=[Path(f'{drive}:/SteamLibrary/steamapps/common/Borderlands 2') for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ']
    candidates.insert(0,Path('C:/Program Files (x86)/Steam/steamapps/common/Borderlands 2'))
    folder=tk.StringVar(value=str(next((p for p in candidates if (p/'Binaries/Win32/Borderlands2.exe').is_file()),'')))
    tk.Entry(window,textvariable=folder,width=78).pack(padx=15)
    tk.Button(window,text='Browse...',command=lambda:folder.set(filedialog.askdirectory(title='Select Borderlands 2 game folder') or folder.get())).pack(pady=7)
    status=tk.StringVar(value='Ready');tk.Label(window,textvariable=status,wraplength=600).pack(pady=15)
    messages=queue.Queue();destination=(Path(sys.executable).parent if getattr(sys,'frozen',False) else HERE.parent)/'sdk_mods/slot_machines'
    def start():
        source=folder.get();button.config(state='disabled')
        def worker():
            try:extract(source,destination,log=lambda text:messages.put(('progress',text)));messages.put(('done','Assets prepared successfully.'))
            except Exception as error:messages.put(('error',str(error)))
        threading.Thread(target=worker,daemon=True).start()
    button=tk.Button(window,text='Prepare assets',command=start);button.pack(pady=10)
    def poll():
        try:
            while True:
                kind,text=messages.get_nowait();status.set(text)
                if kind in ('done','error'):
                    button.config(state='normal')
                    if kind=='error':messagebox.showerror('Extraction failed',text)
                    else:messagebox.showinfo('Ready',text+'\nNext: follow README.txt to copy sdk_mods and OakGame into Borderlands 4.')
        except queue.Empty:pass
        window.after(100,poll)
    poll();window.mainloop()

if __name__=='__main__':
    if len(sys.argv)==1:gui()
    else:
        parser=argparse.ArgumentParser();parser.add_argument('--game',required=True);parser.add_argument('--output',required=True);parser.add_argument('--tools');args=parser.parse_args()
        try:extract(args.game,args.output,args.tools)
        except Exception as error:
            if sys.stderr is not None:print('Asset preparation failed: '+str(error),file=sys.stderr)
            raise SystemExit(1)
