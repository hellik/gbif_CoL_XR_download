"""
===============================================================================
01_namensabgleich.py
CoL-XR Namensabgleich (pygbif >= 0.7.0), Zaehlung und Batch-Planung.

Grundsaetze:
- Es gibt genau EINEN Taxon-Key: den alphanumerischen CoL-XR-Key (z. B. "4R5YN").
  Kein Backbone-Key, kein name_lookup.
- Synonyme werden ueber 'acceptedUsage' auf den akzeptierten Namen aufgeloest.
- Nur sichere Treffer (matchType EXACT/VARIANT, passender Rang, passendes Kingdom)
  gelangen in Zaehlung, Plan und Download.
- Die AOI wird einmal aufbereitet (CRS, CCW-Ringe, Vereinfachung) und in
  aoi_used.json abgelegt; Skript 02 und 03 verwenden dieselbe Datei.
- 01 veraendert nichts bei GBIF (nur lesende Abfragen).
===============================================================================
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import pandas as pd

from gbif_common import (COL_CHECKLIST_KEY, KINGDOM_MAP, build_aoi_info, load_aoi_union,
                         md5_text, setup_logging, with_retry)

log = setup_logging("01_namensabgleich")

ACCEPT_MATCH_TYPES = {"EXACT", "VARIANT"}
INFRASPECIFIC_RANKS = {"SUBSPECIES", "VARIETY", "FORM"}
DEFAULT_RANKS = "SPECIES,SUBSPECIES,VARIETY,FORM"

REPORT_COLUMNS = [
    "raw_input_name", "parsed_search_name", "match_type", "confidence", "match_note",
    "matched_key", "matched_name", "matched_status", "synonym",
    "accepted_key", "accepted_name", "accepted_rank", "kingdom", "species_key", "species_name",
    "decision", "reason", "download_key", "download_name", "download_rank", "duplicate_of",
    "count_global", "count_aoi", "count_error", "aoi_hash",
]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def clean_name(raw):
    """Entfernt Kommentare/Whitespace. Rangmarker bleiben erhalten (v2-Match kommt damit zurecht)."""
    return re.sub(r"\s+", " ", re.sub(r"#.*", "", raw)).strip()


def parse_match(res):
    """Liest die v2-Antwort (usage / acceptedUsage / classification / diagnostics / synonym)."""
    diag = res.get("diagnostics") or {}
    usage = res.get("usage") or {}
    accepted = res.get("acceptedUsage") or usage
    by_rank = {c.get("rank"): c for c in (res.get("classification") or [])}
    return {
        "match_type": diag.get("matchType"),
        "confidence": diag.get("confidence"),
        "match_note": diag.get("note"),
        "usage_key": usage.get("key"),
        "usage_name": usage.get("name"),
        "usage_status": usage.get("status"),
        "synonym": bool(res.get("synonym")),
        "accepted_key": accepted.get("key"),
        "accepted_name": accepted.get("name"),
        "accepted_rank": (accepted.get("rank") or "").upper() or None,
        "kingdom": (by_rank.get("KINGDOM") or {}).get("name"),
        "species_key": (by_rank.get("SPECIES") or {}).get("key"),
        "species_name": (by_rank.get("SPECIES") or {}).get("name"),
    }


def decide(p, kingdom_name, accept_ranks, infraspecific):
    """Gibt (decision, reason, download_key, download_name, download_rank) zurueck."""
    mt = p["match_type"]
    if mt in (None, "NONE") or not p["accepted_key"]:
        return "REJECTED", "NO_MATCH", None, None, None
    if mt not in ACCEPT_MATCH_TYPES:
        return "REJECTED", f"MATCH_TYPE_{mt}", None, None, None
    if kingdom_name:
        if not p["kingdom"]:
            return "REJECTED", "KINGDOM_UNKNOWN", None, None, None
        if p["kingdom"].lower() != kingdom_name.lower():
            return "REJECTED", f"KINGDOM_MISMATCH_{p['kingdom']}", None, None, None
    rank = p["accepted_rank"] or "UNKNOWN"
    if rank not in accept_ranks:
        return "REJECTED", f"RANK_{rank}", None, None, None

    reasons = []
    if p["synonym"]:
        reasons.append("SYNONYM_RESOLVED")
    key, name = p["accepted_key"], p["accepted_name"]
    if rank in INFRASPECIFIC_RANKS:
        if infraspecific == "species" and p["species_key"]:
            key, name, rank = p["species_key"], p["species_name"], "SPECIES"
            reasons.append("COLLAPSED_TO_SPECIES")
        else:
            reasons.append("INFRASPECIFIC_KEPT")
    if mt == "VARIANT":
        reasons.append("NAME_VARIANT")
    return "ACCEPTED", ";".join(reasons), key, name, rank


def match_name(gbif_species, name, kingdom_name):
    return with_retry(gbif_species.name_backbone, scientificName=name,
                      kingdom=kingdom_name, checklistKey=COL_CHECKLIST_KEY)


# --------------------------------------------------------------------------- #
# Zaehlung
# --------------------------------------------------------------------------- #
def count_taxon(gbif_occ, key, wkt=None, include_non_spatial=False):
    """Zaehlt mit denselben Filtern wie der Download (ausser OCCURRENCE_STATUS, siehe README)."""
    kwargs = {"taxonKey": key, "checklistKey": COL_CHECKLIST_KEY, "limit": 0}
    if not include_non_spatial:
        kwargs["hasCoordinate"] = True
        kwargs["hasGeospatialIssue"] = False
    if wkt:
        kwargs["geometry"] = wkt
    res = with_retry(gbif_occ.search, **kwargs)
    return int(res.get("count", 0))


def prepare_aoi(args, gbif_occ):
    """AOI einmalig aufbereiten, gegen GBIF proben (Polygon -> BBox-Fallback) und speichern."""
    union = load_aoi_union(args.aoi, args.aoi_layer)
    info = build_aoi_info(union, max_points=args.aoi_max_points)

    def probe(wkt):
        with_retry(gbif_occ.search, geometry=wkt, hasCoordinate=True,
                   checklistKey=COL_CHECKLIST_KEY, limit=0)

    if info["mode"] == "polygon":
        try:
            probe(info["wkt"])
        except Exception as exc:  # noqa: BLE001
            log.warning(f"GBIF lehnt das AOI-Polygon ab ({exc}); wechsle zur Bounding-Box.")
            from gbif_common import bbox_wkt_from_geom
            info.update({"mode": "bbox", "wkt": bbox_wkt_from_geom(union), "points_used": 5})
            info["wkt_hash"] = md5_text(info["wkt"])
    if info["mode"] == "bbox":
        probe(info["wkt"])  # scheitert diese Probe, bricht das Skript mit Fehler ab
        log.warning("AOI wird nur als Bounding-Box verwendet - Skript 03 muss mit --aoi exakt zuschneiden.")

    info.update({"source": os.path.abspath(args.aoi), "layer": args.aoi_layer,
                 "created": datetime.now().isoformat(timespec="seconds")})
    with open(args.aoi_info, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    ratio = info.get("area_diff_ratio")
    log.info(f"AOI: Modus={info['mode']}, Punkte {info['points_original']} -> {info['points_used']}, "
             f"Flaechenabweichung durch Vereinfachung: {'n/a' if ratio is None else f'{ratio:.2%}'} "
             f"(gespeichert in {args.aoi_info})")
    if ratio is not None and ratio > 0.01:
        log.warning("Vereinfachung aendert die AOI-Flaeche um >1 % - ggf. --aoi-max-points erhoehen.")
    return info


# --------------------------------------------------------------------------- #
# Batch-Plan
# --------------------------------------------------------------------------- #
def safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:40]


def create_batch_plan(taxa, max_records, max_taxa, meta, log_file):
    """taxa: Liste von dicts {key, name, rank, count}. Gibt den Plan zurueck und schreibt ihn."""
    batches, current, current_n, batch_no = {}, [], 0, 1

    def flush():
        nonlocal current, current_n, batch_no
        if current:
            batches[f"batch_{batch_no:02d}"] = {
                "keys": [t["key"] for t in current], "taxa": current,
                "total_records_est": current_n, "taxa_count": len(current),
                "recommended_method": "predicate",
            }
            batch_no += 1
            current, current_n = [], 0

    for t in sorted(taxa, key=lambda x: -x["count"]):
        if t["count"] >= max_records:
            batches[f"batch_single_{t['key']}_{safe_name(t['name'])}"] = {
                "keys": [t["key"]], "taxa": [t], "total_records_est": t["count"],
                "taxa_count": 1, "recommended_method": "predicate",
            }
            continue
        if current and (current_n + t["count"] > max_records or len(current) >= max_taxa):
            flush()
        current.append(t)
        current_n += t["count"]
    flush()

    plan = {"meta": meta, "batches": batches}
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    log.info(f"Batch-Plan geschrieben: {log_file} ({len(batches)} Batches, {len(taxa)} Taxa)")
    return plan


# --------------------------------------------------------------------------- #
# Hilfen
# --------------------------------------------------------------------------- #
def read_species_list(path, limit=None):
    with open(path, "r", encoding="utf-8-sig") as f:
        names = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    seen, unique = set(), []
    for n in names:
        if clean_name(n) and n not in seen:
            seen.add(n)
            unique.append(n)
    return unique[:limit] if limit else unique


def load_cache(path, aoi_hash):
    """Fuer --resume: bereits fertige Zeilen (gleiche AOI, ohne Zaehlfehler) wiederverwenden."""
    cache = {}
    if not os.path.exists(path):
        return cache
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for rec in df.to_dict("records"):
        if (rec.get("aoi_hash", "") == aoi_hash and rec.get("count_error", "") == ""
                and rec.get("decision") in ("ACCEPTED", "REJECTED")):
            cache[rec["raw_input_name"]] = {k: (v if v != "" else None) for k, v in rec.items()}
    return cache


def counts_complete(row, use_aoi):
    field = "count_aoi" if use_aoi else "count_global"
    return row.get(field) is not None and not (isinstance(row.get(field), float) and pd.isna(row.get(field)))


def main():
    ap = argparse.ArgumentParser(description="CoL-XR Namensabgleich, Zaehlung & Batch-Plan")
    ap.add_argument("--input-species", required=True)
    ap.add_argument("--output-name-check", default="species_match_report.csv")
    ap.add_argument("--batch-plan-log", default="download_batch_plan.json")
    ap.add_argument("--kingdom", choices=list(KINGDOM_MAP), default="all")
    ap.add_argument("--accept-ranks", default=DEFAULT_RANKS, help="Kommagetrennte Raenge, die akzeptiert werden")
    ap.add_argument("--infraspecific", choices=["keep", "species"], default="keep",
                    help="keep: Unterart-Key behalten; species: auf die Art hochstufen")
    ap.add_argument("--use-aoi", action="store_true")
    ap.add_argument("--no-aoi", action="store_false", dest="use_aoi")
    ap.add_argument("--aoi", default=None)
    ap.add_argument("--aoi-layer", default=None)
    ap.add_argument("--aoi-info", default="aoi_used.json")
    ap.add_argument("--aoi-max-points", type=int, default=150)
    ap.add_argument("--include-non-spatial", action="store_true")
    ap.add_argument("--count-global", action="store_true",
                    help="Auch bei aktiver AOI die globale Zahl abfragen (sonst nur AOI-Zahl)")
    ap.add_argument("--batch-max-records", type=int, default=500000)
    ap.add_argument("--batch-max-taxa", type=int, default=50)
    ap.add_argument("--delay", type=float, default=0.2, help="Pause zwischen Abfragen (s)")
    ap.add_argument("--limit", type=int, default=None, help="Nur die ersten N Namen (Testlauf)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true", default=True, help="nur Kompatibilitaet; 01 schreibt nichts zu GBIF")
    ap.add_argument("--no-dry-run", action="store_false", dest="dry_run")
    args = ap.parse_args()

    try:
        import pygbif
        from pygbif import occurrences as gbif_occ
        from pygbif import species as gbif_species
    except ImportError:
        log.error("pygbif fehlt: pip install 'pygbif>=0.7.0'")
        sys.exit(1)
    version = tuple(int(x) for x in re.findall(r"\d+", pygbif.__version__)[:3])
    if version < (0, 7, 0):
        log.error(f"pygbif {pygbif.__version__} ist zu alt (Default waere noch der Backbone). Bitte >= 0.7.0.")
        sys.exit(1)

    if not os.path.exists(args.input_species):
        log.error(f"Eingabedatei nicht gefunden: {args.input_species}")
        sys.exit(1)
    if args.use_aoi and not args.aoi:
        log.error("--use-aoi gesetzt, aber --aoi fehlt.")
        sys.exit(1)

    kingdom_name = KINGDOM_MAP[args.kingdom]
    accept_ranks = {r.strip().upper() for r in args.accept_ranks.split(",") if r.strip()}
    names = read_species_list(args.input_species, args.limit)
    if not names:
        log.error("Eingabeliste ist leer.")
        sys.exit(1)

    aoi = prepare_aoi(args, gbif_occ) if args.use_aoi else None
    aoi_wkt = aoi["wkt"] if aoi else None
    aoi_hash = aoi["wkt_hash"] if aoi else "none"
    cache = load_cache(args.output_name_check, aoi_hash) if args.resume else {}
    if cache:
        log.info(f"--resume: {len(cache)} Zeilen aus vorherigem Lauf wiederverwendet.")

    rows = []
    try:
        # Pass 1: Matching
        log.info(f"Match gegen CoL-XR fuer {len(names)} Namen (Kingdom: {args.kingdom}) ...")
        for i, raw in enumerate(names, 1):
            if raw in cache:
                rows.append(cache[raw])
                continue
            parsed_name = clean_name(raw)
            row = {c: None for c in REPORT_COLUMNS}
            row.update({"raw_input_name": raw, "parsed_search_name": parsed_name, "aoi_hash": aoi_hash,
                        "count_error": ""})
            try:
                p = parse_match(match_name(gbif_species, parsed_name, kingdom_name))
                decision, reason, dkey, dname, drank = decide(p, kingdom_name, accept_ranks, args.infraspecific)
                row.update({
                    "match_type": p["match_type"], "confidence": p["confidence"], "match_note": p["match_note"],
                    "matched_key": p["usage_key"], "matched_name": p["usage_name"],
                    "matched_status": p["usage_status"], "synonym": p["synonym"],
                    "accepted_key": p["accepted_key"], "accepted_name": p["accepted_name"],
                    "accepted_rank": p["accepted_rank"], "kingdom": p["kingdom"],
                    "species_key": p["species_key"], "species_name": p["species_name"],
                    "decision": decision, "reason": reason,
                    "download_key": dkey, "download_name": dname, "download_rank": drank,
                })
            except Exception as exc:  # noqa: BLE001
                row.update({"decision": "ERROR", "reason": f"MATCH_ERROR: {exc}"})
                log.error(f"Match-Fehler bei '{parsed_name}': {exc}")
            if row["decision"] == "REJECTED":
                log.warning(f"'{parsed_name}' verworfen: {row['reason']}")
            rows.append(row)
            if i % 25 == 0:
                log.info(f"  {i}/{len(names)} abgeglichen")
            time.sleep(args.delay)

        # Duplikate (mehrere Eingaben -> derselbe Download-Key)
        first_seen = {}
        for idx, row in enumerate(rows):
            row["duplicate_of"] = None
            if row["decision"] == "ACCEPTED":
                k = row["download_key"]
                if k in first_seen:
                    row["duplicate_of"] = first_seen[k]
                else:
                    first_seen[k] = idx + 1
        n_dup = sum(1 for r in rows if r["duplicate_of"])
        if n_dup:
            log.info(f"{n_dup} Eingaben zeigen auf bereits vorhandene Taxa (duplicate_of) und werden nicht doppelt geladen.")

        # Pass 2: Zaehlung
        todo = [r for r in rows if r["decision"] == "ACCEPTED" and not r["duplicate_of"]
                and not counts_complete(r, args.use_aoi)]
        log.info(f"Zaehle Vorkommen fuer {len(todo)} Taxa ...")
        for j, row in enumerate(todo, 1):
            try:
                if args.use_aoi:
                    row["count_aoi"] = count_taxon(gbif_occ, row["download_key"], aoi_wkt, args.include_non_spatial)
                    if args.count_global:
                        row["count_global"] = count_taxon(gbif_occ, row["download_key"], None, args.include_non_spatial)
                else:
                    row["count_global"] = count_taxon(gbif_occ, row["download_key"], None, args.include_non_spatial)
                row["count_error"] = ""
            except Exception as exc:  # noqa: BLE001
                row["count_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.error(f"Zaehlfehler bei '{row['parsed_search_name']}': {exc}")
            if j % 25 == 0:
                log.info(f"  {j}/{len(todo)} gezaehlt")
            time.sleep(args.delay)
    finally:
        if rows:
            pd.DataFrame(rows, columns=REPORT_COLUMNS).to_csv(args.output_name_check, index=False, encoding="utf-8")
            log.info(f"Match-Report gespeichert: {args.output_name_check}")

    # Plan
    count_field = "count_aoi" if args.use_aoi else "count_global"
    taxa, zero, failed = [], [], []
    for r in rows:
        if r["decision"] != "ACCEPTED" or r["duplicate_of"]:
            continue
        if r["count_error"] or r.get(count_field) is None:
            failed.append(r["parsed_search_name"])
            continue
        n = int(float(r[count_field]))
        if n == 0:
            zero.append(r["parsed_search_name"])
            continue
        taxa.append({"key": str(r["download_key"]), "name": r["download_name"],
                     "rank": r["download_rank"], "count": n})

    n_rejected = sum(1 for r in rows if r["decision"] in ("REJECTED", "ERROR"))
    log.info(f"Ergebnis: {len(taxa)} Taxa im Plan, {n_rejected} verworfen/fehlerhaft, "
             f"{len(zero)} ohne Datensaetze, {len(failed)} ohne Zaehlung.")
    if zero:
        log.info("Ohne Datensaetze (nicht im Plan): " + ", ".join(zero[:20]) + (" ..." if len(zero) > 20 else ""))

    meta = {"created": datetime.now().isoformat(timespec="seconds"), "checklistKey": COL_CHECKLIST_KEY,
            "kingdom": args.kingdom, "use_aoi": bool(args.use_aoi), "aoi_hash": aoi_hash,
            "include_non_spatial": bool(args.include_non_spatial), "count_field": count_field,
            "infraspecific": args.infraspecific, "input_species": os.path.abspath(args.input_species)}
    if not taxa:
        log.error("Kein einziges Taxon im Plan - Abbruch.")
        sys.exit(1)
    create_batch_plan(taxa, args.batch_max_records, args.batch_max_taxa, meta, args.batch_plan_log)

    if failed:
        log.error(f"{len(failed)} Taxa ohne Zaehlung ausgeschlossen: {', '.join(failed[:10])} ... "
                  "Mit --resume erneut ausfuehren, bevor Skript 02 startet.")
        sys.exit(2)


if __name__ == "__main__":
    main()
