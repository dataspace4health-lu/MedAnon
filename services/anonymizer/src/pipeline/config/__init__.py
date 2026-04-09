"""Configuration sub-package.

Re-exports ``Settings`` from the loader module so existing imports like
``import pipeline.config as config; config.Settings(...)`` continue to work.
"""

from pipeline.config.loader import Settings  # noqa: F401
