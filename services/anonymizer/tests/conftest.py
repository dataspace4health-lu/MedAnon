"""pytest configuration for the anonymizer test suite.

Injects a `typing.io` compatibility shim required by fhirpathpy/antlr4, which
imports ``from typing.io import TextIO``.  That sub-module was removed in
Python 3.13; this shim recreates it so the import chain succeeds on 3.13+.
"""
import sys
import types
import typing

if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO          # type: ignore[attr-defined]
    _io_mod.TextIO = typing.TextIO  # type: ignore[attr-defined]
    _io_mod.BinaryIO = typing.BinaryIO  # type: ignore[attr-defined]
    sys.modules["typing.io"] = _io_mod
