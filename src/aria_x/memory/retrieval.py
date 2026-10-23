"""Zero-dependency lexical retrieval over the ARIA-X blackboard.

Why this module exists
----------------------
The planner and workers need *relevance*, not another model call: a worker that
owns a TLS intent should see the certificate facts and the earlier TLS traces,
not the entire graph. ARIA-X standard-library-only policy rules out embedding
libraries or BM25 packages, so this module implements a small TF-IDF cosine
ranker over the two retrieval corpora that already exist in the store:

* graph nodes (facts, findings, notes, intents, goals, hints), and
* cross-worker process traces (ARTEX lineage).

Chinese is a first-class engagement language here. Dense CJK writing has no
whitespace boundaries, so unigram tokenization would over-match function words;
we index overlapping bigrams ("数据库" -> 数据, 据库) and also emit a unigram for
single-character runs. Latin text uses word tokens with dotted/URL-ish
identifiers kept whole *and* split into their word parts.

Ranking is deliberately deterministic: equal scores break by kind priority,
graph sequence and id. Two retrievals over an unchanged store must produce the
same order, because the planner prompt is compared in tests and replay.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..models import NODE_KINDS, Node, Trace
from ..store import Store
from ..util import json_dumps, truncate

#: Node kinds that participate in retrieval by default.
DEFAULT_NODE_KINDS: tuple[str, ...] = ("goal", "intent", "fact", "finding", "hint", "note", "guide")
#: Sort priority used for deterministic tie-breaking (lower wins).
_KIND_ORDER = {kind: index for index, kind in enumerate(DEFAULT_NODE_KINDS + ("trace",))}
#: Separators between snippets.
_WS = re.compile(r"\s+")
#: A CJK run or a Latin identifier-ish token.
_TOKEN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+"          # CJK ideographs
    r"|[A-Za-z0-9][A-Za-z0-9_.:/@\-]*[A-Za-z0-9]"           # identifiers / urls / ips
    r"|[A-Za-z0-9]"                                          # single alphanumerics
)
_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")


@dataclass(slots=True, frozen=True)
class SearchHit:
    """One ranked retrieval result."""

    id: str
    kind: str
    title: str
    snippet: str
    score: float
    seq: int = 0

    @property
    def node_id(self) -> str | None:
        return None if self.kind == "trace" else self.id

    @property
    def trace_id(self) -> str | None:
        return self.id if self.kind == "trace" else None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "snippet": self.snippet, "score": round(self.score, 6), "seq": self.seq}

    def line(self, limit: int = 180) -> str:
        """Compact one-line rendering used by the context renderer."""
        text = truncate(self.snippet or self.title, limit).replace("\n", " ")
        return f"[{self.kind}] {self.title} — {text} (score {self.score:.2f})"


# ---------------------------------------------------------------------------
# tokenization
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Tokenize Latin/CJK text into index terms.

    Latin identifiers are emitted whole and also split into word parts, so
    ``https://app.example.test:8443/api`` matches both the full literal and
    ``app``/``example``/``test``. CJK runs are emitted as overlapping bigrams
    (plus a unigram for a one-character run).
    """
    if not text:
        return []
    tokens: list[str] = []
    for match in _TOKEN.finditer(str(text)):
        chunk = match.group(0).strip(".:/@-")
        if not chunk:
            continue
        if _is_cjk(chunk):
            if len(chunk) == 1:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
            continue
        lowered = chunk.lower()
        tokens.append(lowered)
        for word in _LATIN_WORD.findall(lowered):
            if word and word != lowered:
                tokens.append(word)
    return tokens


def _is_cjk(text: str) -> bool:
    return bool(text) and "\u3400" <= text[0] <= "\u9fff" or (bool(text) and "\uf900" <= text[0] <= "\ufaff")


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Document:
    id: str
    kind: str
    title: str
    text: str
    seq: int = 0


@dataclass(slots=True)
class RetrievalIndex:
    """An in-memory TF-IDF cosine index over immutable document snapshots."""

    documents: list[_Document] = field(default_factory=list)
    _doc_tokens: list[Counter[str]] = field(default_factory=list, init=False)
    _doc_weights: list[dict[str, float]] = field(default_factory=list, init=False)
    _doc_norms: list[float] = field(default_factory=list, init=False)
    _idf: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        count = len(self.documents)
        self._doc_tokens = [Counter(tokenize(doc.text)) for doc in self.documents]
        df: Counter[str] = Counter()
        for tokens in self._doc_tokens:
            df.update(tokens.keys())
        self._idf = {term: math.log((count + 1) / (freq + 1)) + 1.0 for term, freq in df.items()}
        for tokens in self._doc_tokens:
            weights: dict[str, float] = {}
            for term, freq in tokens.items():
                weights[term] = (1.0 + math.log(freq)) * self._idf.get(term, 0.0)
            norm = math.sqrt(sum(value * value for value in weights.values()))
            self._doc_weights.append(weights)
            self._doc_norms.append(norm)

    def __len__(self) -> int:
        return len(self.documents)

    def search(self, query: str, *, limit: int = 8, kinds: Iterable[str] | None = None) -> list[SearchHit]:
        """Return the highest-scoring documents for *query*."""
        if not query or not str(query).strip() or not self.documents:
            return []
        query_tokens = Counter(tokenize(query))
        if not query_tokens:
            return []
        query_weights: dict[str, float] = {}
        for term, freq in query_tokens.items():
            if term not in self._idf:
                continue
            query_weights[term] = (1.0 + math.log(freq)) * self._idf[term]
        query_norm = math.sqrt(sum(value * value for value in query_weights.values()))
        if query_norm <= 0:
            return []
        allowed = set(kinds) if kinds else None
        scored: list[tuple[float, int, _Document]] = []
        for index, doc in enumerate(self.documents):
            if allowed is not None and doc.kind not in allowed:
                continue
            doc_weights = self._doc_weights[index]
            dot = sum(weight * doc_weights.get(term, 0.0) for term, weight in query_weights.items())
            if dot <= 0:
                continue
            score = dot / (query_norm * (self._doc_norms[index] or 1.0))
            if score > 0:
                scored.append((score, index, doc))
        scored.sort(key=lambda item: (-item[0], _KIND_ORDER.get(item[2].kind, 99),
                                      item[2].seq, item[2].id))
        terms = [term for term in query_tokens if term in self._idf]
        hits: list[SearchHit] = []
        for score, _index, doc in scored[: max(1, int(limit))]:
            hits.append(SearchHit(id=doc.id, kind=doc.kind, title=doc.title,
                                  snippet=_snippet(doc.text, terms), score=score, seq=doc.seq))
        return hits


def _snippet(text: str, terms: Sequence[str], limit: int = 220) -> str:
    normalized = _WS.sub(" ", text or "").strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    for term in terms:
        if not term:
            continue
        position = lowered.find(term.lower())
        if position >= 0:
            start = max(0, position - 70)
            end = min(len(normalized), position + limit)
            prefix = "…" if start else ""
            suffix = "…" if end < len(normalized) else ""
            return prefix + normalized[start:end] + suffix
    return truncate(normalized, limit)


# ---------------------------------------------------------------------------
# document assembly + convenience API
# ---------------------------------------------------------------------------


def _normalize_kinds(kinds: Iterable[str] | None) -> tuple[set[str], bool]:
    """Return (node kinds, include traces) for a user-provided kind filter."""
    if kinds is None:
        return set(DEFAULT_NODE_KINDS), True
    wanted = list(kinds) if not isinstance(kinds, str) else [kinds]
    wanted = [str(item).strip().lower() for item in wanted if str(item).strip()]
    if not wanted:
        return set(DEFAULT_NODE_KINDS), True
    node_kinds: set[str] = set()
    include_traces = False
    for kind in wanted:
        if kind in {"all", "any", "nodes", "node", "graph"}:
            node_kinds.update(DEFAULT_NODE_KINDS)
        elif kind in {"trace", "traces", "process", "process_trace"}:
            include_traces = True
        elif kind in NODE_KINDS:
            node_kinds.add(kind)
    if not node_kinds and not include_traces:
        node_kinds.update(DEFAULT_NODE_KINDS)
    return node_kinds, include_traces


def _node_document(node: Node) -> _Document:
    title = node.title or node.kind
    body = node.body or ""
    meta = ""
    if node.meta:
        meta = json_dumps(node.meta, ensure_ascii=False)
    return _Document(id=node.id, kind=node.kind, title=title,
                     text=f"{title}\n{body}\n{meta}", seq=node.seq)


def _trace_document(trace: Trace) -> _Document:
    title = f"{trace.agent or 'worker'} {trace.tool_name or trace.kind}".strip()
    parts = [trace.content or ""]
    if trace.tool_name:
        parts.append(trace.tool_name)
    if trace.tool_args:
        parts.append(json_dumps(trace.tool_args, ensure_ascii=False))
    if trace.tool_result:
        parts.append(truncate(json_dumps(trace.tool_result, ensure_ascii=False), 2000))
    return _Document(id=trace.id, kind="trace", title=title,
                     text="\n".join(part for part in parts if part), seq=trace.seq)


def build_index(store: Store, project_id: str, *, kinds: Iterable[str] | None = None,
                max_nodes: int = 4000, max_traces: int = 500) -> RetrievalIndex:
    """Snapshot the store into a retrieval index.

    The index is intentionally rebuilt per retrieval call. Graph-sized
    workloads make that cheap, and freshness matters more than micro-caching:
    a worker's search must see facts persisted seconds earlier by a sibling.
    """
    node_kinds, include_traces = _normalize_kinds(kinds)
    documents: list[_Document] = []
    for kind in sorted(node_kinds):
        for node in store.list_nodes(project_id, kind=kind, limit=max_nodes):
            documents.append(_node_document(node))
    if include_traces:
        traces = store.list_traces(project_id, limit=max_traces, newest_first=True)
        # Oldest-first is the natural reading order; scoring does not depend on it.
        traces.sort(key=lambda item: (item.created_at, item.task_id, item.seq, item.id))
        documents.extend(_trace_document(trace) for trace in traces)
    return RetrievalIndex(documents=documents)


def search(store: Store, project_id: str, query: str, *, kinds: Iterable[str] | None = None,
           limit: int = 8, max_nodes: int = 4000, max_traces: int = 500) -> list[SearchHit]:
    """Search graph nodes and/or traces for *query*.

    ``kinds`` accepts node kinds plus the pseudo-kinds ``"nodes"`` (all node
    kinds) and ``"trace"`` (process traces). ``None`` searches everything.
    """
    if not query or not str(query).strip():
        return []
    index = build_index(store, project_id, kinds=kinds, max_nodes=max_nodes, max_traces=max_traces)
    node_kinds, include_traces = _normalize_kinds(kinds)
    allowed = set(node_kinds)
    if include_traces:
        allowed.add("trace")
    return index.search(query, limit=limit, kinds=allowed)


__all__ = [
    "DEFAULT_NODE_KINDS", "RetrievalIndex", "SearchHit", "build_index", "search", "tokenize",
]
