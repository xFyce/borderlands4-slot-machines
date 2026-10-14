"""Read BL4's reflected local-player audio sliders on the game thread."""
import math
import time


class GameVolume:
    def __init__(self, ctx):
        self.ctx = ctx
        self.next_read = 0
        self.value = 1.0

    def read(self, pc=None):
        now = time.monotonic()
        if now < self.next_read:
            return self.value
        self.next_read = now + .1
        if pc is None:
            pc = self.ctx.get_pc(possibly_loading=True)
        # OakLocalPlayer and these two scalar properties were captured from
        # this game's native UI context. Never change the game's preferences.
        context = getattr(getattr(pc, 'UIViewHandle', None), 'Context', None)
        if context is None:
            context = getattr(pc, 'Player', None)
        try:
            master = float(context.OverallVolume)
            effects = float(context.SFXVolume)
            if not math.isfinite(master) or not math.isfinite(effects):
                raise ValueError('Non-finite audio setting')
            master = max(0., min(1., master))
            effects = max(0., min(1., effects))
            self.value = master * effects
            self.ctx._report['game_audio_volume'] = {
                'master': master, 'effects': effects, 'gain': self.value,
                'source': 'OakLocalPlayer live audio preferences'}
        except (AttributeError, TypeError, ValueError) as error:
            self.ctx._report['game_audio_volume'] = {
                'gain': self.value, 'state': 'last known volume', 'error': str(error)}
        return self.value
