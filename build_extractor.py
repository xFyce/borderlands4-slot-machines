"""Build the Windows extractor from these review sources and upstream tools."""
from pathlib import Path
import argparse,os

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools',type=Path,required=True,
        help='Folder containing umodel and vgmstream subfolders with their EXEs/DLLs')
    parser.add_argument('--dist',type=Path,default=Path('dist'))
    parser.add_argument('--work',type=Path,default=Path('build'))
    args=parser.parse_args()
    tools=args.tools.resolve()
    for name in ('umodel/umodel.exe','vgmstream/vgmstream-cli.exe'):
        if not (tools/name).is_file():parser.error('Missing upstream tool: '+name)
    os.environ['SLOT_MACHINES_TOOLS']=str(tools)
    import PyInstaller.__main__
    PyInstaller.__main__.run([str(Path(__file__).with_name('extractor.spec')),
        '--distpath',str(args.dist.resolve()),'--workpath',str(args.work.resolve()),'--noconfirm'])

if __name__=='__main__':main()
