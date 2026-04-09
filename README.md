# ClearSpend Analytics

Streamlit app for **forensic accounts payable** review: Python runs deterministic checks on uploaded ledgers; optional **LLM** narration uses only a compact KPI + sample brief (not the full file).

## Stack

| Layer | Technology |
| ----- | ---------- |
| UI | [Streamlit](https://streamlit.io/) |
| Data | [pandas](https://pandas.pydata.org/), NumPy |
| Classic audit engine | In-repo `audit_engine.py` (tool-based checks, vendor filters) |
| Manufacturing AP workflow | `manufacturing_ap_audit.py` (ERP-style columns, exposure vs savings split) |
| LLM | [Anthropic](https://www.anthropic.com/) API and/or [OpenAI](https://platform.openai.com/) API (`forensic_agent.py`, retry + rate limits in `services/`) |
| Auth / persistence | `bcrypt`, SQLite audit store (`services/audit_store.py`), JSONL audit trail |
| Export | Markdown + JSON + optional PDF (`fpdf2`, `services/export_report.py`) |
| Tests | `pytest` |

## Manufacturing reference dataset

- **File:** `sample_data/manufacturing_ap_reference_ledger.csv` (~500 rows).
- **Regenerate:** `python scripts/generate_manufacturing_reference_ledger.py`  
  Produces planted duplicate payments, tax variances, price drift, and control-only rows; **estimated savings** lands near **$15,000** while **flagged exposure** is much larger.
- **Framing** for columns and rules: see `DATASET_GUIDE_MD` in `manufacturing_ap_audit.py` and the in-app expander.

## Run locally

From the repo root:

```bash
pip install -r requirements.txt
streamlit run main.py
```

Or:

```bash
chmod +x run_streamlit.sh
./run_streamlit.sh
```

**Streamlit:** pinned in `requirements.txt` as `>=1.45.0,<2` (installs latest 1.x, e.g. 1.56.x). The app defaults to **http://localhost:8501**. File watching uses **poll** in `.streamlit/config.toml` so macOS FSevents failures do not break reloads.

Optional one-off without touching config:

```bash
STREAMLIT_SERVER_FILE_WATCHER_TYPE=poll streamlit run main.py
```

API keys: environment variables `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, Streamlit secrets, or server-side `llm_settings.json`.

## Video assignment (talking points)

Use **at least half** the recording for non-coding uses of AI: how your team briefed models for **business decisions**, what **context** you supplied over the term, which **tools** (e.g. Cursor, ChatGPT, Claude), and how you **collaborated** across platforms.

For the **technical** portion, narrate this repo:

1. **Python-first audit** — Normalization, duplicate detection, tax and drift logic, and separation of **flagged exposure** vs **estimated savings** (`manufacturing_ap_audit.py`).
2. **AI behind the scenes** — `compact_context_for_llm()` sends KPIs, top flagged vendors, rule counts, and small row samples; `run_manufacturing_executive_narrative` / chat append that brief so the model **explains** without re-deriving numbers from a 500-row CSV.
3. **Workflow** — Adding features via Cursor/agent, `pytest` for regression (`tests/`), reference data via `scripts/generate_manufacturing_reference_ledger.py`.
4. **UX** — Streamlit tabs (Executive summary, Duplicate payments, Tax errors, Price drift, Control issues, Vendor drilldown, Export report) and downloads (Markdown, JSON, PDF).
