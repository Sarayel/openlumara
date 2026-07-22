#!/usr/bin/env python3
"""
semantic_retrieval.py — embedding-based fallback retrieval (tier 2.5).

DESIGN PRINCIPLE: This is additive to deterministic key retrieval, NOT
a replacement. The primary path remains scene_index.py's exact tag matching.
This module only fires when tag matching returns insufficient results.

Flow:
  1. scene_index.py query by keys → if results found, done (deterministic)
  2. If no/insufficient results → embed the query, cosine similarity search
  3. Log similarity scores of top-k candidates for debuggability
  4. Return results with provenance (which path found them)

Embedding backend:
  - Default: sentence-transformers (all-MiniLM-L6-v2, 384-dim, ~80MB)
  - Alternative: Ollama embedding endpoint (nomic-embed-text)
  - Configurable via GM_EMBEDDING_BACKEND env var

Storage: embeddings stored alongside scene_index.json as scene_embeddings.json

Usage:
  python3 semantic_retrieval.py --campaign <name> index   # build index
  python3 semantic_retrieval.py --campaign <name> search "the guy with the weird ring"
  python3 semantic_retrieval.py --campaign <name> search "velkyn betrayal" --top-k 3
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Optional

from paths import find_campaign


# ── Config ──────────────────────────────────────────────────────────────────

DEFAULT_TOP_K = 3
DEFAULT_THRESHOLD = 0.3  # minimum cosine similarity to include
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2


def _emb_path(campaign: str) -> Path:
    return find_campaign(campaign) / "scene_embeddings.json"


def _load_embeddings(campaign: str) -> dict:
    p = _emb_path(campaign)
    if not p.exists():
        return {"version": 1, "embeddings": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "embeddings": []}


def _save_embeddings(campaign: str, data: dict) -> None:
    p = _emb_path(campaign)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Embedding backends ──────────────────────────────────────────────────────

def _embed_sentence_transformers(text: str) -> list[float]:
    """Embed using sentence-transformers (default, local, no API call)."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        raise ImportError("sentence-transformers not installed (pip install sentence-transformers)")

    # Cache the model globally
    global _ST_MODEL
    if "_ST_MODEL" not in globals():
        _ST_MODEL = None
    if _ST_MODEL is None:
        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")

    vec = _ST_MODEL.encode(text, normalize_embeddings=True)
    return vec.tolist()


def _embed_ollama(text: str) -> list[float]:
    """Embed using Ollama's embedding endpoint (nomic-embed-text)."""
    import urllib.request
    import urllib.error

    model = os.environ.get("GM_EMBEDDING_MODEL", "nomic-embed-text")
    host = os.environ.get("GM_OLLAMA_HOST", "http://localhost:11434")

    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("embedding", [])
    except Exception as e:
        raise RuntimeError(f"Ollama embedding failed: {e}")


def _embed(text: str) -> list[float]:
    """Embed text using the configured backend."""
    backend = os.environ.get("GM_EMBEDDING_BACKEND", "sentence-transformers").lower()

    if backend == "ollama":
        return _embed_ollama(text)
    else:
        return _embed_sentence_transformers(text)


# ── Cosine similarity ───────────────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── Indexing ────────────────────────────────────────────────────────────────

def cmd_index(args) -> int:
    """Build or update the embedding index from scene_index.json.

    For each scene in scene_index.json, embed the outcome_summary and
    store the vector alongside the scene ID.
    """
    scene_index_path = find_campaign(args.campaign) / "scene_index.json"
    if not scene_index_path.exists():
        print(f"# no scene_index.json found for '{args.campaign}'")
        return 1

    scenes = json.loads(scene_index_path.read_text(encoding="utf-8")).get("scenes", [])
    if not scenes:
        print(f"# no scenes to index")
        return 0

    emb_data = _load_embeddings(args.campaign)
    existing_ids = {e["scene_id"] for e in emb_data.get("embeddings", [])}

    new_count = 0
    for scene in scenes:
        sid = scene.get("id", "")
        if sid in existing_ids and not args.force:
            continue

        # Embed the outcome summary + retrieval keys for richer matching
        text = scene.get("outcome_summary", "") + " " + " ".join(scene.get("retrieval_keys", []))

        try:
            vec = _embed(text)
        except ImportError as e:
            print(f"# embedding backend not available: {e}")
            print(f"# install with: pip install sentence-transformers")
            return 2
        except Exception as e:
            print(f"# embedding failed for scene {sid}: {e}")
            continue

        # Remove old entry if force-rebuilding
        emb_data["embeddings"] = [e for e in emb_data.get("embeddings", []) if e.get("scene_id") != sid]

        emb_data["embeddings"].append({
            "scene_id": sid,
            "session": scene.get("session", 0),
            "text": text[:200],
            "vector": vec,
            "dim": len(vec),
        })
        new_count += 1
        print(f"  indexed: {sid} ({text[:60]}...)")

    _save_embeddings(args.campaign, emb_data)
    print(f"\n# indexed {new_count} new scene(s), {len(emb_data['embeddings'])} total")
    return 0


# ── Search ──────────────────────────────────────────────────────────────────

def cmd_search(args) -> int:
    """Search the embedding index for scenes matching a natural language query."""
    emb_data = _load_embeddings(args.campaign)
    embeddings = emb_data.get("embeddings", [])

    if not embeddings:
        print(f"# no embeddings indexed — run: python3 semantic_retrieval.py --campaign {args.campaign} index")
        return 1

    # Embed the query
    try:
        query_vec = _embed(args.query)
    except ImportError as e:
        print(f"# embedding backend not available: {e}")
        return 2
    except Exception as e:
        print(f"# query embedding failed: {e}")
        return 1

    # Compute similarity scores
    scored = []
    for emb in embeddings:
        score = _cosine_similarity(query_vec, emb.get("vector", []))
        scored.append({
            "scene_id": emb.get("scene_id"),
            "session": emb.get("session"),
            "text": emb.get("text", ""),
            "similarity": round(score, 4),
        })

    # Sort by similarity descending
    scored.sort(key=lambda x: x["similarity"], reverse=True)

    # Filter by threshold
    top_k = args.top_k or DEFAULT_TOP_K
    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLD
    results = [r for r in scored[:top_k] if r["similarity"] >= threshold]

    if not results:
        print(f"# no scenes matched (threshold={threshold})")
        print(f"# top score was {scored[0]['similarity'] if scored else 'N/A'}")
        return 0

    print(f"# semantic search: \"{args.query}\"\n")
    print(f"# {len(results)} match(es) (top {top_k}, threshold {threshold})\n")

    for i, r in enumerate(results, 1):
        print(f"  [{i}] {r['scene_id']} (s{r['session']}) — similarity: {r['similarity']}")
        print(f"      {r['text'][:80]}...")
        print()

    # Log for debuggability
    print(f"## similarity scores (all candidates, for debugging):")
    for r in scored[:10]:
        print(f"  {r['scene_id']}: {r['similarity']}")
    return 0


# ── Integration helper ──────────────────────────────────────────────────────

def semantic_fallback_search(campaign: str, query: str, top_k: int = 3) -> list[dict]:
    """Programmatic API for the retrieval layer.

    Called by scene_loader.py when deterministic key retrieval returns
    insufficient results. Returns a list of scene summaries with similarity
    scores.

    Returns [] if embeddings not available or no matches.
    """
    emb_data = _load_embeddings(campaign)
    embeddings = emb_data.get("embeddings", [])

    if not embeddings:
        return []

    try:
        query_vec = _embed(query)
    except Exception:
        return []

    scored = []
    for emb in embeddings:
        score = _cosine_similarity(query_vec, emb.get("vector", []))
        if score >= DEFAULT_THRESHOLD:
            scored.append({
                "scene_id": emb.get("scene_id"),
                "session": emb.get("session"),
                "text": emb.get("text", ""),
                "similarity": round(score, 4),
                "source": "semantic",  # provenance tag
            })

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaign", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="Build/update embedding index from scene_index.json")
    s.add_argument("--force", action="store_true", help="Rebuild all embeddings")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="Semantic search for scenes")
    s.add_argument("query", help="Natural language query")
    s.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    s.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    s.set_defaults(func=cmd_search)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
