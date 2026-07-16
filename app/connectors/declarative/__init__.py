"""Constrained, data-only REST/OpenAPI connector primitives.

This package deliberately compiles published declarations into a fixed set of
operations.  It is not a general HTTP proxy, template engine, or plugin host.
"""

from .connector import DeclarativeConnector
from .http_client import SafeHttpClient, TargetGuard
from .models import (
    AuthScheme,
    DeclarativeOperation,
    DeclarativeRevision,
    InputMapping,
    OutputMapping,
    PaginationPolicy,
    SpecValidationError,
    SyncSpec,
    UnknownToolError,
    UnsafeTargetError,
)
from .validator import import_openapi_revision, validate_revision

__all__ = (
    "AuthScheme",
    "DeclarativeConnector",
    "DeclarativeOperation",
    "DeclarativeRevision",
    "InputMapping",
    "OutputMapping",
    "PaginationPolicy",
    "SafeHttpClient",
    "SpecValidationError",
    "SyncSpec",
    "TargetGuard",
    "UnknownToolError",
    "UnsafeTargetError",
    "import_openapi_revision",
    "validate_revision",
)
