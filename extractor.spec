from pathlib import Path
import os

root=Path(SPECPATH)
source=root/'extractor'
tools=Path(os.environ['SLOT_MACHINES_TOOLS'])
data=[(str(source/name),'.') for name in (
    'pearl_icon.png','cabinet_glow_template.py','bl2_lighting_recipe.json','audio_recipe.json')]
data += [(str(tools/'umodel'),'tools/umodel'),(str(tools/'vgmstream'),'tools/vgmstream')]
a=Analysis([str(source/'extractor.py')],pathex=[],binaries=[],datas=data,
    hiddenimports=[],hookspath=[],hooksconfig={},runtime_hooks=[],excludes=[],
    noarchive=True,optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='Prepare BL2 Assets',
    debug=False,bootloader_ignore_signals=False,strip=False,upx=True,
    console=False,disable_windowed_traceback=False,argv_emulation=False,
    target_arch=None,codesign_identity=None,entitlements_file=None)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=True,upx_exclude=[],name='Prepare BL2 Assets')
