"""Shared laboratory-model client for the mock instruments behind the SiLA2 and LADS servers.

Two concerns, both of which every server needs and neither of which is instrument-specific:
`config` reads where the laboratory model is, and `transport` talks to it. Re-exported here
so servers import from the package root (`from laboratory_client import ...`) and are
unaffected if the module layout below changes.
"""

from .config import (
    LOCATION_VARIABLE,
    URL_VARIABLE,
    LaboratoryModelConfig,
    LaboratoryModelConfigError,
    load_laboratory_model_config,
)
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    LaboratoryModelRequestError,
    get_location,
    request_laboratory_model,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LOCATION_VARIABLE",
    "URL_VARIABLE",
    "LaboratoryModelConfig",
    "LaboratoryModelConfigError",
    "LaboratoryModelRequestError",
    "get_location",
    "load_laboratory_model_config",
    "request_laboratory_model",
]
