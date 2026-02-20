import streamlit as st
import pandas as pd
import time

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

# ----------------------------
# Premium Styling
# ----------------------------
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

# ----------------------------
# Helpers
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        key = _norm(cand)
        if key in norm_map:
            return norm_map[key]
    for cand in candidates:
        key = _norm(cand)
        for k, real in norm_map.items():
            if key and (key in k or k in key):
                return real
    return None

def to_dt(s):
    return pd.to_datetime(s, errors="coerce")

def to_num(s):
    return pd.to_numeric(s, errors="coerce")

def load_uploaded(uploaded_file, sheet_name=None) -> pd.DataFrame:
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            # Added encoding to handle special characters in CSVs
            return pd.read_csv(uploaded_file, encoding='utf-8', on_bad_lines='skip')
        if name.endswith(".xlsx"):
            return pd.read_excel(uploaded_file, engine="openpyxl", sheet_name=sheet_name)
    except Exception as e:
        st.error(f"Error reading file: {e}")
        return pd.DataFrame() # Returns an empty table instead of crashing
    raise ValueError("Unsupported file type.")

def build_audit(df_raw: pd.DataFrame):
    df = df_raw.copy()

    # Column Mapping
    col_invoice = find_col(df, ["Invoice_ID", "Invoice ID", "InvoiceNumber"])
    col_vendor  = find_col(df, ["Vendor_Name", "Vendor Name", "Vendor"])
    col_invdate = find_col(df, ["Invoice_Date", "Invoice Date", "Date"])
    col_unit    = find_col(df, ["Unit_Price", "Unit Price", "Price"])
    col_lineamt = find_col(df, ["Line_Amount", "Line Amount", "Amount"])
    col_total   = find_col(df, ["Invoice_Total", "Invoice Total", "Total"])

    # Normalization
    df["__Invoice_ID"] = df[col_invoice].astype(str).str.strip() if col_invoice else "UNKNOWN"
    df["__Vendor"]     = df[col_vendor].astype(str).str.strip() if col_vendor else "UNKNOWN"
    df["__Invoice_Date"] = to_dt(df[col_invdate]) if col_invdate else pd.NaT
    df["__Unit_Price"] = to_num(df[col_unit]) if col_unit else 0.0
    df["__Line_Amount"]= to_num(df[col_lineamt]) if col_lineamt else 0.0
    df["__Invoice_Total"] = to_num(df[col_total]) if col_total else 0.0

    issues = []

    # 1) Duplicate Invoices
    dup_ids = df["__Invoice_ID"][df["__Invoice_ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__Invoice_ID"] != "UNKNOWN").any():
        amt = df[df["__Invoice_ID"].isin(dup_ids.unique())].groupby("__Invoice_ID")["__Invoice_Total"].max().sum()
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(amt), "Count": int(dup_ids.nunique()), "Priority": "🔴 Critical"})

    # 2) STRATEGIC ADDITION: Price Creep (Contract Drift)
    # Checks if unit price increased for the same vendor over time
    if col_vendor and col_unit and col_invdate:
        creep_amt = 0
        creep_count = 0
        for vendor, group in df.sort_values("__Invoice_Date").groupby("__Vendor"):
            if len(group) > 1:
                first_price = group["__Unit_Price"].iloc[0]
                last_price = group["__Unit_Price"].iloc[-1]
                if last_price > (first_price * 1.05): # 5% threshold
                    creep_amt += (last_price - first_price) * group["__Line_Amount"].count()
                    creep_count += 1
        if creep_count > 0:
            issues.append({"Category": "Price Creep / Drift", "Amount ($)": float(creep_amt), "Count": int(creep_count), "Priority": "🟠 High"})

    # 3) Negative values
    neg_count = (df["__Line_Amount"] < 0).sum()
    if neg_count > 0:
        amt = df.loc[df["__Line_Amount"] < 0, "__Line_Amount"].abs().sum()
        issues.append({"Category": "Negative Amount Leak", "Amount ($)": float(amt), "Count": int(neg_count), "Priority": "🟣 High"})

    # 4) Total Mismatch
    if col_invoice and col_lineamt and col_total:
        sums = df.groupby("__Invoice_ID")["__Line_Amount"].sum()
        totals = df.groupby("__Invoice_ID")["__Invoice_Total"].max()
        mismatch = (sums.round(2) != totals.round(2)).sum()
        if mismatch > 0:
            issues.append({"Category": "Invoice Total Mismatch", "Amount ($)": float(sums.sum() * 0.05), "Count": int(mismatch), "Priority": "🔴 Critical"})

    audit = pd.DataFrame(issues)
    if audit.empty:
        audit = pd.DataFrame([{"Category": "Clean Audit", "Amount ($)": 0.0, "Count": 0, "Priority": "🟢 Low"}])

    audit.insert(0, "Vendor", df["__Vendor"].iloc[0] if not df.empty else "N/A")
    return audit, df

def money_fmt(x):
    return f"${float(x):,.2f}"

# ----------------------------
# 3) UI Logic
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    _, center_col, _ = st.columns([1, 2, 1])
    with center_col:
        user = st.text_input("Username")
        pw = st.text_input("Password", type="password")
        if st.button("Access Dashboard", use_container_width=True):
            if user == "admin" and pw == "uic2026": # STRATEGIC NOTE: Use secrets.toml for production
                st.session_state["logged_in"] = True
                st.rerun()
            else:
                st.error("Invalid credentials")
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.write(f"👤 **{st.session_state['user_name']}**")
        if st.button("Log Out"):
            st.session_state["logged_in"] = False
            st.rerun()

    st.title("📊 Executive Recovery Dashboard")
    uploaded_file = st.file_uploader("Upload AP Ledger", type=["csv", "xlsx"])

    if uploaded_file:
        with st.status("🚀 AI Forensic Engine Scanning...", expanded=False):
            df_raw = load_uploaded(uploaded_file)
            audit_df, df_work = build_audit(df_raw)

        total_leak = audit_df["Amount ($)"].sum()
        m1, m2, m3 = st.columns(3)
        m1.metric("Recoverable Cash", money_fmt(total_leak))
        m2.metric("Leaks Found", int(audit_df["Count"].sum()))
        m3.metric("Projected ROI", f"{(total_leak/15000):.1f}x" if total_leak > 0 else "0x")

        st.divider()
        st.write("### 🔍 Audit Detail")
        st.dataframe(audit_df, use_container_width=True, hide_index=True)

        with st.expander("🧪 View Parsed Data"):
            st.dataframe(df_raw.head(10))
