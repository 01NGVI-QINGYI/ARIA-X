"""Engagement report rendering.

The report is generated from :meth:`Store.export_project` only — every statement
is derived from persisted nodes, assets, evidence, traces and audit rows. That
keeps the narrative honest (no invented findings) and makes the renderer fully
deterministic, which matters when a report is an audit artifact.

Two formats share one structure:

* **Markdown** for review, diffs and archiving;
* **self-contained HTML** (inline CSS, no CDN, no JavaScript) for delivery. All
  dynamic values are HTML-escaped, so a finding title containing ``<script>``
  cannot execute when the report is opened.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import __version__
from ..models import SEVERITIES, SEVERITY_RANK
from ..util import json_dumps, truncate
from .i18n import get_report_strings, resolve_report_language

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SAFE_URL = re.compile(r"^(https?://|mailto:|#)")


# ---------------------------------------------------------------------------
# data helpers
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _nodes(export: Mapping[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    nodes = [n for n in _as_list(export.get("nodes")) if isinstance(n, dict)]
    if kind:
        nodes = [n for n in nodes if n.get("kind") == kind]
    return nodes


def _findings(export: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings = [n for n in _nodes(export, "finding") if isinstance(n, dict)]
    findings.sort(key=lambda n: (SEVERITY_RANK.get(str((n.get("meta") or {}).get("severity", "info")).lower(), 99),
                                 -int(n.get("seq") or 0)))
    return findings


def _severity(finding: Mapping[str, Any]) -> str:
    value = str((finding.get("meta") or {}).get("severity", "info")).lower()
    return value if value in SEVERITIES else "info"


def _assets_by_id(export: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(a.get("id")): a for a in _as_list(export.get("assets")) if isinstance(a, dict)}


def _nodes_by_id(export: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n.get("id")): n for n in _nodes(export)}


def _evidence_for(export: Mapping[str, Any], finding_id: str) -> list[dict[str, Any]]:
    evidence = export.get("evidence") or {}
    if not isinstance(evidence, Mapping):
        return []
    return [e for e in _as_list(evidence.get(finding_id)) if isinstance(e, dict)]


def _asset_links_for(export: Mapping[str, Any], finding_id: str) -> list[dict[str, Any]]:
    """Assets are linked through anchors; memoize the reverse index per call."""
    anchor_map = export.get("_anchors")  # optional precomputed map
    if isinstance(anchor_map, Mapping) and finding_id in anchor_map:
        return [a for a in _as_list(anchor_map[finding_id]) if isinstance(a, dict)]
    return []


def _escape(text: Any) -> str:
    return _html.escape(str(text if text is not None else ""), quote=True)


def _md_cell(text: Any, limit: int = 300) -> str:
    value = truncate(str(text if text is not None else ""), limit)
    value = value.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return value.strip()


def _plain(text: Any, limit: int = 600) -> str:
    return truncate(str(text if text is not None else "").replace("\r", ""), limit).strip()


def _counts(export: Mapping[str, Any]) -> dict[str, Any]:
    nodes = _nodes(export)
    by_kind: dict[str, int] = {}
    for node in nodes:
        kind = str(node.get("kind") or "note")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    by_sev: dict[str, int] = {severity: 0 for severity in SEVERITIES}
    for finding in _findings(export):
        by_sev[_severity(finding)] = by_sev.get(_severity(finding), 0) + 1
    assets = [a for a in _as_list(export.get("assets")) if isinstance(a, dict)]
    by_asset: dict[str, int] = {}
    for asset in assets:
        kind = str(asset.get("kind") or "artifact")
        by_asset[kind] = by_asset.get(kind, 0) + 1
    return {"nodes": by_kind, "findings": by_sev, "assets": by_asset,
            "total_findings": sum(by_sev.values()), "total_assets": len(assets),
            "traces": len(_as_list(export.get("traces"))),
            "todos": len(_as_list(export.get("todos"))),
            "edges": len(_as_list(export.get("edges")))}


def _meta_text(meta: Mapping[str, Any], key: str, default: str = "") -> str:
    value = meta.get(key, default)
    return str(value) if value is not None else default


def _generated_at(meta: Mapping[str, Any]) -> str:
    return _meta_text(meta, "generated_at") or datetime.now(timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _project(export: Mapping[str, Any]) -> dict[str, Any]:
    project = export.get("project")
    return dict(project) if isinstance(project, Mapping) else {}


def _scope_lines(
    export: Mapping[str, Any],
    meta: Mapping[str, Any],
    strings: Mapping[str, str],
) -> list[str]:
    project = _project(export)
    scope = project.get("scope") if isinstance(project.get("scope"), Mapping) else {}
    targets = _as_list(scope.get("targets")) or _as_list(meta.get("targets"))
    deny = _as_list(scope.get("deny"))
    lines: list[str] = []
    if targets:
        lines.append(strings["scope_authorized_targets"])
        lines.extend(f"- `{target}`" for target in targets)
    else:
        lines.append(strings["scope_no_targets"])
    if deny:
        lines.append("")
        lines.append(strings["scope_excluded_targets"])
        lines.extend(f"- `{target}`" for target in deny)
    notes = _meta_text(meta, "scope_notes") or _meta_text(scope, "notes")
    if notes:
        lines.append("")
        lines.append(notes)
    lines.append("")
    lines.append(strings["scope_authorization"])
    return lines


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def render_markdown(
    export: Mapping[str, Any],
    *,
    meta: Mapping[str, Any] | None = None,
    language: str | None = None,
) -> str:
    """Render a professional Markdown report from a project export."""
    meta = dict(meta or {})
    strings = get_report_strings(language, meta)
    project = _project(export)
    counts = _counts(export)
    findings = _findings(export)
    assets = [a for a in _as_list(export.get("assets")) if isinstance(a, dict)]
    nodes_by_id = _nodes_by_id(export)
    title = _meta_text(meta, "title") or strings["default_title"].format(
        project=project.get("name", strings["default_project"])
    )
    out: list[str] = []

    out.append(f"# {title}")
    out.append("")
    out.append(f"| {strings['meta_col_field']} | {strings['meta_col_value']} |")
    out.append("| --- | --- |")
    for label, value in (
        (strings["meta_engagement"], project.get("name", "—")),
        (strings["meta_project_id"], project.get("id", "—")),
        (strings["meta_run_id"], _meta_text(meta, "run_id", "—")),
        (strings["meta_generated"], _generated_at(meta)),
        (strings["meta_operator"], _meta_text(meta, "operator", strings["default_operator"])),
        (strings["meta_classification"], _meta_text(meta, "classification", strings["default_classification"])),
    ):
        out.append(f"| {_md_cell(label)} | {_md_cell(value)} |")
    out.append("")

    # 1. Executive summary -------------------------------------------------
    out.append(strings["sec_1"])
    out.append("")
    summary = _meta_text(meta, "summary")
    if summary:
        out.append(summary.strip())
        out.append("")
    severity_summary = ", ".join(f"{severity} {counts['findings'].get(severity, 0)}" for severity in SEVERITIES)
    out.append(strings["exec_summary_body"].format(
        goal=_plain(project.get("goal", "—"), 600),
        facts=counts["nodes"].get("fact", 0),
        intents=counts["nodes"].get("intent", 0),
        total_findings=counts["total_findings"],
        total_assets=counts["total_assets"],
        severity_summary=severity_summary,
    ))
    out.append("")
    if findings:
        top = findings[0]
        out.append(strings["exec_top_finding"].format(
            title=_plain(top.get("title"), 300),
            severity=_severity(top).upper(),
        ))
    else:
        out.append(strings["exec_no_findings"])
    out.append("")

    # 2. Scope -------------------------------------------------------------
    out.append(strings["sec_2"])
    out.append("")
    out.extend(_scope_lines(export, meta, strings))
    out.append("")

    # 3. Methodology -------------------------------------------------------
    out.append(strings["sec_3"])
    out.append("")
    out.append(strings["methodology_body"])
    out.append("")
    footprint = strings["methodology_footprint"].format(
        traces=counts["traces"],
        edges=counts["edges"],
        todos=counts["todos"],
    )
    extra_notes = _meta_text(meta, "methodology_notes", "").strip()
    out.append(f"{footprint} {extra_notes}" if extra_notes else f"{footprint} ")
    out.append("")

    # 4. Findings ----------------------------------------------------------
    out.append(strings["sec_4"])
    out.append("")
    if not findings:
        out.append(strings["findings_none"])
        out.append("")
    for index, finding in enumerate(findings, start=1):
        severity = _severity(finding)
        meta_data = finding.get("meta") or {}
        out.append(f"### 4.{index} {_plain(finding.get('title'), 300)}")
        out.append("")
        out.append(f"| {strings['finding_col_attr']} | {strings['finding_col_val']} |")
        out.append("| --- | --- |")
        out.append(f"| {strings['finding_attr_severity']} | **{severity.upper()}** |")
        if meta_data.get("cvss") is not None:
            out.append(f"| {strings['finding_attr_cvss']} | {_md_cell(meta_data.get('cvss'))} |")
        out.append(f"| {strings['finding_attr_confidence']} | {_md_cell(meta_data.get('confidence', 'medium'))} |")
        out.append(f"| {strings['finding_attr_id']} | `{_md_cell(finding.get('id'), 60)}` |")
        if finding.get("parent_id"):
            parent = nodes_by_id.get(str(finding.get("parent_id")))
            label = _plain(parent.get("title"), 200) if parent else str(finding.get("parent_id"))
            out.append(f"| {strings['finding_attr_intent']} | {_md_cell(label)} |")
        out.append("")
        body = _plain(finding.get("body"), 8000)
        out.append(body if body else strings["finding_no_detail"])
        out.append("")
        evidence = _evidence_for(export, str(finding.get("id")))
        if evidence:
            out.append(strings["finding_evidence_heading"])
            out.append("")
            out.append(
                f"| {strings['ev_col_kind']} | {strings['ev_col_ref']} "
                f"| {strings['ev_col_sha256']} | {strings['ev_col_excerpt']} |"
            )
            out.append("| --- | --- | --- | --- |")
            for item in evidence:
                digest = str(item.get("sha256") or "")
                out.append(f"| {_md_cell(item.get('kind'))} | {_md_cell(item.get('path') or item.get('url'))} "
                           f"| `{_md_cell(digest[:16] if digest else '—', 20)}` "
                           f"| {_md_cell(item.get('excerpt'), 240)} |")
            out.append("")

    # 5. Attack path -------------------------------------------------------
    out.append(strings["sec_5"])
    out.append("")
    out.extend(_attack_path(export, nodes_by_id, strings))
    out.append("")
    graph = attack_graph_mermaid(export)
    if graph:
        out.append(f"**{strings['graph_title']}**")
        out.append("")
        out.append(graph)
        out.append("")

    # 6. Assets ------------------------------------------------------------
    out.append(strings["sec_6"])
    out.append("")
    if assets:
        out.append(
            f"| {strings['asset_col_value']} | {strings['asset_col_kind']} "
            f"| {strings['asset_col_parent']} | {strings['asset_col_first_seen']} |"
        )
        out.append("| --- | --- | --- | --- |")
        for asset in assets:
            parent = nodes_by_id.get(str(asset.get("parent_id")))
            parent_label = _plain(parent.get("value") if parent else "", 80) or "—"
            out.append(f"| `{_md_cell(asset.get('value'), 200)}` | {_md_cell(asset.get('kind'))} "
                       f"| {_md_cell(parent_label)} | {_md_cell(asset.get('first_seen'))} |")
    else:
        out.append(strings["assets_none"])
    out.append("")

    # 7. Coverage ----------------------------------------------------------
    out.append(strings["sec_7"])
    out.append("")
    open_intents = [n for n in _nodes(export, "intent") if n.get("status") in {"open", "active"}]
    audit = [a for a in _as_list(export.get("audit")) if isinstance(a, dict)]
    denied = [a for a in audit if str(a.get("decision")).lower() == "denied"]
    out.append(strings["cov_facts"].format(count=counts["nodes"].get("fact", 0)))
    out.append(strings["cov_intents"].format(count=counts["nodes"].get("intent", 0), open_count=len(open_intents)))
    out.append(strings["cov_assets"].format(count=counts["total_assets"]))
    severity_breakdown = ", ".join(
        f"{sev}={counts['findings'].get(sev, 0)}" for sev in SEVERITIES)
    out.append(strings["cov_findings"].format(count=counts["total_findings"], breakdown=severity_breakdown))
    out.append(strings["cov_denied"].format(count=len(denied)))
    out.append("")
    out.append(strings["cov_limitations"])
    out.append("")

    # 8. Appendix ----------------------------------------------------------
    out.append(strings["sec_8"])
    out.append("")
    out.append(strings["sec_8_1"])
    out.append("")
    all_evidence: list[dict[str, Any]] = []
    for finding in findings:
        for item in _evidence_for(export, str(finding.get("id"))):
            all_evidence.append({"finding": finding.get("title"), **item})
    if all_evidence:
        out.append(
            f"| {strings['app_ev_col_finding']} | {strings['app_ev_col_kind']} "
            f"| {strings['app_ev_col_ref']} | {strings['app_ev_col_sha256']} |"
        )
        out.append("| --- | --- | --- | --- |")
        for item in all_evidence[:200]:
            digest = str(item.get("sha256") or "")
            out.append(f"| {_md_cell(item.get('finding'), 160)} | {_md_cell(item.get('kind'))} "
                       f"| {_md_cell(item.get('path') or item.get('url'))} "
                       f"| `{_md_cell(digest[:16] if digest else '—', 20)}` |")
    else:
        out.append(strings["app_ev_none"])
    out.append("")

    out.append(strings["sec_8_2"])
    out.append("")
    todos = [t for t in _as_list(export.get("todos")) if isinstance(t, dict)]
    if todos:
        out.append(
            f"| {strings['app_todo_col_pos']} | {strings['app_todo_col_status']} "
            f"| {strings['app_todo_col_title']} |"
        )
        out.append("| --- | --- | --- |")
        for todo in todos[:100]:
            out.append(f"| {_md_cell(todo.get('position'))} | {_md_cell(todo.get('status'))} "
                       f"| {_md_cell(todo.get('title'), 240)} |")
    else:
        out.append(strings["app_todo_none"])
    out.append("")

    out.append(strings["sec_8_3"])
    out.append("")
    traces = [t for t in _as_list(export.get("traces")) if isinstance(t, dict)]
    if traces:
        out.append(
            f"| {strings['app_trace_col_task']} | {strings['app_trace_col_agent']} "
            f"| {strings['app_trace_col_kind']} | {strings['app_trace_col_tool']} "
            f"| {strings['app_trace_col_content']} |"
        )
        out.append("| --- | --- | --- | --- | --- |")
        for trace in traces[-30:]:
            out.append(f"| {_md_cell(trace.get('task_id'), 40)} | {_md_cell(trace.get('agent'))} "
                       f"| {_md_cell(trace.get('kind'))} | {_md_cell(trace.get('tool_name'))} "
                       f"| {_md_cell(trace.get('content'), 200)} |")
    else:
        out.append(strings["app_trace_none"])
    out.append("")

    out.append(strings["sec_8_4"])
    out.append("")
    if audit:
        out.append(
            f"| {strings['app_audit_col_time']} | {strings['app_audit_col_actor']} "
            f"| {strings['app_audit_col_action']} | {strings['app_audit_col_decision']} "
            f"| {strings['app_audit_col_target']} |"
        )
        out.append("| --- | --- | --- | --- | --- |")
        for entry in list(reversed(audit))[-40:]:
            out.append(f"| {_md_cell(entry.get('ts'))} | {_md_cell(entry.get('actor'))} "
                       f"| {_md_cell(entry.get('action'))} | {_md_cell(entry.get('decision'))} "
                       f"| {_md_cell(entry.get('target'), 200)} |")
    else:
        out.append(strings["app_audit_none"])
    out.append("")
    out.append("---")
    out.append("")
    raw_ver = _meta_text(meta, "version", __version__)
    ver_label = raw_ver if raw_ver.startswith("v") else f"v{raw_ver}"
    out.append(strings["md_footer"].format(version=ver_label, generated_at=_generated_at(meta)))
    out.append("")
    return "\n".join(out)


def _attack_path(
    export: Mapping[str, Any],
    nodes_by_id: Mapping[str, dict[str, Any]],
    strings: Mapping[str, str],
) -> list[str]:
    edges = [e for e in _as_list(export.get("edges")) if isinstance(e, dict)]
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(str(edge.get("src_id")), []).append(edge)
    findings_by_intent: dict[str, list[dict[str, Any]]] = {}
    for finding in _findings(export):
        parent = str(finding.get("parent_id") or "")
        if parent:
            findings_by_intent.setdefault(parent, []).append(finding)
    intents = [n for n in _nodes(export, "intent")]
    intents.sort(key=lambda n: int(n.get("seq") or 0))
    if not intents and not edges:
        return [strings["attack_none"]]
    lines: list[str] = []
    if intents:
        lines.append(strings["attack_intro"])
        lines.append("")
        for index, intent in enumerate(intents, start=1):
            intent_id = str(intent.get("id"))
            produced = [e for e in outgoing.get(intent_id, []) if e.get("rel") in {"yields", "proves", "spawns"}]
            targets = [nodes_by_id.get(str(e.get("dst_id")), {}) for e in produced]
            titles = [_plain(item.get("title"), 140) for item in targets if item.get("title")]
            finding_titles = [_plain(f.get("title"), 160) for f in findings_by_intent.get(intent_id, [])]
            line = strings["attack_intent_line"].format(
                index=index,
                title=_plain(intent.get("title"), 240),
                status=intent.get("status", "open"),
            )
            lines.append(line)
            if titles:
                lines.append(strings["attack_observed"].format(
                    items="; ".join(titles[:5]) + (" …" if len(titles) > 5 else "")
                ))
            if finding_titles:
                lines.append(strings["attack_findings"].format(items="; ".join(finding_titles[:5])))
            if not produced and not finding_titles:
                lines.append(strings["attack_no_downstream"])
        lines.append("")
    # Derived-from / contradicts relationships between facts and findings.
    interesting = [e for e in edges if e.get("rel") in {"derived_from", "supports", "contradicts"}]
    if interesting:
        lines.append(strings["attack_cross_links"])
        lines.append("")
        for edge in interesting[:20]:
            src = nodes_by_id.get(str(edge.get("src_id")), {})
            dst = nodes_by_id.get(str(edge.get("dst_id")), {})
            lines.append(f"- `{_plain(src.get('title'), 160)}` —{edge.get('rel')}→ "
                         f"`{_plain(dst.get('title'), 160)}`")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# minimal, safe markdown -> HTML
# ---------------------------------------------------------------------------


def _inline(text: str) -> str:
    """Escape HTML then apply a tiny, safe inline Markdown subset."""
    pieces: list[str] = []
    last = 0
    pattern = re.compile(r"(`[^`]+`|\*\*.+?\*\*|\[[^\]]+\]\([^)\s]+\))")
    for match in pattern.finditer(text):
        pieces.append(_escape(text[last:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            pieces.append(f"<code>{_escape(token[1:-1])}</code>")
        elif token.startswith("**"):
            pieces.append(f"<strong>{_escape(token[2:-2])}</strong>")
        else:
            label, _, url = token.partition("](")
            label = label[1:]
            url = url[:-1]
            if _SAFE_URL.match(url):
                pieces.append(f'<a href="{_escape(url)}">{_escape(label)}</a>')
            else:
                pieces.append(_escape(label))
        last = match.end()
    pieces.append(_escape(text[last:]))
    return "".join(pieces)


def _split_table_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def markdown_to_html(markdown: str, *, mermaid_svg: str = "") -> str:
    """Convert the report's Markdown subset to escaped HTML.

    When *mermaid_svg* is provided, ``mermaid`` fences are replaced by that
    inline SVG so the HTML report needs no JavaScript or CDN.
    """
    lines = str(markdown or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        fence = re.match(r"^```(\w*)\s*$", stripped)
        if fence:
            flush_paragraph()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            if fence.group(1) == "mermaid" and mermaid_svg:
                out.append(f'<figure class="attack-graph">{mermaid_svg}</figure>')
                continue
            language = f' class="language-{_escape(fence.group(1))}"' if fence.group(1) else ""
            out.append(f"<pre><code{language}>{_escape(chr(10).join(code))}</code></pre>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_paragraph()
            out.append("<hr>")
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and \
                re.match(r"^\|?\s*:?-{2,}", lines[index + 1].strip()):
            flush_paragraph()
            header = _split_table_row(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_table_row(lines[index]))
                index += 1
            out.append('<div class="table-wrap"><table><thead><tr>'
                       + "".join(f"<th>{_inline(cell)}</th>" for cell in header)
                       + "</tr></thead><tbody>")
            for row in rows:
                cells = row + [""] * (len(header) - len(row))
                out.append("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in cells) + "</tr>")
            out.append("</tbody></table></div>")
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        ordered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if bullet or ordered:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            out.append(f"<{tag}>")
            while index < len(lines):
                item_line = lines[index].strip()
                item = re.match(r"^[-*]\s+(.*)$", item_line)
                num = re.match(r"^\d+[.)]\s+(.*)$", item_line)
                nested = item_line.startswith("  ") and (item or num)
                if item:
                    out.append(f"<li>{_inline(item.group(1))}</li>")
                elif num:
                    out.append(f"<li>{_inline(num.group(1))}</li>")
                elif item_line.startswith("  ") and item_line.strip().startswith("-"):
                    out.append(f"<li>{_inline(item_line.lstrip('- ').strip())}</li>")
                else:
                    break
                index += 1
                _ = nested
            out.append(f"</{tag}>")
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            out.append("<blockquote>" + _inline(" ".join(quote)) + "</blockquote>")
            continue
        paragraph.append(stripped)
        index += 1
    flush_paragraph()
    return "\n".join(out)


_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:#0b0f14;color:#dbe4ee;font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,"PingFang SC","Hiragino Sans","Yu Gothic UI","Noto Sans CJK SC","Noto Sans JP","Microsoft YaHei",sans-serif;}
a{color:#6cb6ff;text-decoration:none}
a:hover{text-decoration:underline}
.hero{background:linear-gradient(135deg,#111a24 0%,#0d141d 55%,#101d2b 100%);border-bottom:1px solid #1f2c3a;padding:42px 28px 30px}
.hero .inner{max-width:1040px;margin:0 auto}
.eyebrow{font-size:11px;letter-spacing:.24em;text-transform:uppercase;color:#5f8fb4;margin-bottom:10px}
.hero h1{font-size:30px;line-height:1.25;margin:0 0 12px;color:#f2f7fc;font-weight:650}
.hero .meta{color:#8fa3b8;font-size:13px;margin:0}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin-top:24px}
.stat{background:#101924;border:1px solid #1e2c3b;border-radius:10px;padding:10px 16px;min-width:108px}
.stat .value{font-size:22px;font-weight:650;color:#eef5fb;line-height:1.1}
.stat .label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7f93a8;margin-top:4px}
.stat.critical .value{color:#ff6b6b}.stat.high .value{color:#ff9f43}.stat.medium .value{color:#ffd166}
.stat.low .value{color:#69d2a0}.stat.info .value{color:#8fa3b8}
main{max-width:1040px;margin:0 auto;padding:34px 28px 64px}
h1,h2,h3,h4{color:#f0f6fc;line-height:1.3;font-weight:640}
main h1{font-size:26px;margin-top:34px}
main h2{font-size:21px;margin-top:40px;padding-top:18px;border-top:1px solid #1b2836}
main h3{font-size:16.5px;margin-top:26px}
p{margin:12px 0}
code{background:#121c27;border:1px solid #223142;border-radius:4px;padding:1px 5px;font-size:13px;color:#a9d1f5;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{background:#0e1620;border:1px solid #1e2c3b;border-radius:8px;padding:14px 16px;overflow:auto}
pre code{background:none;border:0;padding:0;color:#c6dced}
.table-wrap{overflow-x:auto;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border:1px solid #223142;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#111b26;color:#cfe2f4;font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:#0e151e}
blockquote{margin:14px 0;padding:10px 16px;border-left:3px solid #2f5f88;background:#0e1721;color:#b6c8da}
hr{border:0;border-top:1px solid #1e2c3b;margin:28px 0}
ul,ol{padding-left:24px}
li{margin:5px 0}
footer{border-top:1px solid #1e2c3b;color:#72869a;font-size:12px;padding:20px 28px 40px;text-align:center}
@media print{body{background:#fff;color:#111}.hero{background:#fff;border:0}main{max-width:none}.stat{border-color:#ccc}code,pre,th,td{border-color:#c8c8c8;background:#f7f7f7;color:#111}th{background:#eee}a{color:#0645ad}}
figure.attack-graph{margin:1.2em 0;overflow-x:auto;background:#0f172a;border-radius:10px;padding:8px}
figure.attack-graph svg{max-width:100%;height:auto}
"""


def render_html(
    markdown: str,
    export: Mapping[str, Any],
    meta: Mapping[str, Any] | None = None,
    *,
    language: str | None = None,
    graph_svg: str = "",
) -> str:
    """Wrap rendered Markdown in a self-contained, escaped dark HTML document."""
    meta = dict(meta or {})
    lang = resolve_report_language(language, meta)
    strings = get_report_strings(language, meta)
    project = _project(export)
    counts = _counts(export)
    title = _meta_text(meta, "title") or strings["default_title"].format(
        project=project.get("name", strings["default_project"])
    )
    stats = "".join(
        f'<div class="stat {severity}"><div class="value">{counts["findings"].get(severity, 0)}</div>'
        f'<div class="label">{_escape(strings[f"html_stat_sev_{severity}"])}</div></div>'
        for severity in SEVERITIES
    )
    stats += (f'<div class="stat"><div class="value">{counts["total_assets"]}</div>'
              f'<div class="label">{_escape(strings["html_stat_assets"])}</div></div>'
              f'<div class="stat"><div class="value">{counts["nodes"].get("fact", 0)}</div>'
              f'<div class="label">{_escape(strings["html_stat_facts"])}</div></div>')
    body = markdown_to_html(markdown, mermaid_svg=graph_svg)
    raw_ver = _meta_text(meta, "version", __version__)
    ver_label = raw_ver if raw_ver.startswith("v") else f"v{raw_ver}"
    run_id = _meta_text(meta, "run_id")
    run_part = f' · {_escape(strings["html_meta_run"].format(run_id=run_id))}' if run_id else ""
    gen_part = _escape(strings["html_meta_generated"].format(generated_at=_generated_at(meta)))
    return "\n".join([
        "<!DOCTYPE html>",
        f'<html lang="{_escape(lang)}">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="generator" content="ARIA-X">',
        f"<title>{_escape(title)}</title>",
            f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<header class="hero"><div class="inner">',
        f'<div class="eyebrow">{_escape(strings["html_eyebrow"])}</div>',
        f"<h1>{_escape(title)}</h1>",
        f'<p class="meta">{_escape(project.get("name", ""))}'
        f'{run_part}'
        f' · {gen_part}</p>',
        f'<div class="stats">{stats}</div>',
        "</div></header>",
        f'<main class="report">{body}</main>',
        f'<footer>{_escape(strings["html_footer"].format(version=ver_label))}</footer>',
        "</body>",
        "</html>",
    ])


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------


def _findings_payload(export: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
    findings = []
    for finding in _findings(export):
        item = dict(finding)
        item["severity"] = _severity(finding)
        item["evidence"] = _evidence_for(export, str(finding.get("id")))
        findings.append(item)
    return {"project": _project(export), "meta": dict(meta),
            "generated_at": _generated_at(meta),
            "counts": _counts(export), "findings": findings}


def write_report(
    run_dir: str | Path,
    export: Mapping[str, Any],
    meta: Mapping[str, Any] | None = None,
    *,
    language: str | None = None,
) -> dict[str, str]:
    """Write ``report.md``, ``report.html`` and ``findings.json`` under *run_dir*."""
    meta = dict(meta or {})
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(export, meta=meta, language=language)
    html_text = render_html(markdown, export, meta, language=language,
                           graph_svg=attack_graph_svg(export))
    payload = _findings_payload(export, meta)
    markdown_path = directory / "report.md"
    html_path = directory / "report.html"
    json_path = directory / "findings.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    json_path.write_text(json_dumps(payload, indent=2), encoding="utf-8")
    return {"markdown": str(markdown_path), "html": str(html_path), "json": str(json_path)}


# ---------------------------------------------------------------------------
# attack-path diagram (Mermaid for Markdown, inline SVG for the HTML report)
# ---------------------------------------------------------------------------

_MAX_GRAPH_ASSETS = 18
_MAX_GRAPH_FINDINGS = 12


def _graph_model(export: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Return (asset chain, findings) capped for a readable diagram, or None."""
    assets = [a for a in _as_list(export.get("assets")) if isinstance(a, dict)]
    findings = _findings(export)
    if not assets and not findings:
        return None
    by_id = {str(a.get("id")): a for a in assets}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(asset: dict[str, Any], depth: int = 0) -> None:
        if depth > 6 or len(ordered) >= _MAX_GRAPH_ASSETS:
            return
        aid = str(asset.get("id"))
        if aid in seen:
            return
        seen.add(aid)
        ordered.append(asset)
        for child in assets:
            if str(child.get("parent_id") or "") == aid:
                walk(child, depth + 1)

    for asset in assets:
        parent = str(asset.get("parent_id") or "")
        if not parent or parent not in by_id:
            walk(asset)
    for asset in assets:  # orphans / breadth-first leftovers
        if str(asset.get("id")) not in seen:
            ordered.append(asset)
            seen.add(str(asset.get("id")))
        if len(ordered) >= _MAX_GRAPH_ASSETS:
            break
    return ordered, findings[:_MAX_GRAPH_FINDINGS]


def _mermaid_label(text: Any, limit: int = 46) -> str:
    raw = str(text if text is not None else "").replace("\r", "").replace("\n", " ").strip()
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    cleaned = raw.replace('"', "'")
    return cleaned or "—"


def attack_graph_mermaid(export: Mapping[str, Any]) -> str:
    """Render the asset chain and findings as a Mermaid ``flowchart TD`` block."""
    model = _graph_model(export)
    if model is None:
        return ""
    assets, findings = model
    lines = ["```mermaid", "flowchart TD"]
    for index, asset in enumerate(assets):
        kind = _plain(asset.get("kind"), 18)
        value = _mermaid_label(asset.get("value"))
        lines.append(f'  A{index}["{kind}: {value}"]')
    parent_index = {str(a.get("id")): i for i, a in enumerate(assets)}
    for index, asset in enumerate(assets):
        parent = str(asset.get("parent_id") or "")
        if parent in parent_index and parent_index[parent] != index:
            lines.append(f"  A{parent_index[parent]} --> A{index}")
    for index, finding in enumerate(findings):
        severity = _severity(finding).lower()
        lines.append(f'  F{index}["{severity.upper()}: {_mermaid_label(finding.get("title"))}"]')
        parent = str(finding.get("parent_id") or "")
        anchor = parent_index.get(parent)
        lines.append(f"  A{anchor} --> F{index}" if anchor is not None else f"  A0 --> F{index}")
    lines.append("  classDef sev_critical fill:#7f1d1d,stroke:#f87171,color:#fff")
    lines.append("  classDef sev_high fill:#7c2d12,stroke:#fb923c,color:#fff")
    lines.append("  classDef sev_medium fill:#78350f,stroke:#fbbf24,color:#fff")
    lines.append("  classDef sev_low fill:#14532d,stroke:#4ade80,color:#fff")
    for index, finding in enumerate(findings):
        severity = _severity(finding).lower()
        css = {"critical": "sev_critical", "high": "sev_high",
               "medium": "sev_medium", "low": "sev_low"}.get(severity)
        if css:
            lines.append(f"  class F{index} {css}")
    lines.append("```")
    return "\n".join(lines)


def attack_graph_svg(export: Mapping[str, Any]) -> str:
    """Render the same graph as a standalone inline SVG (no JS, no CDN)."""
    model = _graph_model(export)
    if model is None:
        return ""
    assets, findings = model
    node_h, gap, left_w, right_w = 34, 12, 300, 320
    rows = max(len(assets), len(findings), 1)
    height = 24 + rows * (node_h + gap)
    width = left_w + right_w + 120

    def box(x: int, y: int, w: int, text: str, *, fill: str, stroke: str) -> str:
        label = _escape_text(_plain(text, 44))
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{node_h}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + 10}" y="{y + node_h // 2 + 5}" font-size="12" '
            f'fill="{"#f8fafc" if fill != "#f8fafc" else "#0f172a"}">{label}</text>'
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif,system-ui,sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#0f172a"/>',
    ]
    asset_y: dict[int, int] = {}
    for index, asset in enumerate(assets):
        y = 20 + index * (node_h + gap)
        asset_y[index] = y
        parts.append(box(30, y, left_w, f"{asset.get('kind')}: {asset.get('value')}",
                         fill="#1e293b", stroke="#38bdf8"))
    severities = {"critical": "#f87171", "high": "#fb923c", "medium": "#fbbf24", "low": "#4ade80"}
    fills = {"critical": "#7f1d1d", "high": "#7c2d12", "medium": "#78350f", "low": "#14532d"}
    for index, finding in enumerate(findings):
        severity = _severity(finding).lower()
        y = 20 + index * (node_h + gap)
        parts.append(box(width - right_w - 30, y, right_w,
                         f"{severity.upper()}: {finding.get('title')}",
                         fill=fills.get(severity, "#334155"), stroke=severities.get(severity, "#94a3b8")))
        parent = str(finding.get("parent_id") or "")
        anchor = None
        for i, asset in enumerate(assets):
            if str(asset.get("id")) == parent:
                anchor = i
                break
        y_from = asset_y.get(anchor if anchor is not None else 0, 20) + node_h // 2
        parts.append(
            f'<line x1="{30 + left_w}" y1="{y_from}" x2="{width - right_w - 30}" y2="{y + node_h // 2}" '
            f'stroke="#475569" stroke-width="1.2" marker-end="url(#arrow)"/>'
        )
    parts.insert(1, '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" '
                    'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="#475569"/></marker></defs>')
    parts.append("</svg>")
    return "".join(parts)


def _escape_text(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
