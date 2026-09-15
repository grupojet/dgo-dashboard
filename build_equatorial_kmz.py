#!/usr/bin/env python3
"""Gera um KMZ estadual otimizado para telecom a partir da BDGD da Equatorial GO.

O resultado usa NetworkLinks e mosaicos KML internos para evitar carregar todos os
milhões de elementos ao mesmo tempo no Google Earth. As geometrias são agrupadas
por tipo de rede, reduzindo drasticamente o peso em comparação com um Placemark
por ativo.
"""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import sys
import time
import zipfile
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from osgeo import ogr, osr

ogr.UseExceptions()

TILE_SIZE = float(os.environ.get("TILE_SIZE", "0.50"))
MAX_OPEN_FILES = int(os.environ.get("MAX_OPEN_FILES", "96"))
MIN_LOD_PIXELS = int(os.environ.get("MIN_LOD_PIXELS", "128"))

LAYER_ALIASES = {
    "POSTES": ["PONNOT"],
    "BT": ["SSDBT"],
    "MT": ["SSDMT"],
    "AT": ["SSDAT"],
}

CATEGORY_ORDER = [
    "POSTES",
    "BT_AEREA",
    "BT_SUBTERRANEA",
    "BT_OUTRA",
    "MT_AEREA",
    "MT_SUBTERRANEA",
    "MT_OUTRA",
    "AT_AEREA",
    "AT_SUBTERRANEA",
    "AT_OUTRA",
]

CATEGORY_NAMES = {
    "POSTES": "Postes Equatorial",
    "BT_AEREA": "Baixa tensão - aérea",
    "BT_SUBTERRANEA": "Baixa tensão - subterrânea",
    "BT_OUTRA": "Baixa tensão - não classificada",
    "MT_AEREA": "Média tensão - aérea",
    "MT_SUBTERRANEA": "Média tensão - subterrânea",
    "MT_OUTRA": "Média tensão - não classificada",
    "AT_AEREA": "Alta tensão - aérea",
    "AT_SUBTERRANEA": "Alta tensão - subterrânea",
    "AT_OUTRA": "Alta tensão - não classificada",
}

# KML usa AABBGGRR.
STYLE_XML = {
    "POSTES": """<Style id=\"POSTES\"><IconStyle><color>ff00ffff</color><scale>0.35</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>""",
    "BT_AEREA": """<Style id=\"BT_AEREA\"><LineStyle><color>ffff0000</color><width>1.2</width></LineStyle></Style>""",
    "BT_SUBTERRANEA": """<Style id=\"BT_SUBTERRANEA\"><LineStyle><color>ffff00ff</color><width>1.6</width></LineStyle></Style>""",
    "BT_OUTRA": """<Style id=\"BT_OUTRA\"><LineStyle><color>ffb0b0b0</color><width>1.0</width></LineStyle></Style>""",
    "MT_AEREA": """<Style id=\"MT_AEREA\"><LineStyle><color>ff0099ff</color><width>1.8</width></LineStyle></Style>""",
    "MT_SUBTERRANEA": """<Style id=\"MT_SUBTERRANEA\"><LineStyle><color>ffcc33cc</color><width>2.0</width></LineStyle></Style>""",
    "MT_OUTRA": """<Style id=\"MT_OUTRA\"><LineStyle><color>ff808080</color><width>1.4</width></LineStyle></Style>""",
    "AT_AEREA": """<Style id=\"AT_AEREA\"><LineStyle><color>ff0000ff</color><width>2.4</width></LineStyle></Style>""",
    "AT_SUBTERRANEA": """<Style id=\"AT_SUBTERRANEA\"><LineStyle><color>ff800080</color><width>2.4</width></LineStyle></Style>""",
    "AT_OUTRA": """<Style id=\"AT_OUTRA\"><LineStyle><color>ff404040</color><width>2.0</width></LineStyle></Style>""",
}


class LRUWriters:
    def __init__(self, max_open: int):
        self.max_open = max_open
        self.handles: "OrderedDict[Path, object]" = OrderedDict()

    def get(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path in self.handles:
            handle = self.handles.pop(path)
            self.handles[path] = handle
            return handle
        if len(self.handles) >= self.max_open:
            _, old = self.handles.popitem(last=False)
            old.close()
        handle = path.open("a", encoding="utf-8", buffering=1024 * 1024)
        self.handles[path] = handle
        return handle

    def close_all(self):
        while self.handles:
            _, handle = self.handles.popitem(last=False)
            handle.close()


def field_names(layer: ogr.Layer) -> Dict[str, str]:
    definition = layer.GetLayerDefn()
    result: Dict[str, str] = {}
    for i in range(definition.GetFieldCount()):
        name = definition.GetFieldDefn(i).GetName()
        result[name.upper()] = name
    return result


def find_field(fields: Dict[str, str], candidates: Iterable[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate.upper() in fields:
            return fields[candidate.upper()]
    return None


def locate_layer(ds: ogr.DataSource, aliases: Iterable[str]) -> Optional[ogr.Layer]:
    names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    by_upper = {name.upper(): name for name in names}
    for alias in aliases:
        if alias.upper() in by_upper:
            return ds.GetLayerByName(by_upper[alias.upper()])
    for alias in aliases:
        alias_u = alias.upper()
        for name in names:
            name_u = name.upper()
            if name_u.endswith(alias_u) or alias_u in name_u:
                return ds.GetLayerByName(name)
    return None


def traditional_srs(srs: osr.SpatialReference) -> osr.SpatialReference:
    clone = srs.Clone()
    try:
        clone.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return clone


def build_transform(layer: ogr.Layer) -> Optional[osr.CoordinateTransformation]:
    source = layer.GetSpatialRef()
    if source is None:
        return None
    source = traditional_srs(source)
    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)
    target = traditional_srs(target)
    if source.IsSame(target):
        return None
    return osr.CoordinateTransformation(source, target)


def classify_installation(value: object) -> str:
    text = "" if value is None else str(value).strip().upper()
    if "SUB" in text:
        return "SUBTERRANEA"
    if "AER" in text:
        return "AEREA"
    # Em diversas remessas da BDGD, segmentos sem TIP_INST são redes aéreas.
    # Mantemos em OUTRA para não afirmar algo que não esteja explicitamente codificado.
    return "OUTRA"


def safe_float(value: object) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def tile_key(lon: float, lat: float) -> Tuple[int, int]:
    return (math.floor((lon + 180.0) / TILE_SIZE), math.floor((lat + 90.0) / TILE_SIZE))


def tile_filename(key: Tuple[int, int]) -> str:
    return f"tile_{key[0]:03d}_{key[1]:03d}.kml"


def region_xml(west: float, south: float, east: float, north: float) -> str:
    return (
        "<Region><LatLonAltBox>"
        f"<north>{north:.8f}</north><south>{south:.8f}</south>"
        f"<east>{east:.8f}</east><west>{west:.8f}</west>"
        "</LatLonAltBox><Lod>"
        f"<minLodPixels>{MIN_LOD_PIXELS}</minLodPixels><maxLodPixels>-1</maxLodPixels>"
        "</Lod></Region>"
    )


def main() -> int:
    if len(sys.argv) < 3:
        print("Uso: build_equatorial_kmz.py <arquivo.gdb> <diretorio_saida>", file=sys.stderr)
        return 2

    gdb = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "_work"
    fragments_dir = work_dir / "fragments"
    tiles_dir = work_dir / "tiles"
    shutil.rmtree(work_dir, ignore_errors=True)
    fragments_dir.mkdir(parents=True)
    tiles_dir.mkdir(parents=True)

    ds = ogr.Open(str(gdb), 0)
    if ds is None:
        raise RuntimeError(f"Não foi possível abrir a geodatabase: {gdb}")

    all_layers = []
    for i in range(ds.GetLayerCount()):
        layer = ds.GetLayerByIndex(i)
        all_layers.append({
            "name": layer.GetName(),
            "geometry_type": ogr.GeometryTypeToName(layer.GetGeomType()),
            "feature_count": int(layer.GetFeatureCount()),
            "fields": list(field_names(layer).values()),
        })
    (out_dir / "camadas_bdgd.json").write_text(
        json.dumps(all_layers, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    writers = LRUWriters(MAX_OPEN_FILES)
    tile_meta: Dict[Tuple[int, int], Dict[str, object]] = {}
    summary = defaultdict(lambda: {"count": 0, "length_m": 0.0})
    selected_layers = {}
    started = time.time()

    for logical, aliases in LAYER_ALIASES.items():
        layer = locate_layer(ds, aliases)
        if layer is None:
            print(f"AVISO: camada {logical} não encontrada (aliases={aliases})")
            continue

        selected_layers[logical] = layer.GetName()
        fields = field_names(layer)
        tip_pn_field = find_field(fields, ["TIP_PN", "TIPPN", "TIPO_PONTO"])
        tip_inst_field = find_field(fields, ["TIP_INST", "TIPINST", "TIPO_INST", "TIPO_INSTALACAO"])
        comp_field = find_field(fields, ["COMP", "COMPRIMENTO", "COMPR_M"])
        transform = build_transform(layer)

        print(
            f"Processando {logical}: layer={layer.GetName()} features={layer.GetFeatureCount()} "
            f"TIP_PN={tip_pn_field} TIP_INST={tip_inst_field} COMP={comp_field}"
        )

        layer.ResetReading()
        processed = 0
        skipped = 0
        while True:
            feature = layer.GetNextFeature()
            if feature is None:
                break
            try:
                if logical == "POSTES" and tip_pn_field:
                    tip_pn = feature.GetField(tip_pn_field)
                    if str(tip_pn).strip().upper() != "POS":
                        skipped += 1
                        continue

                geometry_ref = feature.GetGeometryRef()
                if geometry_ref is None or geometry_ref.IsEmpty():
                    skipped += 1
                    continue
                geometry = geometry_ref.Clone()
                if transform is not None:
                    geometry.Transform(transform)

                envelope = geometry.GetEnvelope()  # minx, maxx, miny, maxy
                west, east, south, north = envelope[0], envelope[1], envelope[2], envelope[3]
                if not all(math.isfinite(v) for v in (west, east, south, north)):
                    skipped += 1
                    continue
                lon = (west + east) / 2.0
                lat = (south + north) / 2.0
                if lon < -180 or lon > 180 or lat < -90 or lat > 90:
                    skipped += 1
                    continue

                key = tile_key(lon, lat)
                if logical == "POSTES":
                    category = "POSTES"
                else:
                    install_value = feature.GetField(tip_inst_field) if tip_inst_field else None
                    category = f"{logical}_{classify_installation(install_value)}"

                fragment_path = fragments_dir / f"{key[0]}_{key[1]}" / f"{category}.xml"
                xml = geometry.ExportToKML()
                if not xml:
                    skipped += 1
                    continue
                writers.get(fragment_path).write(xml)
                writers.get(fragment_path).write("\n")

                meta = tile_meta.setdefault(
                    key,
                    {
                        "west": west,
                        "east": east,
                        "south": south,
                        "north": north,
                        "categories": defaultdict(int),
                    },
                )
                meta["west"] = min(float(meta["west"]), west)
                meta["east"] = max(float(meta["east"]), east)
                meta["south"] = min(float(meta["south"]), south)
                meta["north"] = max(float(meta["north"]), north)
                meta["categories"][category] += 1

                summary[category]["count"] += 1
                if comp_field:
                    summary[category]["length_m"] += safe_float(feature.GetField(comp_field))
                processed += 1
                if processed % 250000 == 0:
                    elapsed = time.time() - started
                    print(f"  {logical}: {processed:,} processados; {skipped:,} ignorados; {elapsed/60:.1f} min")
            finally:
                feature = None

        print(f"Concluído {logical}: {processed:,} processados; {skipped:,} ignorados")

    writers.close_all()

    if not tile_meta:
        raise RuntimeError("Nenhuma geometria foi processada; consulte camadas_bdgd.json")

    style_block = "\n".join(STYLE_XML.values())
    tile_records = []
    for key in sorted(tile_meta):
        meta = tile_meta[key]
        filename = tile_filename(key)
        tile_path = tiles_dir / filename
        with tile_path.open("w", encoding="utf-8", buffering=1024 * 1024) as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write('<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n')
            out.write(f"<name>Equatorial GO - {filename[:-4]}</name>\n")
            out.write(style_block)
            out.write("\n")
            out.write(
                region_xml(
                    float(meta["west"]), float(meta["south"]),
                    float(meta["east"]), float(meta["north"]),
                )
            )
            out.write("\n")
            for category in CATEGORY_ORDER:
                fragment_path = fragments_dir / f"{key[0]}_{key[1]}" / f"{category}.xml"
                if not fragment_path.exists():
                    continue
                count = int(meta["categories"].get(category, 0))
                out.write("<Folder>")
                out.write(f"<name>{CATEGORY_NAMES[category]} ({count:,})</name>")
                out.write("<visibility>1</visibility>")
                out.write("<Placemark>")
                out.write(f"<name>{CATEGORY_NAMES[category]}</name><styleUrl>#{category}</styleUrl><MultiGeometry>\n")
                with fragment_path.open("r", encoding="utf-8") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                out.write("</MultiGeometry></Placemark></Folder>\n")
            out.write("</Document></kml>\n")

        tile_records.append(
            {
                "filename": filename,
                "west": float(meta["west"]),
                "east": float(meta["east"]),
                "south": float(meta["south"]),
                "north": float(meta["north"]),
            }
        )

    master_path = work_dir / "doc.kml"
    with master_path.open("w", encoding="utf-8") as master:
        master.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        master.write('<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n')
        master.write("<name>Equatorial Goiás - Posteamento e Redes para Telecom</name>\n")
        master.write(
            "<description><![CDATA[Base BDGD ANEEL/Equatorial GO, posição 31/12/2025. "
            "Camadas: postes, BT, MT e AT; classificação aérea/subterrânea conforme TIP_INST. "
            "Arquivo organizado em mosaicos carregados conforme a área visualizada.]]></description>\n"
        )
        for record in tile_records:
            master.write("<NetworkLink>")
            master.write(f"<name>{record['filename'][:-4]}</name>")
            master.write(
                region_xml(record["west"], record["south"], record["east"], record["north"])
            )
            master.write("<Link>")
            master.write(f"<href>tiles/{record['filename']}</href>")
            master.write("<viewRefreshMode>onRegion</viewRefreshMode>")
            master.write("</Link></NetworkLink>\n")
        master.write("</Document></kml>\n")

    kmz_path = out_dir / "EQUATORIAL_GO_POSTEAMENTO_REDES_TELECOM_2025.kmz"
    with zipfile.ZipFile(kmz_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as kmz:
        kmz.write(master_path, "doc.kml")
        for record in tile_records:
            kmz.write(tiles_dir / record["filename"], f"tiles/{record['filename']}")

    summary_path = out_dir / "EQUATORIAL_GO_RESUMO_TELECOM.csv"
    with summary_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["categoria", "descricao", "quantidade", "comprimento_m", "comprimento_km"])
        for category in CATEGORY_ORDER:
            row = summary.get(category, {"count": 0, "length_m": 0.0})
            writer.writerow(
                [
                    category,
                    CATEGORY_NAMES[category],
                    int(row["count"]),
                    f"{float(row['length_m']):.3f}",
                    f"{float(row['length_m']) / 1000.0:.3f}",
                ]
            )

    diagnostics = {
        "source_gdb": str(gdb),
        "selected_layers": selected_layers,
        "tile_size_degrees": TILE_SIZE,
        "tiles": len(tile_records),
        "summary": summary,
        "kmz_bytes": kmz_path.stat().st_size,
        "elapsed_seconds": time.time() - started,
    }
    (out_dir / "EQUATORIAL_GO_DIAGNOSTICO.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2, default=dict), encoding="utf-8"
    )

    readme = out_dir / "LEIA-ME_EQUATORIAL_GO_TELECOM.txt"
    readme.write_text(
        "EQUATORIAL GO - POSTEAMENTO E REDES PARA TELECOM\n"
        "Fonte: BDGD ANEEL / Equatorial GO, posição 31/12/2025.\n\n"
        "O KMZ utiliza mosaicos internos e NetworkLinks para melhorar o desempenho no Google Earth.\n"
        "Cores: postes amarelos; BT aérea azul; MT aérea laranja; AT aérea vermelha; redes subterrâneas em roxo/magenta.\n"
        "A presença de poste ou rede não confirma vaga técnica para telecom. A ocupação depende de projeto, vistoria e aprovação da distribuidora.\n"
        "Os comprimentos do CSV usam o campo COMP quando disponível na camada da BDGD.\n",
        encoding="utf-8",
    )

    shutil.rmtree(work_dir, ignore_errors=True)
    print(f"KMZ criado: {kmz_path} ({kmz_path.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"Mosaicos: {len(tile_records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
