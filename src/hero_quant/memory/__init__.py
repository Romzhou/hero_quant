"""Memory package."""

from .hierarchy import CATEGORIES, MemoryHierarchy
from .lifecycle import (
    ARCHIVE_THRESHOLD,
    DELETE_THRESHOLD,
    MIN_AGE_DAYS,
    MemoryLifecycle,
    compute_importance,
)
from .store import MemoryStore, PgVectorSidecar, get_vector_dim, is_pgvector_configured

__all__ = [
    "MemoryStore",
    "PgVectorSidecar",
    "MemoryHierarchy",
    "MemoryLifecycle",
    "CATEGORIES",
    "ARCHIVE_THRESHOLD",
    "DELETE_THRESHOLD",
    "MIN_AGE_DAYS",
    "compute_importance",
    "is_pgvector_configured",
    "get_vector_dim",
]
