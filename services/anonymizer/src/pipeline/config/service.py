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

from pipeline.config.loader import Settings

# Directory where bundled config YAML files are located.
_CONFIG_DIR = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")

# Directory where user-defined config YAML files are stored at runtime.
_USER_CONFIG_DIR = os.environ.get("MEDANON_USER_CONFIG_DIR", "/output/user-configs")

# Canonical profile name → config filename mapping (bundled system profiles only).
_PROFILE_MAP = {
    "auto": None,  # triggers auto-selection logic below
    "minimal": "config.yaml",
    "gpas": "config_gpas.yaml",
    "gdpr": "config_gdpr_eu.yaml",
    "hipaa": "config_hipaa_safe_harbor.yaml",
    "research": "config_research_pseudonymous.yaml",
    "structural": "config_structure_preserving.yaml",
    "value-masking": "config_value_masking.yaml",
}

# TTL in seconds (0 = disabled; cache lives for the process lifetime).
_CACHE_TTL = int(os.environ.get("MEDANON_CONFIG_CACHE_TTL", "0"))
_last_clear: float = 0.0

# Max distinct config profiles kept resident in the LRU (E5.4).  Deployments
# with many user-defined profiles can raise this; the default of 32 covers the
# 8 bundled profiles plus headroom for ad-hoc user configs.
_CACHE_SIZE = max(1, int(os.environ.get("MEDANON_CONFIG_CACHE_SIZE", "32")))


@lru_cache(maxsize=_CACHE_SIZE)
def _load_settings(abs_path: str) -> Settings:
    """Internal: load and cache settings by absolute file path."""
    settings = Settings(abs_path)
    try:
        from pipeline.rule_matcher import warm_rule_caches

        warm_rule_caches(settings)
    except Exception:
        pass
    return settings


def _resolve_profile(profile: str) -> str:
    """Resolve a profile alias to an absolute YAML file path.

    Resolution order:
    1. System profiles in _PROFILE_MAP  → _CONFIG_DIR/<filename>
    2. User-defined profiles            → _USER_CONFIG_DIR/<profile>.yaml
    3. ValueError if neither found

    Raises:
        ValueError: If *profile* is unknown and no user config file exists.
    """
    # Re-read from env at call time so test overrides (monkeypatch.setenv,
    # os.environ.setdefault) take effect even after module import.
    config_dir = os.environ.get("MEDANON_CONFIG_DIR", _CONFIG_DIR)
    user_config_dir = os.environ.get("MEDANON_USER_CONFIG_DIR", _USER_CONFIG_DIR)

    # System profile?
    if profile in _PROFILE_MAP:
        filename = _PROFILE_MAP[profile]
        if filename is None:  # 'auto'
            filename = (
                "config_gpas.yaml" if os.environ.get("GPAS_URL") else "config.yaml"
            )
        return os.path.join(config_dir, filename)

    # User-defined profile?
    # Reject path-traversal characters early as defence-in-depth before
    # touching the filesystem (the realpath check below is the actual
    # security boundary).
    if not profile or any(c in profile for c in ("/", "\\", "\x00")) or ".." in profile:
        raise ValueError(
            f"Invalid profile name '{profile}': must not contain path separators."
        )
    user_path = os.path.join(user_config_dir, f"{profile}.yaml")
    # Guard against path traversal: resolved path must stay inside the user config dir.
    resolved = os.path.realpath(user_path)
    allowed_dir = os.path.realpath(user_config_dir)
    if not resolved.startswith(allowed_dir + os.sep) and resolved != allowed_dir:
        raise ValueError(
            f"Invalid profile name '{profile}': resolves outside the user config directory."
        )
    if os.path.isfile(resolved):
        return resolved

    valid = ", ".join(_PROFILE_MAP.keys())
    raise ValueError(
        f"Unknown profile '{profile}'. Built-in profiles: {valid}. "
        "For user-defined configs, create one via POST /v1/configs first."
    )


def clear_settings_cache() -> None:
    """Invalidate the config cache — next call to get_settings() reloads from disk.

    Called from the config CRUD endpoints (POST/PUT/DELETE /v1/configs) so
    profile edits take effect without a process restart.
    """
    global _last_clear
    _load_settings.cache_clear()
    _last_clear = time.monotonic()
    # Also clear rule matcher caches that depend on config content
    try:
        from pipeline.rule_matcher import clear_rule_caches

        clear_rule_caches()
    except ImportError:
        pass
    # And the gPAS params cache (keyed per profile content)
    try:
        from pipeline.gpas_orchestrator import clear_gpas_params_cache

        clear_gpas_params_cache()
    except ImportError:
        pass


def get_settings(profile: str = "auto") -> Settings:
    """Load Settings for the named config profile (results cached per resolved filename).

    Args:
        profile: A built-in profile name (auto, minimal, gpas, gdpr, hipaa, research,
                 structural, value-masking) or a user-defined profile name created via POST /v1/configs.
                 'auto' selects config_gpas.yaml when GPAS_URL is set, else config.yaml.
                 The 'auto' alias is resolved on every call so it always reflects the
                 current environment — it is never cached under the key 'auto'.

    Returns:
        Loaded and validated Settings instance.

    Raises:
        ValueError: If *profile* is unknown and no user config file exists.
        FileNotFoundError: If the resolved config file does not exist on disk.
    """
    global _last_clear
    if _CACHE_TTL > 0:
        now = time.monotonic()
        if now - _last_clear > _CACHE_TTL:
            _load_settings.cache_clear()
            _last_clear = now
    abs_path = _resolve_profile(profile)
    return _load_settings(abs_path)
