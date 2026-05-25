from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from mira.agentic.semantic_catalog import canonical_semantic_tool_name, is_semantic_tool


MISSING_VALUES = (None, "", [], {})
FRAME_META_KEYS = {"context_action", "range_source"}
_INTERNAL_VALIDATION_KEYS = {"_invalid_filters_type", "_invalid_payload_type"}


@dataclass(frozen=True)
class SemanticFrameSchema:
    views: set[str] = field(default_factory=set)
    required: set[str] = field(default_factory=lambda: {"view"})
    optional: set[str] = field(default_factory=set)
    filter_keys: set[str] = field(default_factory=set)
    payload_keys: set[str] = field(default_factory=set)

    @property
    def allowed(self) -> set[str]:
        return set(self.required) | set(self.optional)


@dataclass(frozen=True)
class SemanticViewContract:
    optional: set[str] = field(default_factory=set)
    filter_keys: set[str] = field(default_factory=set)
    payload_keys: set[str] = field(default_factory=set)

    @property
    def allowed(self) -> set[str]:
        return {"view"} | set(self.optional)


@dataclass(frozen=True)
class PreviewPayloadSchema:
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    require_one_of: tuple[set[str], ...] = field(default_factory=tuple)
    require_any_filter: set[str] = field(default_factory=set)

    @property
    def allowed(self) -> set[str]:
        out = set(self.required) | set(self.optional)
        for group in self.require_one_of:
            out.update(group)
        out.update(self.require_any_filter)
        return out


@dataclass(frozen=True)
class SemanticFrameIssue:
    status: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return not self.status


@dataclass(frozen=True)
class SemanticFrameResult:
    tool_name: str
    args: dict[str, Any]
    current_frame: dict[str, Any]
    prior_frame: dict[str, Any] = field(default_factory=dict)
    inherited_keys: list[str] = field(default_factory=list)
    issue: SemanticFrameIssue = field(default_factory=SemanticFrameIssue)

    @property
    def ok(self) -> bool:
        return self.issue.ok


FILTER_KEYS_BY_TOOL: dict[str, set[str]] = {
    "query_transactions": {"merchant", "category", "account", "search", "reviewed", "transaction_id"},
    "summarize_spending": {"merchant", "category"},
    "finance_overview": set(),
    "review_budget": {"category"},
    "review_cashflow": set(),
    "check_affordability": {"category"},
    "review_recurring": {"status"},
    "review_net_worth": set(),
    "review_data_quality": {"transaction_id"},
    "review_financial_context": {"subject_type", "subject_key"},
    "manage_memory": set(),
    "preview_finance_change": set(),
    "make_chart": set(),
}


PAYLOAD_KEYS_BY_TOOL: dict[str, set[str]] = {
    "query_transactions": set(),
    "summarize_spending": {"metric", "group_by", "months"},
    "finance_overview": {"metric", "focus"},
    "review_budget": set(),
    "review_cashflow": {"horizon_days", "buffer_amount"},
    "check_affordability": {"amount", "purpose", "horizon_days", "buffer_amount", "question"},
    "review_recurring": {"all"},
    "review_net_worth": {"interval"},
    "review_data_quality": {"threshold", "include_taxonomy"},
    "review_financial_context": {"question", "intent", "max_facts", "amount", "month_key"},
    "manage_memory": {
        "text",
        "memory_type",
        "topic",
        "memory_id",
        "question",
        "include_expired",
        "include_inactive",
        "source_summary",
        "source_turn_id",
        "pinned",
        "expires_at",
        "normalized_text",
        "sensitivity",
        "confidence",
        "status",
        "force",
    },
    "preview_finance_change": set(),
    "make_chart": {"source_step_id", "title", "series_name", "unit", "labels", "values", "series"},
}


_COMMON_OPTIONAL = {"range", "range_a", "range_b", "filters", "payload", "limit", "offset", "sort"}


SEMANTIC_FRAME_SCHEMAS: dict[str, SemanticFrameSchema] = {
    "query_transactions": SemanticFrameSchema(
        views={"latest", "list", "search", "detail"},
        optional=_COMMON_OPTIONAL,
        filter_keys=FILTER_KEYS_BY_TOOL["query_transactions"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["query_transactions"],
    ),
    "summarize_spending": SemanticFrameSchema(
        views={"period_total", "entity_total", "top", "breakdown", "trend", "compare"},
        optional=_COMMON_OPTIONAL,
        filter_keys=FILTER_KEYS_BY_TOOL["summarize_spending"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["summarize_spending"],
    ),
    "finance_overview": SemanticFrameSchema(
        views={"snapshot", "priorities", "explain_metric"},
        optional={"range", "payload", "limit"},
        payload_keys=PAYLOAD_KEYS_BY_TOOL["finance_overview"],
    ),
    "review_budget": SemanticFrameSchema(
        views={"plan", "category_status", "savings_capacity"},
        optional={"range", "filters", "limit"},
        filter_keys=FILTER_KEYS_BY_TOOL["review_budget"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["review_budget"],
    ),
    "review_cashflow": SemanticFrameSchema(
        views={"forecast", "shortfall"},
        optional={"payload"},
        payload_keys=PAYLOAD_KEYS_BY_TOOL["review_cashflow"],
    ),
    "check_affordability": SemanticFrameSchema(
        views={"purchase"},
        optional={"filters", "payload"},
        filter_keys=FILTER_KEYS_BY_TOOL["check_affordability"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["check_affordability"],
    ),
    "review_recurring": SemanticFrameSchema(
        views={"summary", "changes"},
        optional={"filters", "payload", "limit"},
        filter_keys=FILTER_KEYS_BY_TOOL["review_recurring"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["review_recurring"],
    ),
    "review_net_worth": SemanticFrameSchema(
        views={"balances", "trend", "delta"},
        optional={"range", "payload", "limit"},
        payload_keys=PAYLOAD_KEYS_BY_TOOL["review_net_worth"],
    ),
    "review_data_quality": SemanticFrameSchema(
        views={"health", "enrichment_summary", "low_confidence", "explain_transaction"},
        optional={"filters", "payload", "limit"},
        filter_keys=FILTER_KEYS_BY_TOOL["review_data_quality"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["review_data_quality"],
    ),
    "review_financial_context": SemanticFrameSchema(
        views={"relevant", "lifestyle_profile", "friction_map", "operating_plan", "monthly_retrospective", "forecast_month_outlook.snapshot", "review_cashflow.safe_to_spend"},
        optional={"filters", "payload", "limit"},
        filter_keys=FILTER_KEYS_BY_TOOL["review_financial_context"],
        payload_keys=PAYLOAD_KEYS_BY_TOOL["review_financial_context"],
    ),
    "manage_memory": SemanticFrameSchema(
        views={"remember", "retrieve", "list", "update", "forget"},
        optional={"payload", "limit"},
        payload_keys=PAYLOAD_KEYS_BY_TOOL["manage_memory"],
    ),
    "preview_finance_change": SemanticFrameSchema(
        views={"preview"},
        optional={"payload"},
    ),
    "make_chart": SemanticFrameSchema(
        views={"line", "bar", "donut"},
        optional={"payload"},
        payload_keys=PAYLOAD_KEYS_BY_TOOL["make_chart"],
    ),
}


SEMANTIC_VIEW_CONTRACTS: dict[tuple[str, str], SemanticViewContract] = {
    ("query_transactions", "latest"): SemanticViewContract(
        optional={"range", "filters", "limit", "offset", "sort"},
        filter_keys={"merchant", "category", "account", "search", "reviewed"},
    ),
    ("query_transactions", "list"): SemanticViewContract(
        optional={"range", "filters", "limit", "offset", "sort"},
        filter_keys={"merchant", "category", "account", "search", "reviewed"},
    ),
    ("query_transactions", "search"): SemanticViewContract(
        optional={"range", "filters", "limit", "offset", "sort"},
        filter_keys={"merchant", "category", "account", "search", "reviewed"},
    ),
    ("query_transactions", "detail"): SemanticViewContract(
        optional={"filters"},
        filter_keys={"transaction_id"},
    ),
    ("summarize_spending", "period_total"): SemanticViewContract(
        optional={"range", "payload"},
        payload_keys={"metric"},
    ),
    ("summarize_spending", "entity_total"): SemanticViewContract(
        optional={"range", "filters", "payload"},
        filter_keys={"merchant", "category"},
        payload_keys={"metric"},
    ),
    ("summarize_spending", "top"): SemanticViewContract(
        optional={"range", "filters", "payload", "limit", "sort"},
        filter_keys={"merchant", "category"},
        payload_keys={"group_by", "metric"},
    ),
    ("summarize_spending", "breakdown"): SemanticViewContract(
        optional={"range"},
    ),
    ("summarize_spending", "trend"): SemanticViewContract(
        optional={"range", "filters", "payload", "limit", "sort"},
        filter_keys={"category"},
        payload_keys={"metric", "group_by", "months"},
    ),
    ("summarize_spending", "compare"): SemanticViewContract(
        optional={"range_a", "range_b", "filters", "payload"},
        filter_keys={"merchant", "category"},
        payload_keys={"metric"},
    ),
    ("finance_overview", "snapshot"): SemanticViewContract(optional={"range"}),
    ("finance_overview", "priorities"): SemanticViewContract(
        optional={"range", "payload", "limit"},
        payload_keys={"focus"},
    ),
    ("finance_overview", "explain_metric"): SemanticViewContract(
        optional={"range", "payload", "limit"},
        payload_keys={"metric"},
    ),
    ("review_budget", "plan"): SemanticViewContract(optional={"range"}),
    ("review_budget", "category_status"): SemanticViewContract(
        optional={"range", "filters"},
        filter_keys={"category"},
    ),
    ("review_budget", "savings_capacity"): SemanticViewContract(optional={"range"}),
    ("review_cashflow", "forecast"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"horizon_days", "buffer_amount"},
    ),
    ("review_cashflow", "shortfall"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"horizon_days", "buffer_amount"},
    ),
    ("check_affordability", "purchase"): SemanticViewContract(
        optional={"filters", "payload"},
        filter_keys={"category"},
        payload_keys={"amount", "purpose", "horizon_days", "buffer_amount", "question"},
    ),
    ("review_recurring", "summary"): SemanticViewContract(
        optional={"filters", "payload", "limit"},
        filter_keys={"status"},
        payload_keys={"all"},
    ),
    ("review_recurring", "changes"): SemanticViewContract(
        optional={"range", "limit"},
    ),
    ("review_net_worth", "balances"): SemanticViewContract(),
    ("review_net_worth", "trend"): SemanticViewContract(
        optional={"range", "payload", "limit"},
        payload_keys={"interval"},
    ),
    ("review_net_worth", "delta"): SemanticViewContract(),
    ("review_data_quality", "health"): SemanticViewContract(),
    ("review_data_quality", "enrichment_summary"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"include_taxonomy"},
    ),
    ("review_data_quality", "low_confidence"): SemanticViewContract(
        optional={"payload", "limit"},
        payload_keys={"threshold"},
    ),
    ("review_data_quality", "explain_transaction"): SemanticViewContract(
        optional={"filters"},
        filter_keys={"transaction_id"},
    ),
    ("review_financial_context", "relevant"): SemanticViewContract(
        optional={"filters", "payload", "limit"},
        filter_keys={"subject_type", "subject_key"},
        payload_keys={"question", "intent", "max_facts"},
    ),
    ("review_financial_context", "lifestyle_profile"): SemanticViewContract(
        optional={"filters", "payload", "limit"},
        filter_keys={"subject_type", "subject_key"},
        payload_keys={"question", "intent", "max_facts"},
    ),
    ("review_financial_context", "friction_map"): SemanticViewContract(
        optional={"filters", "payload", "limit"},
        filter_keys={"subject_type", "subject_key"},
        payload_keys={"question", "intent", "max_facts"},
    ),
    ("review_financial_context", "operating_plan"): SemanticViewContract(
        optional={"filters", "payload", "limit"},
        filter_keys={"subject_type", "subject_key"},
        payload_keys={"question", "intent", "max_facts"},
    ),
    ("review_financial_context", "monthly_retrospective"): SemanticViewContract(
        optional={"payload", "limit"},
        payload_keys={"question", "intent", "max_facts", "month_key"},
    ),
    ("review_financial_context", "forecast_month_outlook.snapshot"): SemanticViewContract(
        optional={"payload", "limit"},
        payload_keys={"question", "intent", "max_facts", "amount"},
    ),
    ("review_financial_context", "review_cashflow.safe_to_spend"): SemanticViewContract(
        optional={"payload", "limit"},
        payload_keys={"question", "intent", "max_facts", "amount"},
    ),
    ("manage_memory", "remember"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"text", "memory_type", "topic", "source_summary", "source_turn_id", "pinned", "expires_at"},
    ),
    ("manage_memory", "retrieve"): SemanticViewContract(
        optional={"payload", "limit"},
        payload_keys={"question", "text", "topic", "include_expired", "force"},
    ),
    ("manage_memory", "list"): SemanticViewContract(
        optional={"payload", "limit"},
        payload_keys={"include_inactive", "include_expired", "memory_type"},
    ),
    ("manage_memory", "update"): SemanticViewContract(
        optional={"payload"},
        payload_keys={
            "memory_id",
            "text",
            "normalized_text",
            "memory_type",
            "topic",
            "sensitivity",
            "confidence",
            "pinned",
            "expires_at",
            "status",
            "source_turn_id",
        },
    ),
    ("manage_memory", "forget"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"memory_id", "topic", "text", "source_turn_id"},
    ),
    ("preview_finance_change", "preview"): SemanticViewContract(optional={"payload"}),
    ("make_chart", "line"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"source_step_id", "title", "series_name", "unit", "labels", "values", "series"},
    ),
    ("make_chart", "bar"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"source_step_id", "title", "series_name", "unit", "labels", "values", "series"},
    ),
    ("make_chart", "donut"): SemanticViewContract(
        optional={"payload"},
        payload_keys={"source_step_id", "title", "series_name", "unit", "labels", "values", "series"},
    ),
}


TOP_LEVEL_KEYS_BY_TOOL: dict[str, set[str]] = {
    tool: schema.allowed
    for tool, schema in SEMANTIC_FRAME_SCHEMAS.items()
}


PREVIEW_CHANGE_TO_TOOL: dict[str, str] = {
    "bulk_recategorize": "preview_bulk_recategorize",
    "create_rule": "preview_create_rule",
    "rename_merchant": "preview_rename_merchant",
    "set_budget": "preview_set_budget",
    "create_goal": "preview_create_goal",
    "update_goal_target": "preview_update_goal_target",
    "mark_goal_funded": "preview_mark_goal_funded",
    "set_transaction_note": "preview_set_transaction_note",
    "set_transaction_tags": "preview_set_transaction_tags",
    "mark_reviewed": "preview_mark_reviewed",
    "bulk_mark_reviewed": "preview_bulk_mark_reviewed",
    "update_manual_account_balance": "preview_update_manual_account_balance",
    "split_transaction": "preview_split_transaction",
    "confirm_recurring_obligation": "preview_confirm_recurring_obligation",
    "dismiss_recurring_obligation": "preview_dismiss_recurring_obligation",
    "cancel_recurring": "preview_cancel_recurring",
    "restore_recurring": "preview_restore_recurring",
}


PREVIEW_PAYLOAD_SCHEMAS: dict[str, PreviewPayloadSchema] = {
    "bulk_recategorize": PreviewPayloadSchema(required={"merchant", "category"}),
    "create_rule": PreviewPayloadSchema(required={"pattern", "category"}),
    "rename_merchant": PreviewPayloadSchema(required={"old_name", "new_name"}),
    "set_budget": PreviewPayloadSchema(required={"category", "amount"}, optional={"rollover_mode", "rollover_balance"}),
    "create_goal": PreviewPayloadSchema(required={"name", "target_amount"}, optional={"goal_type", "current_amount", "target_date", "linked_category", "linked_account_id"}),
    "update_goal_target": PreviewPayloadSchema(required={"target_amount"}, optional={"current_amount", "target_date"}, require_one_of=({"goal_id", "name"},)),
    "mark_goal_funded": PreviewPayloadSchema(require_one_of=({"goal_id", "name"},)),
    "set_transaction_note": PreviewPayloadSchema(required={"transaction_id", "note"}),
    "set_transaction_tags": PreviewPayloadSchema(required={"transaction_id", "tags"}),
    "mark_reviewed": PreviewPayloadSchema(required={"transaction_id"}, optional={"reviewed"}),
    "bulk_mark_reviewed": PreviewPayloadSchema(optional={"current_reviewed", "reviewed"}, require_any_filter={"month", "category", "account", "search", "start_date", "end_date"}),
    "update_manual_account_balance": PreviewPayloadSchema(required={"balance"}, optional={"notes"}, require_one_of=({"account_id", "account_name"},)),
    "split_transaction": PreviewPayloadSchema(required={"transaction_id", "splits"}),
    "confirm_recurring_obligation": PreviewPayloadSchema(required={"merchant"}, optional={"pattern", "frequency", "category"}),
    "dismiss_recurring_obligation": PreviewPayloadSchema(required={"merchant"}, optional={"pattern"}),
    "cancel_recurring": PreviewPayloadSchema(required={"merchant"}),
    "restore_recurring": PreviewPayloadSchema(required={"merchant"}),
}


PREVIEW_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    key: tuple(schema.required)
    for key, schema in PREVIEW_PAYLOAD_SCHEMAS.items()
    if schema.required
}
PREVIEW_OPTIONAL_KEYS: dict[str, set[str]] = {}
for _change_type, _schema in PREVIEW_PAYLOAD_SCHEMAS.items():
    allowed = set(_schema.optional) | set(_schema.require_any_filter)
    for _group in _schema.require_one_of:
        allowed.update(_group)
    PREVIEW_OPTIONAL_KEYS[_change_type] = allowed


def complete_semantic_frame(
    tool_name: str,
    args: dict[str, Any],
    *,
    history: list[dict[str, Any]] | None = None,
    call_meta: dict[str, Any] | None = None,
) -> SemanticFrameResult:
    tool_name, current_args = normalize_semantic_frame_args(tool_name, args)
    meta = _frame_meta(call_meta or args or {})
    prior_frame = latest_prior_frame(history)

    inherited: list[str] = []
    if _should_merge_prior(tool_name, current_args, meta, prior_frame):
        prior_args = _args_from_frame(prior_frame)
        allowed = SEMANTIC_FRAME_SCHEMAS.get(tool_name, SemanticFrameSchema()).allowed
        for key in sorted(allowed):
            if current_args.get(key) in MISSING_VALUES and prior_args.get(key) not in MISSING_VALUES:
                current_args[key] = copy.deepcopy(prior_args[key])
                inherited.append(key)

    current_args = _apply_frame_defaults(tool_name, current_args)
    issue = validate_semantic_frame(tool_name, current_args)
    current_frame = semantic_frame_from_args(tool_name, current_args, meta=meta, inherited_keys=inherited)
    return SemanticFrameResult(
        tool_name=tool_name,
        args=current_args,
        current_frame=current_frame,
        prior_frame=prior_frame,
        inherited_keys=inherited,
        issue=issue,
    )


def normalize_semantic_frame_args(tool_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    original_tool_name = str(tool_name or "").strip()
    canonical_tool_name = canonical_semantic_tool_name(original_tool_name)
    if not is_semantic_tool(original_tool_name):
        return canonical_tool_name, _drop_missing(copy.deepcopy(args or {}))
    current_args = _drop_missing(copy.deepcopy(args or {}))
    current_args = _normalize_frame_aliases(original_tool_name, canonical_tool_name, current_args)
    current_args = _drop_missing(current_args)
    return canonical_tool_name, current_args


def validate_semantic_frame(tool_name: str, args: dict[str, Any]) -> SemanticFrameIssue:
    tool_name = canonical_semantic_tool_name(str(tool_name or "").strip())
    if tool_name not in SEMANTIC_FRAME_SCHEMAS:
        return SemanticFrameIssue("blocked", f"unknown semantic tool: {tool_name or '<empty>'}")
    schema = SEMANTIC_FRAME_SCHEMAS[tool_name]

    unknown = sorted(key for key in args if key not in schema.allowed and key not in _INTERNAL_VALIDATION_KEYS)
    if unknown:
        return SemanticFrameIssue("blocked", f"{tool_name} has unsupported arg(s): {', '.join(unknown)}")

    if "_invalid_filters_type" in args or (args.get("filters") not in MISSING_VALUES and not isinstance(args.get("filters"), dict)):
        return SemanticFrameIssue("clarify", "filters must be an object.")
    if "_invalid_payload_type" in args or (args.get("payload") not in MISSING_VALUES and not isinstance(args.get("payload"), dict)):
        return SemanticFrameIssue("clarify", "payload must be an object.")

    missing = [key for key in sorted(schema.required) if args.get(key) in MISSING_VALUES]
    if missing:
        return SemanticFrameIssue("clarify", f"{tool_name} missing required frame field(s): {', '.join(missing)}")

    view = str(args.get("view") or "").strip().lower()
    if view not in schema.views:
        return SemanticFrameIssue("clarify", f"I need a supported {tool_name} view.")

    contract_issue = _validate_view_contract(tool_name, view, args)
    if contract_issue.status:
        return contract_issue

    filters = args.get("filters")
    if isinstance(filters, dict):
        unknown_filters = sorted(key for key in filters if key not in schema.filter_keys)
        if unknown_filters:
            return SemanticFrameIssue("blocked", f"{tool_name} filters have unsupported key(s): {', '.join(unknown_filters)}")

    payload = args.get("payload")
    if isinstance(payload, dict) and tool_name != "preview_finance_change":
        unknown_payload = sorted(key for key in payload if key not in schema.payload_keys)
        if unknown_payload:
            return SemanticFrameIssue("blocked", f"{tool_name} payload has unsupported key(s): {', '.join(unknown_payload)}")

    return _validate_tool_specific_frame(tool_name, args)


def _validate_view_contract(tool_name: str, view: str, args: dict[str, Any]) -> SemanticFrameIssue:
    contract = SEMANTIC_VIEW_CONTRACTS.get((tool_name, view))
    if contract is None:
        return SemanticFrameIssue("blocked", f"{tool_name}.{view} has no semantic arg contract.")

    unknown = sorted(key for key in args if key not in contract.allowed and key not in _INTERNAL_VALIDATION_KEYS)
    if unknown:
        return SemanticFrameIssue("blocked", f"{tool_name}.{view} has unsupported arg(s): {', '.join(unknown)}")

    filters = args.get("filters")
    if isinstance(filters, dict):
        if "filters" not in contract.allowed:
            return SemanticFrameIssue("blocked", f"{tool_name}.{view} does not support filters.")
        unknown_filters = sorted(key for key in filters if key not in contract.filter_keys)
        if unknown_filters:
            return SemanticFrameIssue("blocked", f"{tool_name}.{view} filters have unsupported key(s): {', '.join(unknown_filters)}")

    payload = args.get("payload")
    if isinstance(payload, dict) and tool_name != "preview_finance_change":
        if "payload" not in contract.allowed:
            return SemanticFrameIssue("blocked", f"{tool_name}.{view} does not support payload.")
        unknown_payload = sorted(key for key in payload if key not in contract.payload_keys)
        if unknown_payload:
            return SemanticFrameIssue("blocked", f"{tool_name}.{view} payload has unsupported key(s): {', '.join(unknown_payload)}")

    return SemanticFrameIssue()


def latest_prior_frame(history: list[dict[str, Any]] | None) -> dict[str, Any]:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        frame = answer_context.get("current_frame") if isinstance(answer_context.get("current_frame"), dict) else {}
        normalized = normalize_prior_frame(frame)
        if normalized:
            return normalized

        frames = answer_context.get("current_frames") if isinstance(answer_context.get("current_frames"), list) else []
        for item in frames:
            normalized = normalize_prior_frame(item if isinstance(item, dict) else {})
            if normalized:
                return normalized

        tools = answer_context.get("tools") if isinstance(answer_context.get("tools"), list) else []
        for item in tools:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("name") or item.get("tool") or "").strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            normalized = semantic_frame_from_args(tool_name, args)
            if normalize_prior_frame(normalized):
                return normalized

        tool_context = turn.get("tool_context") if isinstance(turn.get("tool_context"), list) else []
        for item in tool_context:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("name") or item.get("tool") or "").strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            normalized = semantic_frame_from_args(tool_name, args)
            if normalize_prior_frame(normalized):
                return normalized
    return {}


def normalize_prior_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, dict):
        return {}
    tool_name = str(frame.get("tool") or frame.get("name") or "").strip()
    if not is_semantic_tool(tool_name):
        return {}
    args = {
        key: copy.deepcopy(value)
        for key, value in frame.items()
        if key not in {"tool", "name"} and key not in FRAME_META_KEYS and value not in MISSING_VALUES
    }
    canonical_tool, normalized_args = normalize_semantic_frame_args(tool_name, args)
    normalized_args = _apply_frame_defaults(canonical_tool, normalized_args)
    return semantic_frame_from_args(canonical_tool, normalized_args)


def semantic_frame_from_args(
    tool_name: str,
    args: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
    inherited_keys: list[str] | None = None,
) -> dict[str, Any]:
    tool_name, normalized_args = normalize_semantic_frame_args(tool_name, args or {})
    if tool_name not in SEMANTIC_FRAME_SCHEMAS:
        return {}
    schema = SEMANTIC_FRAME_SCHEMAS[tool_name]
    frame = {"tool": tool_name}
    for key in sorted(schema.allowed):
        value = copy.deepcopy(normalized_args.get(key))
        if value not in MISSING_VALUES:
            frame[key] = value
    for key, value in (meta or {}).items():
        if key in FRAME_META_KEYS and value not in MISSING_VALUES:
            frame[key] = value
    if inherited_keys:
        frame["inherited_keys"] = list(inherited_keys)
    return frame


def primary_semantic_frame(frames: list[dict[str, Any]]) -> dict[str, Any]:
    for frame in frames:
        if isinstance(frame, dict) and frame.get("tool") != "make_chart":
            return copy.deepcopy(frame)
    for frame in frames:
        if isinstance(frame, dict):
            return copy.deepcopy(frame)
    return {}


def _validate_tool_specific_frame(tool_name: str, args: dict[str, Any]) -> SemanticFrameIssue:
    view = str(args.get("view") or "").strip().lower()
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}

    if tool_name == "query_transactions":
        if view == "latest" and args.get("limit") in MISSING_VALUES:
            return SemanticFrameIssue()
        if view == "detail" and not filters.get("transaction_id"):
            return SemanticFrameIssue("clarify", "I need transaction_id for a transaction detail lookup.")
        return SemanticFrameIssue()

    if tool_name == "summarize_spending":
        if filters.get("merchant") and filters.get("category"):
            return SemanticFrameIssue("clarify", "I need either a merchant or a category filter, not both.")
        if view != "compare" and (args.get("range_a") not in MISSING_VALUES or args.get("range_b") not in MISSING_VALUES):
            return SemanticFrameIssue("blocked", "summarize_spending range_a/range_b are only supported for view=compare.")
        if view == "entity_total" and not (filters.get("merchant") or filters.get("category")):
            return SemanticFrameIssue("clarify", "I need a merchant or category for an entity total.")
        if view == "compare":
            if not (filters.get("merchant") or filters.get("category")):
                return SemanticFrameIssue("clarify", "I need a merchant or category to compare periods.")
            if args.get("range_a") in MISSING_VALUES or args.get("range_b") in MISSING_VALUES:
                return SemanticFrameIssue("clarify", "I need two ranges to compare.")
        if view == "top" and str(payload.get("group_by") or "").lower() not in {"merchant", "category"}:
            return SemanticFrameIssue("clarify", "I need group_by merchant or category for a top spending list.")
        if view == "top" and (filters.get("merchant") or filters.get("category")):
            return SemanticFrameIssue("clarify", "Top spending lists cannot safely use merchant or category filters yet; ask for the entity total or an unfiltered top list.")
        if view == "trend" and filters.get("merchant"):
            return SemanticFrameIssue("clarify", "Merchant-filtered spending trends are not supported yet.")
        if view == "trend" and not _is_supported_trend_range(args):
            return SemanticFrameIssue("clarify", "Spending trends need a month-count window such as last_6_months, last_12_months, last_90d, or payload.months.")
        metric = str(payload.get("metric") or "summary").strip().lower()
        if metric not in {
            "summary",
            "income",
            "expenses",
            "refunds",
            "savings",
            "credit_card_payments",
            "personal_transfers",
            "cash_deposits",
            "cash_withdrawals",
            "investment_transfers",
        }:
            return SemanticFrameIssue("clarify", f"I need a supported spending metric, not `{payload.get('metric')}`.")
        return SemanticFrameIssue()

    if tool_name == "finance_overview" and view == "explain_metric" and not payload.get("metric"):
        return SemanticFrameIssue("clarify", "I need a metric to explain.")

    if tool_name == "review_budget" and view == "category_status" and not filters.get("category"):
        return SemanticFrameIssue("clarify", "I need a category to review one budget.")

    if tool_name == "check_affordability" and payload.get("amount") in MISSING_VALUES:
        return SemanticFrameIssue("clarify", "I need amount before I can answer that.")

    if tool_name == "review_data_quality" and view == "explain_transaction" and not filters.get("transaction_id"):
        return SemanticFrameIssue("clarify", "I need the transaction_id to explain enrichment for one transaction.")

    if tool_name == "review_financial_context":
        subject_type = str(filters.get("subject_type") or "").strip().lower()
        if subject_type and subject_type not in {"profile", "category", "merchant", "subscription", "account", "cashflow", "habit"}:
            return SemanticFrameIssue("clarify", "I need a supported financial context subject type.")
        max_facts = payload.get("max_facts")
        if max_facts not in MISSING_VALUES:
            try:
                value = int(max_facts)
            except (TypeError, ValueError):
                return SemanticFrameIssue("clarify", "I need max_facts as a small integer.")
            if value < 1 or value > 8:
                return SemanticFrameIssue("clarify", "I can use between 1 and 8 financial context facts.")

    if tool_name == "manage_memory":
        if view == "remember" and not str(payload.get("text") or "").strip():
            return SemanticFrameIssue("clarify", "I need the durable memory text to save.")
        if view == "retrieve" and not str(payload.get("question") or payload.get("text") or payload.get("topic") or "").strip():
            return SemanticFrameIssue("clarify", "I need a memory topic or question to retrieve.")
        if view == "update" and not (payload.get("memory_id") or str(payload.get("text") or payload.get("topic") or "").strip()):
            return SemanticFrameIssue("clarify", "I need a memory_id or matching memory text/topic to update.")
        if view == "forget" and not (payload.get("memory_id") or str(payload.get("text") or payload.get("topic") or "").strip()):
            return SemanticFrameIssue("clarify", "I need a memory_id, topic, or text to forget.")

    if tool_name == "preview_finance_change":
        return _validate_preview_frame(args)

    if tool_name == "make_chart":
        if any(key in payload for key in ("labels", "values", "series")):
            return SemanticFrameIssue("blocked", "make_chart must use prior tool evidence instead of selector-provided labels or values")
        if not payload.get("source_step_id"):
            return SemanticFrameIssue("blocked", "make_chart requires payload.source_step_id from an earlier evidence-producing tool")

    return SemanticFrameIssue()


def _is_supported_trend_range(args: dict[str, Any]) -> bool:
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    if payload.get("months") not in MISSING_VALUES:
        return True
    value = str(args.get("range") or "").strip().lower()
    if not value:
        return True
    if value in {"last_6_months", "last_90d", "last_year", "last_365d"}:
        return True
    if value.startswith("last_") and value.endswith("_months"):
        try:
            months = int(value.split("_")[1])
        except (IndexError, ValueError):
            return False
        return 1 <= months <= 36
    return False


def _validate_preview_frame(args: dict[str, Any]) -> SemanticFrameIssue:
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    change_type = str(payload.get("change_type") or "").strip().lower()
    schema = PREVIEW_PAYLOAD_SCHEMAS.get(change_type)
    if schema is None:
        return SemanticFrameIssue("clarify", "I need a supported finance change_type to preview.")
    allowed = set(schema.allowed) | {"change_type"}
    unknown = sorted(key for key in payload if key not in allowed)
    if unknown:
        return SemanticFrameIssue("blocked", f"preview_finance_change payload has unsupported key(s): {', '.join(unknown)}")
    missing = [key for key in sorted(schema.required) if payload.get(key) in MISSING_VALUES]
    if missing:
        return SemanticFrameIssue("clarify", f"preview_finance_change missing required payload field(s): {', '.join(missing)}")
    for group in schema.require_one_of:
        if not any(payload.get(key) not in MISSING_VALUES for key in group):
            if change_type == "update_goal_target":
                return SemanticFrameIssue("clarify", "I need goal_id or name to update a goal target.")
            if change_type == "mark_goal_funded":
                return SemanticFrameIssue("clarify", "I need goal_id or name to mark a goal funded.")
            if change_type == "update_manual_account_balance":
                return SemanticFrameIssue("clarify", "I need account_id or account_name to preview a manual account balance update.")
            return SemanticFrameIssue("clarify", f"preview_finance_change needs one of: {', '.join(sorted(group))}")
    if schema.require_any_filter and not any(payload.get(key) not in MISSING_VALUES for key in schema.require_any_filter):
        return SemanticFrameIssue("clarify", "I need at least one filter before previewing a bulk reviewed change.")
    return SemanticFrameIssue()


def _should_merge_prior(tool_name: str, args: dict[str, Any], meta: dict[str, Any], prior_frame: dict[str, Any]) -> bool:
    if not prior_frame or prior_frame.get("tool") != tool_name:
        return False
    action = str(meta.get("context_action") or "").strip().lower()
    return action in {"followup", "correction"}


def _args_from_frame(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in (frame or {}).items()
        if key not in {"tool", "name", "inherited_keys"} and key not in FRAME_META_KEYS and value not in MISSING_VALUES
    }


def _normalize_frame_aliases(original_tool_name: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    out = _canonicalize_envelope_aliases(copy.deepcopy(args or {}))
    raw_filters = out.pop("filters", {})
    raw_payload = out.pop("payload", {})
    if raw_filters not in MISSING_VALUES and not isinstance(raw_filters, dict):
        out["_invalid_filters_type"] = type(raw_filters).__name__
    if raw_payload not in MISSING_VALUES and not isinstance(raw_payload, dict):
        out["_invalid_payload_type"] = type(raw_payload).__name__
    filters = _dict(raw_filters)
    payload = _dict(raw_payload)
    _fold_container_aliases(out, filters, payload)

    if original_tool_name == "analyze_entity":
        _fold_entity_aliases(out, filters)
        out.setdefault("view", "entity_total")
    elif original_tool_name == "compare_entity_periods":
        _fold_entity_aliases(out, filters)
        out.setdefault("view", "compare")
    elif original_tool_name == "review_savings_capacity":
        out.setdefault("view", "savings_capacity")

    if tool_name == "query_transactions":
        ambiguous_filter = out.pop("filters_by_merchant_category", None)
        if ambiguous_filter not in MISSING_VALUES and not filters:
            text_filter = str(ambiguous_filter).strip()
            if text_filter.lower() in {"grocery", "groceries"}:
                filters["category"] = "Groceries"
            else:
                filters["search"] = text_filter
        metric = str(payload.pop("metric", "") or "").strip().lower()
        if metric in {"income", "income_total"} and not filters.get("category"):
            filters["category"] = "Income"
        _move_many(out, filters, ("merchant", "category", "account", "search", "reviewed", "transaction_id"))
        filters.pop("status", None)
        if str(out.get("view") or "").lower() in {"recent", "transactions", "query_transactions"}:
            out["view"] = "list"
        if str(out.get("view") or "").lower() == "detail" and not filters.get("transaction_id"):
            out["view"] = "latest" if filters else "list"

    elif tool_name == "summarize_spending":
        ambiguous_filter = out.pop("filters_by_merchant_category", None)
        if ambiguous_filter not in MISSING_VALUES and not filters:
            text_filter = str(ambiguous_filter).strip()
            if text_filter.lower() in {"grocery", "groceries"}:
                filters["category"] = "Groceries"
            else:
                filters["merchant"] = text_filter
        comparison_period = payload.pop("comparison_period", None)
        comparison_text = str(comparison_period or "").strip().lower()
        if (
            comparison_period not in MISSING_VALUES
            and comparison_text not in {"current", "current_month", "this_month"}
            and out.get("range_b") in MISSING_VALUES
        ):
            out["range_b"] = comparison_period
            out["view"] = "compare"
        _fold_entity_aliases(out, filters)
        _move_many(out, payload, ("metric", "group_by", "months"))
        group_by = str(payload.get("group_by") or "").strip().lower()
        view = str(out.get("view") or "").strip().lower()
        if view in {
            "spending",
            "spend",
            "spending_total",
            "spend_total",
            "get_spending_total",
            "summarize_spending",
            "top_spending",
            "compare_spending",
        }:
            view = ""
            out.pop("view", None)
        top_value = payload.pop("top", None)
        if top_value not in MISSING_VALUES and out.get("limit") in MISSING_VALUES:
            out["limit"] = top_value
        payload.pop("sort_by", None)
        payload.pop("sort_order", None)
        if view == "compare" and out.get("range") not in MISSING_VALUES:
            out.setdefault("range_a", out.pop("range"))
        if out.get("range") not in MISSING_VALUES and view != "compare":
            out.pop("range_a", None)
            out.pop("range_b", None)
        if view in {"summary", "total", "expenses", "income"}:
            if view in {"expenses", "income"} and not payload.get("metric"):
                payload["metric"] = view
            out["view"] = "period_total"
        elif view in {"merchant", "category"}:
            out["view"] = "top"
            payload.setdefault("group_by", view)
        elif view in {"month", "monthly"}:
            out["view"] = "trend"
            payload.setdefault("group_by", "month")
        if not out.get("view"):
            if out.get("range_a") or out.get("range_b"):
                out["view"] = "compare"
            elif filters.get("merchant") or filters.get("category"):
                out["view"] = "entity_total"
            elif group_by in {"month", "trend"}:
                out["view"] = "trend"
            elif group_by in {"merchant", "category"}:
                out["view"] = "top"
            elif group_by == "breakdown":
                out["view"] = "breakdown"
            elif payload.get("metric"):
                out["view"] = "period_total"
        if out.get("view") == "period_total":
            category_metric = _normalize_spending_metric(filters.get("category"))
            if category_metric in {"income", "expenses", "refunds", "savings", "credit_card_payments"}:
                payload.setdefault("metric", category_metric)
                filters.pop("category", None)
        if out.get("view") in {"period_total", "breakdown"} and (filters.get("merchant") or filters.get("category")):
            out["view"] = "entity_total"
        if out.get("view") == "top" and (filters.get("merchant") or filters.get("category")):
            filter_type = "merchant" if filters.get("merchant") else "category"
            group_by = str(payload.get("group_by") or "").strip().lower()
            if not group_by or group_by == filter_type:
                out["view"] = "entity_total"
                payload.pop("group_by", None)
        if out.get("view") == "entity_total" and (filters.get("merchant") or filters.get("category")):
            filter_type = "merchant" if filters.get("merchant") else "category"
            if str(payload.get("group_by") or "").strip().lower() == filter_type:
                payload.pop("group_by", None)
        if out.get("view") == "entity_total" and not (filters.get("merchant") or filters.get("category")) and payload.get("metric"):
            out["view"] = "period_total"
        if str(payload.get("group_by") or "").lower() in {"trend", "monthly"}:
            payload["group_by"] = "month"
        if out.get("view") == "breakdown" and str(payload.get("group_by") or "").lower() == "breakdown":
            payload.pop("group_by", None)
        if payload.get("metric") not in MISSING_VALUES:
            payload["metric"] = _normalize_spending_metric(payload.get("metric"))

    elif tool_name == "finance_overview":
        _move_many(out, payload, ("metric", "focus"))

    elif tool_name == "review_budget":
        out.pop("entity_type", None)
        out.pop("entity", None)
        _move_many(out, filters, ("category",))
        budget_view = str(out.get("view") or "").strip().lower()
        if budget_view in {"budget", "review_budget", "overall_budget", "status"}:
            out.pop("view", None)
        elif budget_view in {"save", "saving", "savings", "monthly_savings", "savings_capacity"}:
            out["view"] = "savings_capacity"
        budget_metric = str(payload.get("metric") or "").strip().lower()
        if budget_metric in {"save", "saving", "savings", "monthly_savings", "savings_capacity"}:
            out["view"] = "savings_capacity"
        if not out.get("view"):
            out["view"] = "category_status" if filters.get("category") else "plan"
        elif str(out.get("view") or "").strip().lower() == "plan" and filters.get("category"):
            out["view"] = "category_status"
        elif str(out.get("view") or "").strip().lower() == "category_status" and not filters.get("category"):
            out["view"] = "plan"
        payload.clear()

    elif tool_name == "review_cashflow":
        _move_many(out, payload, ("horizon_days", "buffer_amount"))
        if out.get("mode") and not out.get("view"):
            out["view"] = out.pop("mode")
        metric = str(payload.pop("metric", "") or "").strip().lower()
        view = str(out.get("view") or "").strip().lower()
        if view in {"cashflow", "cash_flow", "review_cashflow"}:
            out["view"] = "forecast"
        if payload.pop("forecast", None) not in MISSING_VALUES and not out.get("view"):
            out["view"] = "forecast"
        if (
            metric in {"shortfall", "shortfall_risk", "risk", "shortfall_risk_assessment"}
            or payload.pop("shortfall", None) not in MISSING_VALUES
        ):
            out["view"] = "shortfall"
        filters.clear()
        out.pop("range", None)

    elif tool_name == "check_affordability":
        _move_many(out, filters, ("category",))
        _move_many(filters, payload, ("amount", "purpose", "horizon_days", "buffer_amount", "question"))
        _move_many(out, payload, ("amount", "purpose", "horizon_days", "buffer_amount", "question"))
        if str(out.get("view") or "").strip().lower() in {"check_affordability", "affordability", "afford"}:
            out["view"] = "purchase"
        if str(payload.get("metric") or "").strip().lower() in {"purchase", "affordability", "afford"}:
            payload.pop("metric", None)
        for key in list(filters):
            if key != "category":
                filters.pop(key, None)

    elif tool_name == "review_recurring":
        _move_many(out, filters, ("status",))
        _move_many(out, payload, ("all",))
        view = str(out.get("view") or "").strip().lower()
        if view in {"subscription", "subscriptions", "recurring", "recurring_summary", "review_recurring"}:
            out["view"] = "summary"
        elif view in {"change", "changed", "recurring_changes"}:
            out["view"] = "changes"
        if str(out.get("view") or "").strip().lower() in {"", "summary"}:
            out.pop("range", None)

    elif tool_name == "review_net_worth":
        _move_many(out, payload, ("interval",))
        view = str(out.get("view") or "").lower()
        if view in {"summary", "net_worth", "networth", "review_net_worth"}:
            out["view"] = "trend"
        if str(out.get("view") or "").lower() in {"balances", "delta"} and str(out.get("range") or "").strip().lower() == "current_month":
            out.pop("range", None)

    elif tool_name == "review_data_quality":
        if out.get("mode") and not out.get("view"):
            out["view"] = out.pop("mode")
        if out.get("action") and not out.get("view"):
            out["view"] = out.pop("action")
        _move_many(out, filters, ("transaction_id",))
        _move_many(out, payload, ("threshold", "include_taxonomy"))
        if filters.pop("low_confidence", None) not in MISSING_VALUES:
            out["view"] = "low_confidence"
        if str(out.get("view") or "").strip().lower() in {"data_quality", "quality", "stale", "data_health", "review_data_quality"}:
            out["view"] = "health"
        if filters.get("transaction_id") and str(out.get("view") or "").strip().lower() in {"", "health", "enrichment_summary"}:
            out["view"] = "explain_transaction"
        if str(out.get("view") or "").strip().lower() in {"health", "enrichment_summary", "low_confidence"}:
            out.pop("range", None)

    elif tool_name == "review_financial_context":
        if out.get("mode") and not out.get("view"):
            out["view"] = out.pop("mode")
        if out.get("action") and not out.get("view"):
            out["view"] = out.pop("action")
        _move_many(out, filters, ("subject_type", "subject_key"))
        _move_many(out, payload, ("question", "intent", "max_facts", "amount", "month_key"))
        _move_many(filters, payload, ("question", "intent", "max_facts", "amount", "month_key"))
        view = str(out.get("view") or "").strip().lower()
        aliases = {
            "financial_context": "relevant",
            "context": "relevant",
            "lifestyle": "lifestyle_profile",
            "profile": "lifestyle_profile",
            "friction": "friction_map",
            "leaks": "friction_map",
            "operating": "operating_plan",
            "plan": "operating_plan",
            "retrospective": "monthly_retrospective",
            "monthly_retrospective": "monthly_retrospective",
            "monthly_diary": "monthly_retrospective",
            "last_month_read": "monthly_retrospective",
            "forecast": "forecast_month_outlook.snapshot",
            "outlook": "forecast_month_outlook.snapshot",
            "money_outlook": "forecast_month_outlook.snapshot",
            "safe_to_spend": "review_cashflow.safe_to_spend",
            "cashflow_safe_to_spend": "review_cashflow.safe_to_spend",
        }
        if view in aliases:
            out["view"] = aliases[view]
        out.pop("range", None)

    elif tool_name == "manage_memory":
        payload_action = str(payload.pop("action", "") or "").strip().lower()
        payload_view = str(payload.pop("view", "") or "").strip().lower()
        if out.get("action") and not out.get("view"):
            out["view"] = out.pop("action")
        if out.get("mode") and not out.get("view"):
            out["view"] = out.pop("mode")
        if str(out.get("view") or "").strip().lower() in {"memory", "manage_memory"} and payload_view:
            out["view"] = payload_view
        if str(out.get("view") or "").strip().lower() in {"memory", "manage_memory"} and payload_action:
            out["view"] = payload_action
        _move_many(out, payload, PAYLOAD_KEYS_BY_TOOL["manage_memory"])
        filters.clear()
        out.pop("range", None)

    elif tool_name == "preview_finance_change":
        incoming_view = str(out.get("view") or "").strip().lower()
        if incoming_view and incoming_view not in {"preview", "preview_finance_change"} and not payload.get("change_type"):
            payload["change_type"] = incoming_view
        out["view"] = "preview"
        if out.get("change_type") and not payload.get("change_type"):
            payload["change_type"] = out.pop("change_type")
        if out.get("action") and not payload.get("change_type"):
            payload["change_type"] = out.pop("action")
        if out.get("mode") and not payload.get("change_type"):
            payload["change_type"] = out.pop("mode")
        _move_many(out, payload, _all_preview_payload_keys())
        _move_many(filters, payload, _all_preview_payload_keys())
        filters.clear()
        out.pop("range", None)
        payload["change_type"] = _change_type_alias(payload.get("change_type"))
        _normalize_preview_payload(payload)
        payload["change_type"] = _change_type_alias(payload.get("change_type"))

    elif tool_name == "make_chart":
        if out.get("chart_type") and not out.get("view"):
            out["view"] = out.pop("chart_type")
        if out.get("type") and not out.get("view"):
            out["view"] = out.pop("type")
        _move_many(out, payload, PAYLOAD_KEYS_BY_TOOL["make_chart"] | {"source_step_id", "chart_type", "type"})
        if str(out.get("view") or "").strip().lower() in {"net_worth", "networth", "chart", "trend"}:
            out["view"] = "line"
        out.pop("range", None)
        payload.pop("chart_type", None)
        payload.pop("type", None)

    if filters:
        if out.get("range") not in MISSING_VALUES:
            filters.pop("date_range_start", None)
            filters.pop("date_range_end", None)
        out["filters"] = filters
    if payload:
        out["payload"] = payload
    out = _drop_unconsumed_universal_noise(tool_name, out)
    return _drop_missing(out)


def _drop_unconsumed_universal_noise(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Remove selector envelope defaults that have no semantic meaning for a view.

    This only drops generic pagination/sort defaults. Domain-bearing fields like
    range, filters, and payload must survive to validation so they can be
    consumed or rejected instead of disappearing silently.
    """
    view = str(args.get("view") or "").strip().lower()
    contract = SEMANTIC_VIEW_CONTRACTS.get((tool_name, view))
    if contract is None:
        return args
    out = dict(args)
    for key in ("sort", "offset", "limit"):
        if key in out and key not in contract.allowed:
            out.pop(key, None)
    return out


def _normalize_spending_metric(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "total": "expenses",
        "total_spend": "expenses",
        "total_spent": "expenses",
        "total_spending": "expenses",
        "spending": "expenses",
        "spend": "expenses",
        "expense": "expenses",
        "expense_total": "expenses",
        "expenses_total": "expenses",
        "total_for_year": "expenses",
        "year_total": "expenses",
        "charges": "expenses",
        "income_total": "income",
        "refund": "refunds",
        "credit_card_payment": "credit_card_payments",
        "cc_payments": "credit_card_payments",
        "savings_transfer": "savings",
    }
    return aliases.get(text, text)


def _canonicalize_envelope_aliases(args: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "time_period": "range",
        "period": "range",
        "date_range": "range",
        "top_n": "limit",
        "view_type": "view",
        "period_1": "range_a",
        "period1": "range_a",
        "range_1": "range_a",
        "range_a_period": "range_a",
        "period_2": "range_b",
        "period2": "range_b",
        "range_2": "range_b",
        "range_b_period": "range_b",
        "horizon": "horizon_days",
        "purchase_amount": "amount",
        "price": "amount",
        "cost": "amount",
        "content": "text",
        "item_description": "purpose",
        "item": "purpose",
        "merchant_name": "merchant",
        "category_name": "category",
        "filters_by_merchant": "merchant",
        "filter_by_merchant": "merchant",
        "merchant_filter": "merchant",
        "filters_by_category": "category",
        "filter_by_category": "category",
        "category_filter": "category",
        "target_category": "category",
        "new_category": "category",
        "account_name": "account",
        "source_entity": "merchant",
        "target_entity": "category",
        "chart_type": "chart_type",
    }
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        canonical = aliases.get(str(key or "").strip().lower(), str(key or "").strip())
        if isinstance(value, dict):
            value = _canonicalize_envelope_aliases(value)
        if canonical not in out or out.get(canonical) in MISSING_VALUES:
            out[canonical] = value
    return out


def _fold_container_aliases(out: dict[str, Any], filters: dict[str, Any], payload: dict[str, Any]) -> None:
    for container in (filters, payload):
        for key in ("range", "month"):
            value = container.pop(key, None)
            if value not in MISSING_VALUES and out.get("range") in MISSING_VALUES:
                out["range"] = value
        for key in ("range_a", "range_b"):
            value = container.pop(key, None)
            if value not in MISSING_VALUES and out.get(key) in MISSING_VALUES:
                out[key] = value
        for key in ("limit", "offset", "sort"):
            value = container.pop(key, None)
            if value not in MISSING_VALUES and out.get(key) in MISSING_VALUES:
                out[key] = value
    typed = str(filters.pop("type", "") or "").strip().lower()
    canonical = filters.pop("canonical", None)
    if typed in {"merchant", "category", "account"} and canonical not in MISSING_VALUES and filters.get(typed) in MISSING_VALUES:
        filters[typed] = canonical
    elif typed and payload.get("metric") in MISSING_VALUES:
        payload["metric"] = typed
    for key in ("metric", "metric_type"):
        value = filters.pop(key, None)
        if value not in MISSING_VALUES and payload.get("metric") in MISSING_VALUES:
            payload["metric"] = value
    group_by = filters.pop("group_by", None)
    if group_by not in MISSING_VALUES and payload.get("group_by") in MISSING_VALUES:
        payload["group_by"] = group_by


def _apply_frame_defaults(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(args or {})
    filters = _dict(out.get("filters"))
    payload = _dict(out.get("payload"))
    if tool_name == "query_transactions":
        out.setdefault("view", "list")
        if out.get("view") == "latest":
            out.setdefault("limit", 1)
            out.setdefault("sort", "date_desc")
    elif tool_name == "summarize_spending":
        if not out.get("view"):
            out["view"] = "entity_total" if filters.get("merchant") or filters.get("category") else "period_total"
        view = str(out.get("view") or "").lower()
        if view in {"period_total", "entity_total", "top", "breakdown"}:
            out.setdefault("range", "current_month")
        elif view == "trend":
            out.setdefault("range", "last_6_months")
            payload.setdefault("group_by", "month")
        elif view == "compare":
            out.setdefault("range_a", "current_month")
            out.setdefault("range_b", "last_month")
        if view in {"period_total", "entity_total"}:
            payload.setdefault("metric", "expenses" if view == "entity_total" else "summary")
        if view in {"top", "breakdown"} and out.get("sort") == "date_desc":
            out["sort"] = "amount_desc"
    elif tool_name == "finance_overview":
        out.setdefault("view", "snapshot")
    elif tool_name == "review_budget":
        out.setdefault("view", "category_status" if filters.get("category") else "plan")
        out.setdefault("range", "current_month")
    elif tool_name == "review_cashflow":
        out.setdefault("view", "forecast")
    elif tool_name == "check_affordability":
        out.setdefault("view", "purchase")
    elif tool_name == "review_recurring":
        out.setdefault("view", "summary")
        out.setdefault("limit", 25)
    elif tool_name == "review_net_worth":
        out.setdefault("view", "trend")
        if out.get("view") == "trend":
            out.setdefault("range", "last_6_months")
            payload.setdefault("interval", "monthly")
    elif tool_name == "review_data_quality":
        out.setdefault("view", "health")
    elif tool_name == "review_financial_context":
        out.setdefault("view", "relevant")
    elif tool_name == "manage_memory":
        if not out.get("view") and payload.get("text"):
            out["view"] = "remember"
    elif tool_name == "preview_finance_change":
        out.setdefault("view", "preview")
    elif tool_name == "make_chart":
        out.setdefault("view", "line")
    if payload:
        out["payload"] = payload
    return _drop_missing(out)


def _frame_meta(source: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key in FRAME_META_KEYS
        if (value := (source or {}).get(key)) not in MISSING_VALUES
    }


def _fold_entity_aliases(args: dict[str, Any], filters: dict[str, Any]) -> None:
    subject_type_value = args.pop("subject_type", "")
    entity_type_value = args.pop("entity_type", "")
    subject_type = str(subject_type_value or entity_type_value or "").strip().lower()
    subject = args.pop("subject", None)
    entity = args.pop("entity", None)
    if args.get("merchant") and not filters.get("merchant"):
        filters["merchant"] = args.pop("merchant")
    if args.get("category") and not filters.get("category"):
        filters["category"] = args.pop("category")
    entity_value = entity if entity not in MISSING_VALUES else subject
    if entity_value not in MISSING_VALUES:
        if subject_type == "category":
            filters.setdefault("category", entity_value)
        else:
            filters.setdefault("merchant", entity_value)


def _move_many(source: dict[str, Any], target: dict[str, Any], keys: set[str] | tuple[str, ...]) -> None:
    for key in keys:
        value = source.pop(key, None)
        if value not in MISSING_VALUES and target.get(key) in MISSING_VALUES:
            target[key] = value


def _normalize_preview_payload(payload: dict[str, Any]) -> None:
    if payload.get("action") and not payload.get("change_type"):
        payload["change_type"] = payload.pop("action")
    if payload.get("mode") and not payload.get("change_type"):
        payload["change_type"] = payload.pop("mode")
    if payload.get("view") and not payload.get("change_type"):
        payload["change_type"] = payload.pop("view")
    if payload.get("merchant_name") and not payload.get("merchant"):
        payload["merchant"] = payload.pop("merchant_name")
    if payload.get("source_category") and not payload.get("merchant"):
        payload["merchant"] = payload.pop("source_category")
    if payload.get("target_category") and not payload.get("category"):
        payload["category"] = payload.pop("target_category")
    if payload.get("budget_category") and not payload.get("category"):
        payload["category"] = payload.pop("budget_category")
    if payload.get("account") and not payload.get("account_name"):
        payload["account_name"] = payload.pop("account")
    change_type = str(payload.get("change_type") or "").strip().lower()
    if change_type == "create_rule" and payload.get("merchant") and not payload.get("pattern"):
        payload["pattern"] = payload.pop("merchant")
    if change_type == "rename_merchant" and payload.get("merchant") and not payload.get("old_name"):
        payload["old_name"] = payload.pop("merchant")
    if change_type == "rename_merchant" and payload.get("entity") and not payload.get("old_name"):
        payload["old_name"] = payload.pop("entity")
    if change_type == "create_goal" and payload.get("goal_name") and not payload.get("name"):
        payload["name"] = payload.pop("goal_name")
    payload.pop("entity_type", None)
    if isinstance(payload.get("category"), str):
        payload["category"] = _display_label(payload["category"])


def _all_preview_payload_keys() -> set[str]:
    keys = {
        "change_type",
        "action",
        "mode",
        "merchant_name",
        "target_category",
        "new_category",
        "source_category",
        "budget_category",
        "account",
        "goal_name",
        "entity",
        "entity_type",
    }
    for schema in PREVIEW_PAYLOAD_SCHEMAS.values():
        keys.update(schema.allowed)
    return keys


def _change_type_alias(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "recategorize": "bulk_recategorize",
        "recategory": "bulk_recategorize",
        "reclassify": "bulk_recategorize",
        "bulk_category": "bulk_recategorize",
        "move_category": "bulk_recategorize",
        "move": "bulk_recategorize",
        "rule": "create_rule",
        "rule_creation": "create_rule",
        "budget": "set_budget",
        "set_budget": "set_budget",
        "update": "set_budget",
        "set": "set_budget",
        "goal": "create_goal",
        "create": "create_goal",
        "note": "set_transaction_note",
        "tags": "set_transaction_tags",
        "reviewed": "mark_reviewed",
        "manual_balance": "update_manual_account_balance",
        "split": "split_transaction",
        "rename": "rename_merchant",
    }
    return aliases.get(text, text)


def _display_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.title() if text == text.lower() else text


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        cleaned_key = str(key or "").strip()
        if not cleaned_key or item in MISSING_VALUES:
            continue
        if isinstance(item, str) and item.strip().lower() in {"null", "none"}:
            continue
        out[cleaned_key] = copy.deepcopy(item)
    return out


def _drop_missing(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(item)
        for key, item in (value or {}).items()
        if item not in MISSING_VALUES and key not in FRAME_META_KEYS and key not in {"tool", "name", "id", "depends_on"}
    }


__all__ = [
    "FILTER_KEYS_BY_TOOL",
    "PAYLOAD_KEYS_BY_TOOL",
    "PREVIEW_CHANGE_TO_TOOL",
    "PREVIEW_OPTIONAL_KEYS",
    "PREVIEW_PAYLOAD_SCHEMAS",
    "PREVIEW_REQUIRED_KEYS",
    "SEMANTIC_FRAME_SCHEMAS",
    "SEMANTIC_VIEW_CONTRACTS",
    "SemanticFrameIssue",
    "SemanticFrameResult",
    "SemanticFrameSchema",
    "SemanticViewContract",
    "TOP_LEVEL_KEYS_BY_TOOL",
    "complete_semantic_frame",
    "latest_prior_frame",
    "normalize_prior_frame",
    "normalize_semantic_frame_args",
    "primary_semantic_frame",
    "semantic_frame_from_args",
    "validate_semantic_frame",
]
