import json
import os
import time
from collections import deque
from pathlib import Path

import pandas as pd
import streamlit as st

from audit_engine import (
    DRILLDOWN_TOOL_ORDER,
    dataframe_from_tool_result,
    deduplicated_flagged_line_exposure,
    ensure_all_checks,
    format_accumulated_for_llm,
    prepare_ledger,
    summary_table_from_accumulated,
)
from forensic_agent import (
    infer_ledger_mapping,
    run_chat_turn,
    run_forensic_agent,
    run_manufacturing_executive_narrative,
)
import manufacturing_ap_audit as mfg_ap
from services.audit_store import delete_audit, get_audit, init_db, list_audits, save_audit
from services.audit_trail import log_event
from services.auth import (
    ensure_default_admin_hashed,
    init_user_file_if_missing,
    load_accounts,
    register_user,
    upgrade_plaintext_if_needed,
    user_role,
    verify_user_password,
)
from services.export_report import build_pdf_bytes, send_audit_email
from services.session_config import session_expired
from services.upload_validate import UploadValidationError, read_ledger_bytes, validate_prepared_context

SAMPLE_LEDGER_PATH = (
    Path(__file__).resolve().parent / "sample_data" / "hard_ledger_inconsistencies.csv"
)
MANUFACTURING_SAMPLE_LEDGER_PATH = (
    Path(__file__).resolve().parent / "sample_data" / "manufacturing_ap_reference_ledger.csv"
)
LLM_SETTINGS_PATH = Path(__file__).resolve().parent / "llm_settings.json"
AUDIT_LOG_PATH = Path(
    os.environ.get("CLEARSPEND_AUDIT_LOG", Path(__file__).resolve().parent / "data" / "audit_trail.jsonl")
)

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(
    page_title="ClearSpend — AP cost recovery",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.stApp { background-color: #FFF0F5; }
section[data-testid="stSidebar"] { background-color: #1e293b !important; }
.brand-text { color: #ffffff !important; font-size: 32px !important; font-weight: 800 !important; }
div[data-testid="stMetric"] {
    background-color: #ffffff;
    border: 1px solid #ffb6c1;
    padding: 25px;
    border-radius: 15px;
    box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
}
h1, h2, h3 { color: #0f172a !important; }
[data-testid="stChatMessage"] p {
    color: #000000 !important;
    font-weight: 700 !important;
    font-size: 1.05rem;
}
</style>
""", unsafe_allow_html=True)

init_user_file_if_missing()
ensure_default_admin_hashed()
init_db()


def _load_llm_settings() -> dict:
    if not LLM_SETTINGS_PATH.is_file():
        return {}
    try:
        with open(LLM_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _secrets_get(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


def _resolve_api_key(provider: str) -> str:
    """Backend-only keys: env → Streamlit secrets → llm_settings.json on server (never from the browser)."""
    if provider == "openai":
        for v in (
            os.environ.get("OPENAI_API_KEY", ""),
            _secrets_get("OPENAI_API_KEY"),
            (_load_llm_settings().get("OPENAI_API_KEY") or "").strip(),
        ):
            if (v or "").strip():
                return str(v).strip()
    if provider == "anthropic":
        for v in (
            os.environ.get("ANTHROPIC_API_KEY", ""),
            _secrets_get("ANTHROPIC_API_KEY"),
            (_load_llm_settings().get("ANTHROPIC_API_KEY") or "").strip(),
        ):
            if (v or "").strip():
                return str(v).strip()
    return ""


def _api_key_source_label(provider: str) -> str:
    key_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    if os.environ.get(key_name):
        return f"environment variable {key_name}"
    if _secrets_get(key_name):
        return ".streamlit/secrets.toml"
    if (_load_llm_settings().get(key_name) or "").strip():
        return "llm_settings.json (on the server)"
    return ""


def _is_admin_session() -> bool:
    return st.session_state.get("user_role") == "admin"


def _logout(clear_trail_user: bool = True) -> None:
    if clear_trail_user and st.session_state.get("username"):
        log_event(
            username=str(st.session_state["username"]),
            action="logout",
            detail={},
        )
    st.session_state["logged_in"] = False
    st.session_state.messages = []
    st.session_state.forensic_ctx = None
    st.session_state.agent_report = ""
    st.session_state.agent_error = None
    st.session_state.audit_ready = False
    st.session_state.forensic_infer_note = None
    st.session_state.pending_save_audit = False
    st.session_state.login_ts = None
    st.session_state.username = None
    st.session_state.user_role = None
    st.session_state.mfg_result = None
    st.session_state.pop("mfg_ai_narrative", None)


def _tail_audit_log(n: int = 100) -> list[str]:
    if not AUDIT_LOG_PATH.is_file():
        return []
    dq: deque[str] = deque(maxlen=n)
    try:
        with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    dq.append(line)
    except OSError:
        return []
    return list(dq)


def _mfg_table(df: pd.DataFrame) -> pd.DataFrame:
    pref = [
        "Rule",
        "Severity",
        "Reason",
        "Invoice_ID",
        "Vendor_ID",
        "Vendor_Name",
        "Total_Amount",
        "Recoverable_Amount",
    ]
    cols = [c for c in pref if c in df.columns]
    rest = [c for c in df.columns if c not in cols]
    return df[cols + rest] if cols else df


def _mfg_findings_for_vendor(
    r: mfg_ap.ManufacturingAuditResult, vendor_id: str
) -> list[tuple[str, pd.DataFrame]]:
    vid = str(vendor_id).strip()
    out: list[tuple[str, pd.DataFrame]] = []
    for name, fdf in r.findings.items():
        if fdf is None or fdf.empty or "Vendor_ID" not in fdf.columns:
            continue
        sub = fdf[fdf["Vendor_ID"].astype(str) == vid].copy()
        if not sub.empty:
            out.append((name, sub))
    return out


def _render_manufacturing_dashboard() -> None:
    r: mfg_ap.ManufacturingAuditResult = st.session_state.mfg_result
    k = r.kpis
    nd_df = r.findings.get("near_duplicate")
    nd_n = len(nd_df) if nd_df is not None and not nd_df.empty else 0

    st.success(
        "Manufacturing / ERP **vendor spend** ledger detected — rules run in **Python**; "
        "the LLM only narrates from a compact brief (not the full file)."
    )
    _mfw = k.get("math_integrity_mass_flag_warning")
    if _mfw:
        st.warning(str(_mfw))
    with st.expander("Dataset explanation (manufacturing AP — use this framing in the app)", expanded=False):
        st.markdown(mfg_ap.DATASET_GUIDE_MD)
        st.caption(
            "Reference file (≈500 rows, savings typically low five figures): "
            "`sample_data/manufacturing_ap_reference_ledger.csv` — regenerate with "
            "`python scripts/generate_manufacturing_reference_ledger.py`."
        )

    r1a, r1b, r1c = st.columns(3)
    r1a.metric("Total spend", f"${k.get('total_ledger_spend_usd', 0):,.0f}")
    r1b.metric("Invoices reviewed", f"{k.get('invoices_reviewed', 0):,}")
    r1c.metric("Vendors reviewed", f"{k.get('vendors_reviewed', 0):,}")

    m1, m2, m3 = st.columns(3)
    m1.metric(
        "Flagged exposure (review $)",
        f"${k.get('flagged_exposure_usd', 0):,.0f}",
        help="Substantive rules (excludes math integrity): each invoice counted once at ledger Total_Amount (near-dup pairs de-duplicated). Not cash recovery.",
    )
    m2.metric(
        "Control issues ($ exposure)",
        f"${k.get('control_issue_exposure_usd', 0):,.0f}",
        help="Near-threshold and payment-lag $ rollup; weekend, vendor-name, and currency-coding rows are excluded from $ (see control tab for all rows). Not savings.",
    )
    m3.metric(
        "Estimated savings",
        f"${k.get('estimated_savings_usd', 0):,.0f}",
        help="Exact duplicate extras + tax variance + price drift deltas only; max recoverable per invoice then summed.",
    )
    d1, d2, d3, _ = st.columns(4)
    d1.metric(
        "Confirmed duplicate rows",
        f"{k.get('confirmed_duplicate_rows', 0):,}",
        help="Rows in exact-duplicate clusters (extra payments carry recoverable amounts).",
    )
    d2.metric(
        "Data quality / control rows",
        f"{k.get('data_quality_issue_rows', 0):,}",
        help="Currency codes, weekend dates, thresholds, vendor name variation (not counted as savings).",
    )
    d3.metric("Near-duplicate pairs", f"{nd_n:,}", help="Review pairs (Recoverable_Amount = 0).")

    prov = st.selectbox(
        "LLM provider (narrative only)",
        options=["anthropic", "openai"],
        format_func=lambda x: "Anthropic" if x == "anthropic" else "OpenAI",
        key="mfg_ai_provider",
    )
    rk = _resolve_api_key(prov)

    tab_es, tab_dup, tab_tax, tab_drift, tab_ctrl, tab_vend, tab_exp = st.tabs(
        [
            "Executive summary",
            "Duplicate payments",
            "Tax errors",
            "Price drift",
            "Control issues",
            "Vendor drilldown",
            "Export report",
        ]
    )
    f = r.findings

    with tab_es:
        es = k.get("executive_summary")
        if isinstance(es, dict) and es.get("message"):
            st.info(str(es["message"]))
        st.markdown(mfg_ap.build_executive_summary_md(r))
        st.subheader("Top flagged vendors (exposure)")
        tv = mfg_ap.top_flagged_vendors_by_exposure(r, top_n=15)
        if tv.empty:
            st.caption("No flagged invoices.")
        else:
            st.dataframe(tv, use_container_width=True, hide_index=True)
        if st.button("Generate AI executive narrative (compact — no full ledger sent)", type="secondary"):
            if not rk:
                st.warning("Configure an API key on the server for this provider.")
            else:
                brief = mfg_ap.compact_context_for_llm(r)
                with st.spinner("Generating narrative..."):
                    md, err = run_manufacturing_executive_narrative(brief, prov, rk)
                if err:
                    st.error(f"LLM error: {err}")
                else:
                    st.session_state.mfg_ai_narrative = md
        if st.session_state.get("mfg_ai_narrative"):
            st.markdown("#### AI narrative (optional)")
            st.markdown(st.session_state.mfg_ai_narrative)

    with tab_dup:
        st.markdown("#### Exact duplicates")
        st.caption("Recoverable savings on rows after the first in each vendor/date/amount cluster.")
        ed = f.get("exact_duplicate")
        if ed is not None and not ed.empty:
            st.dataframe(_mfg_table(ed), use_container_width=True, hide_index=True)
        else:
            st.caption("No exact duplicates.")
        st.markdown("#### Near duplicates (review only)")
        st.caption("Same vendor, invoice dates within 7 days, totals within $5 — not automatic savings.")
        nd = f.get("near_duplicate")
        if nd is not None and not nd.empty:
            st.dataframe(_mfg_table(nd), use_container_width=True, hide_index=True)
        else:
            st.caption("No near duplicates.")

    with tab_tax:
        st.markdown("#### Tax variance")
        st.caption(
            "Savings-eligible: absolute delta vs dataset median implied tax rate unless "
            "`CLEARSPEND_EXPECTED_TAX_RATE` is set."
        )
        tv = f.get("tax_variance")
        if tv is not None and not tv.empty:
            st.dataframe(_mfg_table(tv), use_container_width=True, hide_index=True)
        else:
            st.caption("No tax variance flags.")
        st.markdown("#### Math integrity")
        st.caption("Invoice_Amount + Tax vs Total_Amount — **not** included in the estimated savings KPI.")
        mi = f.get("math_integrity")
        if mi is not None and not mi.empty:
            st.dataframe(_mfg_table(mi), use_container_width=True, hide_index=True)
        else:
            st.caption("No math mismatches.")

    with tab_drift:
        pdv = f.get("price_drift")
        st.caption(
            "Flag when pre-tax amount > peer median + 3×σ within Vendor_ID + Category "
            "(min peer count from env `CLEARSPEND_PRICE_DRIFT_MIN_GROUP`, default 30). "
            "Recoverable = excess over median only (sourcing opportunity estimate)."
        )
        if pdv is not None and not pdv.empty:
            st.dataframe(_mfg_table(pdv), use_container_width=True, hide_index=True)
        else:
            st.caption("No price drift flags.")

    with tab_ctrl:
        parts = []
        for key in ("currency", "weekend", "threshold", "stale_invoice", "vendor_variation"):
            part = f.get(key)
            if part is not None and not part.empty:
                parts.append(_mfg_table(part))
        if parts:
            st.dataframe(pd.concat(parts, ignore_index=True), use_container_width=True, hide_index=True)
        else:
            st.caption("No control issues.")
        st.caption(
            "Stale = payment lag (payment date − invoice date) vs `CLEARSPEND_STALE_INVOICE_DAYS` (default 120). "
            "Weekend, vendor-name, and currency-coding hits are excluded from the **Control issues ($ exposure)** KPI above; "
            "Rows still appear here for review."
        )
        st.caption("Normalized ledger preview (first 200 rows):")
        st.dataframe(r.normalized.head(200), use_container_width=True, hide_index=True)

    with tab_vend:
        norm = r.normalized
        vids = sorted(norm["Vendor_ID"].astype(str).unique().tolist())
        pick = st.selectbox("Vendor_ID", vids, key="mfg_vendor_drill")
        sub = norm[norm["Vendor_ID"].astype(str) == str(pick)]
        st.write(f"**Invoices for {pick}:** {len(sub)}")
        st.dataframe(sub, use_container_width=True, hide_index=True)
        hits = _mfg_findings_for_vendor(r, pick)
        if not hits:
            st.caption("No rule hits for this vendor.")
        else:
            for hname, hdf in hits:
                st.markdown(f"**{hname}**")
                st.dataframe(_mfg_table(hdf), use_container_width=True, hide_index=True)

    summ = json.dumps({"kpis": k, "mode": "manufacturing_ap"}, default=str)
    report_md = mfg_ap.build_export_report_md(r)
    pdf_b = build_pdf_bytes(
        "ClearSpend Manufacturing AP audit",
        report_md + "\n\n" + str(st.session_state.get("mfg_ai_narrative") or ""),
        mfg_ap.top_flagged_vendors_by_exposure(r, top_n=20),
    )

    with tab_exp:
        st.download_button(
            "Download report (Markdown)",
            report_md.encode("utf-8"),
            "ClearSpend_Manufacturing_AP_Report.md",
            mime="text/markdown",
        )
        st.download_button(
            "Download KPIs (JSON)",
            summ.encode("utf-8"),
            "ClearSpend_Manufacturing_KPIs.json",
            mime="application/json",
        )
        if pdf_b:
            st.download_button(
                "Download report (PDF)",
                pdf_b,
                "ClearSpend_Manufacturing_AP_Report.pdf",
                mime="application/pdf",
            )
        else:
            st.caption("PDF export unavailable (install `fpdf2`).")

    if st.button("Save manufacturing audit to history"):
        try:
            report_body = (
                mfg_ap.build_executive_summary_md(r)
                + "\n\n"
                + str(st.session_state.get("mfg_ai_narrative") or "")
            )
            aid = save_audit(
                str(st.session_state.get("username") or "user"),
                str(st.session_state.get("upload_name") or "ledger"),
                prov,
                summ,
                report_body,
                len(r.normalized),
                float(k.get("estimated_savings_usd") or 0),
            )
            log_event(
                username=str(st.session_state["username"]),
                action="audit_saved_manufacturing",
                detail={"id": aid},
            )
            st.success(f"Saved audit {aid[:8]}…")
        except Exception as ex:
            st.error(str(ex))


DRILLDOWN_TOOLS = DRILLDOWN_TOOL_ORDER

# ----------------------------
# 2) State Management
# ----------------------------
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "messages" not in st.session_state:
    st.session_state.messages = []
if "forensic_ctx" not in st.session_state:
    st.session_state.forensic_ctx = None
if "agent_report" not in st.session_state:
    st.session_state.agent_report = ""
if "agent_error" not in st.session_state:
    st.session_state.agent_error = None
if "audit_ready" not in st.session_state:
    st.session_state.audit_ready = False
if "forensic_infer_note" not in st.session_state:
    st.session_state.forensic_infer_note = None
if "login_ts" not in st.session_state:
    st.session_state.login_ts = None
if "username" not in st.session_state:
    st.session_state.username = None
if "user_role" not in st.session_state:
    st.session_state.user_role = None
if "pending_save_audit" not in st.session_state:
    st.session_state.pending_save_audit = False
if "mfg_result" not in st.session_state:
    st.session_state.mfg_result = None

# ----------------------------
# 3) Login & Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    st.markdown(
        "Find **inconsistencies that cost money**: line totals that don’t match invoices, **duplicate IDs**, "
        "the **same vendor + date + amount** posted twice, **price creep**, uncaptured **credits**, and uneven pricing — "
        "with **vendor-level** impact."
    )
    tab_login, tab_signup = st.tabs(["Login", "Create Account"])

    with tab_login:
        u = st.text_input("Username", key="l_user")
        p = st.text_input("Password", type="password", key="l_pass")
        if st.button("Log In", use_container_width=True):
            accounts = load_accounts()
            rec = accounts.get(u)
            if rec and verify_user_password(rec, p):
                upgrade_plaintext_if_needed(u, p, rec, accounts)
                st.session_state.messages = []
                st.session_state.forensic_ctx = None
                st.session_state.mfg_result = None
                st.session_state.pop("mfg_ai_narrative", None)
                st.session_state.agent_report = ""
                st.session_state.agent_error = None
                st.session_state.audit_ready = False
                st.session_state.forensic_infer_note = None
                st.session_state.pending_save_audit = False
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = rec["name"]
                st.session_state["org_name"] = rec.get("org", "UIC")
                st.session_state["username"] = u
                st.session_state["user_role"] = user_role(rec)
                st.session_state["login_ts"] = time.time()
                log_event(username=u, action="login", detail={"role": st.session_state["user_role"]})
                st.rerun()
            else:
                st.error("❌ **Invalid Username or Password.**")

    with tab_signup:
        st.subheader("Register New Account")
        new_u = st.text_input("Choose Username", key="s_user")
        new_p = st.text_input("Choose Password", type="password", key="s_pass")
        new_n = st.text_input("Full Name", key="s_name")
        new_o = st.text_input("Organization", key="s_org")
        if st.button("Create Account", use_container_width=True):
            accounts = load_accounts()
            if new_u in accounts:
                st.error("⚠️ **Account already exists for this username.**")
            elif new_u and new_p and new_n:
                register_user(new_u, new_p, new_n, new_o or "—", role="user")
                st.balloons()
                st.success(f"**Account created for {new_n}! You can now login.**")
                log_event(username=new_u, action="signup", detail={})
            else:
                st.warning("⚠️ **Please fill in all fields.**")

# ----------------------------
# 4) Dashboard
# ----------------------------
else:
    if session_expired(st.session_state.get("login_ts")):
        log_event(
            username=str(st.session_state.get("username") or "unknown"),
            action="session_expired",
            detail={},
        )
        _logout(clear_trail_user=False)
        st.warning("Your session expired. Please sign in again.")
        st.stop()

    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(
            f"👤 **{st.session_state['user_name']}** | 🏢 **{st.session_state['org_name']}** "
            f"| Role: **{st.session_state.get('user_role') or 'user'}**"
        )

        st.subheader("LLM provider")
        st.caption(
            "API keys are **not entered in the browser**. They must be set on the **server** "
            "(environment variables, `.streamlit/secrets.toml`, or `llm_settings.json`). "
            "See **Download sample… → Backend LLM configuration** on the main page."
        )
        provider = st.selectbox(
            "Chat model provider",
            options=["anthropic", "openai"],
            format_func=lambda x: "Anthropic (Claude Haiku 4.5)" if x == "anthropic" else "OpenAI (gpt-4o)",
            key="ai_provider",
        )
        resolved = _resolve_api_key(provider)
        src = _api_key_source_label(provider)
        if resolved:
            st.success(f"Backend LLM key loaded (**{src}**).")
        else:
            st.warning(
                f"No backend key for **{provider}**. Set `{ 'ANTHROPIC_API_KEY' if provider == 'anthropic' else 'OPENAI_API_KEY' }` on the server."
            )

        ctx_sb = st.session_state.forensic_ctx
        ledger_ok = bool(
            isinstance(ctx_sb, dict)
            and ctx_sb.get("source_df") is not None
            and len(ctx_sb["source_df"]) > 0
        ) or st.session_state.get("mfg_result") is not None

        st.subheader("🤖 AI Assistant")
        if not ledger_ok:
            st.caption("Upload a valid ledger (main area) so tools can read your dataset.")
        elif st.session_state.get("mfg_result") is not None:
            st.caption("Manufacturing AP mode: chat uses a **compact Python brief** + classic tool context.")

        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
        if pr := st.chat_input("Ask a follow-up forensic question..."):
            st.session_state.messages.append({"role": "user", "content": pr})
            if not ledger_ok:
                reply = "Upload a ledger file first. If amounts are not mapped yet, run an AI audit or ask me to remap columns."
            elif not resolved:
                reply = (
                    "LLM is not configured on the server for this provider. "
                    "An administrator must set the API key via environment variable, "
                    "`.streamlit/secrets.toml`, or `llm_settings.json` next to the app."
                )
            else:
                with st.spinner("Agent thinking..."):
                    mfg_brief = (
                        mfg_ap.compact_context_for_llm(st.session_state.mfg_result)
                        if st.session_state.get("mfg_result") is not None
                        else None
                    )
                    reply = run_chat_turn(
                        ctx_sb,
                        provider,
                        resolved,
                        st.session_state.messages,
                        extra_system_append=mfg_brief,
                    )
            st.session_state.messages.append({"role": "assistant", "content": reply})
            st.rerun()

        if st.button("Log Out", use_container_width=True):
            _logout()
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} — Cost recovery & leakage dashboard")
    st.markdown(
        "Goal: **spot bad data before cash leaves** the company — math breaks, **duplicate payments** (same ID or "
        "same **vendor + date + amount**), vendor **receipt / posting** clusters worth reviewing, **price drift**, and "
        "stranded credits. Upload your ledger, then **Run AI forensic audit**: the model infers odd column layouts, "
        "**Python runs the numbers** on those fields, and you get vendor-level drilldowns plus an executive narrative."
    )

    with st.expander("Download sample stress-test CSV & backend LLM configuration", expanded=False):
        if MANUFACTURING_SAMPLE_LEDGER_PATH.is_file():
            st.download_button(
                "Download manufacturing_ap_reference_ledger.csv (~500 rows, reference est. savings in app)",
                data=MANUFACTURING_SAMPLE_LEDGER_PATH.read_bytes(),
                file_name="manufacturing_ap_reference_ledger.csv",
                mime="text/csv",
                help="ERP-style AP export for the manufacturing forensic workflow.",
            )
        else:
            st.caption(
                "Manufacturing sample missing — run `python scripts/generate_manufacturing_reference_ledger.py`."
            )
        if SAMPLE_LEDGER_PATH.is_file():
            st.download_button(
                "Download hard_ledger_inconsistencies.csv",
                data=SAMPLE_LEDGER_PATH.read_bytes(),
                file_name="hard_ledger_inconsistencies.csv",
                mime="text/csv",
                help="Synthetic messy ledger with many inconsistency types for QA.",
            )
        else:
            st.caption(
                "Classic stress-test CSV missing. From the repo root run: "
                "`python3 scripts/generate_hard_test_ledger.py`"
            )
        st.markdown(
            """
**Backend LLM API keys (nothing is typed into this website)**

Keys are read only on the machine running Streamlit, in this order:

1. **Environment variables** — set before starting the app (Docker, systemd, shell, hosting dashboard):
   ```bash
   export OPENAI_API_KEY="sk-..."
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```
   Optional: `OPENAI_MODEL`, `ANTHROPIC_MODEL`.

2. **`.streamlit/secrets.toml`** — next to `main.py` (e.g. Streamlit Community Cloud, or local deploy):
   ```toml
   OPENAI_API_KEY = "sk-..."
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```
   Use `.streamlit/secrets.toml.example` as a template. **Never commit real keys.**

3. **`llm_settings.json`** — same directory as `main.py`, created only by ops on the server (gitignored):
   ```json
   { "OPENAI_API_KEY": "...", "ANTHROPIC_API_KEY": "..." }
   ```

Users only choose **OpenAI vs Anthropic** in the sidebar; the app picks up the matching backend key automatically.

**Vendor ignore list:** `config/ignored_vendors.json` (lowercase names, see sample).

**FX to USD:** `config/fx_rates.json` — map ISO currency code to **multiply** factor into USD (example: `"EUR": 1.08`).

**Automation:** run `python scripts/run_headless_audit.py --help` from a system cron job for scheduled file drops (no Streamlit required).
"""
        )

    tab_labels = ["Audit", "Batch & compare", "Saved audits"]
    if _is_admin_session():
        tab_labels.append("Admin")
    tabs = st.tabs(tab_labels)
    tab_audit = tabs[0]
    tab_batch = tabs[1]
    tab_saved = tabs[2]
    tab_admin = tabs[3] if _is_admin_session() else None

    # ----- Audit tab -----
    with tab_audit:
        files = st.file_uploader(
            "Upload AP Ledger (CSV or XLSX) — multiple files: first drives AI workspace; all are summarized in Batch tab",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="ledger_upload",
        )
        if files:
            try:
                primary = files[0]
                raw0 = primary.getvalue()
                df_raw = read_ledger_bytes(raw0, primary.name)
                st.session_state.forensic_ctx = prepare_ledger(df_raw)
                if mfg_ap.has_manufacturing_schema(df_raw):
                    st.session_state.mfg_result = mfg_ap.run_manufacturing_ap_audit(df_raw)
                    st.session_state.mfg_ai_narrative = ""
                else:
                    st.session_state.mfg_result = None
                    st.session_state.pop("mfg_ai_narrative", None)
                st.session_state.upload_name = primary.name
                st.session_state.agent_report = ""
                st.session_state.agent_error = None
                st.session_state.audit_ready = False
                st.session_state.forensic_infer_note = None
                st.session_state.multi_upload_count = len(files)
                if len(files) > 1:
                    st.info(
                        f"**{len(files)} files uploaded.** The first (`{primary.name}`) is the active workspace. "
                        "Use **Batch & compare** for all-file summaries or period A vs B."
                    )
            except UploadValidationError as e:
                st.error(f"Upload validation: {e}")

        ctx = st.session_state.forensic_ctx
        if ctx is None:
            st.info("Upload a CSV or XLSX file to begin.")
        else:
            if st.session_state.get("mfg_result") is not None:
                _render_manufacturing_dashboard()
            else:
                for warn in validate_prepared_context(ctx):
                    st.caption(f"ℹ️ {warn}")
                if ctx.get("fx_normalized"):
                    st.caption("FX normalization to USD was applied using `config/fx_rates.json` where rates existed.")

                if ctx.get("error") == "missing_amount_column":
                    st.warning(
                        "Heuristics did not find a standard amount column. **Run AI forensic audit** — the model will "
                        "infer column roles from your headers and sample rows, then Python runs the math on its picks."
                    )
                elif ctx.get("error") == "empty_dataset":
                    st.warning("The uploaded file has no rows.")

                src = ctx.get("source_df")
                df = ctx.get("df")
                preview_df = src if src is not None and not getattr(src, "empty", True) else df
                if preview_df is not None and len(preview_df) > 0:
                    with st.expander("Data preview & detected columns", expanded=False):
                        st.write("**Mapping source:**", ctx.get("mapping_source", "—"))
                        if ctx.get("llm_rationale"):
                            st.caption(ctx["llm_rationale"])
                        st.write("**Detected mapping:**", ctx.get("columns", {}))
                        st.dataframe(preview_df.head(12), use_container_width=True, hide_index=True)

                prov = st.selectbox(
                    "Provider for audit run",
                    options=["anthropic", "openai"],
                    format_func=lambda x: "Anthropic (Claude Haiku 4.5)" if x == "anthropic" else "OpenAI (gpt-4o)",
                    key="main_ai_provider",
                )
                resolved_main = _resolve_api_key(prov)
                _ms = _api_key_source_label(prov)
                if resolved_main:
                    st.caption(f"Audit uses the backend key from: **{_ms}**")
                else:
                    st.caption(
                        f"Configure **`{'ANTHROPIC_API_KEY' if prov == 'anthropic' else 'OPENAI_API_KEY'}`** on the server to run column inference + agent."
                    )

                run_disabled = bool(src is None or getattr(src, "empty", True))
                if st.button("Run AI forensic audit", type="primary", disabled=run_disabled):
                    st.session_state.audit_ready = True
                    ctx_btn = st.session_state.forensic_ctx
                    ctx_btn["accumulated"] = {}
                    if resolved_main:
                        mapping, infer_err = None, None
                        if src is not None and not getattr(src, "empty", True):
                            with st.spinner(
                                "Inferring columns from headers + sample rows (LLM)..."
                            ):
                                mapping, infer_err = infer_ledger_mapping(
                                    src, prov, resolved_main
                                )
                        st.session_state.forensic_infer_note = infer_err
                        if mapping:
                            st.session_state.forensic_ctx = prepare_ledger(
                                src.copy(), llm_mapping=mapping
                            )
                            ctx_btn = st.session_state.forensic_ctx
                        with st.spinner(
                            "Agent is running tool-backed checks (this may take a minute)..."
                        ):
                            out = run_forensic_agent(ctx_btn, prov, resolved_main)
                            st.session_state.agent_report = out.get("report") or ""
                            st.session_state.agent_error = out.get("error")
                    else:
                        st.session_state.agent_report = ""
                        st.session_state.agent_error = (
                            "no_api_key: set OPENAI_API_KEY or ANTHROPIC_API_KEY on the server (env, secrets.toml, or llm_settings.json)."
                        )
                        st.session_state.forensic_infer_note = None
                        if not ctx_btn.get("error"):
                            ensure_all_checks(ctx_btn)
                    st.session_state.pending_save_audit = True
                    log_event(
                        username=str(st.session_state.get("username") or ""),
                        action="audit_run",
                        detail={
                            "provider": prov,
                            "upload": st.session_state.get("upload_name"),
                            "llm": bool(resolved_main),
                        },
                    )

                if st.session_state.get("forensic_infer_note"):
                    st.caption(f"Column inference: {st.session_state.forensic_infer_note}")

                ctx = st.session_state.forensic_ctx
                df = ctx.get("df") if ctx else None
                src_now = ctx.get("source_df") if ctx else None
                if st.session_state.audit_ready and ctx and src_now is not None and not getattr(
                    src_now, "empty", True
                ):
                    err = st.session_state.agent_error
                    if err:
                        if str(err).startswith("no_api_key") or err == "missing_api_key":
                            st.warning(
                                "Rule-based checks are shown below. Configure LLM API keys **on the server** for inference + narrative."
                            )
                        else:
                            st.error(f"Model/API error: {err}")

                    if st.session_state.agent_report:
                        st.markdown("### AI narrative report")
                        st.markdown(st.session_state.agent_report)

                    if ctx.get("error"):
                        st.warning(
                            "The ledger still has no validated amount column. Check the narrative for **remap_ledger** "
                            "suggestions or fix the source file."
                        )
                        if st.session_state.get("pending_save_audit"):
                            st.session_state.pending_save_audit = False
                    elif df is not None and len(df) > 0:
                        ensure_all_checks(ctx)
                        audit_df = summary_table_from_accumulated(ctx)
                        unique_exp, unique_n = deduplicated_flagged_line_exposure(ctx)
                        total_exp = unique_exp
                        category_sum = (
                            float(audit_df["Amount ($)"].sum())
                            if audit_df is not None and not audit_df.empty
                            else 0.0
                        )
                        if audit_df is not None and not audit_df.empty:
                            st.metric(
                                "Unique flagged line exposure (best estimate)",
                                f"${total_exp:,.2f}",
                                help="Each ledger line is counted once even if multiple checks flag it. "
                                "Category totals below can exceed this when summed.",
                            )
                            st.caption(
                                f"**{unique_n}** distinct flagged lines · Category exposure sum "
                                f"(can double-count): **${category_sum:,.2f}**"
                            )
                        else:
                            st.success(
                                "No quantitative findings from rule-based checks on this file."
                            )

                        if st.session_state.get("pending_save_audit"):
                            try:
                                aid = save_audit(
                                    str(st.session_state.get("username") or "user"),
                                    str(st.session_state.get("upload_name") or "ledger"),
                                    prov,
                                    audit_df.to_json()
                                    if audit_df is not None and not audit_df.empty
                                    else "[]",
                                    st.session_state.agent_report or "",
                                    len(df),
                                    total_exp,
                                )
                                log_event(
                                    username=str(st.session_state["username"]),
                                    action="audit_saved",
                                    detail={"id": aid},
                                )
                                st.caption(f"Saved audit **{aid[:8]}…** (reload **Saved audits** tab).")
                            except Exception as ex:
                                st.caption(f"Could not persist audit: {ex}")
                            st.session_state.pending_save_audit = False

                        filt = audit_df
                        if audit_df is not None and not audit_df.empty:
                            opts = audit_df["Category"].unique().tolist()
                            sel = st.multiselect(
                                "Filter categories",
                                opts,
                                default=opts,
                                key="cat_filter",
                            )
                            filt = audit_df[audit_df["Category"].isin(sel)]

                        col1, col2 = st.columns([1, 1.5])
                        with col1:
                            st.write("### 🔍 Risk summary (rule-based)")
                            if filt is not None and not filt.empty:
                                st.dataframe(
                                    filt, use_container_width=True, hide_index=True
                                )
                                csv_data = filt.to_csv(index=False).encode("utf-8-sig")
                                st.download_button(
                                    "Download summary CSV",
                                    csv_data,
                                    "ClearSpend_Summary.csv",
                                )
                                pdf_b = build_pdf_bytes(
                                    "ClearSpend - AP audit summary",
                                    st.session_state.agent_report or "",
                                    filt,
                                )
                                if pdf_b:
                                    st.download_button(
                                        "Download summary PDF",
                                        data=pdf_b,
                                        file_name="ClearSpend_Summary.pdf",
                                        mime="application/pdf",
                                    )
                                if st.button("Email summary (SMTP on server)"):
                                    body = (
                                        (st.session_state.agent_report or "")
                                        + "\n\n---\n"
                                        + (filt.to_csv(index=False) if filt is not None else "")
                                    )
                                    ok, msg = send_audit_email(
                                        "ClearSpend audit summary",
                                        body[:50000],
                                        secrets_get=_secrets_get,
                                    )
                                    if ok:
                                        st.success(msg)
                                    else:
                                        st.warning(msg)
                            else:
                                st.caption(
                                    "Summary table is empty when all checks return zero exposure."
                                )
                        with col2:
                            st.write("### 📈 Exposure by category")
                            if filt is not None and not filt.empty:
                                st.bar_chart(data=filt, x="Category", y="Amount ($)")
                            else:
                                st.caption("No bar chart when there are no findings.")

                        st.divider()
                        st.write("### Vendor exposure (from flagged rows)")
                        acc = ctx.get("accumulated", {})
                        vrows = []
                        for key, title in DRILLDOWN_TOOLS:
                            payload = acc.get(key) or {}
                            for row in payload.get("by_vendor") or []:
                                if isinstance(row, dict) and row.get("vendor"):
                                    vrows.append(
                                        {
                                            "Check": title,
                                            "Vendor": row.get("vendor"),
                                            "Exposure ($)": row.get(
                                                "exposure_line_amount"
                                            ),
                                        }
                                    )
                        if vrows:
                            vdf = pd.DataFrame(vrows)
                            vendors_pick = sorted(vdf["Vendor"].unique().tolist())
                            pick = st.multiselect(
                                "Filter vendors",
                                vendors_pick,
                                default=vendors_pick,
                                key="vendor_filter",
                            )
                            show_v = vdf[vdf["Vendor"].isin(pick)]
                            st.dataframe(show_v, use_container_width=True, hide_index=True)
                        else:
                            st.caption("No vendor-level exposure rows for this run.")

                        st.write("### Row-level drilldown")
                        for tool_key, title in DRILLDOWN_TOOLS:
                            payload = acc.get(tool_key) or {}
                            exp = float(payload.get("exposure_usd") or 0)
                            n = int(payload.get("flagged_row_count") or 0)
                            sub = dataframe_from_tool_result(tool_key, payload)
                            if sub is None or sub.empty:
                                continue
                            with st.expander(f"{title} — ${exp:,.2f} | {n} rows"):
                                st.dataframe(
                                    sub, use_container_width=True, hide_index=True
                                )

                        with st.expander("Context snippet for AI (debug)"):
                            st.code(
                                format_accumulated_for_llm(ctx)[:8000] or "(empty)"
                            )
                    else:
                        if st.session_state.get("pending_save_audit"):
                            st.session_state.pending_save_audit = False

    # ----- Batch & compare -----
    with tab_batch:
        st.subheader("Multi-file batch (Python checks only)")
        batch_files = st.file_uploader(
            "Upload one or more ledgers",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key="batch_upload",
        )
        if batch_files and st.button("Run batch summaries", key="batch_go"):
            rows_out = []
            for bf in batch_files:
                try:
                    d = read_ledger_bytes(bf.getvalue(), bf.name)
                    bctx = prepare_ledger(d)
                    if bctx.get("error"):
                        rows_out.append(
                            {
                                "File": bf.name,
                                "Error": bctx["error"],
                                "Exposure ($)": 0.0,
                                "Categories": 0,
                            }
                        )
                        continue
                    ensure_all_checks(bctx)
                    adf = summary_table_from_accumulated(bctx)
                    tot = float(adf["Amount ($)"].sum()) if adf is not None and not adf.empty else 0.0
                    rows_out.append(
                        {
                            "File": bf.name,
                            "Error": "",
                            "Exposure ($)": tot,
                            "Categories": len(adf) if adf is not None else 0,
                        }
                    )
                except UploadValidationError as e:
                    rows_out.append(
                        {"File": bf.name, "Error": str(e), "Exposure ($)": 0.0}
                    )
            st.dataframe(pd.DataFrame(rows_out), use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Period A vs period B")
        ca, cb = st.columns(2)
        with ca:
            fa = st.file_uploader("Period A ledger", type=["csv", "xlsx"], key="cmp_a")
        with cb:
            fb = st.file_uploader("Period B ledger", type=["csv", "xlsx"], key="cmp_b")
        if st.button("Compare periods", key="cmp_go") and fa and fb:
            try:
                da = read_ledger_bytes(fa.getvalue(), fa.name)
                db = read_ledger_bytes(fb.getvalue(), fb.name)
                cx_a = prepare_ledger(da)
                cx_b = prepare_ledger(db)
                if cx_a.get("error") or cx_b.get("error"):
                    st.error(
                        f"A: {cx_a.get('error')} | B: {cx_b.get('error')}"
                    )
                else:
                    ensure_all_checks(cx_a)
                    ensure_all_checks(cx_b)
                    sa = summary_table_from_accumulated(cx_a)
                    sb = summary_table_from_accumulated(cx_b)
                    if sa is None or sb is None:
                        st.warning("Could not build summaries.")
                    else:
                        ma = sa.rename(columns={"Amount ($)": "Amount_A", "Rows": "Rows_A"})
                        mb = sb.rename(columns={"Amount ($)": "Amount_B", "Rows": "Rows_B"})
                        merged = pd.merge(
                            ma[["Category", "Amount_A", "Rows_A"]],
                            mb[["Category", "Amount_B", "Rows_B"]],
                            on="Category",
                            how="outer",
                        ).fillna(0)
                        merged["Amount_delta"] = merged["Amount_B"] - merged["Amount_A"]
                        st.dataframe(merged, use_container_width=True, hide_index=True)
            except UploadValidationError as e:
                st.error(str(e))

    # ----- Saved audits -----
    with tab_saved:
        uname = str(st.session_state.get("username") or "")
        adm = _is_admin_session()
        saved = list_audits(uname, limit=80, is_admin=adm)
        if not saved:
            st.caption("No saved audits yet. Run an audit on the **Audit** tab.")
        else:
            for r in saved:
                cols = st.columns([3, 1, 1])
                with cols[0]:
                    owner = r.get("username", "")
                    st.write(
                        f"**{r.get('file_label')}** — {r.get('id')[:8]}… "
                        f"(${float(r.get('total_exposure') or 0):,.2f})"
                        + (f" — _{owner}_" if adm and owner else "")
                    )
                with cols[1]:
                    if st.button("Open", key=f"op_{r['id']}"):
                        st.session_state.saved_audit_view_id = r["id"]
                        st.rerun()
                with cols[2]:
                    if st.button("Delete", key=f"del_{r['id']}"):
                        delete_audit(r["id"], uname, is_admin=adm)
                        st.session_state.pop("saved_audit_view_id", None)
                        st.rerun()
            vid = st.session_state.get("saved_audit_view_id")
            if vid:
                rec = get_audit(str(vid), uname, is_admin=adm)
                if rec:
                    st.markdown("### Saved narrative")
                    st.markdown(rec.get("report_md") or "_(none)_")
                    try:
                        sj = rec.get("summary_json") or "[]"
                        st.dataframe(
                            pd.read_json(sj), use_container_width=True, hide_index=True
                        )
                    except Exception:
                        st.caption("Summary table could not be reloaded.")
                if st.button("Close preview", key="close_saved"):
                    st.session_state.pop("saved_audit_view_id", None)
                    st.rerun()

    # ----- Admin -----
    if tab_admin is not None:
        with tab_admin:
            st.subheader("Audit trail (recent)")
            lines = _tail_audit_log(120)
            if lines:
                st.code("\n".join(lines[-80:]), language="json")
            else:
                st.caption("No events yet (`data/audit_trail.jsonl`).")
            st.subheader("Accounts (from users_db.json)")
            acc = load_accounts()
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "username": k,
                            "name": v.get("name"),
                            "org": v.get("org"),
                            "role": v.get("role", "user"),
                            "auth": "hash"
                            if v.get("pw_hash")
                            else ("legacy" if v.get("pw") else "—"),
                        }
                        for k, v in acc.items()
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
            st.caption(
                "Promote a user to admin by setting `\"role\": \"admin\"` in `users_db.json` on the server."
            )