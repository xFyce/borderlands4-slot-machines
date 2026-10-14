"""Read-only readiness summary and manually advanced reward checklist."""
CASES = (
 ('cash_small',None,'One cherry: $30-$60 stack in front tray; aim at cash and E.'),
 ('cash_medium',None,'Two cherries / matching pair + bell: $100 stack in front tray.'),
 ('cash_large',None,'Three bells: $300 stack in front tray.'),
 ('common','weapon','Common weapon; pistol in tray, larger gun in side door.'),
 ('uncommon','shield','Three cherries: uncommon shield in front tray.'),
 ('rare','classmod','Rare class mod in front tray, at spin-start character level.'),
 ('epic','enhancement','Epic enhancement in front tray, at character level.'),
 ('pearl','weapon','Three pearl icons: one native pearlescent weapon; inspect its item card.'),
 ('legendary','repkit','Legendary repkit in front tray; special win sound.'),
 ('rare','ordnance','Grenade/knife ordnance in front tray; equipment reward, no explosion.'),
 ('rare','heavy','Heavy ordnance held in side door until native pickup.'),
 ('eridium_small',None,'Single-bar triple: small Eridium spill; collect normally off ground.'),
 ('eridium_large',None,'Matching double or triple stacks: large ground spill, 3x small count.'),
 ('grenade',None,'Three psycho faces: dedicated grenade arcs from tray; stand back.'),
 ('jackpot',None,'777: one held epic/legendary equipment +20 forward-scattered items.'),
 ('loss',None,'Loss: $100 wager only, no payout opening, next spin ready.'),
)

def status(ctx):
    """No native reads, actor scans, timer changes, spawns or wallet writes."""
    blockers=[]
    if ctx._actor is None:blockers.append('machine not spawned')
    if ctx._spin is not None:blockers.append('spin running')
    cash=getattr(ctx,'_cash_pickups',None)
    cash_records=cash.records if cash is not None else []
    if cash_records:blockers.append('cash waiting' if any(not r.get('collected') for r in cash_records) else 'cash cleanup/tray closing')
    tray=getattr(ctx,'_tray_state',None)
    if tray and tray.get('state')!='closed':blockers.append('front tray '+str(tray.get('state')))
    door=getattr(ctx,'_weapon_state',None)
    if door:blockers.append('side door '+str(door.get('state')))
    loot=getattr(ctx,'_loot_rewards',None)
    if loot is not None and loot.waiting():blockers.append('native payout waiting')
    grenade=getattr(ctx,'_grenade_test',None)
    if grenade is not None and grenade.waiting():blockers.append('grenade payout active')
    receipt=ctx._report.get('cash_result',{})
    gear=receipt.get('gear',{})
    items=gear.get('items',[]) if isinstance(gear,dict) else []
    return {'ready':not blockers,'blockers':blockers,
            'cash_waiting':sum(r.get('amount',0) for r in cash_records if not r.get('collected')),
            'last_paid_state':receipt.get('state'),'outcome':receipt.get('outcome',{}).get('name'),
            'wager':receipt.get('wager'),'cash_payout':receipt.get('payout'),
            'refunded':receipt.get('state')=='refunded',
            'native_planned':len(items),
            'native_attempts':sum(i.get('native_attempted') is True for i in items),
            'native_unknown_attempts':sum(i.get('native_attempted') is None for i in items),
            'native_failed':sum(i.get('state')=='unavailable' for i in items),
            'native_cancelled':sum(i.get('state')=='cancelled' for i in items),
            'native_unidentified':sum(i.get('display',{}).get('state','').startswith('pickup not identified') for i in items),
            'note':'Readiness and request tracking only; native removal is not proof of successful inventory collection.'}
