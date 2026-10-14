"""Persistent preferences, custom UMG HUD prompt and BL2-only optional audio."""
import json
import os
from pathlib import Path

import unrealsdk
from unrealsdk.hooks import Block

DEFAULTS = {'auto_cash':True,'sounds':True,'loot_enabled':True,'pearl_enabled':True,
            'pearl_defaults_version':1,
            'loot_pools':{},'audio_mode':'positional'}

class _NoTrace:
    def phase(self,name):pass
_NO_TRACE=_NoTrace()


class Experience:
    def __init__(self, ctx):
        self.ctx=ctx
        self.folder=Path(os.environ.get('USERPROFILE',str(Path.home())))/'BL4SlotDiagnostics'
        self.settings=dict(DEFAULTS,loot_pools={})
        self.text=None
        self.last_text=None
        self.hooked=False
        self.player=None
        self.overlay=None
        self.native_overlay_hidden=False
        self.widgets=[]
        self.key_down=False
        self.last_use=-100
        self.in_poll=False
        self.ui_failed=False
        migrate_pearls=False
        try:
            loaded=json.loads((self.folder/'slot_settings.json').read_text(encoding='utf-8'))
            for n in ('auto_cash','sounds','loot_enabled'):
                if type(loaded.get(n)) is bool:self.settings[n]=loaded[n]
            # User confirmed native pearls in v87 and requested them enabled.
            # Migrate the old testing default once, then respect later off choices.
            if type(loaded.get('pearl_defaults_version')) is int and loaded['pearl_defaults_version']==1:
                if type(loaded.get('pearl_enabled')) is bool:self.settings['pearl_enabled']=loaded['pearl_enabled']
            else:migrate_pearls=True
            if loaded.get('audio_mode') in ('positional','legacy'):self.settings['audio_mode']=loaded['audio_mode']
            if isinstance(loaded.get('loot_pools'),dict):
                self.settings['loot_pools']={str(k):str(v) for k,v in loaded['loot_pools'].items()}
        except FileNotFoundError:pass
        except Exception as e:ctx._report['settings_note']=str(e)
        if migrate_pearls:
            try:
                self.save()
                ctx._report['pearl_defaults_migration']='Native pearls confirmed by user; enabled once for existing settings'
            except Exception as e:ctx._report['settings_note']='Pearls enabled; could not persist migration: '+str(e)

    def save(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        target=self.folder/'slot_settings.json'
        temp=target.with_suffix('.tmp')
        temp.write_text(json.dumps(self.settings,indent=2),encoding='utf-8')
        temp.replace(target)
        # Preferences belong to the mod, not the currently selected cabinet.
        fleet=getattr(self.ctx,'_fleet',None)
        services=[self]
        for ctx in tuple(fleet.instances) if fleet is not None else ():
            service=getattr(ctx,'_experience',None)
            if service is not None and service not in services:services.append(service)
        for service in services:
            service.settings=dict(self.settings,loot_pools=dict(self.settings['loot_pools']))
            service.ctx._report['settings']=dict(service.settings)
            if not service.settings['sounds'] and service.player is not None:
                service.player.stop()

    def stop(self):
        if self.hooked:
            from .fleet import manager
            manager(self.ctx).detach_experience(self)
            self.hooked=False
        if self.overlay is not None:
            try:self.ctx._call(self.overlay,'RemoveFromParent')
            except Exception:pass
        self.overlay=None
        self.widgets=[]
        self.text=None
        self.last_text=None
        try:
            if self.player:self.player.close()
            self.player=None
        except Exception:pass

    def setup(self):
        ctx=self.ctx
        self.stop()
        self.ui_failed=False;self.key_down=False;self.next_slow_tick=0
        self.native_overlay_hidden=False
        from .fleet import manager
        manager(ctx).attach_experience(self)
        self.hooked=True
        ctx._call(ctx._default('/Script/Engine.Default__KismetSystemLibrary'),'K2_SetTimer',
                  Object=ctx._actor,FunctionName='FlushNetDormancy',Time=.001,bLooping=True,
                  bMaxOncePerFrame=True,InitialStartDelay=.001,InitialStartDelayVariance=0)
        try:self.setup_hud()
        except Exception as error:
            self.ui_failed=True
            ctx._report['interaction_prompt_error']=str(error)
            if self.overlay is not None:
                try:ctx._call(self.overlay,'RemoveFromParent')
                except Exception:pass
            self.text=None
        ctx._report['settings']=dict(self.settings)

    def setup_hud(self):
        ctx=self.ctx
        pc=ctx.get_pc(possibly_loading=True)
        def create(path,outer):
            widget=unrealsdk.construct_object(unrealsdk.find_class(path),outer)
            self.widgets.append(widget)
            return widget
        def v(x,y):return unrealsdk.make_struct('Vector2D',X=float(x),Y=float(y))
        def color(r,g,b,a=1):return unrealsdk.make_struct('LinearColor',R=r,G=g,B=b,A=a)
        def panel(parent,widget,x,y,w,h):
            slot=ctx._call(parent,'AddChildToCanvas',Content=widget)
            ctx._call(slot,'SetPosition',InPosition=v(x,y))
            ctx._call(slot,'SetSize',InSize=v(w,h))
            return slot
        self.overlay=create('/Script/UMG.UserWidget',pc)
        tree=create('/Script/UMG.WidgetTree',self.overlay)
        self.overlay.WidgetTree=tree
        root=create('/Script/UMG.CanvasPanel',tree);tree.RootWidget=root
        holder=create('/Script/UMG.CanvasPanel',tree)
        slot=panel(root,holder,0,0,360,34)
        ctx._call(slot,'SetAnchors',InAnchors=unrealsdk.make_struct('Anchors',Minimum=v(.575,.52),Maximum=v(.575,.52)))
        bg=create('/Script/UMG.Border',tree)
        ctx._call(bg,'SetBrushColor',InBrushColor=color(.01,.025,.04,.78))
        panel(holder,bg,0,0,360,34)
        stripe=create('/Script/UMG.Border',tree)
        ctx._call(stripe,'SetBrushColor',InBrushColor=color(.02,.7,.92,1))
        panel(holder,stripe,0,0,3,34)
        keybg=create('/Script/UMG.Border',tree)
        ctx._call(keybg,'SetBrushColor',InBrushColor=color(.01,.16,.22,.98))
        panel(holder,keybg,8,3,28,28)
        self.key_text=create('/Script/UMG.TextBlock',tree)
        ctx._call(self.key_text,'SetText',InText=str(getattr(ctx.interact_slot_machine,'key','E')))
        panel(holder,self.key_text,14,5,22,27)
        self.text=create('/Script/UMG.TextBlock',tree)
        panel(holder,self.text,44,5,312,29)
        for label in (self.key_text,self.text):
            font=label.Font;font.Size=16
            ctx._call(label,'SetFont',InFontInfo=font)
            ctx._call(label,'SetColorAndOpacity',InColorAndOpacity=unrealsdk.make_struct('SlateColor',SpecifiedColor=color(.72,.9,1),ColorUseRule=0))
        ctx._call(self.overlay,'AddToViewport',ZOrder=30)
        ctx._call(self.overlay,'SetVisibility',InVisibility=1)
        ctx._report['interaction_prompt']='custom UMG overlay; native BL4 HUD integration pending'

    def menu_open(self,pc):
        if getattr(pc,'bShowMouseCursor',False):return True
        try:
            console=pc.Player.ViewportClient.ViewportConsole
            if str(console.ConsoleState).lower() in ('typing','open'):return True
        except Exception:pass
        try:
            if self.ctx._call(pc,'IsPaused'):return True
        except Exception:pass
        return False

    def poll_input(self,pc):
        ctx=self.ctx
        native=getattr(ctx,'_native_ui',None)
        if native is not None and native.enabled:return
        from .fleet import manager
        key=str(getattr(ctx.interact_slot_machine,'key','E') or '')
        if not key:return
        try:
            down=bool(ctx._call(pc,'IsInputKeyDown',Key=unrealsdk.make_struct('Key',KeyName=key)))
            edge=down and not self.key_down
            self.key_down=down
            if not edge:return
            if manager(ctx).select() is not ctx:return
            if edge and not self.menu_open(pc):
                self.in_poll=True
                try:ctx._interact_machine('controller press polling')
                finally:self.in_poll=False
            ctx._report.setdefault('interaction_input',{})['polling']='active'
        except Exception as error:
            ctx._report.setdefault('interaction_input',{})['polling_error']=str(error)

    def tick(self,obj,args,ret,fn):
        ctx=self.ctx
        # One hook owns FlushNetDormancy: money actors dispatch to cash, the
        # cabinet actor continues through audio/input/HUD. Never block another
        # callback on this same function with a second observer registration.
        pickups=getattr(ctx,'_cash_pickups',None)
        if pickups is not None and any(r['actor']==obj for r in pickups.records):
            return pickups.tick(obj,args,ret,fn)
        if ctx._actor is None or obj!=ctx._actor:return None
        try:
            getattr(ctx,'_native_trace',_NO_TRACE).phase('interaction timer')
            now=ctx._clock()
            slow=now>=getattr(self,'next_slow_tick',0)
            if slow:
                self.next_slow_tick=now+.02
                inputs=ctx._report.setdefault('interaction_input',{})
                inputs['timer_ticks']=inputs.get('timer_ticks',0)+1
                inputs['sdk_key_enabled']=getattr(ctx.interact_slot_machine,'is_enabled',None)
                # Preserve audio pumping even while a player/HUD is unavailable.
                if self.player and hasattr(self.player,'tick'):
                    try:self.player.tick()
                    except Exception as error:
                        ctx._report['audio']={'state':'unavailable','error':str(error),'stage':'native tick'}
                        try:self.player.stop()
                        except Exception:pass
            pc=ctx.get_pc(possibly_loading=True)
            if pc is None or pc.Pawn is None:return Block
            # Animation updates once per game frame; input/audio/UI remain 50 Hz.
            ctx._advance_weapon_door()
            if getattr(ctx,'_tray_state',None) is not None and ctx._spin is None:
                ready=ctx._advance_payout_tray()
                rewards=getattr(ctx,'_loot_rewards',None)
                if ctx._tray_state['state'] in ('opening','open') and rewards is not None and rewards.pending is not None and rewards.pending['route']=='front':
                    rewards.release_pending();ctx._request_report()
                    if not rewards.waiting('front'):ctx._maybe_close_front_tray()
            rewards=getattr(ctx,'_loot_rewards',None)
            if rewards is not None:rewards.tick(now)
            grenades=getattr(ctx,'_grenade_test',None)
            if grenades is not None:grenades.tick(now)
            if not slow:return Block
            ctx._flush_report_if_quiet(now)
            getattr(ctx,'_native_trace',_NO_TRACE).phase('input polling')
            native=getattr(ctx,'_native_ui',None)
            if native is not None:native.tick()
            lights=getattr(ctx,'_cabinet_lights',None)
            if lights is not None:lights.tick(pc)
            self.poll_input(pc)
            # A first real animation callback must arrive after any deferred start.
            if ctx._spin and ctx._report.get('animation',{}).get('ticks',0)==0 and ctx._clock()-ctx._spin['started']>.6:
                ctx._report['last_start_attempt'].update(state='failed',error='Animation timer did not deliver a callback; wager refunded')
                ctx._stop_spin(restore=True);ctx._save_report()
            if native is not None and native.enabled:
                if self.overlay is not None and not getattr(self,'native_overlay_hidden',False):
                    ctx._call(self.overlay,'SetVisibility',InVisibility=1)
                    self.native_overlay_hidden=True
                return Block
            self.native_overlay_hidden=False
            if self.text is None:return Block
            getattr(ctx,'_native_trace',_NO_TRACE).phase('HUD proximity/menu query')
            cash=getattr(ctx,'_cash_pickups',None)
            target=cash.targeted() if cash is not None and any(not r.get('collected') for r in cash.records) else None
            near=(target is not None or ctx._near_machine()) and not self.menu_open(pc)
            ctx._call(self.overlay,'SetVisibility',InVisibility=4 if near else 1)
            if not near:return Block
            from .level_sync import snapshot
            try:
                path=getattr(ctx,'_path',lambda obj:str(id(obj)))
                key=(path(pc),path(pc.Pawn),path(getattr(pc,'PlayerState',None)))
                if key!=getattr(self,'level_key',None) or now>=getattr(self,'next_level_read',0):
                    self.level_price=snapshot(ctx,pc)['price']
                    self.level_key=key;self.next_level_read=now+.5
                price=self.level_price
            except Exception as error:
                message='Level unavailable Â· run slots_level'
                if message!=self.last_text:
                    ctx._call(self.text,'SetText',InText=message);self.last_text=message
                ctx._report['level_sync_error']=str(error)
                return Block
            message=f'Use Slot Machine Â· ${price:,}'
            if target is not None:message=f"Pick up ${target['amount']:,}"
            elif ctx._spin is not None:message='Spinning...'
            elif getattr(ctx,'_grenade_test',None) is not None and ctx._grenade_test.waiting():message='Grenade test â€” stand back!'
            elif getattr(ctx,'_loot_rewards',None) is not None and ctx._loot_rewards.waiting():
                rewards=ctx._loot_rewards
                if getattr(rewards,'burst',None) is not None and rewards.burst.waiting():message='Jackpot! Loot incoming...'
                elif (getattr(rewards,'batch_receipt',None) or {}).get('outcome','').startswith('eridium'):message='Collect Eridium from the tray'
                else:message='Pick up your gear reward'
            elif ctx._weapon_state:message='Weapon door '+ctx._weapon_state['state']+'...'
            elif ctx._tray_state and ctx._tray_state['state']=='closing':message='Tray closing...'
            elif ctx._cash_pickups and ctx._cash_pickups.records:
                amount=sum(r['amount'] for r in ctx._cash_pickups.records if not r.get('collected'))
                message=f'Cash in tray Â· ${amount:,} Â· aim at the stack'
            else:
                getattr(ctx,'_native_trace',_NO_TRACE).phase('HUD wallet property query')
                if ctx._cash_entry(pc)[4]<price:message=f'Not enough cash Â· ${price:,}'
            if message!=self.last_text:
                ctx._call(self.text,'SetText',InText=message)
                self.last_text=message
            getattr(ctx,'_native_trace',_NO_TRACE).phase('HUD text update')
            waiting_cash=cash is not None and any(not r.get('collected') for r in cash.records)
            key='' if waiting_cash and target is None else str(getattr(ctx.interact_slot_machine,'key','E') or '')
            ctx._call(self.key_text,'SetText',InText=key)
        except Exception as error:
            ctx._report['interaction_prompt_error']=str(error)
            if self.overlay is not None:
                try:ctx._call(self.overlay,'SetVisibility',InVisibility=1)
                except Exception:pass
            self.text=None
        return Block

    def audio_service(self):
        if not self.settings['sounds']:return None
        if not any((self.ctx._ROOT/'audio'/name).is_file() for name in ('ASSET_SOURCE.txt','BL2_ORIGINAL.txt')):
            self.ctx._report['audio']={'state':'awaiting prepared audio assets'}
            return None
        if self.player is None:
            if self.settings.get('audio_mode','positional')=='positional':
                from .positioned_audio import PositionedAudio
                self.player=PositionedAudio(self.ctx,self.ctx._ROOT/'audio',self.folder,self.ctx._report)
            else:
                from .positioned_audio import PositionedAudio
                self.player=PositionedAudio(self.ctx,self.ctx._ROOT/'audio',self.folder,self.ctx._report,spatial=False)
        return self.player

    def sound(self,name):
        try:
            player=self.audio_service()
            if player is not None:player.submit(name)
        except Exception as error:
            self.ctx._report['audio']={'state':'unavailable','event':name,'error':str(error)}

    def spin_sound(self,active):
        try:
            player=self.audio_service()
            if player is not None:
                player.submit('spin',(active['pull_length'],[p['duration'] for p in active['stop_plans']]))
        except Exception as error:
            self.ctx._report['audio']={'state':'unavailable','event':'spin','error':str(error)}
