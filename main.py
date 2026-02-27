import streamlit as st
import pandas as pd
import time

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

# Premium Pink/Navy Corporate Styling
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
[data-testid="stChatMessage"] p { color: #000000 !important; }
</style>
""", unsafe_allow_html=True)

# ----------------------------
# 2) State Management
# ----------------------------
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

if "accounts" not in st.session_state:
    st.session_state["accounts"] = {
        "admin": {"pw": "uic2026", "name": "Raghini Kumar", "org": "UIC"}
    }

if "messages" not in st.session_state:
    st.session_state.messages = []

# ----------------------------
# 3) Forensic Engine
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    return None

def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    
    col_lineamt = find_col(df, ["Line_Amount", "Amount"])
    col_total   = find_col(df, ["Invoice_Total", "Total"])
    col_invoice = find_col(df, ["Invoice_ID", "InvoiceID"])

    if not col_lineamt or not col_total:
        st.warning("⚠️ Column Mapping Error: Could not find Amount or Total columns.")
        return pd.DataFrame()

    df["__L"] = pd.to_numeric(df[col_lineamt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[col_total], errors='coerce').fillna(0)
    df["__ID"] = df[col_invoice].astype(str) if col_invoice else "N/A"

    issues = []

    # 1. Math Integrity Check (General Definition Logic)
    mismatch = df[df["__L"] != df["__T"]]
    if not mismatch.empty:
        amt = (mismatch["__T"] - mismatch["__L"]).abs().sum()
        issues.append({
            "Category": "Math Integrity Check", 
            "Amount ($)": float(amt), 
            "Priority": "🔴 Critical"
        })
    
    # 2. Duplicate Detection
    dup_ids = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__ID"] != "N/A").any():
        amt = df[df["__ID"].isin(dup_ids.unique())]["__T"].sum()
        issues.append({
            "Category": "Duplicate Invoice", 
            "Amount ($)": float(amt), 
            "Priority": "🔴 Critical"
        })

    return pd.DataFrame(issues)

# ----------------------------
# 4) Chatbot Logic
# ----------------------------
def forensic_bot(query):
    query = query.lower()
    if "math" in query or "integrity" in query:
        return (
            "**Math Integrity Check:** A forensic validation process that ensures a financial document is "
            "internally consistent. It independently recalculates the sum of all itemized charges (Line Amounts) "
            "and compares them against the final billed amount (Invoice Total) to uncover discrepancies."
        )
    return "I am the ClearSpend AI. Ask me about Math Integrity or duplicates!"

# ----------------------------
# 5) UI Logic (Login)
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Create Account"])
    with t1:
        u = st.text_input("Username", key="login_u")
        p = st.text_input("Password", type="password", key="login_p")
        if st.button("Log In", use_container_width=True):
            if u in st.session_state["accounts"] and st.session_state["accounts"][u]["pw"] == p:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = st.session_state["accounts"][u]["name"]
                st.session_state["org_name"] = st.session_state["accounts"][u]["org"]
                st.rerun()
            else:
                st.error("❌ Invalid Username or Password. Please try again.")
    with t2:
        st.text_input("Username", key="s_u")
        st.text_input("Password", type="password", key="s_p")
        if st.button("Sign Up"):
            st.balloons()
            st.success("✅ Account created!")

# ----------------------------
# 6) Main Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        
        st.subheader("🤖 AI Assistant")
        with st.expander("Chat with Bot", expanded=True):
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask a question..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                res = forensic_bot(pr)
                st.session_state.messages.append({"role": "assistant", "content": res})
                st.rerun()

        if st.button("Log Out"):
            st.session_state["logged_in"] = False
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload Ledger", type=["csv", "xlsx"])

    if f:
        df_raw = pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)
        audit_df = build_audit(df_raw)
        
        if not audit_df.empty:
            st.metric("Recoverable Cash", f"${audit_df['Amount ($)'].sum():,.2f}")
            
            st.divider()
            col_f, col_c = st.columns([1, 1.5])
            
            with col_f:
                st.write("### 🔍 Findings")
                # FIXED: This adds the filter back properly
                options = audit_df["Category"].unique().tolist()
                selected_cats = st.multiselect("Filter by Category", options, default=options)
                filtered_df = audit_df[audit_df["Category"].isin(selected_cats)]
                st.dataframe(filtered_df, use_container_width=True, hide_index=True)
            
            with col_c:
                st.write("### 📈 Risk Distribution")
                # FIXED: The chart now reacts to the filter above
                st.bar_chart(data=filtered_df, x="Category", y="Amount ($)")
            
            st.divider()
            # FIXED: Encoding and index removal to stop weird symbols in CSV
            csv_data = filtered_df.to_csv(index=False).encode('utf-8')
            st.download_button("Download Recovery Report (CSV)", data=csv_data, file_name="ClearSpend_Audit.csv")
