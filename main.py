import streamlit as st
import pandas as pd
import json
import os

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
if "audit_data" not in st.session_state:
    st.session_state.audit_data = None

# ----------------------------
# 4) Forensic Engine (Audit-Grade Upgrades)
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    for col in df.columns:
        c_low = col.lower()
        if any(w in c_low for w in ["id", "key", "ref", "num"]): return col
    return None

def find_amt_col(df: pd.DataFrame, candidates: list[str]):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    nums = df.select_dtypes(include=['number']).columns
    if not nums.empty: return df[nums].mean().idxmax()
    return None

def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)

    c_amt = find_amt_col(df, ["BOOKING_VALUE_USD", "AMOUNT_USD", "Line_Amount", "Amount"])
    c_tot = find_col(df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total"])
    c_id = find_col(df, ["TRANSACTION_ID", "BOOKING_KEY", "Invoice_ID", "InvoiceID"])
    c_unit = find_col(df, ["FX_RATE_TO_USD", "Unit_Price", "Price"])
    c_ven = find_col(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"])
    c_date = find_col(df, ["TRANSACTION_TS", "Invoice_Date", "Date"])

    if not c_id:
        df["__ID"] = range(len(df))
        c_id = "__ID"

    if not c_amt: return pd.DataFrame()

    df["__L"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[c_tot], errors='coerce').fillna(0) if c_tot else df["__L"]
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"] = df[c_id].astype(str)
    df["__V"] = df[c_ven].astype(str) if c_ven else "N/A"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    
    # 1. Math Integrity (Actual Variance)
    mm = df[abs(df["__L"] - df["__T"]) > 0.05]
    if not mm.empty:
        issues.append({
            "Category": "Calculation Variance", 
            "Amount ($)": float(abs(mm["__T"] - mm["__L"]).sum()), 
            "Priority": "🔴 Critical",
            "Action": "Audit Line Item Math"
        })
    
    # 2. Duplicate Invoice (ID match OR Cross-Field Match)
    # Check for identical Amount + Date + Vendor (Fuzzy Match)
    fuzzy_dups = df[df.duplicated(subset=['__L', '__V', '__D'], keep=False)]
    if not fuzzy_dups.empty:
        issues.append({
            "Category": "Fuzzy Duplicate Match", 
            "Amount ($)": float(fuzzy_dups['__L'].sum()), 
            "Priority": "🔴 Critical", 
            "Action": "Verify Original Invoice"
        })
    
    # 3. Price Creep (Historical Regression Trend)
    creep_total = 0
    for v, group in df.sort_values("__D").groupby("__V"):
        if len(group) > 2:
            avg_historical = group["__U"].mean()
            last_price = group["__U"].iloc[-1]
            if last_price > avg_historical:
                creep_total += (last_price - avg_historical) * len(group)
    if creep_total > 0:
        issues.append({
            "Category": "Trend Price Creep", 
            "Amount ($)": float(creep_total), 
            "Priority": "🟠 High",
            "Action": "Renegotiate Rate Card"
        })
    
    # 4. Negative Leak (Refunds)
    negs = df[df["__L"] < 0]
    if not negs.empty:
        issues.append({
            "Category": "Negative Leak", 
            "Amount ($)": float(negs["__L"].abs().sum()), 
            "Priority": "🟣 High",
            "Action": "Confirm Cash Recovery"
        })
    
    # 5. Pricing Inconsistency (Actual Overpayment Modeling)
    if c_unit:
        # Calculate loss based on highest vs lowest price paid to same vendor
        df['best_price'] = df.groupby('__V')['__U'].transform('min')
        df['loss'] = df['__U'] - df['best_price']
        actual_loss = df[df['loss'] > 0]['loss'].sum()
        if actual_loss > 0:
            issues.append({
                "Category": "Contract Variance", 
                "Amount ($)": float(actual_loss), 
                "Priority": "🟡 Medium",
                "Action": "Request Credit Note"
            })

    return pd.DataFrame(issues)

# ----------------------------
# 5) Chatbot Logic
# ----------------------------
def forensic_bot(query):
    query = query.lower()
    if "site" in query or "do" in query or "clearspend" in query:
        return "**ClearSpend Analytics is an audit-grade platform using statistical regression to identify hidden capital leaks and automate vendor credit recovery.**"
    elif "math" in query or "integrity" in query:
        return "**Variance Audit: We model the delta between itemized line data and final settlement totals to catch phantom charges.**"
    elif "duplicate" in query:
        return "**Cross-Field Matching: Our engine flags duplicates by correlating amount, date, and vendor patterns, bypassing ID-obfuscation.**"
    elif "creep" in query:
        return "**Regression Analysis: We track unit price trends against historical baselines to flag unauthorized inflationary creep.**"
    elif "negative" in query or "leak" in query:
        return "**Credit Recovery: We identify recorded refunds that lack settlement confirmation in the general ledger.**"
    elif "inconsistency" in query:
        return "**Contract Variance: We benchmark every purchase against your lowest historical rate to calculate actual overpayment.**"
    return "**I am the ClearSpend AI Assistant. Ask me about Contract Variance, Regression Trends, or Fuzzy Matching!**"

# ----------------------------
# 6) UI Flow: Login & Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    tab_login, tab_signup = st.tabs(["Login", "Create Account"])
    
    with tab_login:
        u = st.text_input("Username", key="l_user")
        p = st.text_input("Password", type="password", key="l_pass")
        if st.button("Log In", use_container_width=True):
            accounts = load_accounts()
            if u in accounts and accounts[u]["pw"] == p:
                st.session_state.messages = []
                st.session_state.audit_data = None
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
        
        st.subheader("🤖 AI Assistant")
        with st.container():
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask a forensic question..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                res = forensic_bot(pr)
                st.session_state.messages.append({"role": "assistant", "content": res})
                st.rerun()

        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
            st.session_state.messages = []
            st.session_state.audit_data = None
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload AP Ledger (CSV or XLSX)", type=["csv", "xlsx"])

    if f:
        df_raw = pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)
        st.session_state.audit_data = build_audit(df_raw)

    if st.session_state.audit_data is not None:
        audit_df = st.session_state.audit_data
        if not audit_df.empty:
            st.metric("Total Recoverable Cash Found", f"${audit_df['Amount ($)'].sum():,.2f}")
            col1, col2 = st.columns([1, 1.5])
            with col1:
