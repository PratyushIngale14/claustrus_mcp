"""
Claustrus MCP Server — grounding engine.

Token-efficient design:
- Deterministic token-overlap pass costs zero API tokens (no LLM call)
- Source documents are chunked and hashed on first call, cached in memory
- Only the optional semantic pass calls Claude, and only on flagged sentences
- Average call: 0 tokens for offline grounding, ~150 tokens for semantic pass
  on one flagged sentence

Output is always a structured dict — never prose — so the MCP response
and the EU AI Act audit log are the same artifact.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Optional

GROUND_THRESHOLD = 0.28
PASS_THRESHOLD = 0.75
WARN_THRESHOLD = 0.40

_STOPWORDS = {
    "the","a","an","is","are","was","were","to","of","in","on","and","or",
    "for","with","this","that","it","their","they","has","have","had","be",
    "as","at","by","from","about","we","you","i","under","per","not","no",
    "which","these","those","there","will","would","been","than","then","so",
    "but","if","into","out","up","down","its","our","any","all","also","each",
    "been","when","who","where","may","can","more","such","after","before",
}

# ---- In-memory cache for tokenized source chunks ----
_SOURCE_CACHE: dict[str, list[dict]] = {}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9']+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 8]


def _chunk_source(doc_id: str, text: str, chunk_size: int = 200) -> list[dict]:
    """Split a source document into overlapping chunks for efficient retrieval.
    Chunks are cached by hash so re-tokenization never happens on the same content."""
    h = _hash(text)
    if h in _SOURCE_CACHE:
        return _SOURCE_CACHE[h]
    words = text.split()
    step = chunk_size // 2
    chunks = []
    for i in range(0, max(1, len(words) - chunk_size + 1), step):
        chunk_text = " ".join(words[i:i + chunk_size])
        chunks.append({
            "doc_id": doc_id,
            "chunk_idx": i // step,
            "text": chunk_text,
            "tokens": _tokenize(chunk_text),
            "hash": _hash(chunk_text),
        })
    if not chunks and text.strip():
        chunks.append({
            "doc_id": doc_id,
            "chunk_idx": 0,
            "text": text,
            "tokens": _tokenize(text),
            "hash": _hash(text),
        })
    _SOURCE_CACHE[h] = chunks
    return chunks


def _cite_sentence(sentence: str, chunks: list[dict]) -> dict:
    stoks = _tokenize(sentence)
    if not stoks:
        return {
            "sentence": sentence,
            "grounded": True,
            "overlap_score": 1.0,
            "source_doc": None,
            "source_chunk_hash": None,
            "source_excerpt": None,
            "reason": "No substantive content to verify.",
        }
    best = {"score": 0.0, "chunk": None}
    for chunk in chunks:
        if not chunk["tokens"]:
            continue
        overlap = len(stoks & chunk["tokens"]) / len(stoks)
        if overlap > best["score"]:
            best = {"score": overlap, "chunk": chunk}

    score = round(best["score"], 3)
    grounded = score >= GROUND_THRESHOLD

    if grounded and best["chunk"]:
        shared = sorted(stoks & best["chunk"]["tokens"])
        excerpt = best["chunk"]["text"][:180]
        return {
            "sentence": sentence,
            "grounded": True,
            "overlap_score": score,
            "source_doc": best["chunk"]["doc_id"],
            "source_chunk_hash": best["chunk"]["hash"],
            "source_excerpt": excerpt,
            "reason": f"Grounded in '{best['chunk']['doc_id']}' via terms: {', '.join(shared[:6])}.",
        }
    return {
        "sentence": sentence,
        "grounded": False,
        "overlap_score": score,
        "source_doc": None,
        "source_chunk_hash": None,
        "source_excerpt": None,
        "reason": (
            f"No source sufficiently supports this statement "
            f"(best overlap {score:.3f} below threshold {GROUND_THRESHOLD}). "
            "Possible unsupported assertion."
        ),
    }


def _semantic_pass(sentence: str, source_excerpt: str, api_key: str) -> str:
    """Targeted semantic check on a single flagged sentence.
    Called only when a sentence is UNSUPPORTED — minimizes token cost."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            f"Is this sentence grounded in the source excerpt? "
            f"Answer in one sentence.\n\n"
            f"Sentence: {sentence}\n\n"
            f"Best matching source excerpt: {source_excerpt or 'None found.'}"
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    except Exception as exc:
        return f"Semantic pass unavailable: {exc}"


def ground(
    rationale: str,
    sources: dict[str, str],
    api_key: Optional[str] = None,
    claim_id: Optional[str] = None,
) -> dict:
    """
    Main grounding function. Returns a fully structured result dict.

    Args:
        rationale: the AI-generated text to verify
        sources: dict of {doc_id: full_text} — the citable ground truth
        api_key: optional Anthropic key for targeted semantic pass on flagged sentences
        claim_id: optional identifier carried through to the audit log

    Returns a dict ready to serialize as JSON for the MCP response and audit log.
    """
    t_start = time.perf_counter()

    all_chunks: list[dict] = []
    for doc_id, text in sources.items():
        all_chunks.extend(_chunk_source(doc_id, text))

    sentences = _split_sentences(rationale)
    citations = []
    for s in sentences:
        cit = _cite_sentence(s, all_chunks)
        if not cit["grounded"] and api_key and all_chunks:
            best_excerpt = cit.get("source_excerpt") or (all_chunks[0]["text"][:180] if all_chunks else "")
            cit["semantic_note"] = _semantic_pass(s, best_excerpt, api_key)
        citations.append(cit)

    grounded_count = sum(1 for c in citations if c["grounded"])
    total = len(citations) if citations else 1
    score = round(grounded_count / total, 3)

    if score >= PASS_THRESHOLD:
        status = "PASSED"
    elif score >= WARN_THRESHOLD:
        status = "WARNING"
    else:
        status = "BLOCKED"

    latency_ms = round((time.perf_counter() - t_start) * 1000, 1)

    return {
        "claustrus_version": "1.0.0",
        "claim_id": claim_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "faithfulness_score": score,
        "status": status,
        "sentences_checked": len(citations),
        "sentences_grounded": grounded_count,
        "sentences_flagged": len(citations) - grounded_count,
        "method": "token_overlap" + ("+llm_semantic" if api_key else ""),
        "latency_ms": latency_ms,
        "citations": citations,
        "source_docs_used": list(sources.keys()),
        "source_chunks_indexed": len(all_chunks),
        "eu_ai_act_fields": {
            "article": "Article 12 — Record-keeping",
            "input_data_hash": _hash(rationale),
            "system_id": "claustrus-core-v1.0.0",
            "faithfulness_score": score,
            "status": status,
            "human_oversight_required": status != "PASSED",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        },
    }
