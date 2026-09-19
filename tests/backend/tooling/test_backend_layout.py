"""Check the installed package, independent of imports from the source checkout."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from astrbot_plugin_image_studio.tests.backend.tooling.test_build_plugin_package import (
    ROOT,
    builder,
)


def test_installable_package_imports_all_backend_modules_without_checkout(tmp_path):
    package_path = builder.build_package(ROOT, tmp_path / "plugin.zip")
    installed = tmp_path / "installed"
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(installed)
    plugin_name, _ = builder.package_identity(ROOT)
    host_spec = importlib.util.find_spec("astrbot")
    assert host_spec is not None and host_spec.submodule_search_locations
    host_parent = Path(next(iter(host_spec.submodule_search_locations))).parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(installed), str(host_parent)))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, pathlib, pkgutil, sys; "
            "name, installed = sys.argv[1:]; "
            "package = importlib.import_module(name); "
            "assert pathlib.Path(package.__file__).is_relative_to(installed); "
            "backend = importlib.import_module(name + '.backend'); "
            "modules = list(pkgutil.walk_packages(backend.__path__, backend.__name__ + '.')); "
            "[importlib.import_module(module.name) for module in modules]; "
            "importlib.import_module(name + '.main'); "
            "print('Installed backend imports passed:', len(modules))",
            plugin_name,
            str(installed),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installed backend imports passed:" in result.stdout
