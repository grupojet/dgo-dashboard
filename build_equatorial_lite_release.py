#!/usr/bin/env python3
from pathlib import Path
from collections import defaultdict
import csv, re, sys, zipfile
import xml.etree.ElementTree as ET

GROUP=4
TAG='equatorial-go-bdgd-2025-lite'
BASE=f'https://github.com/grupojet/dgo-dashboard/releases/download/{TAG}'

def bounds(path):
    m=re.match(r'tile_(\d+)_(\d+)\.kml$',path.name)
    if not m: return None
    x,y=map(int,m.groups()); b={}
    for _,e in ET.iterparse(path,events=('end',)):
        t=e.tag.rsplit('}',1)[-1]
        if t in {'north','south','east','west'} and t not in b: b[t]=float(e.text)
        if len(b)==4: break
        e.clear()
    return x,y,b

def region(b,lod):
    return '<Region><LatLonAltBox>'+''.join(f'<{k}>{b[k]:.8f}</{k}>' for k in ('north','south','east','west'))+f'</LatLonAltBox><Lod><minLodPixels>{lod}</minLodPixels><maxLodPixels>-1</maxLodPixels></Lod></Region>'

def main(src,out):
    src,out=Path(src),Path(out); out.mkdir(parents=True,exist_ok=True); rd=out/'regions'; rd.mkdir(exist_ok=True)
    icon=src/'icons/poste_equatorial.png'; tiles=[]
    for p in sorted((src/'tiles').glob('tile_*.kml')):
        v=bounds(p)
        if v: tiles.append((p,*v))
    groups=defaultdict(list)
    for p,x,y,b in tiles: groups[(x//GROUP,y//GROUP)].append((p,x,y,b))
    rec=[]
    for (gx,gy),items in sorted(groups.items()):
        b={'west':min(i[3]['west'] for i in items),'east':max(i[3]['east'] for i in items),'south':min(i[3]['south'] for i in items),'north':max(i[3]['north'] for i in items)}
        name=f'GO_REGIAO_{gx:03d}_{gy:03d}'; fn=name+'.kmz'
        doc=['<?xml version="1.0" encoding="UTF-8"?>','<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',f'<name>Equatorial Goiás - Região {gx:03d}/{gy:03d}</name>','<open>1</open>']
        for p,_,_,tb in items:
            doc+=['<NetworkLink>',f'<name>{p.stem}</name>',region(tb,32),f'<Link><href>tiles/{p.name}</href><viewRefreshMode>onRegion</viewRefreshMode></Link>','</NetworkLink>']
        doc+=['</Document></kml>']
        rp=rd/fn
        with zipfile.ZipFile(rp,'w',zipfile.ZIP_DEFLATED,compresslevel=9,allowZip64=True) as z:
            z.writestr('doc.kml','\n'.join(doc)); z.write(icon,'icons/poste_equatorial.png')
            for p,_,_,_ in items: z.write(p,'tiles/'+p.name)
        rec.append({'name':name,'file':fn,'gx':gx,'gy':gy,**b,'tiles':len(items),'size_bytes':rp.stat().st_size})
    styles='<Style id="g"><LineStyle><color>aa00ffff</color><width>1.2</width></LineStyle><PolyStyle><color>1000ffff</color><fill>1</fill><outline>1</outline></PolyStyle></Style><Style id="p"><IconStyle><scale>1</scale><Icon><href>icons/poste_equatorial.png</href></Icon></IconStyle></Style>'
    doc=['<?xml version="1.0" encoding="UTF-8"?>','<kml xmlns="http://www.opengis.net/kml/2.2"><Document>','<name>Equatorial Goiás - Telecom LITE</name>','<description><![CDATA[Versão online leve. Aproxime a cidade; o Google Earth baixa somente a região visível. Necessita internet.]]></description>','<open>1</open>','<LookAt><longitude>-49.27</longitude><latitude>-16.68</latitude><range>120000</range></LookAt>',styles,'<Placemark><name>Aproxime para carregar postes e redes</name><styleUrl>#p</styleUrl><Point><coordinates>-49.27,-16.68,0</coordinates></Point></Placemark>','<Folder><name>Regiões</name><open>1</open>']
    for r in rec:
        doc+=['<Placemark>',f'<name>Região {r["gx"]:03d}/{r["gy"]:03d}</name><styleUrl>#g</styleUrl>','<Polygon><outerBoundaryIs><LinearRing><coordinates>',f'{r["west"]},{r["south"]},0 {r["east"]},{r["south"]},0 {r["east"]},{r["north"]},0 {r["west"]},{r["north"]},0 {r["west"]},{r["south"]},0','</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>','<NetworkLink>',f'<name>Dados detalhados {r["gx"]:03d}/{r["gy"]:03d}</name>',region(r,64),f'<Link><href>{BASE}/{r["file"]}</href><viewRefreshMode>onRegion</viewRefreshMode></Link>','</NetworkLink>']
    doc+=['</Folder></Document></kml>']
    master=out/'EQUATORIAL_GO_TELECOM_LITE_ONLINE.kmz'
    with zipfile.ZipFile(master,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        z.writestr('doc.kml','\n'.join(doc)); z.write(icon,'icons/poste_equatorial.png')
    with (out/'REGIOES_LITE.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=['name','file','gx','gy','west','south','east','north','tiles','size_bytes']); w.writeheader(); w.writerows(rec)
    print(len(tiles),len(rec),master.stat().st_size)

if __name__=='__main__': main(sys.argv[1],sys.argv[2])
