"""One native frame hook shared by slots and noclip; no hook mutation on spin."""
import sys
from types import SimpleNamespace
from unrealsdk.hooks import Type, Block, add_hook, remove_hook
FUNCTION='/Script/Engine.Actor:ForceNetUpdate'
HOOK='BL4Slots.SharedFrameDispatcher'
state=sys.modules.setdefault('_bl4_slots_frame_bus',SimpleNamespace(listeners={},registered=False))
def dispatch(obj,args,ret,fn):
    blocked=False
    for callback in tuple(state.listeners.values()):
        result=callback(obj,args,ret,fn)
        if result is Block:blocked=True
    return Block if blocked else None

def subscribe(name,callback):
    if not state.registered:
        if add_hook(FUNCTION,Type.PRE,HOOK,dispatch) is False:
            raise RuntimeError('Could not register shared frame dispatcher')
        state.registered=True
    state.listeners[name]=callback

def unsubscribe(name):
    state.listeners.pop(name,None)
    if state.registered and not state.listeners:
        remove_hook(FUNCTION,Type.PRE,HOOK)
        state.registered=False
