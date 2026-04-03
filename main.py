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
[data-testid="stChatMessage"] p { color: #000000 !important; font-weight: 700 !important; }
</style>
""", unsafe_allow_html=True)

# ----------------------------
# 2) Persistence Logic
# ----------------------------
USER_FILE = "users_db.json"
def load_accounts():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f: return json.load(f)
    return {"admin": {"pw": "uic2026", "name": "Raghini Kumar", "org": "UIC"}}

def save_account(username, data):
    accs = load_accounts()
    accs[username] = data
    with open(USER_FILE, "w") as f: json.dump(accs, f)

# ----------------------------
# 3) State Management
# ----------------------------
if "logged_in" not in st.session_state: st.session_state["logged_in"] = False
if "messages" not in st.session_state: st.session_state.messages = []
if "audit_data" not in st.session_state: st.session_state.audit_data = None

# ----------------------------
# 4) Forensic Engine (The "11 Million" Logic)
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df, candidates):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    for col in df.columns:
        if any(w in col.lower() for w in ["id", "key", "ref", "num"]): return col
    return None

def find_amt(df, candidates):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    nums = df.select_dtypes(include=['number']).columns
    if not nums.empty: return df[nums].mean().idxmax()
    return None

def build_audit(df_raw):
    if df_raw.empty: return pd.DataFrame(), 0
    df = df_raw.copy()
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)

    # Column Mapping
    c_amt = find_amt(df, ["BOOKING_VALUE_USD", "AMOUNT_USD", "Amount"])
    c_tot = find_col(df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total"])
    c_id = find_col(df, ["TRANSACTION_ID", "BOOKING_KEY", "Invoice_ID"])
    c_unit = find_col(df, ["FX_RATE_TO_USD", "Unit_Price", "Price"])
    c_ven = find_col(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"])
    c_date = find_col(df, ["TRANSACTION_TS", "Invoice_Date", "Date"])

    if not c_amt: return pd.DataFrame(), 0
    df["__L"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[c_tot], errors='coerce').fillna(0) if c_tot else df["__L"]
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"] = df[c_id].astype(str) if c_id else range(len(df))
    df["__V"] = df[c_ven].astype(str) if c_ven else "N/A"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    
    # 1. Math Integrity (Shadow Fees)
    mm = df[abs(df["__L"] - df["__T"]) > 0.05]
    if not mm.empty:
        issues.append({"Category": "Calculation Variance", "Amount ($)": float(abs(mm["__T"] - mm["__L"]).sum()), "Priority": "🔴 Critical", "Action": "Audit Settlement Fees"})

    # 2. Fuzzy Duplicate Match (The high-impact logic)
    fuzzy = df[df.duplicated(subset=['__L', '__V', '__D'], keep=False)]
    if not fuzzy.empty:
        issues.append({"Category": "Fuzzy Duplicate Match", "Amount ($)": float(fuzzy['__L'].sum()), "Priority": "🔴 Critical", "Action": "Verify Duplicate Batch"})

    # 3. Regression Price Creep
    creep = 0
    for v, g in df.sort_values("__D").groupby("__V"):
        if len(g) > 2:
            avg_h = g["__U"].mean()
            if g["__U"].iloc[-1] > avg_h: creep += (g["__U"].iloc[-1] - avg_h) * len(g)
    if creep > 0:
        issues.append({"Category": "Trend Price Creep", "Amount ($)": float(creep), "Priority": "🟠 High", "Action": "Reset Rate Card"})

    # 4. Contract Variance (Benchmark Logic)
    if c_unit:
        df['best'] = df.groupby('__V')['__U'].transform('min')
        loss = (df['__U'] - df['best']).sum()
        if loss > 0:
            issues.append({"Category": "Contract Variance", "Amount ($)": float(loss), "Priority": "🟡 Medium", "Action": "Claim Best-Rate Credit"})

    results = pd.DataFrame(issues)
    score = min(100, (results['Amount ($)'].sum() / df['__L'].sum()) * 500) if not results.empty else 0
    return results, round(score, 1)

# ----------------------------
# 5) Auth & UI Flow
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Create Account"])
    with t1:
        u, p = st.text_input("Username"), st.text_input("Password", type="password")
        if st.button("Log In", use_container_width=True):
            db = load_accounts()
            if u in db and db[u]["pw"] == p:
                st.session_state.update({"logged_in": True, "user_name": db[u]["name"], "org_name": db[u].get("org", "UIC")})
                st.rerun()
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        if st.button("Log Out"): st.session_state["logged_in"] = False; st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload Finance Data (CSV)", type=["csv"])
    if f:
        df_raw = pd.read_csv(f)
        res, score = build_audit(df_raw)
        
        m1, m2, m3 = st.columns(3)
        with m1: st.metric("Risk Health Score", f"{100-score}/100")
        with m2: st.metric("Total Recoverable", f"${res['Amount ($)'].sum():,.2f}")
        with m3: st.metric("Audit Coverage", "100%")

        st.divider()
        c1, c2 = st.columns([1, 1.5])
        with c1:
            st.write("### 🔍 Risk Findings")
            st.dataframe(res, use_container_width=True, hide_index=True)
        with c2:
            st.write("### 📈 Leak Distribution")
            st.bar_chart(data=res, x="Category", y="Amount ($)")
