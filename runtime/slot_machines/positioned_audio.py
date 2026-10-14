"""Original BL2 PCM, mixed to Windows stereo using machine/camera coordinates.

No Unreal calls on the audio thread. This is distance/pan rendering, not HRTF
or integration with BL4's Wwise sound-effects bus.
"""
import ctypes
import math
import threading
import time
from array import array

from .original_audio import OriginalAudio, RATE, STOP_CENTRES, STOP_BOUNDARIES

CHUNK = RATE//50
BUFFERS = 4
INNER = 150.0
OUTER = 2000.0


class WaveFormat(ctypes.Structure):
    _fields_ = [('wFormatTag',ctypes.c_uint16),('nChannels',ctypes.c_uint16),
                ('nSamplesPerSec',ctypes.c_uint32),('nAvgBytesPerSec',ctypes.c_uint32),
                ('nBlockAlign',ctypes.c_uint16),('wBitsPerSample',ctypes.c_uint16),
                ('cbSize',ctypes.c_uint16)]


class WaveHeader(ctypes.Structure):
    _fields_ = [('lpData',ctypes.c_void_p),('dwBufferLength',ctypes.c_uint32),
                ('dwBytesRecorded',ctypes.c_uint32),('dwUser',ctypes.c_size_t),
                ('dwFlags',ctypes.c_uint32),('dwLoops',ctypes.c_uint32),
                ('lpNext',ctypes.c_void_p),('reserved',ctypes.c_size_t)]


class StereoDevice:
    """One owned waveOut handle; four reusable 20ms buffers, no callbacks."""
    def __init__(self):
        self.dll=ctypes.WinDLL('winmm')
        self.handle=ctypes.c_void_p()
        self.buffers=[]
        self.closed=False
        self.dll.waveOutOpen.argtypes=[ctypes.POINTER(ctypes.c_void_p),ctypes.c_uint32,
            ctypes.POINTER(WaveFormat),ctypes.c_size_t,ctypes.c_size_t,ctypes.c_uint32]
        self.dll.waveOutOpen.restype=ctypes.c_uint32
        for name in ('waveOutPrepareHeader','waveOutUnprepareHeader','waveOutWrite'):
            fn=getattr(self.dll,name);fn.argtypes=[ctypes.c_void_p,ctypes.POINTER(WaveHeader),ctypes.c_uint32]
            fn.restype=ctypes.c_uint32
        for name in ('waveOutReset','waveOutClose'):
            fn=getattr(self.dll,name);fn.argtypes=[ctypes.c_void_p];fn.restype=ctypes.c_uint32
        self.dll.waveOutGetErrorTextW.argtypes=[ctypes.c_uint32,ctypes.c_wchar_p,ctypes.c_uint32]
        fmt=WaveFormat(1,2,RATE,RATE*4,4,16,0)
        self.check(self.dll.waveOutOpen(ctypes.byref(self.handle),0xffffffff,ctypes.byref(fmt),0,0,0))
        try:
            for _ in range(BUFFERS):
                data=ctypes.create_string_buffer(CHUNK*4)
                header=WaveHeader(ctypes.cast(data,ctypes.c_void_p),CHUNK*4,0,0,0,0,None,0)
                self.check(self.dll.waveOutPrepareHeader(self.handle,ctypes.byref(header),ctypes.sizeof(header)))
                self.buffers.append({'data':data,'header':header,'queued':False})
        except Exception:
            self.close();raise

    def check(self, code):
        if code:
            message=ctypes.create_unicode_buffer(256)
            self.dll.waveOutGetErrorTextW(code,message,len(message))
            raise RuntimeError('Windows stereo output: '+str(code)+' '+message.value)

    def available(self):
        for buffer in self.buffers:
            if not buffer['queued'] or buffer['header'].dwFlags & 1:yield buffer

    def write(self, buffer, samples):
        raw=samples.tobytes()
        if len(raw)!=CHUNK*4:raise ValueError('Stereo buffer size mismatch')
        ctypes.memmove(buffer['data'],raw,len(raw))
        buffer['header'].dwFlags &= ~1
        self.check(self.dll.waveOutWrite(self.handle,ctypes.byref(buffer['header']),ctypes.sizeof(WaveHeader)))
        buffer['queued']=True

    def close(self):
        if self.closed:return
        self.closed=True
        if self.handle.value:
            self.dll.waveOutReset(self.handle)
            for buffer in self.buffers:
                self.dll.waveOutUnprepareHeader(self.handle,ctypes.byref(buffer['header']),ctypes.sizeof(WaveHeader))
            self.dll.waveOutClose(self.handle)
            self.handle=ctypes.c_void_p()


def gains(source, listener, yaw):
    dx,dy,dz=(source[i]-listener[i] for i in range(3))
    distance=math.sqrt(dx*dx+dy*dy+dz*dz)
    horizontal=math.hypot(dx,dy)
    angle=math.radians(yaw)
    pan=(-math.sin(angle)*dx+math.cos(angle)*dy)/horizontal if horizontal>.001 else 0
    pan=max(-1,min(1,pan))
    volume=min(1,INNER/max(INNER,distance))*max(0,min(1,(OUTER-distance)/200))
    return volume*math.sqrt((1-pan)/2),volume*math.sqrt((1+pan)/2),distance,pan


class PositionedAudio(OriginalAudio):
    def __init__(self, context, assets, output, report, spatial=True):
        super().__init__(assets,output,report)
        self.ctx=context
        from .game_volume import GameVolume
        self.game_volume=GameVolume(context)
        self.spatial=spatial
        self.events=[]
        self.world=None
        self.target=(0.0,0.0)
        self.current=(0.0,0.0)
        self.quit=threading.Event()
        self.wake=threading.Event()
        self.thread=None
        self.output_error=None
        self.written_buffers=0
        self.device_open=False
        self.device_factory=StereoDevice

    def note(self,event,state,**details):
        record={'backend':'Windows stereo / camera-relative machine distance and pan',
                'event':event,'state':state,**details}
        self.report['audio']=record
        self.report.setdefault('audio_events',[]).append(dict(record))
        del self.report['audio_events'][:-32]
        self.report['spatial_audio']=dict(record,**self.report.get('positioned_audio',{}))

    def tick(self,force=False):
        # Camera location is preferred over the pawn so cutscenes/third-person
        # views use the actual view origin. Pawn/control rotation is fallback.
        ctx=self.ctx
        if not force:
            with self.lock:
                if self.thread is not None and not self.events:return
        try:
            pc=ctx.get_pc(possibly_loading=True)
            if ctx._actor is None or pc is None or pc.Pawn is None:raise RuntimeError('No active machine/player')
            source=ctx._call(ctx._actor,'K2_GetActorLocation')
            camera=getattr(pc,'PlayerCameraManager',None)
            if camera is not None:
                listener=ctx._call(camera,'GetCameraLocation')
                rotation=ctx._call(camera,'GetCameraRotation')
            else:
                listener=ctx._call(pc.Pawn,'K2_GetActorLocation')
                rotation=ctx._call(pc,'GetControlRotation')
            source=(float(source.X),float(source.Y),float(source.Z)+105*ctx._model_height/180)
            listener=tuple(float(getattr(listener,key)) for key in ('X','Y','Z'))
            yaw=float(rotation.Yaw);now=float(ctx._clock());wall=time.monotonic()
            left,right,distance,pan=gains(source,listener,yaw)
            if not self.spatial:left=right=1.0
            volume=self.game_volume.read(pc)
            left*=volume;right*=volume
            with self.lock:
                previous=self.world
                advancing=previous is None or now>previous['time']+1e-6
                self.world={'source':source,'listener':listener,'yaw':yaw,'time':now,
                            'wall':wall,'advancing':advancing}
                self.target=(left,right)
                if volume==0:self.current=(0.0,0.0)
                active=len(self.events)
            status={'source_cm':source,'listener_cm':listener,'camera_yaw':yaw,
                    'distance_cm':distance,'pan':pan,'left_gain':left,'right_gain':right,
                    'active_voices':active,'camera_available':camera is not None,
                    'output_error':self.output_error,'device_open':self.device_open,
                    'written_buffers':self.written_buffers,
                    'game_volume_gain':volume, 'positional':self.spatial,
                    'note':'Software stereo output follows live BL4 master and effects sliders.'}
            self.report['positioned_audio']=status
            self.report.setdefault('spatial_audio',{}).update(status)
        except Exception as error:
            with self.lock:self.target=(0,0);self.world=None
            self.report['positioned_audio']={'state':'muted','error':str(error)}

    def submit(self,event,payload=None):
        if self.closed:return None
        self.tick(force=True)
        with self.lock:
            if self.world is None:
                self.note(event,'unavailable',error='Machine/camera coordinates unavailable');return None
            epoch=self.epoch;requested=self.world['time']
            if self.output_error:
                self.note(event,'unavailable',error=self.output_error);return None
            if self.thread is None:
                self.quit.clear()
                self.thread=threading.Thread(target=self.pump,name='BL2-slot-stereo',daemon=True)
                self.thread.start()
        return self.pool.submit(self.run,epoch,requested,event,payload)

    def run(self,epoch,requested,event,payload):
        try:
            if event=='spin':
                self.queue_spin(epoch,requested,payload)
                return
            if event=='jackpot' or event=='win' and payload:
                selected=self.variant('jackpot_gunshot',6)
                family='jackpot' if event=='jackpot' else 'win_jackpot'
                samples=self.bank('prebuilt/'+family+'_'+selected.rsplit('_',1)[1])
            else:
                selected=self.variant(event,7 if event=='cash_drop' else 3) if event in ('cash_drop','grenade_explosion') else event
                samples=self.bank(selected)  # PCM already encoded; avoid per-sample remix/normalization.
            with self.lock:
                if self.closed or epoch!=self.epoch:return
                if len(self.events)>=12:raise RuntimeError('Too many simultaneous slot sounds')
                self.events.append({'event':event,'variant':selected,'samples':samples,
                                    'requested':requested,'cursor':None})
                self.wake.set()
            self.note(event,'queued',variant=selected,duration=len(samples)/RATE)
        except Exception as error:self.note(event,'unavailable',error=str(error))

    def queue_spin(self,epoch,requested,payload):
        """Schedule original PCM stems; never render an entire spin in Python."""
        pull,durations=payload
        stops=[pull+2+d for d in durations]
        if len(stops)!=3 or stops!=sorted(stops):raise ValueError('Expected three ordered visual reel stops')
        jobs=[{'samples':self.bank('lever'),'requested':requested},
              {'samples':self.bank('rolling'),'requested':requested+1,
               'loop':True,'length':max(0,int(stops[-1]*RATE)-RATE)}]
        ending=self.bank('reel_stop')
        for i,stop in enumerate(stops):
            a,b=int(STOP_BOUNDARIES[i]*RATE),int(STOP_BOUNDARIES[i+1]*RATE)
            delay=max(0,int((stop-(STOP_CENTRES[i]-STOP_BOUNDARIES[i]))*RATE))/RATE
            jobs.append({'samples':ending[a:b],'requested':requested+delay})
        with self.lock:
            if self.closed or epoch!=self.epoch:return
            if len(self.events)+len(jobs)>24:raise RuntimeError('Too many simultaneous slot sounds')
            self.events.extend(dict(job,event='spin',variant='spin',cursor=None,scheduled=True) for job in jobs)
            self.wake.set()
        self.note('spin','queued',variant='original PCM stems',duration=stops[-1])

    def mix_chunk(self,game_time,playing=True,count=CHUNK):
        # Called under lock by the device thread. No object pointers/Unreal calls.
        result=array('h',[0])*(count*2)
        if not playing or not self.events:return result
        smoothing=1-math.exp(-count/RATE/.04)
        left=self.current[0]+(self.target[0]-self.current[0])*smoothing
        right=self.current[1]+(self.target[1]-self.current[1])*smoothing
        self.current=(left,right)
        mono=[0]*count
        for voice in self.events[:]:
            offset=max(0,int((voice['requested']-game_time)*RATE)) if voice.get('scheduled') and voice['cursor'] is None else 0
            if offset>=count:continue
            if voice['cursor'] is None:
                voice['cursor']=max(0,int((game_time-voice['requested'])*RATE)) if voice['event']=='spin' else 0
            start=voice['cursor'];source=voice['samples'];length=voice.get('length',len(source))
            available=min(count-offset,max(0,length-start))
            if voice.get('loop'):
                for i in range(available):
                    sample=start+i
                    fade=max(0,min(1,(length-sample)/(.015*RATE),sample/(.01*RATE)))
                    mono[offset+i]+=int(source[sample%len(source)]*fade)
            else:
                gain=voice.get('gain',1)
                if gain==1:
                    for i in range(available):mono[offset+i]+=source[start+i]
                else:
                    for i in range(available):mono[offset+i]+=int(source[start+i]*gain)
            voice['cursor']+=count-offset
            if voice['cursor']>=length:self.events.remove(voice)
        for i,value in enumerate(mono):
            result[i*2]=max(-32768,min(32767,int(value*left)))
            result[i*2+1]=max(-32768,min(32767,int(value*right)))
        return result

    def pump(self):
        device=None
        try:
            device=self.device_factory()
            self.device_open=True
            self.note('output','Windows stereo device opened',sample_rate=RATE,channels=2)
            while not self.quit.is_set():
                for buffer in device.available():
                    with self.lock:
                        world=self.world
                        elapsed=time.monotonic()-world['wall'] if world else 1
                        playing=bool(world and world['advancing'] and elapsed<.1)
                        now=world['time']+min(.08,elapsed) if world else 0
                        result=self.mix_chunk(now,playing)
                    device.write(buffer,result)
                    self.written_buffers+=1
                # Keep queued PCM tails playing, but stop polling empty devices.
                # Clearing and signalling share the lock so a new sound cannot
                # lose its wakeup between checking the queue and waiting.
                with self.lock:
                    if self.quit.is_set():break
                    idle=not self.events
                    if idle:self.wake.clear()
                if idle:self.wake.wait()
                else:self.quit.wait(.004)
        except Exception as error:
            self.output_error=str(error);self.note('output','unavailable',error=self.output_error)
        finally:
            if device is not None:device.close()
            self.device_open=False

    def stop(self):
        with self.lock:
            self.quit.set()
            self.wake.set()
        thread=self.thread
        if thread is not None and thread is not threading.current_thread():thread.join(timeout=1)
        self.thread=None
        super().stop()
        with self.lock:self.events.clear();self.current=(0,0)
        self.output_error=None

    def abandon(self):
        # This backend touches Windows PCM handles only; no Unreal wrappers.
        self.close()
