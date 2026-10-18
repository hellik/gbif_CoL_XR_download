"""
===============================================================================
03_h3_spatial_analysis.py
Liest die entpackten GBIF-Downloads (SIMPLE_CSV oder SQL_TSV_ZIP), klassifiziert jede
Zeile, exportiert gueltige Vorkommen als Punkt-Layer und H3-Hexagone (GeoPackage) und
schreibt Ausschluesse sowie einen Quality-Check-Report.

- Spaltennamen werden in Kleinbuchstaben normalisiert (SIMPLE_CSV = camelCase, SQL = lower).
- Jede Zeile faellt in genau EINE Kategorie -> Bilanzpruefung (PASSED/FAILED).
- Optional exakter AOI-Zuschnitt (--aoi), da Downloads mit vereinfachtem Polygon/BBox
  laufen koennen.
- Pro Art (oder gesamt) ein GeoPackage mit den Layern 'points' und 'h3_res<N>'.
===============================================================================
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd

from gbif_common import (GEOSPATIAL_ISSUES, KINGDOM_MAP, load_aoi_union, ogr, osr, setup_logging)

try:
    import h3
except ImportError:
    h3 = None

log = setup_logging("03_h3_spatial_analysis")

NON_DATA_FILES = {"citation.txt", "rights.txt", "metadata.xml", "meta.xml", "eml.xml",
                  "gbif_citations.txt", "quality_check_report.csv"}
INT64_COLS = {"gbifid", "individualcount"}
INT_COLS = {"year", "month", "day"}
REAL_COLS = {"coordinateuncertaintyinmeters", "elevation", "depth", "coordinateprecision",
             "elevationaccuracy", "depthaccuracy", "decimallatitude", "decimallongitude"}
CSV_REASONS = ("absent", "not_spatial", "geospatial_issue", "no_species")
CATEGORIES = ("kingdom_excluded", "absent", "not_spatial", "geospatial_issue", "no_species",
              "outside_aoi", "uncertainty_too_high")


# --------------------------------------------------------------------------- #
# H3-Kompatibilitaet (h3 v4, Fallback v3)
# --------------------------------------------------------------------------- #
def h3_cell(lat, lon, res):
    return h3.latlng_to_cell(lat, lon, res) if hasattr(h3, "latlng_to_cell") else h3.geo_to_h3(lat, lon, res)


def h3_boundary(cell):
    return h3.cell_to_boundary(cell) if hasattr(h3, "cell_to_boundary") else h3.h3_to_geo_boundary(cell)


# --------------------------------------------------------------------------- #
# Einlesen
# --------------------------------------------------------------------------- #
def discover_files(input_dir, input_file):
    if input_file:
        return [input_file]
    manifest = os.path.join(input_dir, "download_manifest.json")
    if os.path.exists(manifest):
        with open(manifest, "r", encoding="utf-8") as f:
            entries = json.load(f)
        files = [p for e in entries.values() for p in e.get("files", []) if os.path.exists(p)]
        if files:
            return files
        log.warning("Manifest enthaelt keine vorhandenen Dateien - scanne Verzeichnis.")
    found = []
    if os.path.isdir(input_dir):
        for root, dirs, names in os.walk(input_dir):
            dirs[:] = [d for d in dirs if d.lower() not in ("dataset", "metadata", "h3_outputs")]
            for n in names:
                if n.lower() in NON_DATA_FILES or not n.lower().endswith((".csv", ".tsv", ".txt")):
                    continue
                found.append(os.path.join(root, n))
    return sorted(found)


def read_gbif_table(path):
    """Tab-getrennt, ohne Quote-Interpretation; 'NA' (Namibia) bleibt ein String."""
    df = pd.read_csv(path, sep="\t", dtype=str, quoting=csv.QUOTE_NONE, encoding="utf-8",
                     encoding_errors="replace", keep_default_na=False, na_values=[""],
                     on_bad_lines="warn")
    df.columns = [c.strip().lower() for c in df.columns]
    for col in INT64_COLS | INT_COLS | REAL_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "issue" in df.columns:
        df["issue"] = df["issue"].str.replace("\x02", ";", regex=False)
    return df


# --------------------------------------------------------------------------- #
# Klassifikation
# --------------------------------------------------------------------------- #
def points_in_aoi(sub, aoi_geom):
    """Exakter Test auf eindeutigen Koordinatenpaaren (Bounding-Box-Vorfilter)."""
    minx, maxx, miny, maxy = aoi_geom.GetEnvelope()
    uniq = sub[["decimallatitude", "decimallongitude"]].drop_duplicates().copy()
    inside = []
    for lat, lon in zip(uniq["decimallatitude"], uniq["decimallongitude"]):
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            inside.append(False)
            continue
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(float(lon), float(lat))
        inside.append(bool(aoi_geom.Intersects(pt)))
    uniq["_in"] = inside
    merged = sub[["decimallatitude", "decimallongitude"]].merge(uniq, on=["decimallatitude", "decimallongitude"], how="left")
    return pd.Series(merged["_in"].to_numpy(dtype=bool), index=sub.index)


def classify(df, args, target_kingdom, aoi_geom):
    reason = pd.Series("", index=df.index, dtype=object)

    def assign(mask, label):
        reason.loc[mask & (reason == "")] = label

    if target_kingdom and "kingdom" in df.columns:
        assign(df["kingdom"].fillna("").str.lower() != target_kingdom.lower(), "kingdom_excluded")

    if "occurrencestatus" in df.columns and not args.include_absences:
        assign(df["occurrencestatus"].fillna("").str.upper() == "ABSENT", "absent")

    lat, lon = df["decimallatitude"], df["decimallongitude"]
    valid_coord = lat.between(-90, 90) & lon.between(-180, 180) & ~((lat == 0) & (lon == 0))
    if "hascoordinate" in df.columns:
        valid_coord &= ~df["hascoordinate"].fillna("").str.lower().isin(["false", "0"])
    assign(~valid_coord, "not_spatial")

    if "issue" in df.columns and not args.no_issue_filter:
        pattern = r"(?:^|;)(?:" + "|".join(GEOSPATIAL_ISSUES) + r")(?:;|$)"
        assign(df["issue"].fillna("").str.contains(pattern, regex=True), "geospatial_issue")

    assign(df["species"].isna(), "no_species")

    if aoi_geom is not None:
        candidates = reason == ""
        if candidates.any():
            inside = points_in_aoi(df.loc[candidates], aoi_geom)
            outside_idx = inside.index[~inside]
            reason.loc[outside_idx] = "outside_aoi"

    if args.max_uncertainty is not None and "coordinateuncertaintyinmeters" in df.columns:
        assign(df["coordinateuncertaintyinmeters"] > args.max_uncertainty, "uncertainty_too_high")
    return reason


# --------------------------------------------------------------------------- #
# Aufbereitung gueltiger Datensaetze
# --------------------------------------------------------------------------- #
def add_helper_columns(valid, res):
    year = valid["year"] if "year" in valid.columns else pd.Series(float("nan"), index=valid.index)
    if "eventdate" in valid.columns:
        year = year.fillna(pd.to_numeric(valid["eventdate"].str[:4], errors="coerce"))
    year = year.where(year.between(1500, datetime.now().year + 1))
    valid["_year"] = year
    valid["_decade"] = (year // 10 * 10).map(lambda d: f"dec_{int(d)}s" if pd.notna(d) else "dec_UNKNOWN")
    basis = valid["basisofrecord"] if "basisofrecord" in valid.columns else pd.Series(index=valid.index, dtype=object)
    valid["_basis"] = "bor_" + basis.fillna("UNKNOWN").astype(str)

    uniq = valid[["decimallatitude", "decimallongitude"]].drop_duplicates().copy()
    uniq["h3_index"] = [h3_cell(a, b, res) for a, b in zip(uniq["decimallatitude"], uniq["decimallongitude"])]
    merged = valid[["decimallatitude", "decimallongitude"]].merge(uniq, on=["decimallatitude", "decimallongitude"], how="left")
    valid["h3_index"] = merged["h3_index"].to_numpy()
    return valid


def build_h3_wide(parts):
    long = pd.concat(parts, ignore_index=True).groupby(["species", "h3_index", "decade", "basis"], as_index=False)["n"].sum()
    keys = ["species", "h3_index"]
    total = long.groupby(keys)["n"].sum().rename("n_total")
    dec = long.groupby(keys + ["decade"])["n"].sum().unstack(fill_value=0)
    bor = long.groupby(keys + ["basis"])["n"].sum().unstack(fill_value=0)
    dec = dec[sorted(dec.columns)]
    bor = bor[sorted(bor.columns)]
    return pd.concat([total, dec, bor], axis=1).reset_index()


# --------------------------------------------------------------------------- #
# GeoPackage-Ausgabe
# --------------------------------------------------------------------------- #
def safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_")[:80] or "unknown"


def open_gpkg(path):
    driver = ogr.GetDriverByName("GPKG")
    return driver.Open(path, 1) if os.path.exists(path) else driver.CreateDataSource(path)


def wgs84():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def field_kind(col):
    if col in INT64_COLS:
        return "i64"
    if col in INT_COLS:
        return "int"
    if col in REAL_COLS:
        return "real"
    return "str"


def write_points(path, df, layer_name="points"):
    """Schreibt alle Spalten (ohne Hilfsspalten) als 2D-Punkte. Gibt (Anzahl, Feldanzahl) zurueck."""
    cols = [c for c in df.columns if not c.startswith("_") and c not in ("decimallatitude", "decimallongitude")]
    cols = ["decimallatitude", "decimallongitude"] + [c for c in cols]
    ds = open_gpkg(path)
    layer = ds.GetLayerByName(layer_name)
    if layer is None:
        layer = ds.CreateLayer(layer_name, wgs84(), ogr.wkbPoint)
        for c in cols:
            ftype = {"i64": ogr.OFTInteger64, "int": ogr.OFTInteger, "real": ogr.OFTReal, "str": ogr.OFTString}[field_kind(c)]
            layer.CreateField(ogr.FieldDefn(c, ftype))
    defn = layer.GetLayerDefn()
    mapping = []
    for c in cols:
        idx = defn.GetFieldIndex(c)
        if idx < 0:
            log.warning(f"Spalte '{c}' fehlt im bestehenden Layer '{layer_name}' ({path}) - uebersprungen.")
            continue
        mapping.append((idx, field_kind(c), df[c].astype(object).where(df[c].notna(), None).tolist()))
    lats, lons = df["decimallatitude"].tolist(), df["decimallongitude"].tolist()

    layer.StartTransaction()
    try:
        for i in range(len(df)):
            feat = ogr.Feature(defn)
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(float(lons[i]), float(lats[i]))
            feat.SetGeometry(pt)
            for idx, kind, values in mapping:
                v = values[i]
                if v is None:
                    continue
                if kind == "i64":
                    feat.SetFieldInteger64(idx, int(v))
                elif kind == "int":
                    feat.SetField(idx, int(v))
                elif kind == "real":
                    feat.SetField(idx, float(v))
                else:
                    feat.SetField(idx, str(v))
            layer.CreateFeature(feat)
        layer.CommitTransaction()
    except Exception:
        layer.RollbackTransaction()
        raise
    finally:
        ds = None
    return len(df), len(cols)


def write_h3_layer(path, wide, res):
    layer_name = f"h3_res{res}"
    ds = open_gpkg(path)
    old = ds.GetLayerByName(layer_name)
    if old is not None:
        for i in range(ds.GetLayerCount()):
            if ds.GetLayer(i).GetName() == layer_name:
                ds.DeleteLayer(i)
                break
    layer = ds.CreateLayer(layer_name, wgs84(), ogr.wkbPolygon)
    data_cols = [c for c in wide.columns if c not in ("species", "h3_index")]
    layer.CreateField(ogr.FieldDefn("species", ogr.OFTString))
    layer.CreateField(ogr.FieldDefn("h3_index", ogr.OFTString))
    for c in data_cols:
        layer.CreateField(ogr.FieldDefn(c, ogr.OFTInteger64))
    defn = layer.GetLayerDefn()

    layer.StartTransaction()
    try:
        for row in wide.itertuples(index=False):
            rec = dict(zip(wide.columns, row))
            boundary = h3_boundary(rec["h3_index"])
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for lat, lng in boundary:
                ring.AddPoint_2D(lng, lat)
            ring.AddPoint_2D(boundary[0][1], boundary[0][0])
            poly = ogr.Geometry(ogr.wkbPolygon)
            poly.AddGeometry(ring)
            feat = ogr.Feature(defn)
            feat.SetGeometry(poly)
            feat.SetField("species", str(rec["species"]))
            feat.SetField("h3_index", str(rec["h3_index"]))
            for c in data_cols:
                feat.SetFieldInteger64(defn.GetFieldIndex(c), int(rec[c]))
            layer.CreateFeature(feat)
        layer.CommitTransaction()
    except Exception:
        layer.RollbackTransaction()
        raise
    finally:
        ds = None


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="GBIF: Punkt-/H3-Export, Ausschluss-CSV, Quality-Check")
    ap.add_argument("--input-dir", default="gbif_downloads")
    ap.add_argument("--input-file", default=None, help="Einzelne Datei statt Verzeichnis/Manifest")
    ap.add_argument("--kingdom", choices=list(KINGDOM_MAP), default="all")
    ap.add_argument("--split-by-species", action="store_true", default=True)
    ap.add_argument("--no-split", action="store_false", dest="split_by_species")
    ap.add_argument("--output-dir", default="./h3_outputs")
    ap.add_argument("--res", type=int, default=5, help="H3-Aufloesung (4 grob, 5 Standard, 6 detailliert)")
    ap.add_argument("--aoi", default=None, help="GeoPackage fuer exakten AOI-Zuschnitt")
    ap.add_argument("--aoi-layer", default=None)
    ap.add_argument("--max-uncertainty", type=float, default=None, help="Max. Koordinatenunsicherheit in m")
    ap.add_argument("--include-absences", action="store_true")
    ap.add_argument("--no-issue-filter", action="store_true", help="Geospatial-Issues nicht ausfiltern")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--no-dry-run", action="store_false", dest="dry_run")
    args = ap.parse_args()

    if h3 is None:
        log.error("Paket 'h3' fehlt: pip install h3")
        sys.exit(1)
    if not args.dry_run and ogr is None:
        log.error("GDAL/OGR (osgeo) fehlt - bitte in der OSGeo4W-Shell ausfuehren.")
        sys.exit(1)

    files = discover_files(args.input_dir, args.input_file)
    if not files:
        if args.dry_run:
            log.warning("[DRY RUN] Keine Eingabedaten gefunden (Downloads wurden im Dry-Run nicht ausgefuehrt).")
            return
        log.error(f"Keine GBIF-Daten in '{args.input_dir}' gefunden.")
        sys.exit(1)

    aoi_geom = None
    if args.aoi:
        aoi_geom = load_aoi_union(args.aoi, args.aoi_layer)
        log.info(f"Exakter AOI-Zuschnitt aktiv: {args.aoi}")
    target_kingdom = KINGDOM_MAP[args.kingdom]
    if not args.dry_run:
        os.makedirs(args.output_dir, exist_ok=True)

    touched, h3_parts = set(), []
    species_info, reports = {}, []
    csv_path = os.path.join(args.output_dir, "excluded_records.csv")

    def fresh(path):
        """Altes Ergebnis beim ersten Zugriff dieses Laufs entfernen."""
        if path not in touched:
            touched.add(path)
            if os.path.exists(path):
                os.remove(path)

    if not args.dry_run:
        fresh(csv_path)

    for path in files:
        log.info(f"Lese {path}")
        try:
            df = read_gbif_table(path)
        except Exception as exc:  # noqa: BLE001
            log.error(f"Datei nicht lesbar: {path}: {exc}")
            reports.append({"source_file": os.path.basename(path), "record_balance_audit": "READ_ERROR"})
            continue
        missing = [c for c in ("species", "decimallatitude", "decimallongitude") if c not in df.columns]
        if missing:
            log.error(f"Pflichtspalten fehlen in {path}: {missing}")
            reports.append({"source_file": os.path.basename(path), "record_balance_audit": "MISSING_COLUMNS"})
            continue

        total = len(df)
        if "specieskey" in df.columns:
            share = df["specieskey"].dropna().str.fullmatch(r"\d+").mean() if df["specieskey"].notna().any() else 0.0
            if share > 0.9:
                log.warning(f"{os.path.basename(path)}: specieskey ist fast durchgehend numerisch - "
                            "vermutlich Backbone- statt CoL-XR-Daten! Download pruefen.")
        else:
            share = None

        reason = classify(df, args, target_kingdom, aoi_geom)
        counts = reason.value_counts().to_dict()
        valid = df.loc[reason == ""].copy()

        # Ausschluss-CSV
        csv_rows = 0
        if not args.dry_run:
            excl = df.loc[reason.isin(CSV_REASONS)].copy()
            if not excl.empty:
                excl["exclusion_reason"] = reason.loc[excl.index]
                if os.path.exists(csv_path):
                    excl = excl.reindex(columns=list(pd.read_csv(csv_path, nrows=0).columns))
                excl.to_csv(csv_path, mode="a", index=False, encoding="utf-8", header=not os.path.exists(csv_path))
                csv_rows = len(excl)

        exported, point_fields = 0, 0
        if not valid.empty:
            valid = add_helper_columns(valid, args.res)
            h3_parts.append(valid.groupby(["species", "h3_index", "_decade", "_basis"]).size()
                            .rename("n").reset_index().rename(columns={"_decade": "decade", "_basis": "basis"}))
            for spec, g in valid.groupby("species"):
                info = species_info.setdefault(spec, {"species": spec, "specieskey": None, "n_points": 0,
                                                      "year_min": None, "year_max": None})
                if "specieskey" in g.columns and g["specieskey"].notna().any():
                    info["specieskey"] = g["specieskey"].dropna().iloc[0]
                info["n_points"] += len(g)
                if g["_year"].notna().any():
                    ymin, ymax = int(g["_year"].min()), int(g["_year"].max())
                    info["year_min"] = ymin if info["year_min"] is None else min(info["year_min"], ymin)
                    info["year_max"] = ymax if info["year_max"] is None else max(info["year_max"], ymax)
            if args.dry_run:
                exported, point_fields = len(valid), len([c for c in valid.columns if not c.startswith("_")])
            else:
                groups = valid.groupby("species") if args.split_by_species else [("all", valid)]
                for spec, g in groups:
                    gpkg = os.path.join(args.output_dir, f"gbif_{safe_name(spec)}.gpkg" if args.split_by_species
                                        else "gbif_all_species.gpkg")
                    fresh(gpkg)
                    n, point_fields = write_points(gpkg, g)
                    exported += n

        balance = sum(counts.get(c, 0) for c in CATEGORIES) + len(valid)
        report = {"source_file": os.path.basename(path), "total_input_records": total}
        report.update({c: counts.get(c, 0) for c in CATEGORIES})
        report.update({
            "exported_points": exported, "record_balance_audit": "PASSED" if balance == total else "FAILED",
            "raw_column_count": len(df.columns), "point_gpkg_field_count": point_fields,
            "excluded_csv_rows": csv_rows,
            "aoi_clip": "exact" if aoi_geom is not None else "none",
            "specieskey_numeric_share": None if share is None else round(float(share), 3),
        })
        reports.append(report)
        log.info(f"  {total} Zeilen: " + ", ".join(f"{k}={v}" for k, v in report.items()
                                                  if k in CATEGORIES + ("exported_points",) and v) +
                 f" | Bilanz {report['record_balance_audit']}")

    # H3-Layer
    n_hex = {}
    if h3_parts:
        wide = build_h3_wide(h3_parts)
        n_hex = wide.groupby("species").size().to_dict()
        log.info(f"H3 (Res {args.res}): {len(wide)} Hexagone fuer {len(n_hex)} Art(en).")
        if not args.dry_run:
            if args.split_by_species:
                for spec, w in wide.groupby("species"):
                    write_h3_layer(os.path.join(args.output_dir, f"gbif_{safe_name(spec)}.gpkg"), w.reset_index(drop=True), args.res)
            else:
                write_h3_layer(os.path.join(args.output_dir, "gbif_all_species.gpkg"), wide, args.res)

    summary = pd.DataFrame(list(species_info.values()))
    if not summary.empty:
        summary["n_hexagons"] = summary["species"].map(n_hex)
    if args.dry_run:
        log.info("[DRY RUN] Es wurden keine Dateien geschrieben.")
    else:
        pd.DataFrame(reports).to_csv(os.path.join(args.output_dir, "quality_check_report.csv"), index=False, encoding="utf-8")
        summary.to_csv(os.path.join(args.output_dir, "species_summary.csv"), index=False, encoding="utf-8")
        log.info(f"Reports geschrieben in {args.output_dir}")

    if any(r.get("record_balance_audit") != "PASSED" for r in reports):
        log.error("Mindestens eine Datei mit Bilanzfehler/Lesefehler - bitte quality_check_report.csv pruefen.")
        sys.exit(1)


if __name__ == "__main__":
    main()
