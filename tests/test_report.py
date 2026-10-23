"""Report renderer tests: structure, severity order, HTML escaping and file writing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aria_x.models import Evidence, Node, Project, Todo, Trace
from aria_x.report.render import markdown_to_html, render_html, render_markdown, write_report
from aria_x.store import Store
from aria_x.util import new_id


def build_export() -> dict:
    store = Store(":memory:")
    try:
        project = Project.new("ARIA-X report test", "Assess the demo application",
                              scope={"targets": ["http://127.0.0.1:8080"], "deny": []})
        store.create_project(project)
        intent = Node.new(project.id, "intent", "Probe the login form", "Try default credentials",
                          status="done")
        store.add_node(intent)
        fact = Node.new(project.id, "fact", "Login form present at /login",
                        "GET /login returned HTTP 200", status="done")
        store.add_node(fact)
        store.add_edge(project.id, intent.id, fact.id, "yields")
        endpoint = store.upsert_asset(project.id, "endpoint", "https://app.example.test/login")
        host = store.upsert_asset(project.id, "host", "app.example.test", parent_id=endpoint.id)

        high = store.add_finding(project.id, "Reflected XSS <script>alert(1)</script>",
                                 "The q parameter is echoed unencoded. | pipe in table",
                                 severity="high", cvss=7.4, confidence="high",
                                 asset_ids=[endpoint.id], intent_id=intent.id)
        critical = store.add_finding(project.id, "SQL injection in login",
                                     "The id parameter is injectable (UNION based).",
                                     severity="critical", cvss=9.8, confidence="high",
                                     asset_ids=[host.id], intent_id=intent.id)
        store.add_evidence(Evidence(id=new_id("ev"), project_id=project.id,
                                    finding_id=critical.id, kind="http",
                                    path="evidence/http_1.json", sha256="a" * 64,
                                    excerpt="HTTP 200 with SQL syntax error"))
        store.add_evidence(Evidence(id=new_id("ev"), project_id=project.id,
                                    finding_id=high.id, kind="http",
                                    path="evidence/http_2.json", sha256="b" * 64,
                                    excerpt="<b>reflected</b> payload"))
        store.upsert_asset(project.id, "artifact", "weird|value")
        store.add_todo(Todo.new(project.id, 1, "Enumerate the login endpoint",
                                detail="done in run", status="done"))
        store.add_trace(Trace.new(project.id, "r1", "t1", "worker", 0, "tool_call",
                                  "curl -s http://127.0.0.1:8080/login", tool_name="terminal_exec"))
        store.audit(project.id, "tool:worker", "http_request", "denied",
                    target="http://evil.test/", detail={"reason": "target is not in scope"})
        return store.export_project(project.id)
    finally:
        store.close()


META = {"run_id": "r_report", "generated_at": "2026-01-02T03:04:05Z",
        "summary": "The engagement confirmed two exploitable issues on the demo application.",
        "title": "ARIA-X Security Assessment — Demo Application", "version": "v0.1"}


class ReportRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.export = build_export()
        self.markdown = render_markdown(self.export, meta=META)
        self.html = render_html(self.markdown, self.export, META)

    def test_markdown_structure(self):
        for heading in ("# ARIA-X Security Assessment", "## 1. Executive Summary",
                        "## 2. Engagement Scope & Authorization", "## 3. Methodology",
                        "## 4. Findings", "## 5. Attack Path Narrative",
                        "## 6. Asset Inventory", "## 7. Coverage & Limitations", "## 8. Appendix"):
            self.assertIn(heading, self.markdown)
        self.assertIn("The engagement confirmed two exploitable issues", self.markdown)

    def test_findings_are_severity_ordered(self):
        critical = self.markdown.index("SQL injection in login")
        high = self.markdown.index("Reflected XSS")
        self.assertLess(critical, high)
        self.assertIn("CRITICAL", self.markdown)
        self.assertIn("HIGH", self.markdown)

    def test_evidence_and_assets_present(self):
        self.assertIn("evidence/http_1.json", self.markdown)
        self.assertIn("https://app.example.test/login", self.markdown)
        self.assertIn("app.example.test", self.markdown)
        self.assertIn("aaaaaaaaaaaaaaaa", self.markdown)
        self.assertIn("weird\\|value", self.markdown)  # table cells escape pipes

    def test_scope_and_coverage_sections(self):
        self.assertIn("http://127.0.0.1:8080", self.markdown)
        self.assertIn("Out-of-scope or policy-refused actions: 1", self.markdown)
        self.assertIn("Intents explored: 1", self.markdown)

    def test_attack_path_narrative_uses_edges(self):
        self.assertIn("Probe the login form", self.markdown)
        self.assertIn("Login form present at /login", self.markdown)

    def test_html_is_self_contained_and_dark(self):
        self.assertTrue(self.html.startswith("<!DOCTYPE html>"))
        self.assertIn('<meta charset="utf-8">', self.html)
        self.assertIn("<style>", self.html)
        self.assertIn("background:#0b0f14", self.html)
        self.assertNotIn("<script", self.html.lower())
        lowered = self.html.lower()
        for pattern in ("cdn.", "unpkg", "jsdelivr", "googleapis", "fonts."):
            self.assertNotIn(pattern, lowered)
        self.assertTrue(self.html.rstrip().endswith("</html>"))
        self.assertIn("</body>", self.html)

    def test_html_escapes_untrusted_content(self):
        self.assertNotIn("<script>alert(1)</script>", self.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.html)
        self.assertNotIn("<b>reflected</b>", self.html)
        self.assertIn("&lt;b&gt;reflected&lt;/b&gt;", self.html)
        self.assertIn("pipe in table", self.html)  # paragraph text, not a table cell

    def test_html_contains_stat_cards(self):
        self.assertIn('class="stat critical"', self.html)
        self.assertIn('class="stat high"', self.html)
        self.assertIn("facts", self.html)

    def test_markdown_to_html_handles_tables_lists_code(self):
        sample = ("# Title\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
                  "- one\n- two\n\n```sh\necho hi\n```\n\n**bold** and `code`\n")
        converted = markdown_to_html(sample)
        self.assertIn("<table>", converted)
        self.assertIn("<th>A</th>", converted)
        self.assertIn("<ul>", converted)
        self.assertIn("<pre><code class=\"language-sh\">", converted)
        self.assertIn("<strong>bold</strong>", converted)
        self.assertIn("<code>code</code>", converted)

    def test_unsafe_link_scheme_is_not_emitted(self):
        converted = markdown_to_html("click [me](javascript:alert(1)) now")
        self.assertNotIn("javascript:", converted)
        self.assertIn("me", converted)


class ReportWriteTests(unittest.TestCase):
    def test_write_report_creates_all_artifacts(self):
        export = build_export()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "r_report"
            paths = write_report(run_dir, export, META)
            self.assertEqual(set(paths), {"markdown", "html", "json"})
            for key, value in paths.items():
                self.assertTrue(Path(value).is_file(), key)
            self.assertEqual(Path(paths["markdown"]).name, "report.md")
            self.assertEqual(Path(paths["html"]).name, "report.html")
            self.assertEqual(Path(paths["json"]).name, "findings.json")
            payload = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["counts"]["total_findings"], 2)
            self.assertEqual(payload["findings"][0]["severity"], "critical")
            self.assertEqual(payload["findings"][0]["evidence"][0]["kind"], "http")
            html_text = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", html_text)
            markdown_text = Path(paths["markdown"]).read_text(encoding="utf-8")
            self.assertIn("## 4. Findings", markdown_text)

    def test_empty_export_still_renders(self):
        project = Project.new("Empty", "nothing found")
        store = Store(":memory:")
        try:
            store.create_project(project)
            export = store.export_project(project.id)
        finally:
            store.close()
        markdown = render_markdown(export, meta={"run_id": "r0"})
        self.assertIn("_No findings were confirmed during this engagement._", markdown)
        html_text = render_html(markdown, export, {"run_id": "r0"})
        self.assertIn("<!DOCTYPE html>", html_text)


if __name__ == "__main__":
    unittest.main()
