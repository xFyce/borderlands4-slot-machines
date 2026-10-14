"""Read exact native item ownership; retain no native wrappers between frames."""
import json
from collections import deque
from pathlib import Path

_events=deque(maxlen=12)


def item(value):
    # InventoryBase and InventoryItem both expose the authored lowercase field.
    for name in ('item','Item'):
        try:return getattr(value,name)
        except AttributeError:pass
    return None


def instance_id(value):
    try:
        result=value.data.InstanceId
        return result if type(result) is int and result>0 else None
    except (AttributeError,TypeError,ValueError):return None


def quantity(actor):
    try:
        result=item(actor).State.Quantity
        return result if type(result) is int else None
    except (AttributeError,TypeError,ValueError):return None


def owned(player,wanted):
    """Confirm committed ownership, never pending equips or matching item names."""
    reads={'active_slots':0,'backpack_slots':0}
    if wanted is None:return None,reads
    pawn=getattr(player,'Pawn',None)
    try:
        for slot in pawn.ActiveWeapons.Slots:
            reads['active_slots']+=1
            if instance_id(item(slot.Weapon))==wanted:
                return 'active weapon',reads
    except (AttributeError,TypeError,ValueError):pass
    # All committed equipment slots, including shields and inactive weapons.
    # ActiveWeapons alone only covers the weapon currently in the player's hands.
    try:
        for slot in pawn.EquippedInventorySlots.items:
            reads['equipped_slots']=reads.get('equipped_slots',0)+1
            value=item(slot.InstancedInventory)
            if instance_id(value)==wanted and value.State.Quantity>0:
                return 'equipped inventory',reads
    except (AttributeError,TypeError,ValueError):pass
    state=getattr(player,'PlayerState',None)
    if state is None:state=getattr(pawn,'PlayerState',None)
    try:
        for slot in state.BackpackItems.items:
            reads['backpack_slots']+=1
            value=item(slot.InventoryItem)
            if instance_id(value)==wanted and value.State.Quantity>0:
                return 'backpack item',reads
    except (AttributeError,TypeError,ValueError):pass
    return None,reads


def report(ctx,record,event):
    from . import diagnostics
    if not diagnostics.enabled:return
    # Only small Python values reach the file worker. No game objects or full
    # mutable reward receipts are serialized here; diagnostics need no console.
    try:
        info=record['info']
        _events.append(dict(event=event,actor=record['path'],route=record['route'],
            item_id=record.get('item_id'),current_item_id=info.get('current_item_id'),
            quantity=info.get('quantity'),owned_at_claim=record.get('owned_at_claim'),
            ownership=info.get('ownership'),ownership_reads=info.get('ownership_reads'),
            state=info.get('state'),cleanup=info.get('cleanup'),
            tracking_error=info.get('tracking_error'),metrics=dict(record['metrics'])))
        from .report_io import submit
        folder=Path.home()/'BL4SlotDiagnostics'
        folder.mkdir(exist_ok=True)
        submit(folder/'slots_prize_v139.json',json.dumps(dict(version=139,events=list(_events)),indent=2))
    except Exception:pass
