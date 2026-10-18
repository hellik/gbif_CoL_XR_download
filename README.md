# gbif_CoL_XR_download
Protototype of downloading GBIF data via the new CoL XR code 

# CoL-XR GBIF Pipeline-System

Ein modulares, robustes Python- und Batch-basiertes Automatisierungssystem für den kontrollierten Abruf und die räumliche Aggregation von GBIF-Vorkommensdaten (Global Biodiversity Information Facility).

Das System verwendet konsequent den **Catalogue of Life Extended Release (CoL-XR)** mit alphanumerischen Taxon-Keys (z. B. `4R5YN`) über `pygbif >= 0.7.0`.

---

## Inhaltsverzeichnis

- Architektur & Workflow
- Schnellstart
- Skript-Referenz
- Outputs & Berichte

---

## 🏗️ Architektur & Workflow

Die Pipeline gliedert sich in drei aufeinanderfolgende Kernskripte. Die Ausführung kann entweder über die Batch-Datei `run_gbif_pipeline.bat` oder durch Aufruf der Einzelskripte erfolgen.

### 1. `01_namensabgleich.py`

- Liest eine Rohartenliste ein.
- Gleicht Artnamen gegen CoL-XR ab.
- Filtert nach Rang und Kingdom.
- Löst Synonyme auf.
- Bereitet die AOI auf.
- Plant optimierte Download-Batches.

### 2. `02_daten_download.py`

- Steuert die GBIF-Download-API.
- Unterstützt Predicate-Downloads (`SIMPLE_CSV`) und optional SQL-Downloads.
- Überwacht Jobstatus.
- Unterstützt Wiederaufnahme unterbrochener Downloads.
- Dokumentiert DOI und Zitierung.

### 3. `03_h3_spatial_analysis.py`

- Verarbeitet heruntergeladene CSV- und TSV-Dateien.
- Führt Qualitätsprüfungen durch.
- Schneidet Daten exakt auf die AOI zu.
- Exportiert Punktdaten und H3-Hexagone als GeoPackage (`.gpkg`).

---

## 🚀 Schnellstart

### Voraussetzungen

- Python 3.9+
- `pygbif >= 0.7.0`
- `pandas`
- `requests`
- `h3`
- GDAL/OGR (`osgeo`) für AOI-Funktionen

### Konfiguration

Passen Sie die Parameter in der Datei `run_gbif_pipeline.bat` an.

### Ausführung

```bat
run_gbif_pipeline.bat
```

---

## ⚙️ Skript-Referenz

Die vollständige GitHub-optimierte Version enthält die detaillierten Parameterbeschreibungen für:

- `01_namensabgleich.py`
- `02_daten_download.py`
- `03_h3_spatial_analysis.py`

sowie die Berichts- und Ausgabeformate.

---

## 📊 Outputs & Berichte

- `species_match_report.csv`
- `download_batch_plan.json`
- `gbif_citations.txt`
- `quality_check_report.csv`
- `excluded_records.csv`
- GeoPackages (`.gpkg`)

