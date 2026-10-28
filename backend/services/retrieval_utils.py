"""Pure retrieval helpers (no models, no DB), so they can be unit-tested and reused."""


def reciprocal_rank_fusion(rankings, k=60):
    """RRF: score(d) = sum over rankings of 1 / (k + rank(d)).

    Fuses rankings whose raw scores aren't comparable (cosine similarity vs BM25) using positions only.
    k=60 is the standard constant: it damps the head of each list so one retriever can't dominate.
    """
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores
