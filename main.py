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
# 2) Persistence Logic (Memory Fix)
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
# 4) Forensic Engine (Heuristic Backup Plan)
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_id_col(df: pd.DataFrame, candidates: list[str]):
    # Strategy A: Exact Match
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    # Strategy B: Fuzzy Keywords (Backup Plan)
    for col in df.columns:
        c_low = col.lower()
        if any(w in c_low for w in ["id", "key", "ref", "num", "code"]): return col
    return None

def find_amt_col(df: pd.DataFrame, candidates: list[str]):
    # Strategy A: Exact Match
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    # Strategy B: Numeric Profile (Backup Plan)
    numeric_cols = df.select_dtypes(include=['number']).columns
    if not numeric_cols.empty:
        # Guess the column with the highest average value (usually the total)
        return df[numeric_cols].mean().idxmax()
    return None

def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    
    # Clean currency characters
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)

    # Smart Mapping
    c_amt = find_amt_col(df, ["BOOKING_VALUE_USD", "AMOUNT_USD", "Line_Amount", "Amount", "Total"])
    c_tot = find_col_name(df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total_Amount"]) 
    c_id = find_id_col(df, ["BOOKING_KEY", "TRANSACTION_ID", "Invoice_ID", "InvoiceID"])
    c_unit = find_col_name(df, ["FX_RATE_TO_USD", "Unit_Price", "Price"])
    c_ven = find_col_name(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"])
    c_date = find_col_name(df, ["TRANSACTION_TS", "Invoice_Date", "Date"])

    # Emergency Fallback: If no ID found, use index
    if not c_id:
        df["__ID_VIRTUAL"] = range(len(df))
        c_id = "__ID_VIRTUAL"
    
    # Critical Failure: If no Amount column exists anywhere
    if not c_amt: return pd.DataFrame()

    # Normalize data for 5-Factor Logic
    df["__L"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[c_tot], errors='coerce').fillna(0) if c_tot else df["__L"]
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"] = df[c_id].astype(str)
    df["__V"] = df[c_ven].astype(str) if c_ven else "Unknown Vendor"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    
    # 1. Math Integrity
    mm = df[abs(df["__L"] - df["__T"]) > 0.05]
    if not mm.empty:
        issues.append({"Category": "Math Integrity Check", "Amount ($)": float(abs(mm["__T"] - mm["__L"]).sum()), "Priority": "🔴 Critical"})
    
    # 2. Duplicate Invoice
    dup_ids = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__ID"] != "N/A").any():
        issues.append({"Category": "Duplicate Transaction", "Amount ($)": float(df[df["__ID"].isin(dup_ids.unique())]["__L"].sum()), "Priority": "🔴 Critical"})
    
    # 3. Price/FX Creep
    creep_amt = 0
    for v, group in df.sort_values("__D").groupby("__V"):
        if len(group) > 1:
            diff = group["__U"].iloc[-1] - group["__U"].iloc[0]
            if diff > 0: creep_amt += diff * len(group)
    if creep_amt > 0:
        issues.append({"Category": "Price/FX Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})
    
    # 4. Negative Leak (Refunds)
    negs = df[df["__L"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__L"].abs().sum()), "Priority": "🟣 High"})
    
    # 5. Pricing Inconsistency
    if c_unit:
        inc_count = (df.groupby("__V")["__U"].nunique() > 1).sum()
        if inc_count > 0:
            issues.append({"Category": "Pricing Inconsistency", "Amount ($)": float(inc_count * 500), "Priority": "🟡 Medium"})

    return pd.DataFrame(issues)

def find_col_name(df, candidates):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    return None

# ----------------------------
# 5) UI Flow: Security Portal
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Register"])
    with t1:
        u = st.text_input("User", key="lu")
        p = st.text_input("Pass", type="password", key="lp")
        if st.button("Log In", use_container_width=True):
            db = load_accounts()
            if u in db and db[u]["pw"] == p:
                st.session_state.update({"logged_in": True, "user_name": db[u]["name"], "org_name": db[u].get("org", "UIC")})
                st.rerun()
            else: st.error("❌ Invalid")
    with t2:
        nu, np, nn, no = st.text_input("User", key="su"), st.text_input("Pass", type="password", key="sp"), st.text_input("Name"), st.text_input("Org")
        if st.button("Create Account", use_container_width=True):
            save_account(nu, {"pw": np, "name": nn, "org": no})
            st.balloons()
            st.success("✅ Created")

# ----------------------------
# 6) UI Flow: Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        if st.button("Log Out"): 
            st.session_state["logged_in"] = False
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload Finance Data (CSV)", type=["csv"])

    if f:
        df = pd.read_csv(f)
        results = build_audit(df)
        st.write("### Data Preview", df.head())
        
        if not results.empty:
            st.metric("Total Recoverable Cash Found", f"${results['Amount ($)'].sum():,.2f}")
            c1, c2 = st.columns([1, 1.5])
            with c1: st.dataframe(results, hide_index=True)
            with c2: st.bar_chart(data=results, x="Category", y="Amount ($)")
        else:
            st.success("✅ No leaks detected based on current mapping.")
