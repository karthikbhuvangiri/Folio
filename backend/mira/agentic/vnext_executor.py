from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from typing import Any, Callable

from mira.agentic.confidence import compact_confidence_summary
from mira.agentic.schemas import EvidencePacket, ToolPlanStep, ValidationResult
from mira.agentic.semantic_catalog import is_semantic_tool
from mira.agentic.semantic_frames import semantic_frame_from_args
from mira.agentic.semantic_tool_adapter import adapt_semantic_execution


ExecuteTool = Callable[[str, dict[str, Any], str | None], Any]


def execute_vnext_plan(
    validation: ValidationResult,
    *,
    question: str,
    profile: str | None = None,
    execute_tool_fn: Callable[..., Any] | None = None,
    cache: dict | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> EvidencePacket:
    evidence: EvidencePacket | None = None
    for event in iter_execute_vnext_events(
        validation,
        question=question,
        profile=profile,
        execute_tool_fn=execute_tool_fn,
        cache=cache,
    ):
        if event.get("type") == "evidence":
            evidence = event.get("evidence")
        elif event_callback is not None:
            event_callback(copy.deepcopy(event))
    if evidence is None:
        raise ValueError("vNext executor did not produce evidence")
    return evidence


def iter_execute_vnext_events(
    validation: ValidationResult,
    *,
    question: str,
    profile: str | None = None,
    execute_tool_fn: Callable[..., Any] | None = None,
    cache: dict | None = None,
) -> Iterator[dict[str, Any]]:
    if validation.status != "ready":
        raise ValueError(f"cannot execute validation status {validation.status}")

    execute = execute_tool_fn or _default_execute_tool
    records: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    display_rows: list[dict[str, Any]] = []
    charts: list[dict[str, Any]] = []
    caveats: list[str] = []
    previous_results: dict[str, dict[str, Any]] = {}

    for step in validation.normalized_plan:
        execution_name, execution_args = _execution_call(step, previous_results)
        yield {
            "type": "tool_call",
            "step_id": step.step_id,
            "name": step.tool_name,
            "tool_name": step.tool_name,
            "execution_tool_name": execution_name,
            "args": copy.deepcopy(step.args),
            "execution_args": copy.deepcopy(execution_args),
        }
        started = time.perf_counter()
        try:
            result = execute(execution_name, execution_args, profile, cache=cache)
        except TypeError:
            result = execute(execution_name, execution_args, profile)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        status = "error" if isinstance(result, dict) and result.get("error") else "ok"
        compact_result = compact_tool_result(step.tool_name, execution_args, result)
        record = {
            "step_id": step.step_id,
            "tool_name": step.tool_name,
            "execution_tool_name": execution_name,
            "args": copy.deepcopy(execution_args),
            "semantic_args": copy.deepcopy(step.args),
            "reason": step.reason,
            "depends_on": list(step.depends_on or []),
            "status": status,
            "result": compact_result,
            "ms": elapsed_ms,
        }
        records.append(record)
        previous_results[step.step_id] = record
        facts.extend(_facts_from_result(record))
        rows.extend(_rows_from_result(compact_result))
        display_rows.extend(_display_rows_from_result(result))
        charts.extend(_charts_from_result(compact_result, step))
        caveats.extend(_caveats_from_result(compact_result))
        yield {
            "type": "tool_result",
            "step_id": step.step_id,
            "name": step.tool_name,
            "tool_name": step.tool_name,
            "execution_tool_name": execution_name,
            "duration_ms": elapsed_ms,
            "status": status,
        }

    yield {
        "type": "evidence",
        "evidence": EvidencePacket(
            question=question,
            tool_results=records,
            facts=facts,
            rows=rows[:100],
            display_rows=display_rows[:100],
            charts=charts,
            caveats=_dedupe_text(caveats),
            provenance={
                "planner": "agentic_vnext_selector",
                "tool_plan": validation.decision.to_dict().get("tool_plan", []),
                "normalized_tool_plan": [step.to_dict() for step in validation.normalized_plan],
                "semantic_frames": [
                    semantic_frame_from_args(step.tool_name, step.args or {})
                    for step in validation.normalized_plan
                    if semantic_frame_from_args(step.tool_name, step.args or {})
                ],
                "grounded_entities": copy.deepcopy(validation.grounded_entities),
                "tool_result_count": len(records),
                "legacy_router_used": False,
            },
        ),
    }


def compact_tool_result(tool_name: str, args: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"tool": tool_name, "args": copy.deepcopy(args), "value": compact_value(result)}

    out: dict[str, Any] = {"tool": tool_name, "args": copy.deepcopy(args)}
    scalar_keys = (
        "error",
        "summary",
        "merchant_query",
        "category",
        "merchant",
        "range",
        "month",
        "start",
        "end",
        "total",
        "amount",
        "count",
        "txn_count",
        "row_count",
        "total_matching_transactions",
        "income",
        "expenses",
        "net",
        "net_flow",
        "balance",
        "budget",
        "actual",
        "remaining",
        "metric_id",
        "metric_definition_summary",
        "calculation_basis",
        "confidence",
        "status",
        "metric",
        "value",
        "saved",
        "updated",
        "forgot",
        "reason",
        "confirmation_id",
        "rows_affected",
    )
    for key in scalar_keys:
        if key in result and result.get(key) not in (None, "", [], {}):
            out[key] = compact_value(result.get(key))

    for key in ("filters", "data_quality", "contract", "memory", "memory_trace", "compact_memory", "compact_memory_trace"):
        if isinstance(result.get(key), dict):
            out[key] = compact_mapping(result[key], max_items=8)

    for key in ("caveats", "known_caveats", "sample_transaction_ids", "matched_merchants"):
        if isinstance(result.get(key), list):
            out[key] = [compact_value(item) for item in result[key][:8]]

    for key in ("recent", "rows", "samples", "items", "data", "transactions", "preview_changes", "categories", "merchants", "budgets", "series", "memories", "facts"):
        if isinstance(result.get(key), list):
            out[key] = [
                compact_transaction_row(item) if key in {"recent", "rows", "data", "transactions"} else compact_mapping(item, max_items=12)
                for item in result[key][:8]
                if isinstance(item, dict)
            ]

    confidence_summary = compact_confidence_summary(result)
    if confidence_summary:
        out["confidence_summary"] = confidence_summary

    for key in ("labels", "values"):
        if isinstance(result.get(key), list):
            out[key] = [compact_value(item) for item in result[key][:36]]

    if result.get("_chart"):
        out["_chart"] = True
        for key in ("type", "title", "labels", "values", "series", "annotations", "series_name", "unit"):
            if key in result:
                out[key] = compact_value(result[key])

    if len(out) <= 2:
        out["result"] = compact_mapping(result, max_items=16)
    return out


def compact_transaction_row(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "transaction_id",
        "date",
        "description",
        "merchant_name",
        "merchant_key",
        "amount",
        "category",
        "transaction_type",
        "type",
        "account",
        "note",
        "tags",
    )
    return {
        key: compact_value(value.get(key))
        for key in keys
        if value.get(key) not in (None, "", [], {})
    }


def compact_display_row(value: dict[str, Any]) -> dict[str, Any]:
    transaction_markers = {
        "id",
        "transaction_id",
        "date",
        "description",
        "merchant_name",
        "merchant_key",
        "account",
    }
    if any(key in value for key in transaction_markers):
        return compact_transaction_row(value)
    return compact_mapping(value, max_items=12)


def _display_rows_from_result(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("transactions", "data", "recent", "rows", "items", "categories", "merchants", "budgets"):
        values = result.get(key)
        if isinstance(values, list):
            rows.extend(
                compact_display_row(item) if isinstance(item, dict) else {}
                for item in values[:50]
                if isinstance(item, dict)
            )
            break
    return [row for row in rows if row]


def compact_mapping(value: dict[str, Any], *, max_items: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= max_items:
            out["truncated"] = True
            break
        if str(key).startswith("_"):
            continue
        out[str(key)] = compact_value(item)
    return out


def compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return compact_mapping(value, max_items=12)
    if isinstance(value, list):
        return [compact_value(item) for item in value[:8]]
    if isinstance(value, str):
        text = " ".join(value.split())
        return text[:500] + "..." if len(text) > 500 else text
    return value


def pending_write_from_evidence(evidence: EvidencePacket) -> dict | None:
    for record in evidence.tool_results:
        execution_name = str(record.get("execution_tool_name") or "")
        semantic_name = str(record.get("tool_name") or "")
        if not (execution_name.startswith("preview_") or semantic_name.startswith("preview_") or semantic_name == "preview_finance_change"):
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        if not any(key in result for key in ("confirmation_id", "preview_changes", "rows_affected")):
            continue
        return {
            "confirmation_id": result.get("confirmation_id"),
            "preview_changes": result.get("preview_changes", []),
            "rows_affected": result.get("rows_affected", result.get("count", 0)),
            "samples": result.get("samples", []),
            "summary": result.get("summary"),
        }
    return None


def tool_trace_from_evidence(evidence: EvidencePacket) -> list[dict]:
    return [
        {
            "name": record.get("tool_name"),
            "execution_tool_name": record.get("execution_tool_name"),
            "args": copy.deepcopy(record.get("semantic_args") or record.get("args") or {}),
            "duration_ms": record.get("ms", 0),
            "status": record.get("status"),
        }
        for record in evidence.tool_results
    ]


def data_from_evidence(evidence: EvidencePacket, pending_write: dict | None) -> tuple[Any, str | None]:
    if pending_write:
        return pending_write.get("samples") or evidence.rows, "write_preview"
    if evidence.display_rows:
        return evidence.display_rows, "agentic_vnext_tools"
    if evidence.rows:
        return evidence.rows, "agentic_vnext_tools"
    return None, None


def evidence_summary(evidence: EvidencePacket) -> dict[str, Any]:
    return {
        "facts": copy.deepcopy(evidence.facts[:6]),
        "row_count": len(evidence.rows),
        "display_row_count": len(evidence.display_rows),
        "chart_count": len(evidence.charts),
        "caveats": list(evidence.caveats or []),
        "provenance": copy.deepcopy(evidence.provenance),
    }


def chart_from_evidence(evidence: EvidencePacket) -> dict | None:
    if evidence.charts:
        return copy.deepcopy(evidence.charts[0])
    return None


def _execution_call(step: ToolPlanStep, previous_results: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    if is_semantic_tool(step.tool_name):
        execution = adapt_semantic_execution(step.tool_name, step.args or {})
        if execution.registry_tool == "plot_chart":
            return "plot_chart", _plot_args(execution.registry_args, previous_results)
        return execution.registry_tool, copy.deepcopy(execution.registry_args)
    if step.tool_name == "plot_chart":
        return "plot_chart", _plot_args(step.args or {}, previous_results)
    return step.tool_name, copy.deepcopy(step.args or {})


def _plot_args(args: dict[str, Any], previous_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_id = str(args.get("source_step_id") or "")
    source_result = previous_results.get(source_id, {}).get("result")
    chart_args = {
        "type": args.get("chart_type") or args.get("type") or "line",
        "title": args.get("title") or "",
        "series_name": args.get("series_name") or "",
        "unit": args.get("unit") or "currency",
    }
    labels, values = _labels_values(source_result)
    if labels:
        chart_args["labels"] = labels
        chart_args["values"] = values
    return chart_args


def _labels_values(result: Any) -> tuple[list[str], list[float]]:
    if not isinstance(result, dict):
        return [], []
    labels = result.get("labels")
    values = result.get("values")
    if isinstance(labels, list) and isinstance(values, list):
        out_labels = [str(label) for label in labels]
        out_values = []
        for value in values:
            try:
                out_values.append(float(value or 0))
            except (TypeError, ValueError):
                out_values.append(0.0)
        return out_labels, out_values
    series = result.get("series") if isinstance(result.get("series"), list) else []
    if series:
        labels = []
        values = []
        for row in series:
            if not isinstance(row, dict):
                continue
            label = row.get("month") or row.get("date") or row.get("label")
            value = _first_number(row, ("total", "value", "net_worth", "balance", "amount"))
            if label is not None and value is not None:
                labels.append(str(label))
                values.append(float(value))
        return labels, values
    if result.get("range_a") and result.get("range_b") and result.get("total_a") is not None and result.get("total_b") is not None:
        return [str(result.get("range_a")), str(result.get("range_b"))], [
            float(result.get("total_a") or 0),
            float(result.get("total_b") or 0),
        ]
    if result.get("budget") is not None and result.get("actual") is not None:
        return ["Actual", "Budget"], [float(result.get("actual") or 0), float(result.get("budget") or 0)]
    budgets = result.get("budgets") if isinstance(result.get("budgets"), list) else []
    if budgets:
        labels = []
        values = []
        for row in budgets:
            if not isinstance(row, dict):
                continue
            label = row.get("category") or row.get("name")
            value = _first_number(row, ("amount", "budget", "limit", "value"))
            if label is not None and value is not None:
                labels.append(str(label))
                values.append(float(value))
        return labels, values
    items = result.get("items") if isinstance(result.get("items"), list) else []
    if items:
        labels = []
        values = []
        for row in items:
            if not isinstance(row, dict):
                continue
            label = row.get("display_name") or row.get("merchant") or row.get("merchant_name") or row.get("name")
            value = _first_number(row, ("amount", "monthly_amount", "total_monthly", "value"))
            if value is None and row.get("amount_cents") is not None:
                value = float(row.get("amount_cents") or 0) / 100
            if label is not None and value is not None:
                labels.append(str(label))
                values.append(float(value))
        return labels, values
    categories = result.get("categories") if isinstance(result.get("categories"), list) else []
    if categories:
        labels = []
        values = []
        for row in categories:
            if not isinstance(row, dict):
                continue
            label = row.get("category") or row.get("name")
            value = _first_number(row, ("total", "amount", "net", "value"))
            if label is not None and value is not None:
                labels.append(str(label))
                values.append(float(value))
        return labels, values
    merchants = result.get("merchants") if isinstance(result.get("merchants"), list) else []
    if merchants:
        labels = []
        values = []
        for row in merchants:
            if not isinstance(row, dict):
                continue
            label = row.get("merchant") or row.get("name")
            value = _first_number(row, ("total", "amount", "net", "value"))
            if label is not None and value is not None:
                labels.append(str(label))
                values.append(float(value))
        return labels, values
    return [], []


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if row.get(key) is None:
            continue
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _facts_from_result(record: dict[str, Any]) -> list[dict[str, Any]]:
    result = record.get("result")
    if not isinstance(result, dict):
        return [{"tool": record.get("tool_name"), "value": result}]
    result_facts = result.get("facts")
    if isinstance(result_facts, list):
        facts: list[dict[str, Any]] = []
        for item in result_facts[:8]:
            if not isinstance(item, dict):
                continue
            facts.append({
                "step_id": record.get("step_id"),
                "tool": record.get("tool_name"),
                "execution_tool": record.get("execution_tool_name"),
                **copy.deepcopy(item),
            })
        if facts:
            return facts
    fact: dict[str, Any] = {
        "step_id": record.get("step_id"),
        "tool": record.get("tool_name"),
        "execution_tool": record.get("execution_tool_name"),
    }
    if result.get("summary"):
        fact["summary"] = result.get("summary")
    for key, value in result.items():
        if key.startswith("_") or key in {"args", "transactions", "recent", "rows", "items", "series", "categories", "preview_changes", "samples", "contract"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            fact[key] = value
    return [fact]


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("transactions", "recent", "rows", "items", "data", "merchants", "categories", "budgets"):
        values = result.get(key)
        if isinstance(values, list):
            rows.extend(copy.deepcopy([item for item in values if isinstance(item, dict)]))
    return rows


def _charts_from_result(result: Any, step: ToolPlanStep) -> list[dict[str, Any]]:
    if isinstance(result, dict) and result.get("_chart"):
        chart = copy.deepcopy(result)
        chart["step_id"] = step.step_id
        return [chart]
    return []


def _caveats_from_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    caveats = []
    for key in ("caveats", "known_caveats"):
        values = result.get(key)
        if isinstance(values, list):
            caveats.extend(str(item) for item in values if str(item or "").strip())
    if isinstance(result.get("contract"), dict) and isinstance(result["contract"].get("caveats"), list):
        caveats.extend(str(item) for item in result["contract"]["caveats"] if str(item or "").strip())
    if result.get("error"):
        caveats.append(str(result.get("error")))
    return caveats


def _dedupe_text(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if text and key not in seen:
            out.append(text)
            seen.add(key)
    return out


def _default_execute_tool(name: str, args: dict[str, Any], profile: str | None, cache: dict | None = None) -> Any:
    from copilot_tools import execute_tool

    return execute_tool(name, args, profile, cache=cache)


__all__ = [
    "chart_from_evidence",
    "compact_tool_result",
    "data_from_evidence",
    "evidence_summary",
    "execute_vnext_plan",
    "iter_execute_vnext_events",
    "pending_write_from_evidence",
    "tool_trace_from_evidence",
]
