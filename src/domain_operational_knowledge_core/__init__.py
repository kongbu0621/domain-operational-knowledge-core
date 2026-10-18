"""Public library for a single trusted local operational knowledge domain."""
from .model import KnowledgeError
from .service import initialize, open_store

__all__ = ["initialize", "open_store", "KnowledgeError"]
