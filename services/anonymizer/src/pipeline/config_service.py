"""Centralised config profile loader.

Single source of truth for profile-name → YAML-filename mapping and the
LRU-cached settings loader. Used by both the FastAPI layer (api/deps.py)
and any other entry point that needs profile-based config selection.

Caching design:
  - ``_load_settings(filename)`` is @lru_cache(maxsize=8); cache key is the
    *resolved* filename, not the profile alias.  This prevents the classic
    ``'auto'``-over-caching bug where the profile alias is cached before
    ``GPAS_URL`` is set, causing the wrong config to be served for the rest
    of the process lifetime.
  - ``_resolve_profile(profile)`` reads the current env on every call so
    that ``'auto'`` always reflects the live ``GPAS_URL`` value.
  - ``MEDANON_CONFIG_CACHE_TTL`` (seconds; 0 = forever) enables hot-config-
    reload in long-lived processes without a restart.
  - ``clear_settings_cache()`` exposes manual invalidation for tests and
    admin tooling.
"""

import os
import time
from functools import lru_cache

import pipeline.config as config

# Directory where config YAML files are located.
_CONFIG_DIR = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")

# Canonical profile name → config filename mapping.
_PROFILE_MAP = {
    'auto':       None,                              # triggers auto-selection logic below
    'minimal':    'config.yaml',
    'gpas':       'config_gpas.yaml',
    'gdpr':       'config_gdpr_eu.yaml',
    'hipaa':      'config_hipaa_safe_harbor.yaml',
    'research':   'config_research_pseudonymous.yaml',
    'structural': 'config_structure_preserving.yaml',
}

# TTL in seconds (0 = disabled; cache lives for the process lifetime).
_CACHE_TTL = int(os.environ.get("MEDANON_CONFIG_CACHE_TTL", "0"))
_last_clear: float = 0.0


@lru_cache(maxsize=8)
def _load_settings(filename: str) -> config.Settings:
    """Internal: load and cache settings by resolved filename."""
    return config.Settings(os.path.join(_CONFIG_DIR, filename))


def _resolve_profile(profile: str) -> str:
    """Resolve a profile alias to a concrete YAML filename using the current env.

    Raises:
        ValueError: If *profile* is not in _PROFILE_MAP.
    """
    if profile not in _PROFILE_MAP:
        valid = ', '.join(_PROFILE_MAP.keys())
        raise ValueError(f"Unknown profile '{profile}'. Valid: {valid}")
    filename = _PROFILE_MAP[profile]
    if filename is None:  # 'auto'
        filename = 'config_gpas.yaml' if os.environ.get('GPAS_URL') else 'config.yaml'
    return filename


def clear_settings_cache() -> None:
    """Invalidate the config cache — next call to get_settings() reloads from disk."""
    global _last_clear
    _load_settings.cache_clear()
    _last_clear = time.monotonic()


def get_settings(profile: str = 'auto') -> config.Settings:
    """Load Settings for the named config profile (results cached per resolved filename).

    Args:
        profile: One of: auto, minimal, gpas, gdpr, hipaa, research, structural.
                 'auto' selects config_gpas.yaml when GPAS_URL is set, else config.yaml.
                 The 'auto' alias is resolved on every call so it always reflects the
                 current environment — it is never cached under the key 'auto'.

    Returns:
        Loaded and validated Settings instance.

    Raises:
        ValueError: If *profile* is not in _PROFILE_MAP.
        FileNotFoundError: If the resolved config file does not exist.
    """
    global _last_clear
    if _CACHE_TTL > 0:
        now = time.monotonic()
        if now - _last_clear > _CACHE_TTL:
            _load_settings.cache_clear()
            _last_clear = now
    return _load_settings(_resolve_profile(profile))
