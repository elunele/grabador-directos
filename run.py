"""Punto de entrada (para ejecutar con `python run.py` y para PyInstaller)."""
import sys

if "--selftest" in sys.argv:
    # Comprobación rápida del empaquetado: python run.py --selftest URL
    import json
    from grabador import core
    core.setup_bundled_paths()
    url = sys.argv[sys.argv.index("--selftest") + 1]
    info = core.analyze(url)
    print(json.dumps({"ffmpeg": core.ffmpeg_path(), "kind": info.kind, "title": info.title,
                      "qualities": info.qualities, "preview": bool(info.preview_url),
                      "error": info.error[:200]}, ensure_ascii=False))
    sys.exit(0)

from grabador.app import main

main()
