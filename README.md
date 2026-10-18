# gbif_CoL_XR_download
Protototype of downloading GBIF data via the new CoL XR code 

# CoL-XR GBIF Pipeline-System

Ein modulares, robustes Python- und Batch-basiertes Automatisierungssystem für den kontrollierten Abruf und die räumliche Aggregation von GBIF-Vorkommensdaten (Global Biodiversity Information Facility). Das System setzt konsequent auf den **Catalogue of Life Extended Release (CoL-XR)** mit alphanumerischen Taxon-Keys (z. B. `"4R5YN"`) über `pygbif >= 0.7.0`.

## 🏗️ Architektur & Workflow

Die Pipeline gliedert sich in drei aufeinanderfolgende Kernskripte, die über eine automatisierte Windows-Batch-Datei (`run_gbif_pipeline.bat`) oder einzeln gesteuert werden können:

1. **`01_namensabgleich.py`**: Liest eine Rohartenliste ein, gleicht die Namen gegen das CoL-XR-Checklist ab, filtert nach Rang/Kingdom, löst Synonyme auf, bereitet die Area of Interest (AOI) auf und plant optimierte Download-Batches.

2. **`02_daten_download.py`**: Steuert die GBIF Download-API (Predicate-basiert als `SIMPLE_CSV` oder optional als SQL), prüft den Job-Status, verwaltet Wiederaufnahmen und sichert DOIs sowie Zitierungen.

3. **`03_h3_spatial_analysis.py`**: Parst die heruntergeladenen CSV/TSV-Daten, führt eine strenge Qualitäts- und Statusklassifikation durch, schneidet exakt gegen die AOI zu und exportiert Punktdaten sowie H3-Hexagone als GeoPackage (`.gpkg`).

## 🚀 Schnellstart

1. **Voraussetzungen:** Python 3.9+, `pygbif >= 0.7.0`, `pandas`, `requests`, `h3` sowie GDAL/OGR (`osgeo`) für AOI-Operationen (empfohlen in der OSGeo4W-Shell).

2. **Konfiguration:** Passen Sie die Variablen in der `run_gbif_pipeline.bat` an (z. B. Pfad zur Artenliste, AOI-Datei und `RUN_MODE=dry`).

3. **Ausführung:** Starten Sie die Pipeline per Doppelklick auf die `run_gbif_pipeline.bat` oder in der Konsole.

## ⚙️ Skript-Referenz & Optionen

### Gemeinsame Basis (`gbif_common.py`)

Stellt globale Hilfsfunktionen bereit:

* **Logging:** Schreibt formatierte Logs in das Verzeichnis `logs/<skriptname>.log`.

* **Netzwerk-Resilienz:** Automatisierter Retry-Mechanismus mit exponentiellem Backoff bei Netzwerk- oder Serverfehlern (HTTP 5xx, 429).

* **AOI-Verarbeitung:** Liest GeoPackage-Geometrien, transformiert nach EPSG:4326, korrigiert Ringorientierungen (CCW für GBIF) und vereinfacht komplexe Polygone verlustarm.

### 1. Namensabgleich & Batch-Planung (`01_namensabgleich.py`)

Führt ausschließlich lesende Abfragen gegen GBIF durch und generiert den Abgleichsreport sowie den Download-Plan.

```
python 01_namensabgleich.py --input-species artenliste.txt [Optionen]

```

| 

| **Option** | **Standard** | **Beschreibung** | 
| `--input-species` | *(Pflicht)* | Pfad zur Textdatei mit den wissenschaftlichen Artnamen (ein Name pro Zeile). | 
| `--output-name-check` | `species_match_report.csv` | Pfad für den detaillierten Match- und Zählungsreport. | 
| `--batch-plan-log` | `download_batch_plan.json` | Pfad für den generierten Download-Batch-Plan. | 
| `--kingdom` | `all` | Taxonomische Einschränkung (`animals`, `plants`, `fungi`, `all`). | 
| `--accept-ranks` | `SPECIES,SUBSPECIES,VARIETY,FORM` | Kommagetrennte Liste akzeptierter taxonomischer Ränge. | 
| `--infraspecific` | `keep` | Umgang mit Unterarten: `keep` (Beibehaltung des Infraspezies-Keys) oder `species` (Hochstufung auf Art-Key). | 
| `--use-aoi` / `--no-aoi` | *(Aus)* | Aktiviert/Deaktiviert die räumliche Einschränkung und Zählung über eine AOI. | 
| `--aoi` | `None` | Pfad zur AOI-Datei (GeoPackage, `.gpkg`). | 
| `--aoi-layer` | `None` | Name des Layers in der GeoPackage-Datei (Standard: erster Layer). | 
| `--aoi-info` | `aoi_used.json` | Ablageort für die aufbereitete AOI-Meta-JSON. | 
| `--aoi-max-points` | `150` | Maximale Punktanzahl für die Polygonvereinfachung vor BBox-Fallback. | 
| `--include-non-spatial` | *(Aus)* | Schließt Datensätze ohne Koordinaten in die Zählung ein. | 
| `--count-global` | *(Aus)* | Fragt zusätzlich zur AOI-Zahl auch die globale GBIF-Zahl ab. | 
| `--batch-max-records` | `500000` | Maximale Datensatzanzahl pro Download-Batch. | 
| `--batch-max-taxa` | `50` | Maximale Anzahl an Taxa pro Download-Batch. | 
| `--delay` | `0.2` | Pause zwischen API-Abfragen in Sekunden (Vermeidung von Rate-Limits). | 
| `--limit` | `None` | Begrenzt die Verarbeitung auf die ersten $N$ Namen (ideal für Testläufe). | 
| `--resume` | *(Aus)* | Verwendet bereits abgeglichene Zeilen aus dem vorherigen Report als Cache. | 
| `--dry-run` / `--no-dry-run` | `--dry-run` | Kompatibilitätsflag (Skript 01 führt keine Schreibvorgänge bei GBIF aus). | 

### 2. Download-Manager (`02_daten_download.py`)

Verwaltet asynchrone GBIF-Downloads (Predicate-Downloads als `SIMPLE_CSV` oder experimentell per SQL), überwacht den Status und lädt die ZIP-Pakete herunter.

```
python 02_daten_download.py [Optionen]

```

| **Option** | **Standard** | **Beschreibung** | 
| `--batch-plan-log` | `download_batch_plan.json` | Pfad zum Batch-Plan aus Skript 01. | 
| `--batch-id` | `all` | Lädt entweder alle Batches (`all`) oder eine spezifische Batch-ID. | 
| `--aoi-info` | `aoi_used.json` | Pfad zur AOI-Meta-Datei zur Validierung des WKT-Hash. | 
| `--use-aoi` / `--no-aoi` | *(Aus)* | Aktiviert/Deaktiviert die AOI-Filterung im Download-Predicate. | 
| `--download-dir` | `gbif_downloads` | Zielverzeichnis für heruntergeladene ZIPs, entpackte Daten und Manifeste. | 
| `--download-method` | `auto` | Methode: `auto` (nutzt Predicate), `predicate` oder `sql` (experimentell). | 
| `--include-non-spatial` | *(Aus)* | Lädt auch Datensätze ohne Koordinaten oder mit Geo-Issues (bei AOI inaktiv). | 
| `--include-absences` | *(Aus)* | Lädt auch Datensätze mit `OCCURRENCE_STATUS = ABSENT`. | 
| `--max-parallel` | `2` | Maximale Anzahl gleichzeitig aktiver Download-Jobs bei GBIF. | 
| `--max-wait-hours` | `24.0` | Maximales Zeitlimit in Stunden für das Polling laufender Jobs. | 
| `--force-resubmit` | *(Aus)* | Zwingt zum Neustart von Jobs, selbst wenn Statusdateien existieren. | 
| `--delete-zip` | *(Aus)* | Löscht die heruntergeladenen ZIP-Archive nach erfolgreichem Entpacken. | 
| `--dry-run` / `--no-dry-run` | `--dry-run` | Gibt die generierten API-Payloads aus, ohne echte Jobs zu starten. | 

*Hinweis für den Echtbetrieb (`--no-dry-run`):* Benötigt Umgebungsvariablen `GBIF_USER`, `GBIF_PWD` und optional `GBIF_EMAIL`.

### 3. Räumliche Analyse & H3-Export (`03_h3_spatial_analysis.py`)

Analysiert die entpackten GBIF-Tabellen, filtert Qualitätsmängel aus, führt einen exakten AOI-Zuschnitt durch und exportiert Geodaten.

```
python 03_h3_spatial_analysis.py [Optionen]

```

| **Option** | **Standard** | **Beschreibung** | 
| `--input-dir` | `gbif_downloads` | Quellverzeichnis der entpackten Daten (nutzt `download_manifest.json`). | 
| `--input-file` | `None` | Verarbeitet alternativ eine einzelne spezifische Tabellendatei. | 
| `--kingdom` | `all` | Taxonomische Filterung auf Reichsebene (`animals`, `plants`, `fungi`, `all`). | 
| `--split-by-species` / `--no-split` | `--split-by-species` | Speichert pro Art ein separates GeoPackage (`gbif_<art>.gpkg`) oder alles in einer Datei (`gbif_all_species.gpkg`). | 
| `--output-dir` | `./h3_outputs` | Zielverzeichnis für GeoPackages, Ausschluss-CSV und Reports. | 
| `--res` | `5` | H3-Auflösungsebene (4 = grob, 5 = Standard, 6 = detailliert). | 
| `--aoi` | `None` | GeoPackage-Datei für den exakten polygonbasierten räumlichen Zuschnitt. | 
| `--aoi-layer` | `None` | Schichtname im GeoPackage für den exakten Zuschnitt. | 
| `--max-uncertainty` | `None` | Filtert Datensätze heraus, deren Koordinatenunsicherheit diesen Wert (in Metern) überschreitet. | 
| `--include-absences` | *(Aus)* | Behält Abwesenheitsmeldungen (`ABSENT`) bei. | 
| `--no-issue-filter` | *(Aus)* | Deaktiviert das Herausfiltern typischer Geodatenfehler (`GEOSPATIAL_ISSUES`). | 
| `--dry-run` / `--no-dry-run` | `--dry-run` | Führt den Durchlauf lesend durch, ohne Ausgabedateien zu schreiben. | 

## 📊 Outputs & Berichte

Nach erfolgreichem Durchlauf stellt die Pipeline folgende Berichte bereit:

* **`species_match_report.csv`**: Detailliertes Protokoll des Namensabgleichs und der Vorkommenszählungen.

* **`download_batch_plan.json`**: Strukturierter Plan zur Aufteilung der Taxa in Download-Tranches.

* **`gbif_citations.txt`**: Automatisch generierte DOIs und Zitierhinweise für wissenschaftliche Publikationen.

* **`quality_check_report.csv`**: Audit-Protokoll der Datenbilanz (Record-Balance-Check: `PASSED` / `FAILED`).

* **`excluded_records.csv`**: Protokoll aller aussortierten Datensätze inkl. Ausschlussgrund (`absent`, `not_spatial`, `geospatial_issue`, `outside_aoi`, etc.).

* **GeoPackages (`.gpkg`)**: Enthalten den Punkt-Layer (`points`) sowie die aggregierten H3-Hexagone (`h3_res<N>`) inklusive Dekaden- und Belegstatistiken.
