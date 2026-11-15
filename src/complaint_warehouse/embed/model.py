"""Embedding model wrapper.

The job and the search API depend on the small `Encoder` protocol rather than on
sentence-transformers directly, so tests can swap in a deterministic fake and the heavy
torch import only happens when a real model is loaded.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class Encoder(Protocol):
    name: str
    dim: int

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Return an (n, dim) float32 array of L2-normalised vectors."""
        ...

    def encode_query(self, text: str) -> np.ndarray:
        """Return a (dim,) float32 L2-normalised vector."""
        ...


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


class SentenceTransformerEncoder:
    """A sentence-transformers model, run in fp16 on CUDA and fp32 on CPU.

    Vectors are L2-normalised at encode time, so cosine distance in pgvector (`<=>`) and
    inner product rank identically and the index never has to normalise.
    """

    def __init__(
        self,
        model_name: str,
        *,
        dim: int,
        device: str = "auto",
        batch_size: int = 128,
        max_seq_length: int = 512,
        query_instruction: str = "",
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.name = model_name
        self.dim = dim
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        self.device = resolve_device(device)

        self._model = SentenceTransformer(model_name, device=self.device)
        self._model.max_seq_length = max_seq_length
        if self.device.startswith("cuda"):
            # Half precision roughly doubles throughput; on normalised vectors the
            # difference in cosine similarity is in the 4th decimal place.
            self._model.half()

        # Renamed in sentence-transformers 6; keep working on 5.x.
        get_dim = (
            getattr(self._model, "get_embedding_dimension", None)
            or self._model.get_sentence_embedding_dimension
        )
        actual = get_dim()
        if actual != dim:
            raise ValueError(f"{model_name} outputs {actual} dimensions, expected {dim}")
        log.info("loaded %s on %s (max_seq_length=%d)", model_name, self.device, max_seq_length)

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.astype(np.float32, copy=False)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode_documents([self.query_instruction + text])[0]


def load_encoder(settings) -> SentenceTransformerEncoder:  # type: ignore[no-untyped-def]
    return SentenceTransformerEncoder(
        settings.embedding_model,
        dim=settings.embedding_dim,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        max_seq_length=settings.embedding_max_seq_length,
        query_instruction=settings.embedding_query_instruction,
    )
