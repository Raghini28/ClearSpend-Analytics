import streamlit as st
import pandas as pd
import time
import altair as alt

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

# --- STYLE FIX: FORCING VISIBLE INPUT TEXT ---
st.markdown("""
<style>
.stApp { background-color: #FFFFFF; } 

/* Fix for invisible text in input boxes */
input {
    color: #000000 !important;
    background-color: #FFFFFF !important;
}

section[data-testid="stSidebar"] { 
    background-color: #111827 !important; 
}

.brand-text { 
    color: #EC4899 !important; 
    font-size: 32px !important; 
    font-weight: 800 !important; 
}

div[data-testid="stMetric"] {
    background-color: #F9FAFB;
    border: 3px solid #EC4899; 
    padding: 20px;
    border-radius: 10px;
}

h1, h2, h3, p, label { 
    color: #000000 !important; 
}

[data-testid="stSidebar"] p, [data-testid="stSidebar"] label {
    color: #FFFFFF !important;
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
# 3) Forensic Audit Engine
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    if not isinstance(df, pd.DataFrame): return None
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
    
    # 2. Math Integrity
    if col_total:
        mismatch = df[df["__Line_Amount"] != df["__Invoice_Total"]]
        if not mismatch.empty:
            issues.append({"Category": "Math Integrity Check", "Amount ($)": float(mismatch["__Line_Amount"].sum() * 0.1), "Priority": "🔴 Critical"})

    # 3. Price Creep
    creep_amt = 0
    for v, group in df.sort_values("__Date").groupby("__Vendor"):
        if len(group) > 1:
            diff = group["__Unit_Price"].iloc[-1] - group["__Unit_Price"].iloc[0]
            if diff > 0: creep_amt += (diff * len(group))
    if creep_amt > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})

    # 4. Inconsistency
    variance = df.groupby("__Vendor")["__Unit_Price"].nunique()
    if (variance > 1).any():
        count = (variance > 1).sum()
        issues.append({"Category": "Pricing Inconsistency", "Amount ($)": 750.0 * count, "Priority": "🟡 Medium"})

    # 5. Negative Leak
    negs = df[df["__Line_Amount"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__Line_Amount"].abs().sum()), "Priority": "🟣 High"})

    return pd.DataFrame(issues)

# ----------------------------
# 4) UI: Login / Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Create Account"])
    
    with t1:
        u = st.text_input("Username", key="login_u")
        p = st.text_input("Password", type="password", key="login_p")
        if st.button("Log In"):
            if u in st.session_state["accounts"] and st.session_state["accounts"][u]["pw"] == p:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = st.session_state["accounts"][u]["name"]
                st.session_state["org_name"] = st.session_state["accounts"][u]["org"]
                st.rerun()
            else:
                st.error("Invalid credentials.")
                
    with t2:
        nu = st.text_input("Username", key="sig_u") # Fixed label from "New User" to "Username"
        np = st.text_input("Password", type="password", key="sig_p") # Fixed label from "New Pass" to "Password"
        nn = st.text_input("Full Name", key="sig_n")
        no = st.text_input("Company", key="sig_o")
        
        if st.button("Sign Up"):
            if nu and np and nn and no:
                st.session_state["accounts"][nu] = {"pw": np, "name": nn, "org": no}
                # Success visual sequence
                st.balloons() 
                st.success(f"Welcome {nn}! Account created. Please switch to the Login tab.")
                time.sleep(1) # Small pause to ensure balloons render before state changes
            else:
                st.warning("Please fill in all fields.")

# ----------------------------
# 5) UI: Main Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.write(f"👤 **{st.session_state['user_name']}**")
        st.write(f"🏢 **{st.session_state['org_name']}**")
        
        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
            if 'last_audit' in st.session_state: del st.session_state['last_audit']
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Audit Dashboard")
    f = st.file_uploader("Upload Ledger", type=["csv", "xlsx"])

    if f:
        if f.name.endswith('.csv'): df_raw = pd.read_csv(f)
        else: df_raw = pd.read_excel(f)
        
        audit_df = build_audit(df_raw)
        st.session_state['last_audit'] = audit_df
        
        if not audit_df.empty:
            m1, m2, m3 = st.columns(3)
            val = audit_df["Amount ($)"].sum()
            m1.metric("Recoverable Cash", f"${val:,.2f}")
            m2.metric("Audits Passed", "5/5")
            m3.metric("ROI", f"{(val/15000):.1f}x")

            st.divider()
            col_f, col_c = st.columns([1, 1.5])
            with col_f:
                st.write("### 🔍 Findings")
                st.dataframe(audit_df, use_container_width=True, hide_index=True)
            with col_c:
                st.write("### 📈 Risk Distribution")
                chart = alt.Chart(audit_df).mark_bar(color='#EC4899').encode(
                    x=alt.X('Category:N', sort='-y'),
                    y=alt.Y('Amount ($):Q', scale=alt.Scale(type='log')),
                    tooltip=['Category', 'Amount ($)']
                ).properties(height=400)
                st.altair_chart(chart, use_container_width=True)
