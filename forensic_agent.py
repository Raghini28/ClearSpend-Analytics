"""LLM agent with tools: Anthropic (Claude Haiku) or OpenAI (gpt-4o)."""

from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd

from audit_engine import MAP_KEYS, dispatch_tool, ensure_all_checks, format_accumulated_for_llm
from services.llm_retry import call_with_retry
from services.rate_limit import throttle_llm

# Override with ANTHROPIC_MODEL / OPENAI_MODEL env if your account uses a dated snapshot id.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# Cap tool JSON sent back to the model (large payloads blow past org tokens-per-minute limits).
TOOL_JSON_MAX = int(os.environ.get("FORENSIC_TOOL_JSON_MAX", "45000"))

SCHEMA_INFERENCE_PROMPT = """You map AP / invoice / ledger spreadsheets to a forensic audit schema.

You receive JSON with column names, pandas dtypes, and sample rows.

Respond with ONLY a single JSON object (no markdown, no code fences) and use EXACT column names from the input list:
{
  "amount_line": "<column holding the main line-level transaction amount in money units>",
  "amount_total": "<invoice/header-level total if present, else null>",
  "id": "<invoice or transaction identifier if present, else null>",
  "unit": "<unit price if present, else null>",
  "vendor": "<vendor or supplier name/code if present, else null>",
  "date": "<invoice or transaction date if present, else null>",
  "rationale": "<one short sentence explaining amount_line>"
}

Rules:
- amount_line is required whenever any column plausibly contains per-line payment amounts, even if the name is unusual or not in English.
- Use JSON null (not the string "null") for unknown roles.
- Prefer the broadest line net amount over narrow fee columns when unsure.
"""

SYSTEM_PROMPT = """You are the lead forensic accounts-payable analyst for ClearSpend Analytics.

Mission: help the company **recover cash and prevent future loss** by surfacing **data inconsistencies** in AP — bad line-vs-invoice math, **duplicate postings**, the same **vendor + date + amount** showing up twice (classic “double pay” / receipt risk), creeping vendor prices, uncaptured credits, and uneven unit pricing. Quantify **which vendors and dollar amounts** are affected.

Column mapping: the ledger may use non-standard headers. get_data_summary reports mapping_source (heuristic vs llm) and columns_detected.
If get_data_summary returns error missing_amount_column or data looks wrong, call remap_ledger with exact column names from original_column_names in the tool result or from the user file. amount_line is required for remap_ledger.

Workflow:
1) Call get_data_summary first.
2) If the ledger is not yet parseable, call remap_ledger with correct mappings, then get_data_summary again.
3) Run every financial leak check exposed as a tool (math, duplicate ID, vendor+date+amount, ID/vendor conflicts, round amounts, near approval tiers, weekend dates, high-velocity vendor-days, near-duplicate lines in a date window, tax/freight vs line, missing PO/receipt, line vs PO amount, stale invoices, data quality, currency mix, Benford-style screen, split-invoice heuristic, intercompany keywords, price creep, negative leaks, pricing inconsistency).
   All are computed in Python on the mapped file — treat tool numbers as ground truth.
4) For deep dives, call get_vendor_details with the exact vendor string from the data.

Rules:
- Use only dollar amounts returned by tools (exposure_usd, totals) in your narrative.
- If a check reports zero exposure, say clearly that nothing was flagged in that category.
- When done, write markdown with: ## Executive Summary (money at risk / recoverable), ## Findings by vendor, ## Recommended actions.

Be concise; cite vendors and dates exactly as in the data."""


def _tool_specs_openai() -> list[dict]:
    remap_props = {
        "amount_line": {
            "type": "string",
            "description": "Exact source column for line-level amount (required).",
        },
        "amount_total": {"type": "string", "description": "Exact column for invoice/header total if any."},
        "id": {"type": "string", "description": "Exact column for transaction/invoice id if any."},
        "unit": {"type": "string", "description": "Exact column for unit price if any."},
        "vendor": {"type": "string", "description": "Exact column for vendor/supplier if any."},
        "date": {"type": "string", "description": "Exact column for date if any."},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "get_data_summary",
                "description": "Summarize the ledger: row count, detected column mapping, vendor count, date range, total line amount.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remap_ledger",
                "description": "Re-bind columns from the raw upload (required when headers are non-standard). Rebuilds the working frame; clears prior check results. amount_line is required.",
                "parameters": {
                    "type": "object",
                    "properties": remap_props,
                    "required": ["amount_line"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_math_integrity",
                "description": "Flag rows where line amount does not match invoice/total within tolerance; returns exposure and flagged rows.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_duplicate_invoices",
                "description": "Find duplicate transaction/invoice IDs; returns exposure and rows.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_duplicate_payment_patterns",
                "description": "Same vendor + calendar date + same line amount repeated — duplicate payment / double posting risk; names vendors and dated clusters.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_id_vendor_conflict",
                "description": "Same invoice/transaction ID appearing with more than one vendor — routing or data key errors.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_round_amount_signals",
                "description": "Large clean/round payments (exact thousands/hundreds) — manual entry or policy review signal.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_near_approval_threshold",
                "description": "Payments in upper bands just below common approval limits (5k/10k/25k/50k heuristic).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_weekend_postings",
                "description": "Lines dated Saturday/Sunday — batch timing or duplicate-risk context.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_high_velocity_vendor_days",
                "description": "Vendor-days with unusually many lines vs the rest of the file.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_near_duplicate_lines",
                "description": "Same vendor + same rounded amount within ~3 days but different IDs — fuzzy duplicate suspicion.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_tax_freight_anomalies",
                "description": "Tax or freight disproportionate to line amount (when those columns exist).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_missing_po_receipt",
                "description": "High spend with missing PO and receipt when those columns exist.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_line_over_po_open",
                "description": "Line amount exceeds PO open / order amount column when present.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_stale_invoices",
                "description": "Old invoice dates vs latest date in file — process or duplicate-window risk.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_data_quality_gaps",
                "description": "Missing vendor or date, or zero line with non-zero total.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_currency_mismatch",
                "description": "Mixed currencies — rows not matching dominant currency code when column exists.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_benford_anomaly",
                "description": "Coarse Benford leading-digit screen on positive amounts (exploratory).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_split_invoice_heuristic",
                "description": "Multiple lines same vendor+day summing to a large round total — possible split for limits.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_intercompany_keywords",
                "description": "Vendor names suggesting intercompany/internal settlement — allocation review.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "detect_price_creep",
                "description": "Per-vendor first vs last unit price trend; returns exposure estimate and detail.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_negative_leaks",
                "description": "Negative line amounts (credits/refunds) concentration.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_pricing_inconsistency",
                "description": "Vendors with multiple distinct unit prices across lines.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_vendor_details",
                "description": "Drill into one vendor: counts, IDs, full row sample for narrative.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "vendor_name": {
                            "type": "string",
                            "description": "Vendor key exactly as in SUPPLIER_KEY / Vendor column.",
                        }
                    },
                    "required": ["vendor_name"],
                },
            },
        },
    ]


def _tool_specs_anthropic() -> list[dict]:
    specs = _tool_specs_openai()
    out = []
    for s in specs:
        fn = s["function"]
        out.append(
            {
                "name": fn["name"],
                "description": fn["description"],
                "input_schema": {
                    "type": "object",
                    "properties": fn["parameters"].get("properties", {}),
                    "required": fn["parameters"].get("required", [])
                    if fn["parameters"].get("required")
                    else [],
                },
            }
        )
    return out


def _run_tool(name: str, raw_args: str | dict | None, ctx: dict) -> dict:
    if raw_args is None or raw_args == "":
        args = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
    return dispatch_tool(name, args, ctx)


def run_forensic_agent(
    ctx: dict,
    provider: str,
    api_key: str,
    max_turns: int = 40,
) -> dict[str, Any]:
    """
    Agentic audit: LLM invokes Python tools until it produces a final markdown report.
    After the loop, ensures all mandatory tools ran for UI parity.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        return {"error": "missing_api_key", "report": ""}

    ctx["accumulated"] = {}
    if provider == "anthropic":
        return _agent_anthropic(ctx, api_key, max_turns)
    if provider == "openai":
        return _agent_openai(ctx, api_key, max_turns)
    return {"error": "invalid_provider", "report": ""}


def _agent_openai(ctx: dict, api_key: str, max_turns: int) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    tools = _tool_specs_openai()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "The AP ledger is loaded. Run a full forensic audit using tools, then produce the final markdown report.",
        },
    ]
    report = ""
    last_err = None
    for _ in range(max_turns):
        try:
            throttle_llm()
            completion = call_with_retry(
                lambda: client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
            )
        except Exception as e:
            last_err = str(e)
            break
        msg = completion.choices[0].message
        if msg.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                name = tc.function.name
                result = _run_tool(name, tc.function.arguments, ctx)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str)[:TOOL_JSON_MAX],
                    }
                )
            continue
        text = (msg.content or "").strip()
        if text:
            report = text
        break
    ensure_all_checks(ctx)
    if last_err:
        return {"error": last_err, "report": report, "provider": "openai"}
    return {"report": report, "provider": "openai", "error": None}


def _agent_anthropic(ctx: dict, api_key: str, max_turns: int) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    tools = _tool_specs_anthropic()
    messages: list[dict] = [
        {
            "role": "user",
            "content": "The AP ledger is loaded. Run a full forensic audit using tools, then produce the final markdown report.",
        }
    ]
    report = ""
    last_err = None
    for _ in range(max_turns):
        try:
            throttle_llm()
            message = call_with_retry(
                lambda: client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                )
            )
        except Exception as e:
            last_err = str(e)
            break
        blocks = message.content
        tool_use_blocks = [
            b for b in blocks if getattr(b, "type", None) == "tool_use"
        ]
        text_blocks = [
            getattr(b, "text", "")
            for b in blocks
            if getattr(b, "type", None) == "text"
        ]
        if text_blocks:
            report = "\n".join(text_blocks).strip()
        if tool_use_blocks:
            messages.append({"role": "assistant", "content": blocks})
            tool_results = []
            for b in tool_use_blocks:
                name = b.name
                payload = _run_tool(name, b.input, ctx)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(payload, default=str)[:TOOL_JSON_MAX],
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue
        break
    ensure_all_checks(ctx)
    if last_err:
        return {"error": last_err, "report": report, "provider": "anthropic"}
    return {"report": report, "provider": "anthropic", "error": None}


def run_chat_turn(
    ctx: dict,
    provider: str,
    api_key: str,
    chat_messages: list[dict],
    max_turns: int = 16,
) -> str:
    """
    Follow-up chat with tools. chat_messages ends with the latest user message.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        return (
            "No LLM API key on the server. Configure OPENAI_API_KEY or ANTHROPIC_API_KEY "
            "(environment, .streamlit/secrets.toml, or llm_settings.json)."
        )

    ctx_blob = format_accumulated_for_llm(ctx)
    enriched_system = (
        SYSTEM_PROMPT
        + "\n\n## Latest quantitative context from this ledger (from prior audit tools):\n"
        + ctx_blob
    )

    if provider == "openai":
        return _chat_openai(ctx, api_key, chat_messages, max_turns, enriched_system)
    if provider == "anthropic":
        return _chat_anthropic(ctx, api_key, chat_messages, max_turns, enriched_system)
    return "Unknown provider."


def _chat_openai(
    ctx: dict,
    api_key: str,
    chat_messages: list[dict],
    max_turns: int,
    system_text: str,
) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    tools = _tool_specs_openai()
    messages: list[dict] = [{"role": "system", "content": system_text}] + [
        {"role": m["role"], "content": m["content"]}
        for m in chat_messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    reply = ""
    for _ in range(max_turns):
        try:
            throttle_llm()
            completion = call_with_retry(
                lambda: client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
            )
        except Exception as e:
            return f"OpenAI error: {e}"
        msg = completion.choices[0].message
        if msg.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                result = _run_tool(tc.function.name, tc.function.arguments, ctx)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str)[:TOOL_JSON_MAX],
                    }
                )
            continue
        reply = (msg.content or "").strip()
        break
    return reply or "(No response text.)"


def _chat_anthropic(
    ctx: dict,
    api_key: str,
    chat_messages: list[dict],
    max_turns: int,
    system_text: str,
) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    tools = _tool_specs_anthropic()
    messages = _anthropic_history_from_chat(chat_messages)
    if not messages:
        return "Send a message first."
    reply = ""
    for _ in range(max_turns):
        try:
            throttle_llm()
            message = call_with_retry(
                lambda: client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=2048,
                    system=system_text,
                    tools=tools,
                    messages=messages,
                )
            )
        except Exception as e:
            return f"Anthropic error: {e}"
        blocks = message.content
        tool_use_blocks = [
            b for b in blocks if getattr(b, "type", None) == "tool_use"
        ]
        text_blocks = [
            getattr(b, "text", "")
            for b in blocks
            if getattr(b, "type", None) == "text"
        ]
        if text_blocks:
            reply = "\n".join(text_blocks).strip()
        if tool_use_blocks:
            messages.append({"role": "assistant", "content": blocks})
            tool_results = []
            for b in tool_use_blocks:
                payload = _run_tool(b.name, b.input, ctx)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(payload, default=str)[:TOOL_JSON_MAX],
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue
        break
    return reply or "(No response text.)"


def _anthropic_history_from_chat(chat_messages: list[dict]) -> list[dict]:
    """Map UI chat to Anthropic messages (text only)."""
    out: list[dict] = []
    for m in chat_messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    if "```" in text:
        for block in text.split("```"):
            b = block.strip()
            if b.lower().startswith("json"):
                b = b[4:].strip()
            if b.startswith("{"):
                text = b
                break
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(text[s : e + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _validate_llm_mapping(
    df: pd.DataFrame,
    raw: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    cols = {str(c) for c in df.columns}
    out: dict[str, Any] = {}
    for k in MAP_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip().lower() in ("", "null"):
            continue
        sv = str(v).strip()
        if sv not in cols:
            return None, f"{k}: column '{sv}' not in file (check spelling/case)."
        out[k] = sv
    if "amount_line" not in out:
        return None, "LLM response did not include a valid amount_line present in the file."
    rat = raw.get("rationale")
    if rat is not None and str(rat).strip():
        out["rationale"] = str(rat).strip()[:2000]
    return out, None


def infer_ledger_mapping(
    df: pd.DataFrame,
    provider: str,
    api_key: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Ask the model which columns hold amounts, ids, vendor, etc.; validates against file headers."""
    api_key = (api_key or "").strip()
    if not api_key or df is None or df.empty:
        return None, None
    sample = df.head(25)
    try:
        rows = json.loads(sample.to_json(orient="records", date_format="iso"))
    except Exception:
        rows = sample.replace({pd.NA: None}).to_dict(orient="records")
    payload = {
        "columns": [str(c) for c in df.columns],
        "dtypes": {str(c): str(df[c].dtype) for c in df.columns},
        "sample_rows": rows,
    }
    user_msg = "Ledger sample:\n" + json.dumps(payload, default=str)[:100000]
    text = ""
    try:
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

            def _infer_oai():
                return client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": SCHEMA_INFERENCE_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                )

            throttle_llm()
            r = call_with_retry(_infer_oai)
            text = r.choices[0].message.content or ""
        elif provider == "anthropic":
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)

            def _infer_ant():
                return client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=SCHEMA_INFERENCE_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                )

            throttle_llm()
            r = call_with_retry(_infer_ant)
            parts: list[str] = []
            for b in r.content:
                if getattr(b, "type", None) == "text":
                    parts.append(getattr(b, "text", ""))
            text = "\n".join(parts)
        else:
            return None, "invalid_provider"
    except Exception as e:
        return None, str(e)

    parsed = _extract_json_object(text)
    if not parsed:
        return None, "Could not parse LLM column mapping (expected a JSON object)."
    return _validate_llm_mapping(df, parsed)
