"""Shared serialization and audio mix preparation for extracted assets."""
import gzip, json, wave
from array import array

SYMBOLS=['Purple triple stack','Seven','Legs','Grenade','Vault','Bell','Portrait','Cherries','Purple bar','Pearl']

def save_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(data,separators=(',',':')).encode()
    path.write_bytes(gzip.compress(raw,mtime=0) if path.suffix=='.gz' else raw)

def write_wave(path,pcm,rate=22050):
    with wave.open(str(path),'wb') as out:out.setparams((1,2,rate,0,'NONE','not compressed'));out.writeframes(pcm.tobytes())

def prebuild_audio(root):
    # Same normalization as the gameplay renderer; prepared outside game callbacks.
    def read(name):
        with wave.open(str(root/(name+'.wav')),'rb') as w:return array('h',w.readframes(w.getnframes()))
    def mix(names):
        values=[read(n) for n in names];result=[0]*max(map(len,values))
        for source in values:
            for i,v in enumerate(source):result[i]+=v
        peak=max(map(abs,result),default=1);gain=min(1,31000/peak) if peak else 1
        return array('h',(int(v*gain) for v in result))
    folder=root/'prebuilt';folder.mkdir(exist_ok=True)
    for i in range(1,7):
        write_wave(folder/f'jackpot_{i}.wav',mix(['jackpot',f'jackpot_gunshot_{i}']))
        write_wave(folder/f'win_jackpot_{i}.wav',mix(['win','jackpot',f'jackpot_gunshot_{i}']))
