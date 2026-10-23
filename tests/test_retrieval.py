"""Tests for zero-dependency lexical retrieval (CJK bigrams + TF-IDF cosine)."""

from __future__ import annotations

import unittest

from aria_x.memory import retrieval
from aria_x.memory.retrieval import RetrievalIndex, build_index, search, tokenize
from aria_x.models import Node, Project, Trace
from aria_x.store import Store


class TokenizerTests(unittest.TestCase):
    def test_cjk_bigrams_and_latin_words(self) -> None:
        tokens = tokenize("扫描数据库 Apache server")
        self.assertIn("数据", tokens)
        self.assertIn("据库", tokens)
        self.assertIn("apache", tokens)
        self.assertIn("server", tokens)

    def test_single_cjk_character(self) -> None:
        self.assertIn("云", tokenize("云"))

    def test_identifier_is_kept_whole_and_split(self) -> None:
        tokens = tokenize("https://app.example.test:8443/api")
        self.assertIn("https://app.example.test:8443/api", tokens)
        self.assertIn("app", tokens)
        self.assertIn("8443", tokens)

    def test_empty(self) -> None:
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None), [])  # type: ignore[arg-type]


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.project = Project.new("retrieval-test", "Map a mock target")
        self.store.create_project(self.project)

    # -- helpers --------------------------------------------------------

    def fact(self, title: str, body: str = "") -> Node:
        node = Node.new(self.project.id, "fact", title, body, status="done")
        self.store.add_node(node)
        return node

    def trace(self, content: str, *, task_id: str = "task-1") -> Trace:
        trace = Trace.new(self.project.id, "run-1", task_id, "worker", 0, "tool_result", content)
        self.store.add_trace(trace)
        return trace

    # -- tests ----------------------------------------------------------

    def test_ranking_prefers_more_relevant_node(self) -> None:
        apache = self.fact("Apache web server", "Apache/2.4 on port 80 apache apache")
        self.fact("SSH daemon", "OpenSSH 8.9 on port 22")
        hits = search(self.store, self.project.id, "apache", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].id, apache.id)
        self.assertGreater(hits[0].score, 0.0)

    def test_cjk_query_matches_chinese_fact(self) -> None:
        cjk = self.fact("数据库凭据泄露", "MySQL 数据库监听 3306，疑似弱口令")
        self.fact("Web server", "nginx on port 443")
        hits = search(self.store, self.project.id, "数据库", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].id, cjk.id)

    def test_kind_filter_includes_and_excludes_traces(self) -> None:
        self.fact("Target host", "mock.test resolves to 127.0.0.1")
        trace = self.trace("curl http://mock.test -> 200 OK")
        trace_hits = search(self.store, self.project.id, "curl", kinds=["trace"], limit=5)
        self.assertEqual([hit.id for hit in trace_hits], [trace.id])
        fact_hits = search(self.store, self.project.id, "curl", kinds=["fact"], limit=5)
        self.assertEqual(fact_hits, [])
        all_hits = search(self.store, self.project.id, "curl", limit=5)
        self.assertEqual(all_hits[0].kind, "trace")

    def test_limit_and_determinism(self) -> None:
        for index in range(6):
            self.fact(f"Shared marker {index}", "shared marker shared marker")
        first = search(self.store, self.project.id, "shared marker", limit=3)
        second = search(self.store, self.project.id, "shared marker", limit=3)
        self.assertEqual(len(first), 3)
        self.assertEqual([(h.id, round(h.score, 8)) for h in first],
                         [(h.id, round(h.score, 8)) for h in second])

    def test_empty_query_and_no_match_return_empty(self) -> None:
        self.fact("Only fact", "nothing relevant")
        self.assertEqual(search(self.store, self.project.id, ""), [])
        self.assertEqual(search(self.store, self.project.id, "   "), [])
        self.assertEqual(search(self.store, self.project.id, "zzz_unseen_term"), [])

    def test_index_counts(self) -> None:
        self.fact("A", "alpha")
        self.fact("B", "beta")
        self.trace("gamma")
        index = build_index(self.store, self.project.id)
        self.assertIsInstance(index, RetrievalIndex)
        self.assertEqual(len(index), 3)
        only_nodes = build_index(self.store, self.project.id, kinds=["nodes"])
        self.assertEqual(len(only_nodes), 2)

    def test_hit_helpers_and_serialisation(self) -> None:
        self.fact("Serializable fact", "body")
        hit = search(self.store, self.project.id, "serializable", limit=1)[0]
        self.assertEqual(hit.node_id, hit.id)
        self.assertIsNone(hit.trace_id)
        self.assertIn("Serializable", hit.line())
        self.assertIn("score", hit.to_dict())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
