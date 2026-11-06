#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Modules - API client (Hermes build stub).

The upstream module talks to third-party embedding/rerank HTTP APIs. The
Hermes build of Webnovel Studio makes no network calls of its own, so every
client here refuses to start. Callers in the engine already treat a failing
client as "vector retrieval unavailable" and fall back to local retrieval.
"""

from dataclasses import dataclass


class VectorRetrievalDisabledError(RuntimeError):
    """Raised whenever code tries to reach an embedding or rerank API."""


_MESSAGE = "vector retrieval is disabled in the Hermes build of Webnovel Studio"


@dataclass
class APIStats:
    total_calls: int = 0
    total_time: float = 0.0
    errors: int = 0


class _DisabledClient:
    def __init__(self, config=None):
        raise VectorRetrievalDisabledError(_MESSAGE)


class EmbeddingAPIClient(_DisabledClient):
    pass


class RerankAPIClient(_DisabledClient):
    pass


class ModalAPIClient(_DisabledClient):
    pass


def get_client(config=None) -> ModalAPIClient:
    raise VectorRetrievalDisabledError(_MESSAGE)
