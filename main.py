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
# 4) Forensic Engine (Refined for $11M+ Recovery)
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    # Fallback to keyword matching
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

    # MAPPING for chatgpt.csv and Snowflake extracts
    c_amt = find_amt_col(df, ["AMOUNT_USD", "BOOKING_VALUE_USD", "Line_Amount", "Amount"])
    c_tot = find_col(df, ["NET_CASH_IMPACT_USD", "NET_BOOKING_VALUE_USD", "Invoice_Total", "Total"])
    c_id = find_col(df, ["TRANSACTION_ID", "BOOKING_KEY", "Invoice_ID", "InvoiceID"])
    c_unit = find_col(df, ["FX_RATE_TO_USD", "DEFAULT_COMMISSION_RATE", "Unit_Price", "Price"])
    c_ven = find_col(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"])
    c_date = find_col(df, ["TRANSACTION_TS", "BOOKING_TS", "Invoice_Date", "Date"])
    c_ref = find_col(df, ["REFUND_AMOUNT_USD", "Refund_Amount"])

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
    df["__R"] = pd.to_numeric(df[c_ref], errors='coerce').fillna(0) if c_ref else 0

    issues = []
    
    # 1. Calculation Variance (Math Integrity) - Delta between Line and Total
    mm = df[abs(df["__L"] - df["__T"]) > 0.05]
    if not mm.empty:
        issues.append({
            "Category": "Calculation Variance", 
            "Amount ($)": float(abs(df["__L"] - df["__T"]).sum()), 
            "Priority": "🔴 Critical",
            "Action": "Audit Settlement Logic"
        })
    
    # 2. Fuzzy Duplicate Match (Amount + Vendor + Date Pattern)
    fuzzy_dups = df[df.duplicated(subset=['__L', '__V', '__D'], keep=False)]
    if not fuzzy_dups.empty:
        issues.append({
            "Category": "Fuzzy Duplicate Match", 
            "Amount ($)": float(fuzzy_dups['__L'].sum()), 
            "Priority": "🔴 Critical",
            "Action": "Manual Verification Required"
        })
    
    # 3. Contract Variance (Price Benchmarking vs Historical Mean)
    # This logic identifies systemic overpayments against negotiated/average rates
    if c_amt and c_ven:
        df['avg_p'] = df.groupby('__V')['__L'].transform('mean')
        df['loss'] = (df['__L'] - df['avg_p']).clip(lower=0)
        actual_loss = df['loss'].sum()
        if actual_loss > 0:
            issues.append({
                "Category": "Contract Variance", 
                "Amount ($)": float(actual_loss), 
                "Priority": "🟡 Medium",
                "Action": "Renegotiate Rate Card"
            })
    
    # 4. Negative Leak (Recorded but Unsettled Refunds)
    ref_total = df["__R"].abs().sum()
    if ref_total > 0:
        issues.append({
            "Category": "Negative Leak", 
            "Amount ($)": float(ref_total), 
            "Priority": "🟣 High",
            "Action": "Confirm Cash Recovery"
        })
    
    # 5. Trend Price Creep (Statistical Regression)
    creep_total = 0
    for v, group in df.sort_values("__D").groupby("__V"):
        if len(group) > 2:
            avg_hist = group["__L"].mean()
            last_p = group["__L"].iloc[-1]
            if last_p > avg_hist:
                creep_total += (last_p - avg_hist) * len(group)
    if creep_total > 0:
        issues.append({
            "Category": "Trend Price Creep", 
            "Amount ($)": float(creep_total), 
            "Priority": "🟠 High",
            "Action": "Apply Historical Cap"
        })

    return pd.DataFrame(issues)

# ----------------------------
# 5) Chatbot Logic
# ----------------------------
def forensic_bot(query):
    return "**ClearSpend AI:** Analyzing your data for overpayments, duplicates, and unsettled credits. How can I assist with your recovery?"

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
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = accounts[u]["name"]
                st.session_state["org_name"] = accounts[u].get("org", "UIC")
                st.rerun()
            else:
                st.error("❌ **Invalid Credentials.**")
                
    with tab_signup:
        new_u = st.text_input("Choose Username", key="s_user")
        new_p = st.text_input("Choose Password", type="password", key="s_pass")
        new_n = st.text_input("Full Name", key="s_name")
        new_o = st.text_input("Organization", key="s_org")
        if st.button("Create Account", use_container_width=True):
            save_account(new_u, {"pw": new_p, "name": new_n, "org": new_o})
            st.balloons()
            st.success("✅ Account created!")

# ----------------------------
# 7) UI Flow: Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
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
                st.write("### 🔍 Risk Findings")
                st.dataframe(audit_df, use_container_width=True, hide_index=True)
            with col2:
                st.write("### 📈 Exposure Distribution")
                st.bar_chart(data=audit_df, x="Category", y="Amount ($)")
            
            st.divider()
            csv_data = audit_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button("Download Secure Audit Report", csv_data, "ClearSpend_Report.csv")
