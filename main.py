import streamlit as st
import pandas as pd
import time

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

# Premium Styling
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
</style>
""", unsafe_allow_html=True)

# ----------------------------
# 2) State Management
# ----------------------------
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "user_name" not in st.session_state:
    st.session_state["user_name"] = "Raghini Kumar"
if "org_name" not in st.session_state:
    st.session_state["org_name"] = "UIC"
if "accounts" not in st.session_state:
    st.session_state["accounts"] = {"admin": "uic2026"}

# ----------------------------
# Helpers & Forensic Logic
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    if not isinstance(df, pd.DataFrame): return None
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        key = _norm(cand)
        if key in norm_map: return norm_map[key]
    return None

def load_uploaded(uploaded_file) -> pd.DataFrame:
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded_file, encoding='utf-8', on_bad_lines='skip')
        else:
            return pd.read_excel(uploaded_file, engine="openpyxl")
    except:
        return pd.DataFrame()

def build_audit(df_raw: pd.DataFrame):
    df = df_raw.copy()
    col_invoice = find_col(df, ["Invoice_ID", "InvoiceID"])
    col_vendor  = find_col(df, ["Vendor_Name", "VendorName", "Vendor"])
    col_invdate = find_col(df, ["Invoice_Date", "Date"])
    col_unit    = find_col(df, ["Unit_Price", "Price"])
    col_lineamt = find_col(df, ["Line_Amount", "Amount"])
    col_total   = find_col(df, ["Invoice_Total", "Total"])

    df["__Invoice_ID"] = df[col_invoice].astype(str) if col_invoice else "N/A"
    df["__Vendor"] = df[col_vendor].astype(str) if col_vendor else "N/A"
    df["__Line_Amount"] = pd.to_numeric(df[col_lineamt], errors='coerce').fillna(0)
    df["__Invoice_Total"] = pd.to_numeric(df[col_total], errors='coerce').fillna(0)
    df["__Unit_Price"] = pd.to_numeric(df[col_unit], errors='coerce').fillna(0)
    df["__Invoice_Date"] = pd.to_datetime(df[col_invdate], errors='coerce')

    issues = []
    # 1. Duplicates
    dup_ids = df["__Invoice_ID"][df["__Invoice_ID"].duplicated(keep=False)]
    if not dup_ids.empty:
        amt = df[df["__Invoice_ID"].isin(dup_ids.unique())]["__Invoice_Total"].sum()
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(amt), "Priority": "🔴 Critical"})
    
    # 2. Price Creep
    creep_amt = 0
    for v, group in df.sort_values("__Invoice_Date").groupby("__Vendor"):
        if len(group) > 1:
            diff = group["__Unit_Price"].iloc[-1] - group["__Unit_Price"].iloc[0]
            if diff > 0: creep_amt += diff * group["__Line_Amount"].count()
    if creep_amt > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})

    # 3. Negatives
    negs = df[df["__Line_Amount"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__Line_Amount"].abs().sum()), "Priority": "🟣 High"})

    return pd.DataFrame(issues)

# ----------------------------
# 3) UI Logic (Login/Signup)
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    tab1, tab2 = st.tabs(["Login", "Create Account"])
    
    with tab1:
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Log In"):
            if u in st.session_state["accounts"] and st.session_state["accounts"][u] == p:
                st.session_state["logged_in"] = True
                st.rerun()
            else:
                st.error("Invalid credentials.")
                
    with tab2:
        st.subheader("Welcome to the ClearSpend Family! 🎈")
        new_u = st.text_input("New Username")
        new_p = st.text_input("New Password", type="password")
        new_n = st.text_input("Full Name")
        if st.button("Sign Up"):
            if new_u and new_p:
                st.session_state["accounts"][new_u] = new_p
                st.session_state["user_name"] = new_n
                # --- BALLOONS TRIGGERED HERE ---
                st.balloons()
                st.success(f"Account for {new_n} created! You can now log in.")
            else:
                st.warning("Please enter a username and password.")

# ----------------------------
# 4) Main Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.write(f"👤 **{st.session_state['user_name']}**")
        if st.button("Log Out"):
            st.session_state["logged_in"] = False
            st.rerun()

    st.title("📊 Executive Recovery Dashboard")
    f = st.file_uploader("Upload AP Ledger", type=["csv", "xlsx"])

    if f:
        with st.status("🚀 AI Engine Scanning..."):
            raw = load_uploaded(f)
            audit_df = build_audit(raw)
        
        # KPI Row
        m1, m2, m3 = st.columns(3)
        total = audit_df["Amount ($)"].sum() if not audit_df.empty else 0
        m1.metric("Recoverable Cash", f"${total:,.2f}")
        m2.metric("Leaks Found", len(audit_df))
        m3.metric("ROI", f"{(total/15000):.1f}x")

        st.divider()
        
        # FILTERS & CHARTS
        col_f, col_c = st.columns([1, 1.5])
        
        with col_f:
            st.write("### 🔍 Filters")
            if not audit_df.empty:
                cats = st.multiselect("Select Categories", options=audit_df["Category"].unique(), default=audit_df["Category"].unique())
                filtered = audit_df[audit_df["Category"].isin(cats)]
                st.dataframe(filtered, use_container_width=True, hide_index=True)
            else:
                st.info("No leaks detected.")

        with col_c:
            st.write("### 📈 Leak Distribution")
            if not audit_df.empty and not filtered.empty:
                st.bar_chart(data=filtered, x="Category", y="Amount ($)")
            
        # DOWNLOAD SECTION
        if not audit_df.empty:
            st.divider()
            st.write("### 📥 Export Evidence")
            csv = filtered.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="Download Recovery Report (CSV)",
                data=csv,
                file_name='ClearSpend_Recovery_Report.csv',
                mime='text/csv'
            )
