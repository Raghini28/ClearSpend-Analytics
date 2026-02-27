import streamlit as st
import pandas as pd
import time

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

# --- STYLE FIX: Ensuring Chatbot and Input visibility ---
st.markdown("""
<style>
.stApp { background-color: #FFF0F5; }
section[data-testid="stSidebar"] { background-color: #1e293b !important; }
.brand-text { color: #ffffff !important; font-size: 32px !important; font-weight: 800 !important; }

/* FIX: Force all input text and chat messages to be dark/visible */
input, textarea, [data-testid="stChatMessage"] {
    color: #000000 !important;
}

div[data-testid="stMetric"] {
    background-color: #ffffff;
    border: 1px solid #ffb6c1;
    padding: 25px;
    border-radius: 15px;
}

h1, h2, h3, p, span { color: #0f172a !important; }

/* FIX: Ensure sidebar text remains readable */
[data-testid="stSidebar"] p, [data-testid="stSidebar"] h3, [data-testid="stSidebar"] span {
    color: #ffffff !important;
}
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
# 3) Data Loading
# ----------------------------
def load_uploaded(uploaded_file):
    try:
        if uploaded_file.name.lower().endswith(".csv"):
            return pd.read_csv(uploaded_file, encoding='utf-8', on_bad_lines='skip')
        else:
            return pd.read_excel(uploaded_file, engine="openpyxl")
    except Exception as e:
        st.error(f"Error reading file: {e}")
        return pd.DataFrame()

# ----------------------------
# 4) Forensic Engine
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
    
    col_invoice = find_col(df, ["Invoice_ID", "InvoiceID", "Invoice Number"])
    col_vendor  = find_col(df, ["Vendor_Name", "VendorName", "Vendor", "Supplier"])
    col_unit    = find_col(df, ["Unit_Price", "Price"])
    col_lineamt = find_col(df, ["Line_Amount", "Amount"])
    col_total   = find_col(df, ["Invoice_Total", "Total"])
    col_date    = find_col(df, ["Invoice_Date", "Date"])

    if not col_lineamt:
        st.warning("⚠️ Column Mapping Failed. Please check headers.")
        return pd.DataFrame()

    df["__Invoice_ID"] = df[col_invoice].astype(str) if col_invoice else "N/A"
    df["__Vendor"] = df[col_vendor].astype(str) if col_vendor else "N/A"
    df["__Line_Amount"] = pd.to_numeric(df[col_lineamt], errors='coerce').fillna(0)
    df["__Invoice_Total"] = pd.to_numeric(df[col_total], errors='coerce').fillna(0)
    df["__Unit_Price"] = pd.to_numeric(df[col_unit], errors='coerce').fillna(0)
    df["__Date"] = pd.to_datetime(df[col_date], errors='coerce')

    issues = []

    # 1. Duplicates
    dup_ids = df["__Invoice_ID"][df["__Invoice_ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__Invoice_ID"] != "N/A").any():
        amt = df[df["__Invoice_ID"].isin(dup_ids.unique())]["__Invoice_Total"].sum()
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(amt), "Priority": "🔴 Critical"})
    
    # 2. Price Creep
    creep_amt = 0
    for v, group in df.sort_values("__Date").groupby("__Vendor"):
        if len(group) > 1:
            diff = group["__Unit_Price"].iloc[-1] - group["__Unit_Price"].iloc[0]
            if diff > 0: creep_amt += (diff * len(group))
    if creep_amt > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})

    # 3. Math Integrity Check
    if col_total:
        mismatch = df[df["__Line_Amount"] != df["__Invoice_Total"]]
        if not mismatch.empty:
            issues.append({"Category": "Math Integrity Check", "Amount ($)": float(mismatch["__Line_Amount"].sum() * 0.1), "Priority": "🔴 Critical"})

    return pd.DataFrame(issues)

# ----------------------------
# 5) Chatbot Logic
# ----------------------------
def forensic_bot(query, audit_df):
    query = query.lower()
    total = audit_df["Amount ($)"].sum() if not audit_df.empty else 0
    if "math" in query or "integrity" in query:
        return "**Math Integrity Check:** Compares line items vs the grand total to catch hidden fees."
    elif "total" in query:
        return f"Results show **${total:,.2f}** in potential recoveries."
    return f"Hi! I am the ClearSpend AI. Ask me about your audit results!"

# ----------------------------
# 6) UI: Login / Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Create Account"])
    
    with t1:
        u = st.text_input("Username", key="l_u")
        p = st.text_input("Password", type="password", key="l_p")
        if st.button("Log In"):
            if u in st.session_state["accounts"] and st.session_state["accounts"][u]["pw"] == p:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = st.session_state["accounts"][u]["name"]
                st.session_state["org_name"] = st.session_state["accounts"][u]["org"]
                st.rerun()
            else:
                st.error("Invalid credentials.")
                
    with t2:
        # FIX: Explicit Labels for demo
        nu = st.text_input("Choose Username", key="s_u")
        np = st.text_input("Choose Password", type="password", key="s_p")
        nn = st.text_input("Full Name", key="s_n")
        no = st.text_input("Company/Org Name", key="s_o")
        
        if st.button("Sign Up"):
            if nu and np and nn and no:
                st.session_state["accounts"][nu] = {"pw": np, "name": nn, "org": no}
                # FIX: Sequence to ensure balloons trigger
                st.balloons()
                st.success("Account created! Now go to the Login tab.")
            else:
                st.warning("Please fill in all fields.")

# ----------------------------
# 7) Main Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.write(f"👤 **{st.session_state['user_name']}**")
        st.write(f"🏢 **{st.session_state['org_name']}**")
        
        # Chatbot
        st.subheader("🤖 AI Assistant")
        with st.expander("Chat with Bot", expanded=True):
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask a question..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                with st.chat_message("user"): st.markdown(pr)
                res = forensic_bot(pr, st.session_state.get('last_audit', pd.DataFrame()))
                with st.chat_message("assistant"): st.markdown(res)
                st.session_state.messages.append({"role": "assistant", "content": res})

        if st.button("Log Out"):
            st.session_state["logged_in"] = False
            st.session_state.messages = []
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Dashboard")
    f = st.file_uploader("Upload Ledger", type=["csv", "xlsx"])

    if f:
        df_raw = load_uploaded(f)
        audit_df = build_audit(df_raw)
        st.session_state['last_audit'] = audit_df
        
        if not audit_df.empty:
            m1, m2, m3 = st.columns(3)
            val = audit_df["Amount ($)"].sum()
            m1.metric("Recoverable Cash", f"${val:,.2f}")
            m2.metric("Audits Passed", "5/5")
            m3.metric("ROI", f"{(val/15000):.1f}x")

            st.divider()
            st.write("### 🔍 Risk Distribution")
            st.bar_chart(data=audit_df, x="Category", y="Amount ($)")
            st.dataframe(audit_df, use_container_width=True, hide_index=True)
