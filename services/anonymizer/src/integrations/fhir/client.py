"""FHIR client barrel — re-exports all public names for backward compatibility."""
from integrations.fhir._transport import *  # noqa: F401,F403
from integrations.fhir.reader import *  # noqa: F401,F403
from integrations.fhir.writer import *  # noqa: F401,F403
from integrations.fhir.bulk import *  # noqa: F401,F403
