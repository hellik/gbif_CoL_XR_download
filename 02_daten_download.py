"""
===============================================================================
02_daten_download.py
GBIF Download-Manager fuer den CoL-XR-Workflow.

- Standard: Predicate-Download (SIMPLE_CSV) mit alphanumerischen CoL-XR-Keys.
  'checklistKey' steht auf Root-Ebene UND im TAXON_KEY-Praedikat (wie in den
  GBIF-Techdocs bzw. pygbif 0.7.0 dokumentiert).
- AOI als 'within'-Praedikat (Polygon aus aoi_used.json, CCW-orientiert).
- SQL (SQL_TSV_ZIP) nur auf ausdruecklichen Wunsch (--download-method sql, experimentell).
- Job-Statusdatei (download_jobs.json): ein Neustart loest KEINE doppelten Jobs aus.
- Polling ueber pygbif.occurrences.download_meta, begrenzte Parallelitaet, Zeitlimit.
- DOI/Zitierung pro Batch (gbif_citations.txt) und download_manifest.json fuer Skript 03.
===============================================================================
"""
import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from datetime import datetime

import requests

from gbif_common import (COL_CHECKLIST_KEY, PLACEHOLDER_PREFIXES, setup_logging, with_retry)

log = setup_logging("02_daten_download")

DOWNLOAD_URL = "https://api.gbif.org/v1/occurrence/download/request"
FAILED_STATES = {"KILLED", "CANCELLED", "FAILED", "FILE_ERASED"}
NON_DATA_FILES = {"citation.txt", "rights.txt", "metadata.xml", "meta.xml", "eml.xml"}

# Spaltenliste des bereits getesteten SQL-Downloads (siehe test_csv.csv)
SQL_COLUMNS = [
    "gbifid", "datasetkey", "occurrenceid", "kingdom", "phylum", "class", "order", "family", "genus",
    "species", "infraspecificepithet", "taxonrank", "scientificname", "countrycode", "locality",
    "stateprovince", "occurrencestatus", "individualcount", "publishingorgkey", "decimallatitude",
    "decimallongitude", "coordinateuncertaintyinmeters", "elevation", "depth", "eventdate", "day",
    "month", "year", "taxonkey", "specieskey", "basisofrecord", "institutioncode", "collectioncode",
    "catalognumber", "recordnumber", "identifiedby", "dateidentified", "license", "recordedby",
    "establishmentmeans", "lastinterpreted", "hascoordinate", "issue",
]


class SubmitRejected(Exception):
    """GBIF hat den Request eindeutig abgelehnt (kein Job angelegt)."""


# --------------------------------------------------------------------------- #
# Hilfen
# --------------------------------------------------------------------------- #
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_credentials(dry_run):
    user, pwd, email = os.getenv("GBIF_USER"), os.getenv("GBIF_PWD"), os.getenv("GBIF_EMAIL")

    def placeholder(v):
        return (not v) or v.startswith(PLACEHOLDER_PREFIXES)

    if dry_run:
        return user or "<GBIF_USER>", pwd, (email if not placeholder(email) else None)
    if placeholder(user) or placeholder(pwd):
        log.error("GBIF_USER/GBIF_PWD fehlen oder sind noch Platzhalter.")
        sys.exit(1)
    if placeholder(email):
        log.warning("GBIF_EMAIL fehlt/Platzhalter - es wird keine Benachrichtigungs-Mail angefordert.")
        email = None
    return user, pwd, email


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #
def build_predicate(keys, wkt, include_non_spatial, include_absences):
    preds = [{"type": "in", "key": "TAXON_KEY", "values": list(keys), "checklistKey": COL_CHECKLIST_KEY}]
    if not include_absences:
        preds.append({"type": "equals", "key": "OCCURRENCE_STATUS", "value": "PRESENT"})
    if not include_non_spatial:
        preds.append({"type": "equals", "key": "HAS_COORDINATE", "value": "true"})
        preds.append({"type": "equals", "key": "HAS_GEOSPATIAL_ISSUE", "value": "false"})
    if wkt:
        preds.append({"type": "within", "geometry": wkt})
    return {"type": "and", "predicates": preds}


def build_sql_query(keys, wkt, include_non_spatial, include_absences):
    keys_sql = ", ".join("'" + str(k).replace("'", "''") + "'" for k in keys)
    where = [f'"specieskey" IN ({keys_sql})']
    if not include_absences:
        where.append("\"occurrencestatus\" = 'PRESENT'")
    if not include_non_spatial:
        where.append('"hascoordinate" = TRUE')
    if wkt:
        where.append(f"GBIF_Within('{wkt}', \"decimallatitude\", \"decimallongitude\")")
    cols = ", ".join(f'"{c}"' for c in SQL_COLUMNS)
    return f"SELECT {cols} FROM occurrence WHERE {' AND '.join(where)}"


def build_payload(method, info, wkt, args, creds):
    user, _, email = creds
    base = {"creator": user, "notificationAddresses": [email] if email else [], "sendNotification": bool(email)}
    keys = info["keys"]
    if method == "sql":
        non_species = [t["name"] for t in info.get("taxa", []) if t.get("rank") != "SPECIES"]
        if non_species:
            raise ValueError("SQL-Pfad unterstuetzt nur Art-Keys (specieskey); Unterarten im Batch: "
                             + ", ".join(non_species[:5]))
        base.update({"format": "SQL_TSV_ZIP",
                     "sql": build_sql_query(keys, wkt, args.include_non_spatial, args.include_absences)})
    else:
        base.update({"format": "SIMPLE_CSV", "checklistKey": COL_CHECKLIST_KEY,
                     "predicate": build_predicate(keys, wkt, args.include_non_spatial, args.include_absences)})
    return base


# --------------------------------------------------------------------------- #
# GBIF-Kommunikation
# --------------------------------------------------------------------------- #
def submit_job(payload, user, pwd):
    """POST ohne automatische Wiederholung (sonst drohen doppelte Jobs)."""
    res = requests.post(DOWNLOAD_URL, json=payload, auth=(user, pwd), timeout=(10, 60))
    if res.status_code == 201:
        return res.text.strip().strip('"')
    raise SubmitRejected(f"HTTP {res.status_code}: {res.text[:500]}")


def get_status(gbif_occ, job_key):
    return with_retry(gbif_occ.download_meta, job_key, timeout=(10, 30))


def fetch_zip(link, zip_path):
    part = zip_path + ".part"
    with requests.get(link, stream=True, timeout=(15, 300)) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    os.replace(part, zip_path)


def finalize_job(batch_id, job_key, meta, method, args):
    """ZIP laden, pruefen, entpacken, Zitierung schreiben. Gibt Manifest-Eintrag zurueck."""
    os.makedirs(args.download_dir, exist_ok=True)
    zip_path = os.path.join(args.download_dir, f"{job_key}.zip")
    extract_dir = os.path.join(args.download_dir, batch_id)

    if not os.path.exists(zip_path):
        log.info(f"[{batch_id}] Lade ZIP ({meta.get('size', '?')} Bytes) ...")
        with_retry(fetch_zip, meta["downloadLink"], zip_path, tries=3)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            os.remove(zip_path)
            raise RuntimeError(f"ZIP defekt (Eintrag {bad}); Datei geloescht - bitte erneut ausfuehren.")
        zf.extractall(extract_dir)

    files = []
    for root, dirs, names in os.walk(extract_dir):
        dirs[:] = [d for d in dirs if d.lower() not in ("dataset", "metadata")]
        for n in names:
            if n.lower() in NON_DATA_FILES or not n.lower().endswith((".csv", ".tsv", ".txt")):
                continue
            files.append(os.path.join(root, n))

    doi = meta.get("doi", "N/A")
    created = str(meta.get("created", datetime.now().isoformat()))[:10]
    doi_url = f"https://doi.org/{doi}" if doi != "N/A" else "N/A"
    citation = f"GBIF.org ({created}) GBIF Occurrence Download {doi_url}"
    with open(os.path.join(args.download_dir, "gbif_citations.txt"), "a", encoding="utf-8") as f:
        f.write(f"{batch_id} | Job {job_key} | Methode {method} | Records {meta.get('totalRecords', 'N/A')}\n"
                f"  {citation}\n")
    if args.delete_zip:
        os.remove(zip_path)
    log.info(f"[{batch_id}] fertig: {len(files)} Datendatei(en), DOI {doi}")
    return {"job_key": job_key, "doi": doi, "method": method, "dir": extract_dir, "files": files,
            "total_records": meta.get("totalRecords"), "citation": citation}


# --------------------------------------------------------------------------- #
# Job-Verwaltung
# --------------------------------------------------------------------------- #
def ensure_job(batch_id, payload, method, state, state_path, creds, args):
    """Gibt job_key zurueck (neu oder fortgesetzt) bzw. None, wenn der Batch bereits fertig ist."""
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    st = state.get(batch_id)
    if st and not args.force_resubmit:
        if st.get("status") == "DONE" and os.path.isdir(st.get("dir", "")):
            log.info(f"[{batch_id}] bereits fertig (Job {st.get('job_key')}) - uebersprungen.")
            return None
        if st.get("payload_hash") != payload_hash:
            raise RuntimeError("Plan/Parameter haben sich seit dem letzten Lauf geaendert "
                               "(--force-resubmit, um neu zu beauftragen).")
        if st.get("status") == "SUBMITTING" and not st.get("job_key"):
            raise RuntimeError("Vorheriger Submit ist unklar (Timeout?). Bitte auf "
                               "https://www.gbif.org/user/download pruefen; danach --force-resubmit.")
        if st.get("job_key") and st.get("status") != "FAILED":
            log.info(f"[{batch_id}] setze bestehenden Job {st['job_key']} fort.")
            return st["job_key"]
        if st.get("status") == "FAILED":
            raise RuntimeError(f"Job {st.get('job_key')} ist fehlgeschlagen (--force-resubmit fuer neuen Versuch).")

    state[batch_id] = {"status": "SUBMITTING", "payload_hash": payload_hash, "method": method,
                       "submitted": datetime.now().isoformat(timespec="seconds")}
    save_json(state_path, state)
    try:
        job_key = submit_job(payload, creds[0], creds[1])
    except SubmitRejected:
        state.pop(batch_id, None)          # eindeutig abgelehnt -> kein Job, sauberer Neustart moeglich
        save_json(state_path, state)
        raise
    state[batch_id].update({"job_key": job_key, "status": "SUBMITTED"})
    save_json(state_path, state)
    log.info(f"[{batch_id}] Job gestartet: {job_key} ({method.upper()})")
    return job_key


def run_batches(batches, args, creds, wkt, gbif_occ):
    os.makedirs(args.download_dir, exist_ok=True)
    state_path = os.path.join(args.download_dir, "download_jobs.json")
    manifest_path = os.path.join(args.download_dir, "download_manifest.json")
    state = load_json(state_path, {})
    manifest = load_json(manifest_path, {})

    pending = list(batches.items())
    active, failed = {}, []
    deadline = time.time() + args.max_wait_hours * 3600

    while pending or active:
        while pending and len(active) < args.max_parallel:
            bid, info = pending.pop(0)
            method = "predicate" if args.download_method == "auto" else args.download_method
            try:
                payload = build_payload(method, info, wkt, args, creds)
                job_key = ensure_job(bid, payload, method, state, state_path, creds, args)
            except Exception as exc:  # noqa: BLE001
                log.error(f"[{bid}] nicht gestartet: {exc}")
                failed.append(bid)
                continue
            if job_key:
                active[bid] = {"key": job_key, "method": method, "interval": 30, "next": time.time() + 15,
                               "errors": 0, "last_status": None}

        now = time.time()
        for bid in list(active):
            a = active[bid]
            if now < a["next"]:
                continue
            try:
                meta = get_status(gbif_occ, a["key"])
                a["errors"] = 0
            except Exception as exc:  # noqa: BLE001
                a["errors"] += 1
                log.warning(f"[{bid}] Statusabfrage fehlgeschlagen ({a['errors']}/10): {exc}")
                if a["errors"] >= 10:
                    failed.append(bid)
                    del active[bid]
                    continue
                a["next"] = now + a["interval"]
                continue

            status = meta.get("status")
            if status != a["last_status"]:
                log.info(f"[{bid}] Job {a['key']}: {status}")
                a["last_status"] = status
            if status == "SUCCEEDED":
                try:
                    manifest[bid] = finalize_job(bid, a["key"], meta, a["method"], args)
                    state[bid].update({"status": "DONE", "dir": manifest[bid]["dir"], "doi": manifest[bid]["doi"]})
                except Exception as exc:  # noqa: BLE001
                    log.error(f"[{bid}] Download/Entpacken fehlgeschlagen: {exc}")
                    failed.append(bid)
                else:
                    save_json(manifest_path, manifest)
                save_json(state_path, state)
                del active[bid]
            elif status in FAILED_STATES:
                log.error(f"[{bid}] Job {a['key']} endete mit {status}.")
                state[bid]["status"] = "FAILED"
                save_json(state_path, state)
                failed.append(bid)
                del active[bid]
            else:
                a["interval"] = min(int(a["interval"] * 1.3), 180)
                a["next"] = now + a["interval"]

        if time.time() > deadline:
            log.error(f"Zeitlimit von {args.max_wait_hours} h erreicht; laufende Jobs bleiben bei GBIF bestehen "
                      "und werden beim naechsten Lauf fortgesetzt.")
            failed.extend(active.keys())
            break
        if pending or active:
            time.sleep(5)
    return failed


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="GBIF Download-Manager (CoL-XR)")
    ap.add_argument("--batch-plan-log", default="download_batch_plan.json")
    ap.add_argument("--batch-id", default="all")
    ap.add_argument("--aoi-info", default="aoi_used.json")
    ap.add_argument("--use-aoi", action="store_true")
    ap.add_argument("--no-aoi", action="store_false", dest="use_aoi")
    ap.add_argument("--download-dir", default="gbif_downloads")
    ap.add_argument("--download-method", choices=["predicate", "sql", "auto"], default="auto",
                    help="auto = predicate. sql ist experimentell.")
    ap.add_argument("--include-non-spatial", action="store_true",
                    help="Auch Datensaetze ohne Koordinaten/mit Geo-Issues laden (bei AOI wirkungslos)")
    ap.add_argument("--include-absences", action="store_true", help="Auch OCCURRENCE_STATUS=ABSENT laden")
    ap.add_argument("--max-parallel", type=int, default=2)
    ap.add_argument("--max-wait-hours", type=float, default=24.0)
    ap.add_argument("--force-resubmit", action="store_true")
    ap.add_argument("--delete-zip", action="store_true")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--no-dry-run", action="store_false", dest="dry_run")
    args = ap.parse_args()

    if not os.path.exists(args.batch_plan_log):
        log.error(f"Batch-Plan nicht gefunden: {args.batch_plan_log} (zuerst Skript 01 ausfuehren)")
        sys.exit(1)
    plan = load_json(args.batch_plan_log, {})
    meta, all_batches = plan.get("meta", {}), plan.get("batches", {})
    if not all_batches:
        log.error("Batch-Plan enthaelt keine Batches.")
        sys.exit(1)

    if meta.get("checklistKey") != COL_CHECKLIST_KEY:
        log.error("Plan wurde nicht mit CoL-XR erstellt - bitte Skript 01 neu ausfuehren.")
        sys.exit(1)
    if bool(meta.get("use_aoi")) != bool(args.use_aoi):
        log.error(f"AOI-Einstellung passt nicht zum Plan (Plan: use_aoi={meta.get('use_aoi')}). Skript 01 neu ausfuehren.")
        sys.exit(1)
    if bool(meta.get("include_non_spatial")) != bool(args.include_non_spatial):
        log.warning("--include-non-spatial weicht vom Plan ab: die Mengenschaetzung im Plan stimmt dann nicht.")

    wkt = None
    if args.use_aoi:
        if not os.path.exists(args.aoi_info):
            log.error(f"AOI-Info nicht gefunden: {args.aoi_info} (Skript 01 mit --use-aoi ausfuehren)")
            sys.exit(1)
        aoi = load_json(args.aoi_info, {})
        if aoi.get("wkt_hash") != meta.get("aoi_hash"):
            log.error("aoi_used.json passt nicht zum Batch-Plan (andere AOI). Skript 01 neu ausfuehren.")
            sys.exit(1)
        wkt = aoi["wkt"]
        if args.include_non_spatial:
            log.warning("Mit AOI werden ohnehin nur Datensaetze mit Koordinaten geladen.")
        if aoi.get("mode") == "bbox":
            log.warning("AOI nur als Bounding-Box - Skript 03 muss mit --aoi exakt zuschneiden.")

    if args.batch_id == "all":
        batches = all_batches
    elif args.batch_id in all_batches:
        batches = {args.batch_id: all_batches[args.batch_id]}
    else:
        log.error(f"Batch-ID '{args.batch_id}' nicht im Plan. Verfuegbar: {', '.join(all_batches)}")
        sys.exit(1)

    if args.download_method == "sql":
        log.warning("SQL-Download ist experimentell (CoL-XR-Spalten/Funktionen vorab im Dry-Run pruefen).")

    creds = get_credentials(args.dry_run)
    n_taxa = sum(b.get("taxa_count", len(b["keys"])) for b in batches.values())
    log.info(f"{len(batches)} Batch(es), {n_taxa} Taxa, Methode: "
             f"{'predicate' if args.download_method == 'auto' else args.download_method}, "
             f"AOI: {'ja' if wkt else 'nein'}, Dry-Run: {args.dry_run}")

    if args.dry_run:
        method = "predicate" if args.download_method == "auto" else args.download_method
        for bid, info in batches.items():
            payload = build_payload(method, info, wkt, args, creds)
            shown = json.loads(json.dumps(payload))
            if wkt and len(wkt) > 300:  # Ausgabe kuerzen
                for p in shown.get("predicate", {}).get("predicates", []):
                    if p.get("type") == "within":
                        p["geometry"] = p["geometry"][:120] + f"... ({len(wkt)} Zeichen)"
            log.info(f"[DRY RUN] {bid} (ca. {info.get('total_records_est', '?')} Records):\n"
                     + json.dumps(shown, indent=2, ensure_ascii=False)[:4000])
        return

    from pygbif import occurrences as gbif_occ
    failed = run_batches(batches, args, creds, wkt, gbif_occ)
    if failed:
        log.error(f"Fehlgeschlagene/unvollstaendige Batches: {', '.join(sorted(set(failed)))}")
        sys.exit(1)
    log.info("Alle Batches abgeschlossen.")


if __name__ == "__main__":
    main()
