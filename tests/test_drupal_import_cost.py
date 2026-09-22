"""`import graphify` must stay lazy after the Drupal seam is wired in.

graphify/__init__.py exists to be cheap: `graphify install` has to work before
tree-sitter and friends are installed. Measured costs are 1 ms for `graphify`
against 809 ms for `graphify.extract`, so the seam may install a hook but must
never import the modules it patches.
"""
from __future__ import annotations

import subprocess
import sys


def _probe(expr: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", f"import sys, graphify; print({expr})"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_importing_graphify_does_not_import_extract():
    assert _probe("'graphify.extract' in sys.modules") == "False"


def test_importing_graphify_does_not_import_detect():
    assert _probe("'graphify.detect' in sys.modules") == "False"


def test_importing_graphify_does_not_import_yaml():
    assert _probe("'yaml' in sys.modules") == "False"


def test_seam_is_installed_by_importing_graphify():
    expr = "any(type(f).__name__ == '_DrupalFinder' for f in sys.meta_path)"
    assert _probe(expr) == "True"


def test_extract_is_patched_on_first_import_not_before():
    code = (
        "import sys, graphify;"
        "import graphify.extract as e;"
        "print(getattr(e._get_extractor, '_drupal_patched', False))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"
