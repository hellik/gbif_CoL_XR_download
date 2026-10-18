"""
===============================================================================
check_and_install_packages.py
Automatische Überprüfung und Installation aller Python-Pakete,
die in den Skripts (01_namensabgleich.py, 02_daten_download.py, 
03_h3_spatial_analysis.py und gbif_common.py) verwendet werden.
===============================================================================
"""

import ast
import os
import subprocess
import sys

# Definieren Sie hier die Standardbibliothek von Python, um sie nicht als fehlendes Paket zu erkennen
STANDARD_LIBRARY = {
    "argparse", "ast", "csv", "datetime", "hashlib", "json", "logging", 
    "os", "re", "subprocess", "sys", "time", "zipfile"
}

# Mapping von Import-Namen auf pip-Installationsnamen, falls diese abweichen
PACKAGE_MAPPING = {
    "pandas": "pandas",
    "pygbif": "pygbif>=0.7.0",
    "requests": "requests",
    "h3": "h3",
    "osgeo": "GDAL",  # GDAL/OGR erfordert oft spezielle Binärpakete, Hinweis erfolgt im Skript
}

# Liste der zu durchsuchenden Skripte
TARGET_SCRIPTS = [
    "01_namensabgleich.py",
    "02_daten_download.py",
    "03_h3_spatial_analysis.py",
    "gbif_common.py"
]

def extract_imports_from_file(filepath):
    """Extrahiert alle importierten Top-Level-Module aus einer Python-Datei mittels AST."""
    imports = set()
    if not os.path.exists(filepath):
        print(f"[Warnung] Datei nicht gefunden: {filepath}")
        return imports
    
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=filepath)
        except SyntaxError as e:
            print(f"[Fehler] Syntaxfehler in {filepath}: {e}")
            return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Nur das Top-Level-Modul (z. B. 'os' aus 'os.path') erfassen
                top_level = alias.name.split('.')[0]
                imports.add(top_level)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level = node.module.split('.')[0]
                imports.add(top_level)
                
    return imports

def check_package_installed(package_name):
    """Überprüft, ob ein Paket in der aktuellen Python-Umgebung installiert ist."""
    try:
        __import__(package_name)
        return True
    except ImportError:
        return False

def install_package(package_spec):
    """Installiert ein Paket via pip."""
    print(f"[Installation] Installiere Paket: {package_spec} ...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_spec])
        print(f"[Erfolgreich] {package_spec} wurde installiert.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Fehler] Konnte {package_spec} nicht installieren: {e}")
        return False

def main():
    print("Suche nach importierten Paketen in den Skripts...")
    all_imports = set()

    for script in TARGET_SCRIPTS:
        found = extract_imports_from_file(script)
        print(f"  - {script}: Gefundene Module -> {sorted(list(found))}")
        all_imports.update(found)

    # Filtern der Standardbibliothek
    external_packages = {imp for imp in all_imports if imp not in STANDARD_LIBRARY}
    print(f"\nRelevante externe Pakete identifiziert: {sorted(list(external_packages))}\n")

    missing_or_to_check = []
    for pkg in external_packages:
        # Modulname auf Import-Ebene mappen (z. B. osgeo -> GDAL)
        check_name = "osgeo" if pkg == "GDAL" else pkg
        if not check_package_installed(check_name):
            missing_or_to_check.append(pkg)
        else:
            print(f"[OK] Paket '{pkg}' ist bereits installiert.")

    if not missing_or_to_check:
        print("\nAlle benötigten Pakete sind bereits installiert!")
        return

    print(f"\nFolgende Pakete fehlen und werden installiert: {missing_or_to_check}")
    
    for pkg in missing_or_to_check:
        pip_target = PACKAGE_MAPPING.get(pkg, pkg)
        if pkg == "GDAL":
            print("\nHinweis: Das Paket 'GDAL' (osgeo) erfordert unter Windows oft vorkompilierte Binärdateien (z. B. über Gohlke Wheels oder OSGeo4W).")
        install_package(pip_target)

    print("\nÜberprüfung und Installation abgeschlossen.")

if __name__ == "__main__":
    main()