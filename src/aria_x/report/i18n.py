"""Trilingual fixed-copy templates for ARIA-X reports (en, zh-CN, ja)."""

from __future__ import annotations

from typing import Any, Mapping

SUPPORTED_REPORT_LANGUAGES = ("en", "zh-CN", "ja")
DEFAULT_REPORT_LANGUAGE = "en"


def normalize_report_language(value: Any) -> str:
    """Normalize a language tag to ``en``, ``zh-CN``, or ``ja`` (default ``en``)."""
    raw = str(value or "").strip().lower()
    if not raw:
        return DEFAULT_REPORT_LANGUAGE
    if raw == "zh" or raw.startswith("zh-cn") or raw.startswith("zh_cn") or raw.startswith("zh-hans"):
        return "zh-CN"
    if raw == "ja" or raw == "jp" or raw.startswith("ja-") or raw.startswith("ja_"):
        return "ja"
    if raw == "en" or raw.startswith("en-") or raw.startswith("en_"):
        return "en"
    return DEFAULT_REPORT_LANGUAGE


def resolve_report_language(
    language: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """Resolve report fixed-copy language using Option A precedence.

    Precedence: explicit *language* arg > ``meta["report_language"]`` >
    ``meta["lang"]`` > ``"en"``. ``meta["language"]`` (LLM/prompt language)
    is intentionally ignored so existing CLI/Demo runs default to English.
    """
    if language is not None and str(language).strip():
        return normalize_report_language(language)
    if isinstance(meta, Mapping):
        for key in ("report_language", "lang"):
            candidate = meta.get(key)
            if candidate is not None and str(candidate).strip():
                return normalize_report_language(candidate)
    return DEFAULT_REPORT_LANGUAGE


REPORT_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "default_title": "Security Assessment Report — {project}",
        "default_project": "Engagement",
        "meta_col_field": "Field",
        "meta_col_value": "Value",
        "meta_engagement": "Engagement",
        "meta_project_id": "Project ID",
        "meta_run_id": "Run ID",
        "meta_generated": "Generated",
        "meta_operator": "Operator",
        "meta_classification": "Classification",
        "default_operator": "ARIA-X autonomous engine",
        "default_classification": "Confidential — authorized testing only",

        "sec_1": "## 1. Executive Summary",
        "exec_summary_body": (
            "This engagement pursued the goal **{goal}**. "
            "The autonomous loop recorded {facts} verified facts, "
            "{intents} explored intents and "
            "{total_findings} findings across {total_assets} distinct assets. "
            "Actionable issues by severity: {severity_summary}."
        ),
        "exec_top_finding": "The highest-severity issue is **{title}** ({severity}).",
        "exec_no_findings": (
            "No confirmed security findings were recorded. Negative results and the "
            "coverage boundary are documented in §7."
        ),

        "sec_2": "## 2. Engagement Scope & Authorization",
        "scope_authorized_targets": "Authorized targets:",
        "scope_no_targets": "No explicit target list was recorded in the project scope.",
        "scope_excluded_targets": "Excluded targets:",
        "scope_authorization": (
            "Authorization: this engagement was executed only against explicitly authorized "
            "assets; every out-of-scope action was refused by the scope engine and appears in "
            "the audit appendix."
        ),

        "sec_3": "## 3. Methodology",
        "methodology_body": (
            "ARIA-X runs a blackboard-driven OODA loop: a planner emits bounded intents, workers "
            "execute them through scoped tools inside an isolated sandbox, and every observation "
            "is written back as a fact, asset or finding. The shared asset graph and todo chain "
            "persist across workers, so each probe builds on the previous one instead of "
            "restarting. Network access is authorized per request, rate-limited per host and "
            "recorded as evidence."
        ),
        "methodology_footprint": (
            "Execution footprint: {traces} process traces, {edges} graph "
            "edges, {todos} plan items."
        ),

        "sec_4": "## 4. Findings",
        "findings_none": "_No findings were confirmed during this engagement._",
        "finding_col_attr": "Attribute",
        "finding_col_val": "Value",
        "finding_attr_severity": "Severity",
        "finding_attr_cvss": "CVSS",
        "finding_attr_confidence": "Confidence",
        "finding_attr_id": "Finding ID",
        "finding_attr_intent": "Proven by intent",
        "finding_no_detail": "_No detail recorded._",
        "finding_evidence_heading": "**Evidence**",
        "ev_col_kind": "Kind",
        "ev_col_ref": "Reference",
        "ev_col_sha256": "SHA-256",
        "ev_col_excerpt": "Excerpt",

        "sec_5": "## 5. Attack Path Narrative",
        "graph_title": "Attack path diagram",
        "attack_none": "No attack path was recorded: the engagement produced neither intents nor lineage edges.",
        "attack_intro": (
            "Each item below is one decision point: the intent, what it observed and what it "
            "proved. Lineage is taken directly from graph edges."
        ),
        "attack_intent_line": "{index}. **{title}** — status: {status}.",
        "attack_observed": "   - Observed: {items}",
        "attack_findings": "   - Findings: {items}",
        "attack_no_downstream": "   - No downstream node recorded (negative or incomplete exploration).",
        "attack_cross_links": "Cross-links recorded during analysis:",

        "sec_6": "## 6. Asset Inventory",
        "asset_col_value": "Value",
        "asset_col_kind": "Kind",
        "asset_col_parent": "Parent",
        "asset_col_first_seen": "First seen",
        "assets_none": "_No assets were recorded._",

        "sec_7": "## 7. Coverage & Limitations",
        "cov_facts": "- Verified facts: {count}",
        "cov_intents": "- Intents explored: {count} ({open_count} still open)",
        "cov_assets": "- Assets discovered: {count}",
        "cov_findings": "- Findings: {count} ({breakdown})",
        "cov_denied": "- Out-of-scope or policy-refused actions: {count} (audited, not executed)",
        "cov_limitations": (
            "Limitations: the assessment reflects the tool results recorded above as of the "
            "generation timestamp. Any service that was unavailable, rate-limited or outside "
            "the authorized scope is not covered. Absence of a finding is not proof of absence "
            "of a vulnerability; open intents mark the remaining uncertainty."
        ),

        "sec_8": "## 8. Appendix",
        "sec_8_1": "### 8.1 Evidence index",
        "app_ev_col_finding": "Finding",
        "app_ev_col_kind": "Kind",
        "app_ev_col_ref": "Reference",
        "app_ev_col_sha256": "SHA-256",
        "app_ev_none": "_No evidence records were attached to findings._",

        "sec_8_2": "### 8.2 Plan state",
        "app_todo_col_pos": "#",
        "app_todo_col_status": "Status",
        "app_todo_col_title": "Title",
        "app_todo_none": "_No todo chain was recorded._",

        "sec_8_3": "### 8.3 Process trace (recent)",
        "app_trace_col_task": "Task",
        "app_trace_col_agent": "Agent",
        "app_trace_col_kind": "Kind",
        "app_trace_col_tool": "Tool",
        "app_trace_col_content": "Content",
        "app_trace_none": "_No traces were recorded._",

        "sec_8_4": "### 8.4 Audit (recent decisions)",
        "app_audit_col_time": "Time",
        "app_audit_col_actor": "Actor",
        "app_audit_col_action": "Action",
        "app_audit_col_decision": "Decision",
        "app_audit_col_target": "Target",
        "app_audit_none": "_No audit entries were recorded._",

        "md_footer": (
            "_Generated by ARIA-X {version} — "
            "{generated_at}. This report is an audit artifact; evidence files are stored "
            "alongside it._"
        ),
        "html_eyebrow": "ARIA-X · Authorized Security Assessment",
        "html_meta_run": "run {run_id}",
        "html_meta_generated": "generated {generated_at}",
        "html_stat_assets": "assets",
        "html_stat_facts": "facts",
        "html_stat_sev_critical": "critical",
        "html_stat_sev_high": "high",
        "html_stat_sev_medium": "medium",
        "html_stat_sev_low": "low",
        "html_stat_sev_info": "info",
        "html_footer": "Generated by ARIA-X {version} · AGPL-3.0-or-later · confidential",
    },

    "zh-CN": {
        "default_title": "安全评估报告 — {project}",
        "default_project": "评估项目",
        "meta_col_field": "字段",
        "meta_col_value": "值",
        "meta_engagement": "评估项目",
        "meta_project_id": "项目 ID",
        "meta_run_id": "运行 ID",
        "meta_generated": "生成时间",
        "meta_operator": "执行主体",
        "meta_classification": "密级",
        "default_operator": "ARIA-X 自主安全评估引擎",
        "default_classification": "机密 — 仅限授权测试",

        "sec_1": "## 1. 执行摘要",
        "exec_summary_body": (
            "本次评估围绕目标 **{goal}** 展开。"
            "自主评估循环共记录 {facts} 项已验证事实、{intents} 项已探索意图，"
            "以及跨 {total_assets} 项独立资产的 {total_findings} 项安全发现。"
            "按严重程度分布如下：{severity_summary}。"
        ),
        "exec_top_finding": "最高严重程度问题为 **{title}**（{severity}）。",
        "exec_no_findings": "本次评估未记录到已确认的安全发现。阴性结果与覆盖边界见第 7 节。",

        "sec_2": "## 2. 评估范围与授权说明",
        "scope_authorized_targets": "授权目标：",
        "scope_no_targets": "项目范围中未记录显式目标列表。",
        "scope_excluded_targets": "排除目标：",
        "scope_authorization": (
            "授权声明：本次评估仅针对明确授权的资产执行；所有越界操作均已被范围引擎拒绝，"
            "并记录在审计附录中。"
        ),

        "sec_3": "## 3. 评估方法",
        "methodology_body": (
            "ARIA-X 运行基于黑板的 OODA 循环：规划器发出受约束的意图，工作器在隔离沙箱中"
            "通过受范围限制的工具执行探索，并将每项观测写回为事实、资产或发现。"
            "共享资产图谱与待办链在多个工作器之间持久化，使每次探测都能基于已有结果推进而非重复起点。"
            "网络访问按请求逐次授权、按主机限速并留存证据。"
        ),
        "methodology_footprint": "执行轨迹统计：{traces} 条过程轨迹、{edges} 条图谱边、{todos} 项计划条目。",

        "sec_4": "## 4. 安全发现",
        "findings_none": "_本次评估未确认任何安全发现。_",
        "finding_col_attr": "属性",
        "finding_col_val": "值",
        "finding_attr_severity": "严重程度",
        "finding_attr_cvss": "CVSS",
        "finding_attr_confidence": "置信度",
        "finding_attr_id": "发现 ID",
        "finding_attr_intent": "由意图证实",
        "finding_no_detail": "_暂无详细说明。_",
        "finding_evidence_heading": "**证据**",
        "ev_col_kind": "类型",
        "ev_col_ref": "引用路径",
        "ev_col_sha256": "SHA-256",
        "ev_col_excerpt": "摘要",

        "sec_5": "## 5. 攻击路径叙述",
        "graph_title": "攻击路径图",
        "attack_none": "未记录攻击路径：本次评估既未产生意图也未产生血缘边。",
        "attack_intro": "以下每条记录对应一个决策点：探索意图、观测结果及证实内容。血缘关系直接来自图谱边。",
        "attack_intent_line": "{index}. **{title}** — 状态：{status}。",
        "attack_observed": "   - 观测结果：{items}",
        "attack_findings": "   - 相关发现：{items}",
        "attack_no_downstream": "   - 未记录下游节点（阴性结果或探索尚未完成）。",
        "attack_cross_links": "分析过程中记录的交叉关联：",

        "sec_6": "## 6. 资产清单",
        "asset_col_value": "资产值",
        "asset_col_kind": "类型",
        "asset_col_parent": "父级资产",
        "asset_col_first_seen": "首次发现",
        "assets_none": "_未记录到任何资产。_",

        "sec_7": "## 7. 覆盖范围与局限性",
        "cov_facts": "- 已验证事实：{count}",
        "cov_intents": "- 已探索意图：{count}（其中 {open_count} 项仍处于开放状态）",
        "cov_assets": "- 已发现资产：{count}",
        "cov_findings": "- 安全发现：{count}（{breakdown}）",
        "cov_denied": "- 越界或被策略拒绝的操作：{count}（已审计，未执行）",
        "cov_limitations": (
            "局限性：本评估仅反映截至报告生成时间戳为止上述工具所记录的结果。"
            "任何不可用、触发限速或超出授权范围的服务均未纳入覆盖。"
            "未发现问题并不等同于不存在漏洞；未关闭的意图标记了尚存的不确定性。"
        ),

        "sec_8": "## 8. 附录",
        "sec_8_1": "### 8.1 证据索引",
        "app_ev_col_finding": "所属发现",
        "app_ev_col_kind": "类型",
        "app_ev_col_ref": "引用路径",
        "app_ev_col_sha256": "SHA-256",
        "app_ev_none": "_未向发现附加任何证据记录。_",

        "sec_8_2": "### 8.2 计划状态",
        "app_todo_col_pos": "#",
        "app_todo_col_status": "状态",
        "app_todo_col_title": "标题",
        "app_todo_none": "_未记录待办计划链。_",

        "sec_8_3": "### 8.3 过程轨迹（近期）",
        "app_trace_col_task": "任务",
        "app_trace_col_agent": "代理",
        "app_trace_col_kind": "类型",
        "app_trace_col_tool": "工具",
        "app_trace_col_content": "内容",
        "app_trace_none": "_未记录过程轨迹。_",

        "sec_8_4": "### 8.4 审计决策（近期）",
        "app_audit_col_time": "时间",
        "app_audit_col_actor": "执行者",
        "app_audit_col_action": "动作",
        "app_audit_col_decision": "决策",
        "app_audit_col_target": "目标",
        "app_audit_none": "_未记录审计条目。_",

        "md_footer": (
            "_由 ARIA-X {version} 生成 — {generated_at}。"
            "本报告为审计产物；相关证据文件已一同保存在同级目录中。_"
        ),
        "html_eyebrow": "ARIA-X · 授权安全评估",
        "html_meta_run": "运行 {run_id}",
        "html_meta_generated": "生成于 {generated_at}",
        "html_stat_assets": "资产",
        "html_stat_facts": "事实",
        "html_stat_sev_critical": "严重",
        "html_stat_sev_high": "高危",
        "html_stat_sev_medium": "中危",
        "html_stat_sev_low": "低危",
        "html_stat_sev_info": "信息",
        "html_footer": "由 ARIA-X {version} 生成 · AGPL-3.0-or-later · 机密文件",
    },

    "ja": {
        "default_title": "セキュリティ評価レポート — {project}",
        "default_project": "エンゲージメント",
        "meta_col_field": "項目",
        "meta_col_value": "値",
        "meta_engagement": "エンゲージメント",
        "meta_project_id": "プロジェクト ID",
        "meta_run_id": "実行 ID",
        "meta_generated": "生成日時",
        "meta_operator": "実行主体",
        "meta_classification": "区分",
        "default_operator": "ARIA-X 自律評価エンジン",
        "default_classification": "機密 — 認可されたテスト専用",

        "sec_1": "## 1. エグゼクティブサマリー",
        "exec_summary_body": (
            "本エンゲージメントは目標 **{goal}** に沿って実施されました。"
            "自律ループにより {facts} 件の検証済み事実、{intents} 件の探索意図、"
            "および {total_assets} 件の固有資産に対する {total_findings} 件の検出事項が記録されました。"
            "深刻度別の内訳: {severity_summary}。"
        ),
        "exec_top_finding": "最も深刻度の高い検出事項は **{title}**（{severity}）です。",
        "exec_no_findings": "確定したセキュリティ上の検出事項は記録されませんでした。陰性結果と検証範囲の境界は §7 に記載されています。",

        "sec_2": "## 2. 評価スコープおよび認可",
        "scope_authorized_targets": "認可された対象:",
        "scope_no_targets": "プロジェクトスコープに明示的な対象リストは記録されていません。",
        "scope_excluded_targets": "除外対象:",
        "scope_authorization": (
            "認可: 本エンゲージメントは明示的に認可された資産に対してのみ実行されました。"
            "スコープ外の操作はすべてスコープエンジンにより拒否され、監査付録に記録されています。"
        ),

        "sec_3": "## 3. 評価手法",
        "methodology_body": (
            "ARIA-X はブラックボード駆動の OODA ループを実行します。プランナーが制約付き意図を発行し、"
            "ワーカーが隔離サンドボックス内でスコープ制御されたツールを用いて実行し、すべての観測結果を"
            "事実・資産・検出事項として書き戻します。共有資産グラフと Todo チェーンはワーカー間で永続化されるため、"
            "各プローブは最初からやり直すのではなく前の成果を土台に進行します。"
            "ネットワークアクセスはリクエストごとに認可・ホストごとにレート制限され、証跡として記録されます。"
        ),
        "methodology_footprint": "実行フットプリント: プロセストレース {traces} 件、グラフエッジ {edges} 件、計画項目 {todos} 件。",

        "sec_4": "## 4. 検出事項",
        "findings_none": "_本エンゲージメントでは確定した検出事項はありませんでした。_",
        "finding_col_attr": "属性",
        "finding_col_val": "値",
        "finding_attr_severity": "深刻度",
        "finding_attr_cvss": "CVSS",
        "finding_attr_confidence": "信頼度",
        "finding_attr_id": "検出 ID",
        "finding_attr_intent": "実証元意図",
        "finding_no_detail": "_詳細は記録されていません。_",
        "finding_evidence_heading": "**証跡**",
        "ev_col_kind": "種別",
        "ev_col_ref": "参照",
        "ev_col_sha256": "SHA-256",
        "ev_col_excerpt": "抜粋",

        "sec_5": "## 5. 攻撃経路ナラティブ",
        "graph_title": "攻撃経路図",
        "attack_none": "攻撃経路は記録されませんでした: 意図および系譜エッジのいずれも生成されていません。",
        "attack_intro": "以下の各項目は 1 つの意思決定ポイント（意図・観測結果・実証内容）を示します。系譜はグラフエッジから直接取得しています。",
        "attack_intent_line": "{index}. **{title}** — 状態: {status}。",
        "attack_observed": "   - 観測結果: {items}",
        "attack_findings": "   - 検出事項: {items}",
        "attack_no_downstream": "   - 下流ノードは記録されていません（陰性結果または未完了の探索）。",
        "attack_cross_links": "分析中に記録された相互リンク:",

        "sec_6": "## 6. 資産インベントリ",
        "asset_col_value": "値",
        "asset_col_kind": "種別",
        "asset_col_parent": "親資産",
        "asset_col_first_seen": "初回検出",
        "assets_none": "_資産は記録されませんでした。_",

        "sec_7": "## 7. カバレッジと制限事項",
        "cov_facts": "- 検証済み事実: {count}",
        "cov_intents": "- 探索済み意図: {count}（うち未完了 {open_count} 件）",
        "cov_assets": "- 検出済み資産: {count}",
        "cov_findings": "- 検出事項: {count}（{breakdown}）",
        "cov_denied": "- スコープ外またはポリシー拒否操作: {count}（監査記録済み・未実行）",
        "cov_limitations": (
            "制限事項: 本評価は生成タイムスタンプ時点までに上記ツールで記録された結果を反映しています。"
            "利用不可、レート制限対象、または認可スコープ外のサービスは対象外です。"
            "検出事項がないことは脆弱性が存在しないことの証明ではなく、未完了の意図は残存する不確実性を示します。"
        ),

        "sec_8": "## 8. 付録",
        "sec_8_1": "### 8.1 証跡インデックス",
        "app_ev_col_finding": "検出事項",
        "app_ev_col_kind": "種別",
        "app_ev_col_ref": "参照",
        "app_ev_col_sha256": "SHA-256",
        "app_ev_none": "_検出事項に添付された証跡レコードはありません。_",

        "sec_8_2": "### 8.2 計画状態",
        "app_todo_col_pos": "#",
        "app_todo_col_status": "状態",
        "app_todo_col_title": "タイトル",
        "app_todo_none": "_Todo チェーンは記録されませんでした。_",

        "sec_8_3": "### 8.3 プロセストレース（直近）",
        "app_trace_col_task": "タスク",
        "app_trace_col_agent": "エージェント",
        "app_trace_col_kind": "種別",
        "app_trace_col_tool": "ツール",
        "app_trace_col_content": "内容",
        "app_trace_none": "_トレースは記録されませんでした。_",

        "sec_8_4": "### 8.4 監査ログ（直近の判定）",
        "app_audit_col_time": "時刻",
        "app_audit_col_actor": "実行者",
        "app_audit_col_action": "操作",
        "app_audit_col_decision": "判定",
        "app_audit_col_target": "対象",
        "app_audit_none": "_監査エントリは記録されませんでした。_",

        "md_footer": (
            "_ARIA-X {version} により生成 — {generated_at}。"
            "本レポートは監査成果物であり、証跡ファイルは同ディレクトリに保存されています。_"
        ),
        "html_eyebrow": "ARIA-X · 認可済みセキュリティ評価",
        "html_meta_run": "実行 {run_id}",
        "html_meta_generated": "生成日時 {generated_at}",
        "html_stat_assets": "資産",
        "html_stat_facts": "事実",
        "html_stat_sev_critical": "緊急",
        "html_stat_sev_high": "高",
        "html_stat_sev_medium": "中",
        "html_stat_sev_low": "低",
        "html_stat_sev_info": "情報",
        "html_footer": "ARIA-X {version} により生成 · AGPL-3.0-or-later · 機密",
    },
}


def get_report_strings(
    language: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the fixed-copy dictionary for the resolved report language."""
    resolved = resolve_report_language(language, meta)
    return REPORT_STRINGS.get(resolved, REPORT_STRINGS[DEFAULT_REPORT_LANGUAGE])
