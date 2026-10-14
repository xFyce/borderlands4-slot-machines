"""Replace the old glyph before placing a compact original pearl illustration."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image,ImageDraw,ImageFilter

REGION=(953,969,1023,1018)

def apply_pearl_symbol(image,art=None,size=34):
    if image.size!=(1024,1024):raise ValueError('Unexpected BL2 reel atlas dimensions')
    image=image.convert('RGB').copy();tile=image.crop(REGION)
    source=np.asarray(tile,dtype=float);h,w=source.shape[:2]
    purple=(source[:,:,0]>45)&(source[:,:,2]>source[:,:,1]*1.2)&(source[:,:,2]>source[:,:,0]*.7)
    ys,xs=np.where(purple)
    if not len(xs):raise ValueError('Could not locate the original double-Eridium symbol')
    fringe=(source[:,:,0]>source[:,:,1]*1.15)&(source[:,:,2]>source[:,:,1]*.7)&(source[:,:,2]>60)
    ys,xs=np.where(purple|fringe)
    # Include the original black outline and gaps between the two bars, not
    # just the purple pixels. The new icon's size is independent of this mask.
    points=sorted(set(zip(xs.tolist(),ys.tolist())))
    def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    lower=[];upper=[]
    for chain,values in ((lower,points),(upper,reversed(points))):
        for p in values:
            while len(chain)>1 and cross(chain[-2],chain[-1],p)<=0:chain.pop()
            chain.append(p)
    erase=Image.new('L',(w,h));ImageDraw.Draw(erase).polygon(lower[:-1]+upper[:-1],fill=255)
    erase=erase.filter(ImageFilter.MaxFilter(7));mask=np.asarray(erase)>0
    # Preserve the actual outer frame while clearing compression tint just
    # beyond the old purple silhouette, including the left pink fringe.
    mask[:4]=False;mask[45:]=False;mask[:,:6]=False;mask[:,66:]=False
    # Fit the paper's local colour/shading from this player's clean yellow
    # samples. No copied BL2 background is included in the extractor download.
    yy,xx=np.mgrid[:h,:w];x=(xx-w/2)/w;y=(yy-h/2)/h
    basis=np.stack((np.ones_like(x),x,y,x*x,x*y,y*y),axis=-1)
    paper=(~mask)&(source[:,:,0]>75)&(source[:,:,1]>65)&(source[:,:,2]<source[:,:,1]*.78)
    if paper.sum()<50:raise ValueError('Not enough clean reel paper to rebuild the background')
    coefficients=np.linalg.lstsq(basis[paper],source[paper],rcond=None)[0]
    background=basis@coefficients
    # Match the actual boundary colours smoothly; a fitted patch alone can
    # leave the old gem's polygon visible as a differently shaded area.
    filled=source.copy();filled[mask]=background[mask]
    for _ in range(900):
        neighbors=(np.roll(filled,1,0)+np.roll(filled,-1,0)+np.roll(filled,1,1)+np.roll(filled,-1,1))*.25
        delta=float(np.max(abs(neighbors[mask]-filled[mask])))
        filled[mask]=neighbors[mask]
        if delta<.005:break
    # Reuse tiny local paper residuals to retain the native printed texture.
    residual=source[paper]-background[paper]
    indices=(xx*17+yy*31)%len(residual)
    filled[mask]+=np.clip(residual[indices][mask],-10,10)*.25
    cleaned=source.copy();cleaned[mask]=filled[mask]
    tile=Image.fromarray(np.uint8(np.clip(cleaned,0,255)))
    if art is None:art=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'pearl_icon.png'
    icon=Image.open(art).convert('RGBA');bounds=icon.getchannel('A').point(lambda a:255 if a>16 else 0).getbbox()
    if bounds is None:raise ValueError('Pearl artwork is empty')
    icon=icon.crop(bounds);icon.thumbnail((size,size),Image.Resampling.LANCZOS)
    tile.paste(icon,((w-icon.width)//2,(h-icon.height)//2),icon)
    image.paste(tile,REGION[:2])
    return image
