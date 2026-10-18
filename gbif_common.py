"""
===============================================================================
gbif_common.py
Gemeinsame Helfer fuer 01_namensabgleich.py, 02_daten_download.py und
03_h3_spatial_analysis.py (CoL-XR-only Workflow, pygbif >= 0.7.0).
===============================================================================
"""
import hashlib
import logging
import math
import os
import sys
import time

try:
    from osgeo import ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()
except ImportError:  # wird erst bei AOI-Nutzung benoetigt
    ogr = osr = None

# CoL Extended Release (alphanumerische Taxon-Keys, z. B. "4R5YN")
COL_CHECKLIST_KEY = "7ddf754f-d193-4cc9-b351-99906754a03b"

KINGDOM_MAP = {"animals": "Animalia", "plants": "Plantae", "fungi": "Fungi", "all": None}

# Naeherung an GBIF "hasGeospatialIssue" (zur lokalen Filterung in Skript 03)
GEOSPATIAL_ISSUES = (
    "ZERO_COORDINATE",
    "COORDINATE_OUT_OF_RANGE",
    "COORDINATE_INVALID",
    "COUNTRY_COORDINATE_MISMATCH",
)

PLACEHOLDER_PREFIXES = ("dein_", "deine_")


def setup_logging(script_name, log_dir="logs"):
    """Konsole + Logdatei (logs/<script>.log, anhaengend)."""
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(os.path.join(log_dir, f"{script_name}.log"), mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    logging.captureWarnings(True)
    return logging.getLogger(script_name)


def with_retry(func, *args, tries=4, **kwargs):
    """Wiederholt bei Netz-/5xx-/429-Fehlern; 4xx (ausser 429) werden sofort geworfen."""
    for attempt in range(tries):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - bewusst breit, wird weitergereicht
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                raise
            if attempt == tries - 1:
                raise
            wait = 2 ** (attempt + 1)
            logging.warning(f"Anfrage fehlgeschlagen ({type(exc).__name__}: {exc}); neuer Versuch in {wait}s")
            time.sleep(wait)


def md5_text(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# AOI
# --------------------------------------------------------------------------- #
def _require_ogr():
    if ogr is None:
        raise RuntimeError("GDAL/OGR (osgeo) nicht verfuegbar - bitte in der OSGeo4W-Shell ausfuehren.")


def count_points(geom):
    n = geom.GetGeometryCount()
    if n == 0:
        return geom.GetPointCount()
    return sum(count_points(geom.GetGeometryRef(i)) for i in range(n))


def _collect_polygons(geom, parts):
    gtype = ogr.GT_Flatten(geom.GetGeometryType())
    if gtype == ogr.wkbPolygon:
        if not geom.IsValid():
            geom = geom.Buffer(0)
            if geom is None or geom.IsEmpty():
                return
            if ogr.GT_Flatten(geom.GetGeometryType()) != ogr.wkbPolygon:
                for i in range(geom.GetGeometryCount()):
                    _collect_polygons(geom.GetGeometryRef(i).Clone(), parts)
                return
        parts.AddGeometry(geom)
    elif gtype in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for i in range(geom.GetGeometryCount()):
            _collect_polygons(geom.GetGeometryRef(i).Clone(), parts)


def load_aoi_union(path, layer_name=None):
    """Liest alle Polygone eines GeoPackage-Layers, transformiert nach EPSG:4326 (lon/lat), vereinigt sie."""
    _require_ogr()
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"AOI-Datei nicht gefunden: {path}")
    ds = ogr.Open(path)
    layer = ds.GetLayerByName(layer_name) if layer_name else ds.GetLayer(0)

    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transform = None
    src = layer.GetSpatialRef()
    if src is None:
        logging.warning("AOI-Layer hat kein CRS - es wird EPSG:4326 angenommen.")
    else:
        src = src.Clone()
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not src.IsSame(dst):
            transform = osr.CoordinateTransformation(src, dst)

    parts = ogr.Geometry(ogr.wkbMultiPolygon)
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None or geom.IsEmpty():
            continue
        geom = geom.Clone()
        if transform is not None:
            geom.Transform(transform)
        geom.FlattenTo2D()
        _collect_polygons(geom, parts)

    if parts.GetGeometryCount() == 0:
        raise ValueError(f"Keine Polygongeometrien in AOI '{path}' gefunden.")

    merged = parts.UnionCascaded()
    if not merged.IsValid():
        merged = merged.Buffer(0)
    return merged


def _ring_points(ring):
    return [(ring.GetX(i), ring.GetY(i)) for i in range(ring.GetPointCount())]


def _signed_area(points):
    """Positiv = gegen den Uhrzeigersinn (CCW). Ring muss geschlossen sein."""
    return 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:]))


def geom_to_ccw_wkt(geom, prec=4):
    """WKT mit CCW-Aussenringen und CW-Loechern (GBIF-Anforderung), gerundet auf prec Stellen."""
    gtype = ogr.GT_Flatten(geom.GetGeometryType())
    if gtype == ogr.wkbPolygon:
        polys = [geom]
    elif gtype == ogr.wkbMultiPolygon:
        polys = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
    else:
        raise ValueError(f"Unerwarteter Geometrietyp fuer AOI: {geom.GetGeometryName()}")

    poly_strings = []
    for poly in polys:
        rings = []
        for r in range(poly.GetGeometryCount()):
            pts = _ring_points(poly.GetGeometryRef(r))
            if len(pts) < 4:
                continue
            want_ccw = r == 0
            if (_signed_area(pts) > 0) != want_ccw:
                pts = pts[::-1]
            rings.append("(" + ", ".join(f"{x:.{prec}f} {y:.{prec}f}" for x, y in pts) + ")")
        if rings:
            poly_strings.append("(" + ", ".join(rings) + ")")
    if not poly_strings:
        raise ValueError("AOI enthaelt keine gueltigen Ringe.")
    if len(poly_strings) == 1:
        return "POLYGON " + poly_strings[0]
    return "MULTIPOLYGON (" + ", ".join(poly_strings) + ")"


def bbox_wkt_from_geom(geom, prec=4):
    minx, maxx, miny, maxy = geom.GetEnvelope()
    f = 10 ** prec
    minx, miny = math.floor(minx * f) / f, math.floor(miny * f) / f   # nach aussen runden
    maxx, maxy = math.ceil(maxx * f) / f, math.ceil(maxy * f) / f
    return (f"POLYGON (({minx:.{prec}f} {miny:.{prec}f}, {maxx:.{prec}f} {miny:.{prec}f}, "
            f"{maxx:.{prec}f} {maxy:.{prec}f}, {minx:.{prec}f} {maxy:.{prec}f}, {minx:.{prec}f} {miny:.{prec}f}))")


def build_aoi_info(union_geom, max_points=150, prec=4):
    """Erzeugt die fuer GBIF verwendbare AOI (vereinfachtes Polygon, sonst BBox) inkl. Kennzahlen."""
    points_original = count_points(union_geom)
    simplified, tolerance, step = union_geom, 0.0, 0.0005
    while count_points(simplified) > max_points and step <= 0.25:
        cand = union_geom.SimplifyPreserveTopology(step)
        if cand is not None and not cand.IsEmpty():
            simplified, tolerance = cand, step
        step *= 1.6

    points_used = count_points(simplified)
    if points_used <= max_points:
        mode, wkt = "polygon", geom_to_ccw_wkt(simplified, prec)
    else:
        mode, wkt = "bbox", bbox_wkt_from_geom(union_geom, prec)

    area_diff = None
    if mode == "polygon" and tolerance > 0:
        try:
            area_diff = union_geom.SymDifference(simplified).GetArea() / union_geom.GetArea()
        except Exception:  # noqa: BLE001
            area_diff = None

    return {
        "mode": mode,
        "wkt": wkt,
        "wkt_hash": md5_text(wkt),
        "points_original": points_original,
        "points_used": points_used if mode == "polygon" else 5,
        "simplify_tolerance_deg": tolerance,
        "area_diff_ratio": area_diff,
        "crs": "EPSG:4326",
    }
