import json
import os

import pandas as pd
import streamlit as st

from audit_engine import (
    DRILLDOWN_TOOL_ORDER,
    dataframe_from_tool_result,
    ensure_all_checks,
    format_accumulated_for_llm,
    prepare_ledger,
    summary_table_from_accumulated,
)
from forensic_agent import infer_ledger_mapping, run_chat_turn, run_forensic_agent

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
/* Bolder Chatbot Responses */
[data-testid="stChatMessage"] p { 
    color: #000000 !important; 
    font-weight: 700 !important; 
    font-size: 1.05rem;
}
</style>
""", unsafe_allow_html=True)

# ----------------------------
# 2) Persistence & Database Logic
# ----------------------------
USER_FILE = "users_db.json"

def load_accounts():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            return json.load(f)
    return {"admin": {"pw": "uic2026", "name": "Raghini Kumar", "org": "UIC"}}

def save_account(username, data):
    accounts = load_accounts()
    accounts[username] = data
    with open(USER_FILE, "w") as f:
        json.dump(accounts, f)

# ----------------------------
# 3) State Management
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

_DEFAULT_KEY_HINT = "Use sidebar field or env OPENAI_API_KEY / ANTHROPIC_API_KEY."


def _resolve_api_key(provider: str, sidebar_key: str) -> str:
    s = (sidebar_key or "").strip()
    if s:
        return s
    if provider == "openai":
        v = os.environ.get("OPENAI_API_KEY", "")
        if v:
            return v
        try:
            return str(st.secrets.get("OPENAI_API_KEY", "") or "")
        except Exception:
            return ""
    if provider == "anthropic":
        v = os.environ.get("ANTHROPIC_API_KEY", "")
        if v:
            return v
        try:
            return str(st.secrets.get("ANTHROPIC_API_KEY", "") or "")
        except Exception:
            return ""
    return ""


DRILLDOWN_TOOLS = DRILLDOWN_TOOL_ORDER

# ----------------------------
# 6) UI Flow: Login & Signup
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
            if u in accounts and accounts[u]["pw"] == p:
                st.session_state.messages = []
                st.session_state.forensic_ctx = None
                st.session_state.agent_report = ""
                st.session_state.agent_error = None
                st.session_state.audit_ready = False
                st.session_state.forensic_infer_note = None
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = accounts[u]["name"]
                st.session_state["org_name"] = accounts[u].get("org", "UIC")
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
                save_account(new_u, {"pw": new_p, "name": new_n, "org": new_o})
                st.balloons()
                st.success(f"**Account created for {new_n}! You can now login.**")
            else:
                st.warning("⚠️ **Please fill in all fields.**")

# ----------------------------
# 7) UI Flow: Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 **{st.session_state['user_name']}** | 🏢 **{st.session_state['org_name']}**")

        st.subheader("AI configuration")
        provider = st.selectbox(
            "Model provider",
            options=["anthropic", "openai"],
            format_func=lambda x: "Anthropic (Claude Haiku 4.5)" if x == "anthropic" else "OpenAI (gpt-4o)",
            key="ai_provider",
        )
        api_field = st.text_input(
            "API key",
            type="password",
            key="ai_api_key",
            help="Session-only unless you set OPENAI_API_KEY / ANTHROPIC_API_KEY or Streamlit secrets.",
        )
        resolved = _resolve_api_key(provider, api_field)
        if resolved and not api_field:
            st.caption("Using API key from environment or Streamlit secrets.")

        ctx = st.session_state.forensic_ctx
        ledger_ok = bool(
            isinstance(ctx, dict)
            and ctx.get("source_df") is not None
            and len(ctx["source_df"]) > 0
        )

        st.subheader("🤖 AI Assistant")
        if not ledger_ok:
            st.caption("Upload a valid ledger (main area) so tools can read your dataset.")

        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
        if pr := st.chat_input("Ask a follow-up forensic question..."):
            st.session_state.messages.append({"role": "user", "content": pr})
            if not ledger_ok:
                reply = "Upload a ledger file first. If amounts are not mapped yet, run an AI audit or ask me to remap columns."
            elif not resolved:
                reply = "Add an API key (or set env / secrets) to use the assistant."
            else:
                with st.spinner("Agent thinking..."):
                    reply = run_chat_turn(ctx, provider, resolved, st.session_state.messages)
            st.session_state.messages.append({"role": "assistant", "content": reply})
            st.rerun()

        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
            st.session_state.messages = []
            st.session_state.forensic_ctx = None
            st.session_state.agent_report = ""
            st.session_state.agent_error = None
            st.session_state.audit_ready = False
            st.session_state.forensic_infer_note = None
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} — Cost recovery & leakage dashboard")
    st.markdown(
        "Goal: **spot bad data before cash leaves** the company — math breaks, **duplicate payments** (same ID or "
        "same **vendor + date + amount**), vendor **receipt / posting** clusters worth reviewing, **price drift**, and "
        "stranded credits. Upload your ledger, then **Run AI forensic audit**: the model infers odd column layouts, "
        "**Python runs the numbers** on those fields, and you get vendor-level drilldowns plus an executive narrative."
    )

    f = st.file_uploader("Upload AP Ledger (CSV or XLSX)", type=["csv", "xlsx"])
    if f:
        df_raw = pd.read_csv(f) if f.name.endswith(".csv") else pd.read_excel(f)
        st.session_state.forensic_ctx = prepare_ledger(df_raw)
        st.session_state.upload_name = f.name
        st.session_state.agent_report = ""
        st.session_state.agent_error = None
        st.session_state.audit_ready = False
        st.session_state.forensic_infer_note = None

    ctx = st.session_state.forensic_ctx
    if ctx is None:
        st.info("Upload a CSV or XLSX file to begin.")
    else:
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
        main_key = st.text_input(
            "API key for audit",
            type="password",
            key="main_ai_key",
            help=_DEFAULT_KEY_HINT,
        )
        resolved_main = _resolve_api_key(prov, main_key)

        run_disabled = bool(
            src is None or getattr(src, "empty", True)
        )
        if st.button("Run AI forensic audit", type="primary", disabled=run_disabled):
            st.session_state.audit_ready = True
            ctx = st.session_state.forensic_ctx
            ctx["accumulated"] = {}
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
                    ctx = st.session_state.forensic_ctx
                with st.spinner(
                    "Agent is running tool-backed checks (this may take a minute)..."
                ):
                    out = run_forensic_agent(ctx, prov, resolved_main)
                    st.session_state.agent_report = out.get("report") or ""
                    st.session_state.agent_error = out.get("error")
            else:
                st.session_state.agent_report = ""
                st.session_state.agent_error = (
                    "no_api_key: add a key or set OPENAI_API_KEY / ANTHROPIC_API_KEY for the AI narrative."
                )
                st.session_state.forensic_infer_note = None
                if not ctx.get("error"):
                    ensure_all_checks(ctx)

        if st.session_state.get("forensic_infer_note"):
            st.caption(f"Column inference: {st.session_state.forensic_infer_note}")

        # After an audit run: narrative optional; Python drilldowns when ledger parses.
        ctx = st.session_state.forensic_ctx
        df = ctx.get("df") if ctx else None
        src_now = ctx.get("source_df") if ctx else None
        if st.session_state.audit_ready and ctx and src_now is not None and not getattr(
            src_now, "empty", True
        ):
            err = st.session_state.agent_error
            if err:
                if str(err).startswith("no_api_key"):
                    st.warning(
                        "Rule-based checks are shown below. Add an API key for LLM column inference + narrative."
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
            elif df is not None and len(df) > 0:
                ensure_all_checks(ctx)
                audit_df = summary_table_from_accumulated(ctx)

                if audit_df is not None and not audit_df.empty:
                    total = float(audit_df["Amount ($)"].sum())
                    st.metric(
                        "Total flagged exposure (Python checks)",
                        f"${total:,.2f}",
                    )
                else:
                    st.success(
                        "No quantitative findings from rule-based checks on this file."
                    )

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
                        st.dataframe(filt, use_container_width=True, hide_index=True)
                        csv_data = filt.to_csv(index=False).encode("utf-8-sig")
                        st.download_button(
                            "Download summary CSV",
                            csv_data,
                            "ClearSpend_Summary.csv",
                        )
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
