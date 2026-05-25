<!-- folio-demo-advisor-read
{
  "action_plan": [
    {
      "action": "Use the leakage check as the first gate before broad category cuts; fix fee, interest, or duplicate-row friction only when the evidence shows it.",
      "pain": "low",
      "rank": 1,
      "thesis_id": "avoidable_leakage_status",
      "title": "Check preventable leakage first",
      "tradeoff": "Do not force a leakage story when the check is clean.",
      "why": "The leakage check is clear, so do not invent fee friction as the main fix."
    },
    {
      "action": "Confirm the current income source labels and whether the recent income stream should be treated as stable.",
      "pain": "medium",
      "rank": 2,
      "thesis_id": "income_continuity_uncertain",
      "title": "Verify income continuity before relying on the plan",
      "tradeoff": "Do not treat liquidity as the whole answer when forward income is still uncertain.",
      "why": "Income continuity/source labeling needs verification before trusting forward assumptions."
    },
    {
      "action": "Assign the available monthly capacity to explicit goals before deciding whether to invest, pay debt faster, or loosen spending.",
      "pain": "low",
      "rank": 3,
      "thesis_id": "goal_capacity_reality",
      "title": "Turn planning capacity into named goals",
      "tradeoff": "Do not call capacity a funded plan until the goal targets exist.",
      "why": "The read must state what monthly capacity is actually available before explicit goals are configured after reconciled operating burn, leakage, and debt movement context. Goal capacity starts with $2,087.75 of monthly room after reconciled operating burn."
    },
    {
      "action": "Verify the fixed commitments that make up the floor, then judge flexible spending only after that floor is covered.",
      "pain": "low",
      "rank": 4,
      "thesis_id": "fixed_floor_matters",
      "title": "Anchor the month to the fixed floor",
      "tradeoff": "Do not compare every month against raw spending without separating structural obligations.",
      "why": "The fixed monthly floor is $1,825.00."
    },
    {
      "action": "Compare, renegotiate, or monitor recurring provider costs before treating them as day-to-day overspending.",
      "pain": "low",
      "rank": 5,
      "thesis_id": "recurring_service_review",
      "title": "Review recurring services like vendors",
      "tradeoff": "Do not reduce coverage or service quality just to make the number smaller.",
      "why": "Recurring service or provider costs are review/quote candidates, not day-to-day overspending."
    }
  ],
  "advisor_think": "0",
  "as_of": "2026-05-25",
  "cards": [
    {
      "action_label": "Walk me through it",
      "detail": "",
      "followup_type": "normal_month",
      "icon": "calendar_month",
      "id": "normal_month",
      "kicker": "Baseline",
      "question": "Walk me through my normal month from Mira's read.",
      "rows": [
        {
          "detail": "The income base I would plan around.",
          "label": "Average monthly income",
          "value": "$4,203.34"
        },
        {
          "detail": "Event-adjusted so trip noise does not become lifestyle drift.",
          "label": "Normal spending",
          "value": "$2,115.59"
        },
        {
          "detail": "The monthly hurdle after the fixed floor is respected.",
          "label": "Reconciled operating burn",
          "value": "$2,115.59"
        },
        {
          "detail": "Cover this before judging flexible spend.",
          "label": "Fixed monthly floor",
          "value": "$1,825.00"
        },
        {
          "detail": "Tune this after the floor is safe.",
          "label": "Visible flexible spend",
          "value": "$290.59"
        },
        {
          "detail": "Planning room, not a finished goal plan.",
          "label": "Room before configured goals",
          "value": "$2,087.75"
        }
      ],
      "summary": "The baseline I would actually use: income, fixed floor, normal flexible spend, recurring commitments, and unassigned room.",
      "title": "Month to plan around",
      "tradeoff": ""
    },
    {
      "action_label": "Find softer cuts",
      "detail": "",
      "followup_type": "money_map",
      "icon": "route",
      "id": "money_map",
      "kicker": "Money map",
      "question": "Walk me through the money map from Mira's read, and what can be reduced without overreacting?",
      "rows": [
        {
          "detail": "Structural floor - Harbor View Apartments $12,775.00",
          "label": "Housing",
          "value": "$1,825.00"
        },
        {
          "detail": "Flexible living - Sunbeam Market $2,034.15",
          "label": "Groceries",
          "value": "$290.59"
        }
      ],
      "summary": "Before cutting anything, separate ordinary living from event noise, private rhythms, and vendors worth reviewing.",
      "title": "Money map",
      "tradeoff": ""
    },
    {
      "action_label": "Why this first?",
      "detail": "Use the leakage check as the first gate before broad category cuts; fix fee, interest, or duplicate-row friction only when the evidence shows it.",
      "followup_type": "first_move",
      "icon": "low_priority",
      "id": "first_move",
      "kicker": "Action",
      "question": "What should I do first from Mira's read, and why is it first?",
      "rows": [],
      "summary": "Check preventable leakage first",
      "title": "Do this first",
      "tradeoff": "Do not force a leakage story when the check is clean."
    },
    {
      "action_label": "Explain the risk",
      "detail": "Confirm the current income source labels and whether the recent income stream should be treated as stable.",
      "followup_type": "risk",
      "icon": "flag",
      "id": "biggest_risk",
      "kicker": "Risk",
      "question": "What is the biggest risk to my goals from Mira's read?",
      "rows": [],
      "summary": "Income continuity/source labeling needs verification before trusting forward assumptions.",
      "title": "Assumption to verify",
      "tradeoff": ""
    },
    {
      "action_label": "Check freshness",
      "detail": "",
      "followup_type": "changes",
      "icon": "check_circle",
      "id": "what_changed",
      "kicker": "Freshness",
      "question": "What changed since Mira's read?",
      "rows": [],
      "summary": "No stored fact changes since this read.",
      "title": "What changed",
      "tradeoff": ""
    }
  ],
  "generated_at": "2026-05-25T20:08:55Z",
  "generation_summary": {
    "candidate_thesis_count": 16,
    "coverage": "14/14",
    "failure_reasons": [],
    "lens_read_count": 8,
    "memo_chars": 5984,
    "missing_theses": []
  },
  "id": "demo-advisor-read-jessica",
  "model": "gemma4:e4b",
  "profile": "jessica",
  "quality": {
    "applicable_required_theses": [
      "period_reliability_matters",
      "cash_flow_compression_matters",
      "money_map_baseline",
      "category_ledger_matters",
      "merchant_lifecycle_matters",
      "goal_capacity_reality",
      "savings_scenarios_are_options",
      "liquidity_not_primary_risk",
      "fixed_floor_matters",
      "income_continuity_uncertain",
      "event_noise_exclusion",
      "avoidable_leakage_status",
      "data_quality_limits_precision",
      "missing_data_caveats"
    ],
    "coverage_count": 14,
    "failure_reasons": [],
    "forbidden_phrase_hits": [],
    "goal_capacity_goal_status_missing": false,
    "invalid_evidence_ids": [],
    "loose_approximation_hits": [],
    "missing_memo_theme_markers": [],
    "missing_theses": [],
    "non_applicable_theses": [
      "external_transfer_labeling",
      "private_discretionary_pause",
      "private_discretionary_rhythm",
      "fees_inspection_first",
      "small_purchase_consolidation",
      "recurring_service_review"
    ],
    "ok": true,
    "raw_field_name_hits": [],
    "required_count": 14,
    "score": 140,
    "shaming_hits": [],
    "unknown_theses": [],
    "unsupported_numbers": []
  },
  "run_status": "ok",
  "source": "local_ollama_product_path",
  "theses": [
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:money_flow_baseline:summary",
        "metric:monthly_operating_statement:summary"
      ],
      "numeric_claims": [],
      "paragraph": "Where the money normally goes should come before risk ranking: average monthly income is $4,203.34, normal spending is $2,115.59, reconciled operating burn is $2,115.59, the fixed monthly floor is $1,825.00, and visible flexible spending is $290.59.",
      "stance": "",
      "summary": "Where the money normally goes starts with a $2,115.59 event-adjusted spending baseline.",
      "thesis_id": "money_map_baseline"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:category_advisor_ledger:1",
        "metric:category_advisor_ledger:2"
      ],
      "numeric_claims": [],
      "paragraph": "The category ledger should explain where the money goes by category and merchant: Housing shows $1,825.00 per month and $1,825.00 average ticket size, so Mira should distinguish repeat pressure from one-off noise before recommending cuts.",
      "stance": "",
      "summary": "The category ledger should explain Housing, not just name it.",
      "thesis_id": "category_ledger_matters"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:merchant_lifecycle:1",
        "metric:merchant_lifecycle:2",
        "metric:merchant_lifecycle:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The read must notice merchant lifecycle patterns such as top, new, dormant, or split-label merchants. Merchant lifecycle matters because there are 2 merchants in the advisor view, the top merchant is Harbor View Apartments, and split-label groups need cleanup before Mira treats merchant drift as a behavior change.",
      "stance": "",
      "summary": "The read must notice merchant lifecycle patterns such as top, new, dormant, or split-label merchants. Merchant lifecycle matters because top, new, dormant, and split-label merchants tell different stories.",
      "thesis_id": "merchant_lifecycle_matters"
    },
    {
      "caveat": "The latest visible month is partial and should not be annualized directly.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:advisor_period_reliability:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The latest visible month is partial, and the analysis period is best anchored by the seven complete months. The analysis covers 8.0 visible months, with 7.0 complete months.",
      "stance": "",
      "summary": "The latest visible month is partial, and the analysis period is best anchored by the seven complete months.",
      "thesis_id": "period_reliability_matters"
    },
    {
      "caveat": "Cash-flow compression is based on complete months; the latest partial month is excluded from recent complete-month rates.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:cash_flow_compression:1",
        "metric:cash_flow_compression:2",
        "metric:cash_flow_compression:3",
        "metric:cash_flow_compression:4",
        "metric:cash_flow_compression:summary"
      ],
      "numeric_claims": [],
      "paragraph": "Cash flow compression requires comparing recent complete-month cash flow against the trailing view. The reliable income period shows an average net cash flow of $2091.49, while the trailing 12 complete months show an average net cash flow of $2087.74. The recent 3 complete months show an average net cash flow of $2038.44. The cash flow rate delta from recent to trailing is -0.011.",
      "stance": "",
      "summary": "Cash flow compression requires comparing recent complete-month cash flow against the trailing view.",
      "thesis_id": "cash_flow_compression_matters"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:goal_capacity_statement:summary",
        "metric:goal_capacity_statement:7",
        "metric:monthly_operating_statement:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The read must state what monthly capacity is actually available before explicit goals are configured after reconciled operating burn, leakage, and debt movement context. The capacity before configured goals is $2,087.75. Goal capacity should be judged from the operating statement: reconciled operating burn is $2,115.59, capacity before configured goals is $2,087.75, configured goals require $0.00 monthly, and capacity after those goal targets is $2,087.75. No active goals are configured, so this is planning capacity before explicit goal targets rather than proof that a specific goal is funded.",
      "stance": "",
      "summary": "The read must state what monthly capacity is actually available before explicit goals are configured after reconciled operating burn, leakage, and debt movement context. Goal capacity starts with $2,087.75 of monthly room after reconciled operating burn.",
      "thesis_id": "goal_capacity_reality"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:avoidable_leakage:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The avoidable leakage check does not show fee, interest, or duplicate-row pressure, so Mira should not force a fee-cut thesis when the evidence points elsewhere.",
      "stance": "",
      "summary": "The leakage check is clear, so do not invent fee friction as the main fix.",
      "thesis_id": "avoidable_leakage_status"
    },
    {
      "caveat": "No travel/event-like clusters were detected in the selected range. This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:spending_event_clusters:summary",
        "metric:money_flow_baseline:summary"
      ],
      "numeric_claims": [],
      "paragraph": "Trip/event or other unusual spend should be separated from the normal lifestyle baseline. No travel/event-like clusters were detected in the selected range. Trip/event spend should be separated from the normal lifestyle baseline before deciding what actually changed.",
      "stance": "",
      "summary": "Trip/event or other unusual spend should be separated from the normal lifestyle baseline. Trip/event spend should be separated from the normal baseline.",
      "thesis_id": "event_noise_exclusion"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:floor_burn:summary",
        "metric:recurring_obligation_calendar:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The fixed monthly floor is $1,825.00, recurring commitments are $0.00, and recurring duplicate candidates are 0, so the floor should anchor the operating plan before flexible spending decisions.",
      "stance": "",
      "summary": "The fixed monthly floor is $1,825.00.",
      "thesis_id": "fixed_floor_matters"
    },
    {
      "caveat": "No matching rows were available for this metric.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:external_transfer_pressure:summary"
      ],
      "numeric_claims": [],
      "paragraph": "External transfers must be separated from lifestyle spending and labeled before they can be treated as goals, debt, support, or discretionary outflow.",
      "stance": "",
      "summary": "External transfers must be separated from lifestyle spending and labeled before they can be treated as goals, debt, support, or discretionary outflow.",
      "thesis_id": "external_transfer_labeling"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:cash_runway:1",
        "metric:cash_vs_liability_position:1",
        "metric:cash_runway:summary",
        "metric:cash_vs_liability_position:summary"
      ],
      "numeric_claims": [],
      "paragraph": "Liquidity appears stable. The current cash-like balance is $7,124.54, providing a cash runway of 99.4 days based on a normal monthly burn of $2,149.77. The total liability is $0.00, resulting in a cash minus liabilities figure of $7,124.54. Liquidity is not the main read, so cash panic should not drive the recommendation. Liquidity is strong enough that cash panic is not the main read: cash-like balance is $7,124.54, cash runway is 99.4 days against a normal monthly burn of $2,149.77, with liabilities at $0.00.",
      "stance": "",
      "summary": "Liquidity appears stable. Liquidity is strong enough that cash panic is not the main read.",
      "thesis_id": "liquidity_not_primary_risk"
    },
    {
      "caveat": "Recent material income contains unlabeled source rows.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:income_source_continuity:summary"
      ],
      "numeric_claims": [],
      "paragraph": "Income continuity/source labeling needs verification before trusting forward assumptions. The summary indicates that the latest material income source is unlabeled, which is a key point of caution for forward planning.",
      "stance": "",
      "summary": "Income continuity/source labeling needs verification before trusting forward assumptions.",
      "thesis_id": "income_continuity_uncertain"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:advisor_data_quality_profile:2",
        "metric:advisor_data_quality_profile:6",
        "metric:advisor_data_quality_profile:7",
        "metric:advisor_data_quality_profile:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The read must state data-quality limits such as low-confidence rows, missing investments, duplicates, or unreviewed transactions. Specifically, 55 transactions are unreviewed, and there are 6 instances of transaction splits. Investment holdings data is not available for this review.",
      "stance": "",
      "summary": "The read must state data-quality limits such as low-confidence rows, missing investments, duplicates, or unreviewed transactions.",
      "thesis_id": "data_quality_limits_precision"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:advisor_data_quality_profile:7",
        "metric:advisor_data_quality_profile:summary"
      ],
      "numeric_claims": [],
      "paragraph": "The recommendation must state what incomplete data could change. Investment holdings data is not available for this review.",
      "stance": "",
      "summary": "The recommendation must state what incomplete data could change.",
      "thesis_id": "missing_data_caveats"
    },
    {
      "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:savings_scenarios:1"
      ],
      "numeric_claims": [],
      "paragraph": "Savings scenarios should be treated as options, not commands: Groceries has a planning sensitivity of $29.22 per month, and the tradeoff is to avoid turning a scenario into a moral judgment.",
      "stance": "",
      "summary": "Savings scenarios should be treated as options, not commands.",
      "thesis_id": "savings_scenarios_are_options"
    },
    {
      "caveat": "No matching rows were available for this metric.",
      "confidence": "medium",
      "evidence_ids": [
        "metric:recurring_obligation_calendar:summary"
      ],
      "numeric_claims": [],
      "paragraph": "Recurring service or provider costs are review/quote candidates, not day-to-day overspending. The recurring obligation calendar currently shows no data, preventing a review of these costs.",
      "stance": "",
      "summary": "Recurring service or provider costs are review/quote candidates, not day-to-day overspending.",
      "thesis_id": "recurring_service_review"
    }
  ],
  "valid_until": "2036-05-22T20:08:55Z",
  "validator_version": "mira_advisor_lens_validator_v1",
  "version": "mira_advisor_lens_synthesis_v1"
}
-->

# Mira's Financial Read

## The Read

Cash is doing its job. With $7,124.54 in cash-like balances, $0.00 in liabilities, and 99.4 days of runway, I would not make this a cash-panic story.

The better read is capacity and control: reconciled operating burn is $2,115.59, leaving $2,087.75 before configured goals.

The leakage check is not showing fee or interest drag, so I would not invent a fee-cut thesis just to have one.

The main caveat is income continuity: I would verify the current source labels before trusting the forward plan.

## The Month I Would Plan Around
This is the month I would actually plan around: ordinary income, the fixed monthly floor, normal flexible spend, and what is left before named goals.
| Line | Amount | Mira's read |
| --- | --- | --- |
| Average monthly income | $4,203.34 | The income base I would plan around. |
| Normal spending | $2,115.59 | Trip/event spend is separated from the normal baseline, so this does not punish a one-off month. |
| Fixed floor already visible | $1,825.00 | Obligations already sitting inside normal spend. |
| Fixed floor gap | $0.00 | The extra floor Mira adds back so burn is not understated. |
| Reconciled operating burn | $2,115.59 | The real monthly hurdle after the fixed monthly floor is respected. |
| Visible flexible spend | $290.59 | The part I would tune after the floor is safe. |
| Recurring commitments | $0.00 | Worth a renewal and duplicate check, not panic. |
| Room before configured goals | $2,087.75 | Planning room, not a finished goal plan. |
No active goals are configured, so this is planning capacity before explicit goal targets rather than proof that a specific goal is funded.

## The Money Map

Before I reach for cuts, I want the map: what is structural, what is flexible, what was event-driven, what is private rhythm, and what is just a vendor worth reviewing.

| Category | Monthly avg | Recent vs prior | Avg ticket | Main drivers | Role |
| --- | --- | --- | --- | --- | --- |
| Housing | $1,825.00 | $0.00 | $1,825.00 | Harbor View Apartments $14,600.00 | Structural floor |
| Groceries | $292.20 | $75.40 | $73.05 | Sunbeam Market $2,337.58 | Flexible living |

Merchant behavior changes the advice: a new spike, a long-running vendor, and a messy label are three different problems.

| Type | Merchant | Total | Category | Last seen |
| --- | --- | --- | --- | --- |
| Top merchant | Harbor View Apartments | $14,600.00 | Housing | 2026-05-02 |
| Top merchant | Sunbeam Market | $2,337.58 | Groceries | 2026-05-26 |

I would label external transfers before judging them as lifestyle spending. Incoming external transfers are $0.00, outgoing external transfers are $0.00, and net external transfer outflow is $0.00; that movement needs a purpose label before it becomes a spending, goal, support, debt, or investing conclusion.

## Moves I Would Make First
Here is the order I would use, because low-regret fixes should come before painful cuts:
- **First: Check preventable leakage first.** The leakage check is clear, so do not invent fee friction as the main fix. Next: Use the leakage check as the first gate before broad category cuts; fix fee, interest, or duplicate-row friction only when the evidence shows it. Tradeoff: Do not force a leakage story when the check is clean.
- **Second: Verify income continuity before relying on the plan.** Income continuity/source labeling needs verification before trusting forward assumptions. Next: Confirm the current income source labels and whether the recent income stream should be treated as stable. Tradeoff: Do not treat liquidity as the whole answer when forward income is still uncertain.
- **Third: Turn planning capacity into named goals.** The read must state what monthly capacity is actually available before explicit goals are configured after reconciled operating burn, leakage, and debt movement context. Goal capacity starts with $2,087.75 of monthly room after reconciled operating burn. Next: Assign the available monthly capacity to explicit goals before deciding whether to invest, pay debt faster, or loosen spending. Tradeoff: Do not call capacity a funded plan until the goal targets exist.
- **Fourth: Anchor the month to the fixed floor.** The fixed monthly floor is $1,825.00. Next: Verify the fixed commitments that make up the floor, then judge flexible spending only after that floor is covered. Tradeoff: Do not compare every month against raw spending without separating structural obligations.
- **Fifth: Review recurring services like vendors.** Recurring service or provider costs are review/quote candidates, not day-to-day overspending. Next: Compare, renegotiate, or monitor recurring provider costs before treating them as day-to-day overspending. Tradeoff: Do not reduce coverage or service quality just to make the number smaller.

## What I Would Leave Alone
- Do not manufacture cash anxiety. Cash-like balance is $7,124.54, cash runway is 99.4 days, and liabilities are $0.00.
- Do not let a trip/event cluster become a permanent lifestyle verdict unless you confirm it is repeating.
- Savings scenarios are optional planning sensitivities, not commands: Groceries has a planning sensitivity of $29.22 per month.

## What Could Change This Read
- I would anchor trends to the reliable analysis period: it starts at 2025-10-02 after the first observed income row on 2025-10-02, and the latest month is partial.
- I would compare recent complete-month cash flow with the trailing view before calling compression structural.
- I would verify income continuity and source labels before trusting forward assumptions.
- Precision has limits: 56 visible transactions, 0 low-confidence spending rows, 0 recurring duplicate row, and 0 investment holdings are visible in this read.
- Recurring duplicate candidates are 0; review duplicates before treating the floor as final.
- Missing goals, budgets, labels, or sync data can change the read, so the recommendation should stay conditional.
