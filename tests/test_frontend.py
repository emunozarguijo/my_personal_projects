"""Los scripts del frontend comparten el ámbito global: un nombre repetido entre archivos (p. ej. `const pct` en uno y
`function pct` en otro) hace que el navegador descarte el archivo completo. Se revisan juntos, en el orden de index.html."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@pytest.mark.skipif(shutil.which("node") is None, reason="requiere Node.js")
def test_scripts_load_together_without_redeclarations(tmp_path):
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/(js/[^"]+)"', html)
    assert scripts, "index.html no carga scripts"
    bundle = tmp_path / "bundle.js"
    bundle.write_text("\n;\n".join((FRONTEND / s).read_text(encoding="utf-8") for s in scripts), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(bundle)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
