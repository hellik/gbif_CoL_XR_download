@echo off
setlocal

:: ============================================================================
:: GBIF PIPELINE AUTOMATION (CoL-XR Workflow)
:: ============================================================================

:: 1. KONFIGURATION (Hier anpassen)
set "INPUT_SPECIES=pflanzen_selten.txt"
set "KINGDOM=plants"
set "USE_AOI=1"
set "AOI_FILE=aoi.gpkg"
set "AOI_LAYER="
set "RES=5"
:: Modus: 1 = Dry-Run (nur Testen/Prüfen), 0 = Echter Lauf (Downloads starten)
set "DRY_RUN=0
set "DOWNLOAD_DIR=gbif_downloads"
set "H3_OUTPUT_DIR=h3_outputs"

echo ============================================================================
echo  GBIF PIPELINE START (Dry-Run Modus: %DRY_RUN%)
echo ============================================================================
echo.

:: Prüfen, ob die Eingabedatei existiert
if not exist "%INPUT_SPECIES%" (
    echo [FEHLER] Eingabedatei "%INPUT_SPECIES%" nicht gefunden!
    goto error
)

:: ----------------------------------------------------------------------------
:: SCHRITT 1: Namensabgleich & Batch-Planung
:: ----------------------------------------------------------------------------
echo [SCHRITT 1/3] Starte Namensabgleich (01_namensabgleich.py)...

set "AOI_ARGS="
if "%USE_AOI%"=="1" (
    if not exist "%AOI_FILE%" (
        echo [FEHLER] AOI-Datei "%AOI_FILE%" nicht gefunden!
        goto error
    )
    set "AOI_ARGS=--use-aoi --aoi %AOI_FILE%"
    if not "%AOI_LAYER%"=="" set "AOI_ARGS=%AOI_ARGS% --aoi-layer %AOI_LAYER%"
)

python 01_namensabgleich.py ^
    --input-species "%INPUT_SPECIES%" ^
    --kingdom "%KINGDOM%" ^
    %AOI_ARGS%

if errorlevel 1 (
    echo [FEHLER] Skript 01 Namensabgleich abgebrochen!
    goto error
)
echo.

:: ----------------------------------------------------------------------------
:: SCHRITT 2: Daten-Download
:: ----------------------------------------------------------------------------
echo [SCHRITT 2/3] Starte GBIF-Daten-Download (02_daten_download.py)...

set "DL_AOI_ARGS="
if "%USE_AOI%"=="1" set "DL_AOI_ARGS=--use-aoi"

set "DRY_ARG=--dry-run"
if "%DRY_RUN%"=="0" set "DRY_ARG=--no-dry-run"

python 02_daten_download.py ^
    --download-dir "%DOWNLOAD_DIR%" ^
    %DL_AOI_ARGS% ^
    %DRY_ARG%

if errorlevel 1 (
    echo [FEHLER] Skript 02 Download abgebrochen!
    goto error
)
echo.

:: ----------------------------------------------------------------------------
:: SCHRITT 3: Räumliche Analyse & H3-Export
:: ----------------------------------------------------------------------------
echo [SCHRITT 3/3] Starte räumliche Analyse und H3-Export (03_h3_spatial_analysis.py)...

set "ANA_AOI_ARGS="
if "%USE_AOI%"=="1" (
    if exist "%AOI_FILE%" (
        set "ANA_AOI_ARGS=--aoi %AOI_FILE%"
        if not "%AOI_LAYER%"=="" set "ANA_AOI_ARGS=%ANA_AOI_ARGS% --aoi-layer %AOI_LAYER%"
    )
)

python 03_h3_spatial_analysis.py ^
    --input-dir "%DOWNLOAD_DIR%" ^
    --kingdom "%KINGDOM%" ^
    --output-dir "%H3_OUTPUT_DIR%" ^
    --res %RES% ^
    %ANA_AOI_ARGS% ^
    %DRY_ARG%

if errorlevel 1 (
    echo [FEHLER] Skript 03 H3-Analyse abgebrochen!
    goto error
)

echo.
echo ============================================================================
echo  PIPELINE ERFOLGREICH BEENDET!
echo ============================================================================
goto end

:error
echo.
echo ============================================================================
echo  PIPELINE MIT FEHLER ABGEBROCHEN.
echo ============================================================================
exit /b 1

:end
endlocal