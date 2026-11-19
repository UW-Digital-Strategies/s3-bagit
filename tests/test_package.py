"""The package's own entry points: ``__version__`` resolution and ``python -m``."""

import importlib
import runpy
import sys

import pytest

import s3_bagit
from s3_bagit._version import __version__ as built_version


def test_version_falls_back_when_build_hook_has_not_run():
    # A fresh clone has no _version.py (hatch-vcs writes it at build time);
    # a None entry in sys.modules makes its import raise ImportError. The
    # reload in `finally` runs after the context restores sys.modules, so
    # later tests see the real version again.
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "s3_bagit._version", None)
            importlib.reload(s3_bagit)
            assert s3_bagit.__version__ == "0.0.0+unknown"
    finally:
        importlib.reload(s3_bagit)


def test_version_comes_from_the_build_hook_when_present():
    assert s3_bagit.__version__ == built_version


def test_python_dash_m_runs_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["s3-bagit", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("s3_bagit", run_name="__main__")
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("s3-bagit ")
