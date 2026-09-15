#!/usr/bin/env python3
"""Converte a BDGD da Equatorial GO em KMZ estadual otimizado para telecom.

O arquivo final contém postes e redes BT/MT/AT organizados em mosaicos internos.
Os mosaicos usam Region/NetworkLink para o Google Earth carregar somente a área
visível. As geometrias de cada categoria são agrupadas em MultiGeometry, evitando
milhões de Placemarks individuais.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
import shutil
import sys
import time
import zipfile
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from osgeo import ogr, osr

ogr.UseExceptions()

TILE_SIZE = float(os.environ.get("TILE_SIZE", "0.25"))
MAX_OPEN_FILES = int(os.environ.get("MAX_OPEN_FILES", "128"))
MIN_LOD_PIXELS = int(os.environ.get("MIN_LOD_PIXELS", "96"))
LOG_EVERY = int(os.environ.get("LOG_EVERY", "250000"))

LAYER_ALIASES = {
    "POSTES": ["PONNOT"],
    "BT": ["SSDBT"],
    "MT": ["SSDMT"],
    "AT": ["SSDAT"],
}

CATEGORY_ORDER = [
    "POSTES",
    "BT_AEREA", "BT_SUBTERRANEA", "BT_OUTRA",
    "MT_AEREA", "MT_SUBTERRANEA", "MT_OUTRA",
    "AT_AEREA", "AT_SUBTERRANEA", "AT_OUTRA",
]

CATEGORY_NAMES = {
    "POSTES": "Postes Equatorial",
    "BT_AEREA": "Baixa tensão - aérea",
    "BT_SUBTERRANEA": "Baixa tensão - subterrânea",
    "BT_OUTRA": "Baixa tensão - instalação não classificada",
    "MT_AEREA": "Média tensão - aérea",
    "MT_SUBTERRANEA": "Média tensão - subterrânea",
    "MT_OUTRA": "Média tensão - instalação não classificada",
    "AT_AEREA": "Alta tensão - aérea",
    "AT_SUBTERRANEA": "Alta tensão - subterrânea",
    "AT_OUTRA": "Alta tensão - instalação não classificada",
}

# KML utiliza a ordem AABBGGRR.
STYLE_XML = {
    "POSTES": '<Style id="POSTES"><IconStyle><color>ff00ffff</color><scale>0.35</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon></IconStyle><LabelStyle><scale>0</scale></LabelStyle></Style>',
    "BT_AEREA": '<Style id="BT_AEREA"><LineStyle><color>ffff0000</color><width>1.2</width></LineStyle></Style>',
    "BT_SUBTERRANEA": '<Style id="BT_SUBTERRANEA"><LineStyle><color>ffff00ff</color><width>1.6</width></LineStyle></Style>',
    "BT_OUTRA": '<Style id="BT_OUTRA"><LineStyle><color>ffb0b0b0</color><width>1.0</width></LineStyle></Style>',
    "MT_AEREA": '<Style id="MT_AEREA"><LineStyle><color>ff0099ff</color><width>1.8</width></LineStyle></Style>',
    "MT_SUBTERRANEA": '<Style id="MT_SUBTERRANEA"><LineStyle><color>ffcc33cc</color><width>2.0</width></LineStyle></Style>',
    "MT_OUTRA": '<Style id="MT_OUTRA"><LineStyle><color>ff808080</color><width>1.4</width></LineStyle></Style>',
    "AT_AEREA": '<Style id="AT_AEREA"><LineStyle><color>ff0000ff</color><width>2.4</width></LineStyle></Style>',
    "AT_SUBTERRANEA": '<Style id="AT_SUBTERRANEA"><LineStyle><color>ff800080</color><width>2.4</width></LineStyle></Style>',
    "AT_OUTRA": '<Style id="AT_OUTRA"><LineStyle><color>ff404040</color><width>2.0</width></LineStyle></Style>',
}


class GzipLRU:
    """Mantém um número limitado de fragmentos gzip abertos para escrita."""

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
        handle = gzip.open(path, mode="at", encoding="utf-8", compresslevel=1)
        self.handles[path] = handle
        return handle

    def close_all(self) -> None:
        while self.handles:
            _, handle = self.handles.popitem(last=False)
            handle.close()


def fields_map(layer: ogr.Layer) -> Dict[str, str]:
    definition = layer.GetLayerDefn()
    result: Dict[str, str] = {}
    for index in range(definition.GetFieldCount()):
        name = definition.GetFieldDefn(index).GetName()
        result[name.upper()] = name
    return result


def first_field(fields: Dict[str, str], candidates: Iterable[str]) -> Optional[str]:
    for candidate in candidates:
        found = fields.get(candidate.upper())
        if found:
            return found
    return None


def find_layer(ds: ogr.DataSource, aliases: Iterable[str]) -> Optional[ogr.Layer]:
    layer_names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    exact = {name.upper(): name for name in layer_names}
    for alias in aliases:
        if alias.upper() in exact:
            return ds.GetLayerByName(exact[alias.upper()])
    for alias in aliases:
        alias_upper = alias.upper()
        for name in layer_names:
            name_upper = name.upper()
            if name_upper.endswith(alias_upper) or alias_upper in name_upper:
                return ds.GetLayerByName(name)
    return None


def normalized_srs(srs: osr.SpatialReference) -> osr.SpatialReference:
    result = srs.Clone()
    try:
        result.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return result


def coordinate_transform(layer: ogr.Layer) -> Optional[osr.CoordinateTransformation]:
    source = layer.GetSpatialRef()
    if source is None:
        return None
    source = normalized_srs(source)
    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)
    target = normalized_srs(target)
    if source.IsSame(target):
        return None
    return osr.CoordinateTransformation(source, target)


def installation_group(value: object) -> str:
    text = "" if value is None else str(value).strip().upper()
    if "SUB" in text:
        return "SUBTERRANEA"
    if "AER" in text:
        return "AEREA"
    return "OUTRA"


def number(value: object) -> float:
    try:
        return 0.0 if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return 0.0


def tile_key(longitude: float, latitude: float) -> Tuple[int, int]:
    return (
        math.floor((longitude + 180.0) / TILE_SIZE),
        math.floor((latitude + 90.0) / TILE_SIZE),
    )


def tile_name(key: Tuple[int, int]) -> str:
    return f"tile_{key[0]:04d}_{key[1]:04d}.kml"


def region(west: float, south: float, east: float, north: float) -> str:
    return (
        "<Region><LatLonAltBox>"
        f"<north>{north:.8f}</north><south>{south:.8f}</south>"
        f"<east>{east:.8f}</east><west>{west:.8f}</west>"
        "</LatLonAltBox><Lod>"
        f"<minLodPixels>{MIN_LOD_PIXELS}</minLodPixels><maxLodPixels>-1</maxLodPixels>"
        "</Lod></Region>"
    )


def stream_file(source, destination, chunk_size: int = 1024 * 1024) -> None:
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            return
        destination.write(chunk)


def main() -> int:
    if len(sys.argv) != 3:
        print("Uso: build_equatorial_kmz_stream.py <arquivo.gdb> <diretorio_saida>", file=sys.stderr)
        return 2

    gdb_path = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / "_fragmentos"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)

    datasource = ogr.Open(str(gdb_path), 0)
    if datasource is None:
        raise RuntimeError(f"Não foi possível abrir: {gdb_path}")

    inventory = []
    for index in range(datasource.GetLayerCount()):
        layer = datasource.GetLayerByIndex(index)
        inventory.append({
            "name": layer.GetName(),
            "geometry_type": ogr.GeometryTypeToName(layer.GetGeomType()),
            "feature_count": int(layer.GetFeatureCount()),
            "fields": list(fields_map(layer).values()),
            "srs": layer.GetSpatialRef().ExportToWkt() if layer.GetSpatialRef() else None,
        })
    (output_dir / "camadas_bdgd.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    writers = GzipLRU(MAX_OPEN_FILES)
    tile_metadata: Dict[Tuple[int, int], Dict[str, object]] = {}
    totals = defaultdict(lambda: {"count": 0, "length_m": 0.0})
    installation_values: Dict[str, Counter] = defaultdict(Counter)
    selected_layers: Dict[str, str] = {}
    started = time.time()

    for logical_name, aliases in LAYER_ALIASES.items():
        layer = find_layer(datasource, aliases)
        if layer is None:
            print(f"AVISO: camada {logical_name} não encontrada")
            continue

        selected_layers[logical_name] = layer.GetName()
        fields = fields_map(layer)
        type_point_field = first_field(fields, ["TIP_PN", "TIPPN", "TIPO_PONTO"])
        installation_field = first_field(fields, ["TIP_INST", "TIPINST", "TIPO_INST", "TIPO_INSTALACAO"])
        length_field = first_field(fields, ["COMP", "COMPRIMENTO", "COMPR_M"])
        transform = coordinate_transform(layer)

        if logical_name == "POSTES" and type_point_field:
            try:
                layer.SetAttributeFilter(f"{type_point_field} = 'POS'")
            except Exception as error:
                print(f"AVISO: filtro TIP_PN não aplicado: {error}")

        declared_count = int(layer.GetFeatureCount())
        print(
            f"INICIO {logical_name}: layer={layer.GetName()} features={declared_count:,} "
            f"TIP_PN={type_point_field} TIP_INST={installation_field} COMP={length_field}",
            flush=True,
        )

        processed = 0
        skipped = 0
        layer.ResetReading()
        while True:
            feature = layer.GetNextFeature()
            if feature is None:
                break
            try:
                geometry_reference = feature.GetGeometryRef()
                if geometry_reference is None or geometry_reference.IsEmpty():
                    skipped += 1
                    continue
                geometry = geometry_reference.Clone()
                try:
                    geometry.FlattenTo2D()
                except Exception:
                    pass
                if transform is not None:
                    geometry.Transform(transform)

                min_x, max_x, min_y, max_y = geometry.GetEnvelope()
                if not all(math.isfinite(value) for value in (min_x, max_x, min_y, max_y)):
                    skipped += 1
                    continue
                longitude = (min_x + max_x) / 2.0
                latitude = (min_y + max_y) / 2.0
                if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
                    skipped += 1
                    continue

                if logical_name == "POSTES":
                    category = "POSTES"
                else:
                    installation_value = feature.GetField(installation_field) if installation_field else None
                    installation_values[logical_name][str(installation_value)] += 1
                    category = f"{logical_name}_{installation_group(installation_value)}"

                key = tile_key(longitude, latitude)
                fragment = work_dir / f"{key[0]}_{key[1]}" / f"{category}.xml.gz"
                geometry_kml = geometry.ExportToKML()
                if not geometry_kml:
                    skipped += 1
                    continue
                handle = writers.get(fragment)
                handle.write(geometry_kml)
                handle.write("\n")

                metadata = tile_metadata.setdefault(
                    key,
                    {
                        "west": min_x, "east": max_x,
                        "south": min_y, "north": max_y,
                        "categories": defaultdict(int),
                    },
                )
                metadata["west"] = min(float(metadata["west"]), min_x)
                metadata["east"] = max(float(metadata["east"]), max_x)
                metadata["south"] = min(float(metadata["south"]), min_y)
                metadata["north"] = max(float(metadata["north"]), max_y)
                metadata["categories"][category] += 1

                totals[category]["count"] += 1
                if length_field:
                    totals[category]["length_m"] += number(feature.GetField(length_field))

                processed += 1
                if processed % LOG_EVERY == 0:
                    print(
                        f"PROGRESSO {logical_name}: {processed:,} processados, "
                        f"{skipped:,} ignorados, {(time.time() - started) / 60:.1f} min",
                        flush=True,
                    )
            finally:
                feature = None

        layer.SetAttributeFilter(None)
        print(
            f"FIM {logical_name}: {processed:,} processados, {skipped:,} ignorados",
            flush=True,
        )

    writers.close_all()

    if not tile_metadata:
        raise RuntimeError("Nenhuma geometria válida foi encontrada")

    tile_records = []
    for key in sorted(tile_metadata):
        metadata = tile_metadata[key]
        tile_records.append({
            "key": key,
            "filename": tile_name(key),
            "west": float(metadata["west"]),
            "east": float(metadata["east"]),
            "south": float(metadata["south"]),
            "north": float(metadata["north"]),
            "categories": dict(metadata["categories"]),
        })

    master_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        '<name>Equatorial Goiás - Posteamento e Redes para Telecom</name>',
        '<description><![CDATA[BDGD ANEEL/Equatorial GO, posição 31/12/2025. '
        'O mapa contém postes e redes BT, MT e AT. A classificação aérea ou subterrânea '
        'usa o campo TIP_INST quando disponível. A existência do poste não confirma '
        'vaga técnica para telecom; ocupação depende de projeto e aprovação da distribuidora.]]></description>',
    ]
    for record in tile_records:
        master_parts.extend([
            '<NetworkLink>',
            f"<name>{record['filename'][:-4]}</name>",
            region(record["west"], record["south"], record["east"], record["north"]),
            '<Link>',
            f"<href>tiles/{record['filename']}</href>",
            '<viewRefreshMode>onRegion</viewRefreshMode>',
            '</Link></NetworkLink>',
        ])
    master_parts.append('</Document></kml>')
    master_kml = "\n".join(master_parts).encode("utf-8")

    kmz_path = output_dir / "EQUATORIAL_GO_POSTEAMENTO_REDES_TELECOM_2025.kmz"
    style_block = "\n".join(STYLE_XML.values()).encode("utf-8")
    with zipfile.ZipFile(
        kmz_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as kmz:
        kmz.writestr("doc.kml", master_kml)
        for index, record in enumerate(tile_records, start=1):
            arcname = f"tiles/{record['filename']}"
            with kmz.open(arcname, mode="w", force_zip64=True) as destination:
                destination.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                destination.write(b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n')
                destination.write(f"<name>Equatorial GO - {record['filename'][:-4]}</name>\n".encode("utf-8"))
                destination.write(style_block)
                destination.write(b"\n")
                destination.write(
                    region(record["west"], record["south"], record["east"], record["north"]).encode("utf-8")
                )
                destination.write(b"\n")

                key = record["key"]
                for category in CATEGORY_ORDER:
                    fragment = work_dir / f"{key[0]}_{key[1]}" / f"{category}.xml.gz"
                    if not fragment.exists():
                        continue
                    count = int(record["categories"].get(category, 0))
                    destination.write(b"<Folder>")
                    destination.write(
                        f"<name>{CATEGORY_NAMES[category]} ({count:,})</name><visibility>1</visibility>".encode("utf-8")
                    )
                    destination.write(
                        f"<Placemark><name>{CATEGORY_NAMES[category]}</name><styleUrl>#{category}</styleUrl><MultiGeometry>\n".encode("utf-8")
                    )
                    with gzip.open(fragment, mode="rb") as source:
                        stream_file(source, destination)
                    destination.write(b"</MultiGeometry></Placemark></Folder>\n")

                destination.write(b"</Document></kml>\n")
            if index % 100 == 0 or index == len(tile_records):
                print(f"KMZ: {index}/{len(tile_records)} mosaicos gravados", flush=True)

    summary_path = output_dir / "EQUATORIAL_GO_RESUMO_TELECOM.csv"
    with summary_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["categoria", "descricao", "quantidade", "comprimento_m", "comprimento_km"])
        for category in CATEGORY_ORDER:
            values = totals.get(category, {"count": 0, "length_m": 0.0})
            writer.writerow([
                category,
                CATEGORY_NAMES[category],
                int(values["count"]),
                f"{float(values['length_m']):.3f}",
                f"{float(values['length_m']) / 1000.0:.3f}",
            ])

    installation_path = output_dir / "EQUATORIAL_GO_VALORES_TIP_INST.csv"
    with installation_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["camada", "valor_tip_inst", "quantidade"])
        for logical_name in ("BT", "MT", "AT"):
            for value, count in installation_values[logical_name].most_common():
                writer.writerow([logical_name, value, count])

    diagnostic = {
        "source_gdb": str(gdb_path),
        "selected_layers": selected_layers,
        "tile_size_degrees": TILE_SIZE,
        "tile_count": len(tile_records),
        "totals": {key: dict(value) for key, value in totals.items()},
        "kmz_bytes": kmz_path.stat().st_size,
        "elapsed_seconds": time.time() - started,
    }
    (output_dir / "EQUATORIAL_GO_DIAGNOSTICO.json").write_text(
        json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    (output_dir / "LEIA-ME_EQUATORIAL_GO_TELECOM.txt").write_text(
        "EQUATORIAL GO - POSTEAMENTO E REDES PARA TELECOM\n"
        "Fonte: BDGD ANEEL / Equatorial GO, posição 31/12/2025.\n\n"
        "Abra o arquivo KMZ no Google Earth Pro. O conteúdo é carregado por mosaicos conforme o zoom.\n"
        "Cores: postes amarelos; BT azul; MT laranja; AT vermelha; subterrâneas em roxo/magenta.\n"
        "O arquivo mostra a localização da infraestrutura, mas não comprova disponibilidade de ponto de fixação.\n"
        "A ocupação para telecom exige projeto, análise mecânica, vistoria e aprovação da Equatorial.\n",
        encoding="utf-8",
    )

    shutil.rmtree(work_dir, ignore_errors=True)
    print(
        f"CONCLUIDO: {kmz_path} | {kmz_path.stat().st_size / 1024 / 1024:.1f} MiB | "
        f"{len(tile_records)} mosaicos | {(time.time() - started) / 60:.1f} min",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
