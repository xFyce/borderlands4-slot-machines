"""Pure payout rules and weighted selection; no engine calls.

Custom v95 odds and gear mix. BL2 symbols retain their rarity meaning,
sevens award an epic/legendary held equipment plus20 random-item burst, purple triples award Eridium.
Cash returns are 30-60% / 100% / 300% of the wager.
It is not a claim that BL4's native slot roller has been activated.
"""
import itertools
import random

SYMBOLS = ('Purple triple stack', 'Seven', 'Legs', 'Grenade', 'Vault',
           'Bell', 'Portrait', 'Cherries', 'Purple bar', 'Pearl')
GEAR_MATCH = {'Vault':'legendary', 'Portrait':'epic', 'Legs':'rare',
              'Cherries':'uncommon','Pearl':'pearl'}
# 7,770,000 tickets preserve all other percentage odds while representing
# legendary1/200 and jackpot1/777 exactly. Added chance comes from losses.
WEIGHTS = {'common':1398600,'uncommon':1243200,'rare':621600,'epic':155400,
           'legendary':38850,'cash':1942500,'loss':1578965,'grenade':466200,
           'eridium_small':233100,'eridium_large':77700,'jackpot':10000,'pearl':3885}
CASH_WEIGHTS = {'cash_small':18,'cash_medium':5,'cash_large':2}
TYPE_WEIGHTS = {'weapon':50,'classmod':10,'shield':10,'enhancement':10,'repkit':10,'ordnance':5,'heavy':5}
JACKPOT_EQUIPMENT_WEIGHTS = {'classmod':20,'shield':20,'enhancement':20,'repkit':20,'ordnance':10,'heavy':10}
JACKPOT_RARITIES = {'epic':80,'legendary':20}
SUPPORTED = ('loss','cash_small','cash_medium','cash_large','common',
             'uncommon','rare','epic','legendary','grenade',
             'eridium_small','eridium_large','jackpot','pearl')
SOURCE = 'Custom v95 balance; BL2 reel art/combinations, BL4 native item pools'



def classify(symbols):
    if len(symbols)!=3 or any(s not in SYMBOLS for s in symbols):
        raise ValueError('A result needs three known reel symbols')
    a,b,c=symbols
    name='loss';kind='none';multiplier=0;rarity=None
    if a==b==c:
        if a in GEAR_MATCH:
            name=rarity=GEAR_MATCH[a];kind='weapon'
        elif a=='Bell':name='cash_large';kind='cash';multiplier=3
        else:
            name={'Grenade':'grenade','Seven':'jackpot',
                  'Purple bar':'eridium_small',
                  'Purple triple stack':'eridium_large'}[a]
            kind='grenade' if name=='grenade' else 'jackpot' if name=='jackpot' else 'eridium'
    elif a==b and c=='Grenade':
        name=rarity='common';kind='weapon'
    elif (a==b and c=='Bell') or (a==b=='Bell' and c!='Grenade'):
        name='cash_medium';kind='cash';multiplier=1
    else:
        cherries=symbols.count('Cherries')
        if cherries:
            name='cash_small' if cherries==1 else 'cash_medium'
            kind='cash';multiplier=0.45 if cherries==1 else 1
    return {'name':name,'kind':kind,'rarity':rarity,
            'cash_multiplier':multiplier,'symbols':list(symbols)}


# Build once, off the game frame loop. Every chosen animation combination
# evaluates to its selected outcome; losses cannot accidentally win.
PATTERNS={name:[] for name in SUPPORTED}
for _indices in itertools.product(range(10),repeat=3):
    _name=classify([SYMBOLS[i] for i in _indices])['name']
    if _name in PATTERNS:PATTERNS[_name].append(_indices)


def weighted_choice(weights,rng):
    if not weights or any(weight<=0 for weight in weights.values()):
        raise ValueError('Weights must be nonempty and positive')
    ticket=rng.randrange(sum(weights.values()))
    for name,weight in weights.items():
        if ticket<weight:return name
        ticket-=weight
    raise AssertionError('Weight selection outside range')


def enabled_weights(gear_enabled=True,grenade_enabled=False,eridium_enabled=True,pearl_enabled=True):
    weights={k:v for k,v in WEIGHTS.items()
            if (k in ('cash','loss')) or
               (k in ('common','uncommon','rare','epic','legendary','jackpot') and gear_enabled) or
               (k=='grenade' and grenade_enabled) or
               (k.startswith('eridium_') and eridium_enabled) or
               (k=='pearl' and gear_enabled and pearl_enabled)}
    # A disabled pearl feature returns its tickets to losses, preserving
    # existing legendary/jackpot odds when all other backends are enabled.
    if 'pearl' not in weights:weights['loss']+=WEIGHTS['pearl']
    return weights


def choose(gear_enabled=True,forced=None,rng=None,grenade_enabled=False,eridium_enabled=True,pearl_enabled=True):
    rng=rng or random.SystemRandom()
    weights=enabled_weights(gear_enabled,grenade_enabled,eridium_enabled,pearl_enabled)
    if forced is not None:
        if forced not in SUPPORTED:raise ValueError('Unknown supported outcome')
        if forced not in CASH_WEIGHTS and forced not in weights:
            raise ValueError('Reward disabled/unavailable; no wager taken')
        name=forced
    else:
        name=weighted_choice(weights,rng)
        if name=='cash':name=weighted_choice(CASH_WEIGHTS,rng)
    return list(rng.choice(PATTERNS[name]))


def odds(gear_enabled=True,grenade_enabled=False,eridium_enabled=True,pearl_enabled=True):
    weights=enabled_weights(gear_enabled,grenade_enabled,eridium_enabled,pearl_enabled)
    total=sum(weights.values());result={}
    for name,weight in weights.items():
        if name=='cash':
            for tier,value in CASH_WEIGHTS.items():
                result[tier]=100*weight/total*value/sum(CASH_WEIGHTS.values())
        else:result[name]=100*weight/total
    return result


def cash_award(name,wager,rng=None):
    """Roll a small return once; keep integer currency amounts for native pickups."""
    rng=rng or random.SystemRandom()
    percent=rng.randint(30,60) if name=='cash_small' else {'cash_medium':100,'cash_large':300}.get(name,0)
    return {'percent':percent,'payout':wager*percent//100}
