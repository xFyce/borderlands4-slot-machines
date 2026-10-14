"""Original BL2 event media; off-thread timelines and independent Windows voices."""
import ctypes
import random
import threading
import time
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor

RATE = 22050
# Three different clicks within BL2's ONE Spin_End sequence, not three copies.
STOP_CENTRES = (.309, .818, 1.357)
STOP_BOUNDARIES = (0, .5635, 1.0875, 1.7980952380952382)


class WindowsVoices:
    """Separate waveaudio devices keep payouts from interrupting reel-stop tails."""
    def __init__(self):
        self.dll = ctypes.WinDLL('winmm')
        self.command = self.dll.mciSendStringW
        self.command.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        self.command.restype = ctypes.c_uint
        self.active = {}
        self.serial = 0
        self.prefix = 'bl2slots'+str(id(self))

    def send(self, command):
        code = self.command(command, None, 0, None)
        if code: raise RuntimeError('Windows waveaudio error '+str(code))

    def close_voice(self, alias):
        path, _ = self.active.pop(alias)
        try: self.send('close '+alias)
        finally:
            try: path.unlink()
            except OSError: pass

    def play(self, path, duration):
        now = time.monotonic()
        for alias, (_, expires) in list(self.active.items()):
            if now >= expires: self.close_voice(alias)
        # Only this mod's own aliases are ever stopped/closed.
        if len(self.active) >= 12:
            self.close_voice(next(iter(self.active)))
        self.serial += 1
        alias = self.prefix+'v'+str(self.serial)
        self.send(f'open "{path}" type waveaudio alias {alias}')
        self.active[alias] = (path, now+duration+.25)
        try: self.send('play '+alias)
        except Exception:
            self.close_voice(alias)
            raise

    def stop(self):
        for alias in list(self.active):
            try: self.close_voice(alias)
            except Exception: pass


class OriginalAudio:
    def __init__(self, assets, output, report):
        self.assets, self.output, self.report = assets, output, report
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='BL2-slot-audio')
        self.lock = threading.Lock()
        self.epoch = 0
        self.serial = 0
        self.closed = False
        self.samples = array('h')
        self.started = 0
        self.windows = None
        self.voices = None
        self.backend_checked = False
        self.backend_note = None
        self.banks = {}
        self.previous = {}
        self.fallback_files = []

    def bank(self, name):
        if name not in self.banks:
            with wave.open(str(self.assets / (name+'.wav')), 'rb') as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, RATE):
                    raise ValueError('Original audio format mismatch: '+name)
                samples = array('h', source.readframes(source.getnframes()))
            if name=='weapon_open' and all((self.assets/(stem+'.wav')).is_file()
                    for stem in ('weapon_open_mechanical','drawer_open_accent')):
                mechanical=self.bank('weapon_open_mechanical')
                # Earlier extractors copied only the door mechanism to this
                # event. Repair that exact legacy payload once on the audio
                # worker; newly baked mixtures must never get a second chime.
                if samples==mechanical:
                    mixed=list(samples)
                    self.mix(mixed,self.bank('drawer_open_accent'),0,10**(-3/20))
                    samples=self.pcm(mixed)
                    self.report['weapon_reward_audio']={
                        'state':'legacy mechanical-only sound repaired in cached PCM',
                        'accent_gain_db':-3,'mixes':1,'runtime_files_written':0}
            self.banks[name]=samples
        return self.banks[name]

    def variant(self, family, count):
        options = [i for i in range(1, count+1) if i != self.previous.get(family)]
        index = random.choice(options)
        self.previous[family] = index
        return family+'_'+str(index)

    @staticmethod
    def mix(target, source, start, gain=1, limit=None):
        start = max(0, int(start * RATE))
        count = len(source) if limit is None else min(len(source), max(0, limit))
        need = start + count
        if need > len(target): target.extend([0.0]*(need-len(target)))
        for i in range(count): target[start+i] += source[i]*gain

    @staticmethod
    def pcm(samples):
        peak = max((abs(v) for v in samples), default=0)
        gain = min(1, 31000/peak) if peak else 1
        return array('h', (int(v*gain) for v in samples))

    def render(self, event, payload):
        target = []
        selected = event
        if event == 'spin':
            pull, durations = payload
            stops = [pull+2+d for d in durations]
            if len(stops) != 3 or stops != sorted(stops):
                raise ValueError('Expected three ordered visual reel stops')
            self.mix(target, self.bank('lever'), 0)
            loop = self.bank('rolling')
            # Keep rolling underneath the first two stops until the last reel lands.
            loop_end = stops[-1]
            for sample in range(RATE, int(loop_end*RATE)):
                elapsed = sample/RATE
                fade = max(0, min(1, (loop_end-elapsed)/.015, (elapsed-1)/.01))
                if sample >= len(target): target.append(0.0)
                target[sample] += loop[(sample-RATE)%len(loop)]*fade
            ending = self.bank('reel_stop')
            for i, stop in enumerate(stops):
                a, b = int(STOP_BOUNDARIES[i]*RATE), int(STOP_BOUNDARIES[i+1]*RATE)
                self.mix(target, ending[a:b], stop-(STOP_CENTRES[i]-STOP_BOUNDARIES[i]))
        elif event == 'jackpot':
            self.mix(target, self.bank('jackpot'), 0)
            selected = self.variant('jackpot_gunshot', 6)
            self.mix(target, self.bank(selected), 0)
        else:
            if event == 'cash_drop': selected = self.variant('cash_drop', 7)
            if event == 'grenade_explosion': selected = self.variant('grenade_explosion', 3)
            self.mix(target, self.bank(selected), 0)
            # Compatibility with the previous diagnostic call; gameplay now
            # requests jackpot independently, including wallet payout mode.
            if event == 'win' and payload:
                self.mix(target, self.bank('jackpot'), 0)
                self.mix(target, self.bank(self.variant('jackpot_gunshot', 6)), 0)
        return target, selected

    def submit(self, event, payload=None):
        with self.lock:
            if self.closed: return None
            # A new event may overlap another. Only stop/cancel invalidates jobs.
            epoch = self.epoch
        return self.pool.submit(self.run, epoch, time.monotonic(), event, payload)

    def run(self, epoch, requested, event, payload):
        try:
            target, selected = self.render(event, payload)
            with self.lock:
                if self.closed or epoch != self.epoch: return
                if not self.backend_checked:
                    self.backend_checked = True
                    try: self.voices = WindowsVoices()
                    except Exception as error: self.backend_note = str(error)
                if self.voices is None and event != 'spin':
                    # Compatibility fallback for machines without MCI waveaudio.
                    elapsed = max(0, int((requested-self.started)*RATE))
                    tail = [float(v) for v in self.samples[elapsed:]]
                    self.mix(tail, target, 0)
                    target = tail
                # Only the long spin is tied to a fixed visual timeline. Keep
                # the attacks of short queued reward sounds intact.
                skipped = max(0, int((time.monotonic()-requested)*RATE)) if event == 'spin' else 0
                result = self.pcm(target)[skipped:]
                if not result: return
                self.output.mkdir(parents=True, exist_ok=True)
                self.serial += 1
                path = self.output / f'bl2_slot_audio_{id(self)}_{self.serial}.wav'
                with wave.open(str(path), 'wb') as dest:
                    dest.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                    dest.writeframes(result.tobytes())
                if self.voices is not None:
                    try: self.voices.play(path, len(result)/RATE)
                    except Exception as error:
                        self.voices.stop();self.voices = None;self.backend_note = str(error)
                if self.voices is None:
                    import winsound
                    self.windows = winsound
                    winsound.PlaySound(str(path), winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
                    # winsound uses one voice; retain its tail for the next event.
                    self.fallback_files.append(path)
                    for old in self.fallback_files[:-2]:
                        try: old.unlink()
                        except OSError: pass
                    self.fallback_files = self.fallback_files[-2:]
                    self.samples = result
                    self.started = time.monotonic()
                record = {'backend':'original BL2 WAV / '+('Windows independent voices' if self.voices else 'Windows single-voice fallback'),
                          'event':event, 'variant':selected, 'state':'requested', 'duration':len(result)/RATE,
                          'render_skipped_seconds':skipped/RATE,
                          'note':'Not positional; independent of the game audio mixer'}
                if self.backend_note: record['backend_note'] = self.backend_note
                self.report['audio'] = record
                history = self.report.setdefault('audio_events', [])
                history.append(dict(record));del history[:-24]
        except Exception as error:
            with self.lock:
                if not self.closed and epoch == self.epoch:
                    self.report['audio'] = {'state':'unavailable','event':event,'error':str(error)}

    def stop(self):
        with self.lock:
            self.epoch += 1
            self.samples = array('h')
            if self.voices: self.voices.stop()
            if self.windows: self.windows.PlaySound(None, self.windows.SND_PURGE)
            for path in self.output.glob('bl2_slot_audio_'+str(id(self))+'_*.wav'):
                try: path.unlink()
                except OSError: pass

    def close(self):
        self.stop()
        with self.lock: self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)

    def abandon(self):
        # Bypass any native-media stop override during world teardown.
        OriginalAudio.stop(self)
        with self.lock:self.closed=True
        self.pool.shutdown(wait=False,cancel_futures=True)
