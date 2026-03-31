"""Re-export from medanon_core for backward compatibility."""
from medanon_core.analytics.synthetic import *  # noqa: F401, F403
# Explicitly re-export private names used by white-box tests
from medanon_core.analytics.synthetic import (  # noqa: F401
    _extract_condition_distributions,
    _extract_distributions,
    _make_condition,
    _make_patient,
)
