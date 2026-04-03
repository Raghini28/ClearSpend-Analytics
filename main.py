import streamlit as st
import pandas as pd
import json
import os

# ----------------------------
# 1) Page Configuration & Style
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
[data-testid="stChatMessage"] p { 
    color: #000000 !important; 
    font-weight: 700 !important; 
    font-size: 1.05rem;
}
</style>
""", unsafe_allow_html=True)

# ----------------------------
# 2) Persistence Logic
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
# 4) Forensic Engine (Validation Logic & Backup Plan)
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_id_col(df: pd.DataFrame, candidates: list):
    # Try exact matches
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    # Backup Plan: Look for ID/Key keywords
    for col in df.columns:
        if any(w in col.lower() for w in ["id", "key", "ref", "num"]): return col
    return None

def find_amt_col(df: pd.DataFrame, candidates: list):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    # Backup Plan: Pick the column with the highest numbers
    nums = df.select_dtypes(include=['number']).columns
    if not nums.empty: return df[nums].mean().idxmax()
    return None

def find_general_col(df: pd.DataFrame, candidates: list):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    return None

def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    
    # Currency Cleaning
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)

    # 5-Factor Mapping (Snowflake & CSV Ready)
    c_amt = find_amt_col(df, ["BOOKING_VALUE_USD", "AMOUNT_USD", "Line_Amount", "Amount"])
    c_tot = find_general_col(df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total"])
    c_id = find_id_col(df, ["BOOKING_KEY", "TRANSACTION_ID", "Invoice_ID", "InvoiceID"])
    c_unit = find_general_col(df, ["FX_RATE_TO_USD", "Unit_Price", "Price"])
    c_ven = find_general_col(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"])
    c_date = find_general_col(df, ["TRANSACTION_TS", "Invoice_Date", "Date"])

    if not c_amt: return pd.DataFrame()
    if not c_id:
        df["__ID"] = range(len(df))
        c_id = "__ID"

    # Normalize Columns
    df["__L"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[c_tot], errors='coerce').fillna(0) if c_tot else df["__L"]
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"] = df[c_id].astype(str)
    df["__V"] = df[c_ven].astype(str) if c_ven else "Unknown"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    
    # Logic 1: Math Integrity
    mm = df[abs(df["__L"] - df["__T"]) > 0.05]
    if not mm.empty:
        issues.append({"Category": "Math Integrity Check", "Amount ($)": float(abs(mm["__T"] - mm["__L"]).sum()), "Priority": "🔴 Critical"})
    
    # Logic 2: Duplicates
    dup = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dup.empty:
        issues.append({"Category": "Duplicate Transaction", "Amount ($)": float(df[df["__ID"].isin(dup.unique())]["__L"].sum()), "Priority": "🔴 Critical"})
    
    # Logic 3: Price Creep
    creep = 0
    for v, g in df.sort_values("__D").groupby("__V"):
        if len(g) > 1:
            diff = g["__U"].iloc[-1] - g["__U"].iloc[0]
            if diff > 0: creep += diff * len(g)
    if creep > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep), "Priority": "🟠 High"})
    
    # Logic 4: Negative Leak
    negs = df[df["__L"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__L"].abs().sum()), "Priority": "🟣 High"})
    
    # Logic 5: Pricing Inconsistency
    if c_unit:
        inc = (df.groupby("__V")["__U"].nunique() > 1).sum()
        if inc > 0: issues.append({"Category": "Pricing Inconsistency", "Amount ($)": float(inc * 500), "Priority": "🟡 Medium"})

    return pd.DataFrame(issues)

# ----------------------------
# 5) Chatbot
# ----------------------------
def forensic_bot(query):
    return "**ClearSpend AI:** Analyzing your financial data for duplicates, math errors, and price creep. How can I help?"

# ----------------------------
# 6) UI Flow
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Register"])
    with t1:
        u, p = st.text_input("User", key="u"), st.text_input("Pass", type="password", key="p")
        if st.button("Log In"):
            db = load_accounts()
            if u in db and db[u]["pw"] == p:
                st.session_state.update({"logged_in": True, "user_name": db[u]["name"], "org_name": db[u].get("org", "UIC")})
                st.rerun()
    with t2:
        nu, np, nn, no = st.text_input("User", key="nu"), st.text_input("Pass", type="password", key="np"), st.text_input("Name"), st.text_input("Org")
        if st.button("Sign Up"):
            save_account(nu, {"pw": np, "name": nn, "org": no})
            st.success("Account Created!")

else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        if st.button("Log Out"): 
            st.session_state["logged_in"] = False
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload Finance CSV", type=["csv"])
    if f:
        df = pd.read_csv(f)
        st.session_state.audit_data = build_audit(df)
        st.write("### Data Preview", df.head())
        if st.session_state.audit_data is not None and not st.session_state.audit_data.empty:
            res = st.session_state.audit_data
            st.metric("Total Recoverable Cash Found", f"${res['Amount ($)'].sum():,.2f}")
            st.dataframe(res, hide_index=True)
            st.bar_chart(data=res, x="Category", y="Amount ($)")
