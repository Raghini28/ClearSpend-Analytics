import streamlit as st
import pandas as pd

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

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
    st.session_state["accounts"] = {"admin": {"pw": "uic2026", "name": "Raghini Kumar", "org": "UIC"}}
if "messages" not in st.session_state:
    st.session_state.messages = []
# Added memory for the audit data so it stays visible
if "audit_data" not in st.session_state:
    st.session_state.audit_data = None

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
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)

    col_lineamt = find_col(df, ["Line_Amount", "Amount"])
    col_total   = find_col(df, ["Invoice_Total", "Total"])
    col_invoice = find_col(df, ["Invoice_ID", "InvoiceID"])
    col_unit    = find_col(df, ["Unit_Price", "Price"])
    col_vendor  = find_col(df, ["Vendor_Name", "Vendor"])
    col_date    = find_col(df, ["Invoice_Date", "Date"])

    if not col_lineamt or not col_total:
        return pd.DataFrame()

    df["__L"] = pd.to_numeric(df[col_lineamt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[col_total], errors='coerce').fillna(0)
    df["__U"] = pd.to_numeric(df[col_unit], errors='coerce').fillna(0) if col_unit else 0
    df["__ID"] = df[col_invoice].astype(str) if col_invoice else "N/A"
    df["__V"] = df[col_vendor].astype(str) if col_vendor else "N/A"
    df["__D"] = pd.to_datetime(df[col_date], errors='coerce')

    issues = []

    # Math Integrity
    mismatch = df[df["__L"] != df["__T"]]
    if not mismatch.empty:
        issues.append({"Category": "Math Integrity Check", "Amount ($)": float((mismatch["__T"] - mismatch["__L"]).abs().sum()), "Priority": "🔴 Critical"})
    
    # Duplicate Invoice
    dup_ids = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__ID"] != "N/A").any():
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(df[df["__ID"].isin(dup_ids.unique())]["__T"].sum()), "Priority": "🔴 Critical"})

    # Price Creep
    creep_amt = 0
    for v, group in df.sort_values("__D").groupby("__V"):
        if len(group) > 1:
            diff = group["__U"].iloc[-1] - group["__U"].iloc[0]
            if diff > 0: creep_amt += diff * len(group)
    if creep_amt > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})

    # Negative Leak
    negs = df[df["__L"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__L"].abs().sum()), "Priority": "🟣 High"})

    # Pricing Inconsistency
    if col_unit:
        variance = df.groupby("__V")["__U"].nunique()
        inc_count = (variance > 1).sum()
        if inc_count > 0:
            issues.append({"Category": "Pricing Inconsistency", "Amount ($)": float(inc_count * 500), "Priority": "🟡 Medium"})

    return pd.DataFrame(issues)

# ----------------------------
# 4) Chatbot Logic (Bullet points, no numbers)
# ----------------------------
def forensic_bot(query):
    query = query.lower()
    if "site" in query or "do" in query or "clearspend" in query:
        return "**ClearSpend Analytics** is a forensic audit platform that identifies financial leaks in Accounts Payable data to recover lost capital."
    elif "math" in query or "integrity" in query:
        return "* **Math Integrity Check:** A Digital Receipt Validator that ensures Line Amounts match the Invoice Total."
    elif "duplicate" in query:
        return "* **Duplicate Invoice:** Scans for identical Invoice IDs to prevent paying the same bill twice."
    elif "creep" in query:
        return "* **Price Creep:** Monitors unit price increases over time for the same vendor items."
    elif "negative" in query or "leak" in query:
        return "* **Negative Leak:** Identifies negative values/credits that haven't been successfully recovered."
    elif "inconsistency" in query:
        return "* **Pricing Inconsistency:** Checks if a vendor charges different prices for the same service across different locations."
    return "I am the ClearSpend AI. Ask me about our forensic checks!"

# ----------------------------
# 5) UI Flow: Login & Dashboard
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    u = st.text_input("Username", key="l_user")
    p = st.text_input("Password", type="password", key="l_pass")
    if st.button("Log In", use_container_width=True):
        if u in st.session_state["accounts"] and st.session_state["accounts"][u]["pw"] == p:
            st.session_state["logged_in"] = True
            st.session_state["user_name"] = st.session_state["accounts"][u]["name"]
            st.session_state["org_name"] = st.session_state["accounts"][u]["org"]
            st.rerun()
        else:
            st.error("❌ Invalid Username or Password")
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        
        st.subheader("🤖 AI Assistant")
        with st.expander("Chat with Bot", expanded=True):
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask about the leaks..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                res = forensic_bot(pr)
                st.session_state.messages.append({"role": "assistant", "content": res})
                st.rerun()

        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
            st.session_state.audit_data = None
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload Financial Ledger", type=["csv", "xlsx"])

    # If new file is uploaded, run audit and save to state
    if f:
        df_raw = pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)
        st.session_state.audit_data = build_audit(df_raw)

    # Display audit if it exists in memory
    if st.session_state.audit_data is not None:
        audit_df = st.session_state.audit_data
        if not audit_df.empty:
            st.metric("Total Recoverable Cash Found", f"${audit_df['Amount ($)'].sum():,.2f}")
            col1, col2 = st.columns([1, 1.5])
            with col1:
                st.write("### 🔍 Findings")
                opts = audit_df["Category"].unique().tolist()
                sel = st.multiselect("Filter Issues", opts, default=opts)
                filt = audit_df[audit_df["Category"].isin(sel)]
                st.dataframe(filt, use_container_width=True, hide_index=True)
            with col2:
                st.write("### 📈 Risk Distribution")
                st.bar_chart(data=filt, x="Category", y="Amount ($)")
            
            st.divider()
            csv_data = filt.to_csv(index=False).encode('utf-8-sig')
            st.download_button("Download Recovery Report", csv_data, "ClearSpend_Audit.csv")
