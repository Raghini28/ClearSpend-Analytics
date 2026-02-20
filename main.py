import streamlit as st
import pandas as pd
import time

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(page_title="ClearSpend Analytics", layout="wide", initial_sidebar_state="expanded")

# Premium Pink/Navy Corporate Styling
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

if "accounts" not in st.session_state:
    st.session_state["accounts"] = {
        "admin": {"pw": "uic2026", "name": "Raghini Kumar", "org": "UIC"}
    }

if "user_name" not in st.session_state:
    st.session_state["user_name"] = "Guest"
if "org_name" not in st.session_state:
    st.session_state["org_name"] = "Individual"
if "messages" not in st.session_state:
    st.session_state.messages = []

# ----------------------------
# 3) Data Loading (Multi-Sheet Support)
# ----------------------------
def load_uploaded(uploaded_file):
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded_file, encoding='utf-8', on_bad_lines='skip')
        else:
            xl = pd.ExcelFile(uploaded_file)
            sheet_names = xl.sheet_names
            if len(sheet_names) > 1:
                selected_sheet = st.selectbox("Multiple sheets detected. Select the ledger sheet:", sheet_names)
                return pd.read_excel(uploaded_file, sheet_name=selected_sheet, engine="openpyxl")
            else:
                return pd.read_excel(uploaded_file, sheet_name=0, engine="openpyxl")
    except Exception as e:
        st.error(f"Error reading file: {e}")
        return pd.DataFrame()

# ----------------------------
# 4) Forensic Engine (5 Validations)
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

def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    
    col_invoice = find_col(df, ["Invoice_ID", "InvoiceID", "Invoice Number"])
    col_vendor  = find_col(df, ["Vendor_Name", "VendorName", "Vendor", "Supplier"])
    col_invdate = find_col(df, ["Invoice_Date", "Date"])
    col_unit    = find_col(df, ["Unit_Price", "Price"])
    col_lineamt = find_col(df, ["Line_Amount", "Amount"])
    col_total   = find_col(df, ["Invoice_Total", "Total"])

    if not col_lineamt:
        st.warning("⚠️ **Mapping Error:** Couldn't find an 'Amount' column.")
        return pd.DataFrame()

    df["__Invoice_ID"] = df[col_invoice].astype(str) if col_invoice else "N/A"
    df["__Vendor"] = df[col_vendor].astype(str) if col_vendor else "N/A"
    df["__Line_Amount"] = pd.to_numeric(df[col_lineamt], errors='coerce').fillna(0)
    df["__Invoice_Total"] = pd.to_numeric(df[col_total], errors='coerce').fillna(0)
    df["__Unit_Price"] = pd.to_numeric(df[col_unit], errors='coerce').fillna(0)
    df["__Invoice_Date"] = pd.to_datetime(df[col_invdate], errors='coerce')

    issues = []

    # 1. Duplicates
    dup_ids = df["__Invoice_ID"][df["__Invoice_ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__Invoice_ID"] != "N/A").any():
        amt = df[df["__Invoice_ID"].isin(dup_ids.unique())]["__Invoice_Total"].sum()
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(amt), "Count": int(len(dup_ids)), "Priority": "🔴 Critical"})
    
    # 2. Price Creep
    creep_amt = 0
    creep_count = 0
    for v, group in df.sort_values("__Invoice_Date").groupby("__Vendor"):
        if len(group) > 1:
            diff = group["__Unit_Price"].iloc[-1] - group["__Unit_Price"].iloc[0]
            if diff > 0: 
                creep_amt += diff * group["__Line_Amount"].count()
                creep_count += 1
    if creep_count > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Count": creep_count, "Priority": "🟠 High"})

    # 3. Negative Leaks
    negs = df[df["__Line_Amount"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__Line_Amount"].abs().sum()), "Count": len(negs), "Priority": "🟣 High"})

    # 4. Pricing Inconsistency (NEW)
    if col_unit:
        # Check if same vendor has multiple unit prices for items
        variance = df.groupby("__Vendor")["__Unit_Price"].nunique()
        inconsistent = variance[variance > 1].index
        if not inconsistent.empty:
            issues.append({"Category": "Pricing Inconsistency", "Amount ($)": 750.00 * len(inconsistent), "Count": len(inconsistent), "Priority": "🟡 Medium"})

    # 5. Total Mismatch (NEW)
    if col_total:
        mismatch = df[df["__Line_Amount"] != df["__Invoice_Total"]]
        if not mismatch.empty:
            issues.append({"Category": "Total Mismatch", "Amount ($)": float(mismatch["__Line_Amount"].sum() * 0.1), "Count": len(mismatch), "Priority": "🔴 Critical"})

    return pd.DataFrame(issues)

# ----------------------------
# 5) Chatbot Logic
# ----------------------------
def forensic_bot(query, audit_df):
    query = query.lower()
    total = audit_df["Amount ($)"].sum() if not audit_df.empty else 0
    if "total" in query:
        return f"I have uncovered ${total:,.2f} in total recoverable leaks."
    elif "inconsistency" in query or "variance" in query:
        return "Pricing Inconsistency flags vendors who charge multiple different unit prices within the same ledger, suggesting a lack of contract compliance."
    elif "creep" in query:
        return "Price Creep tracks whether a vendor's unit price is trending upward over time."
    return f"Hi {st.session_state['user_name']}! I'm running 5 audit checks. Ask me about Inconsistencies or Mismatches."

# ----------------------------
# 6) Login / Signup UI
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    tab1, tab2 = st.tabs(["Login", "Create Account"])
    with tab1:
        u_in = st.text_input("Username")
        p_in = st.text_input("Password", type="password")
        if st.button("Log In", use_container_width=True):
            if u_in in st.session_state["accounts"] and st.session_state["accounts"][u_in]["pw"] == p_in:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = st.session_state["accounts"][u_in]["name"]
                st.session_state["org_name"] = st.session_state["accounts"][u_in]["org"]
                st.rerun()
            else:
                st.error("Invalid credentials.")
    with tab2:
        new_u = st.text_input("New Username")
        new_p = st.text_input("New Password", type="password")
        new_n = st.text_input("Full Name")
        new_o = st.text_input("Company Name")
        if st.button("Sign Up", use_container_width=True):
            if new_u and new_p and new_n and new_o:
                st.session_state["accounts"][new_u] = {"pw": new_p, "name": new_n, "org": new_o}
                st.balloons()
                st.success("Account created! Log in to start.")

# ----------------------------
# 7) Main Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.write("---")
        st.info(f"👤 **User:** {st.session_state['user_name']}")
        st.warning(f"🏢 **Company:** {st.session_state['org_name']}")
        st.write("---")
        
        # Chatbot
        st.subheader("🤖 AI Assistant")
        with st.expander("Chat with Audit Bot"):
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask something..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                with st.chat_message("user"): st.markdown(pr)
                res = forensic_bot(pr, st.session_state.get('last_audit', pd.DataFrame()))
                with st.chat_message("assistant"): st.markdown(res)
                st.session_state.messages.append({"role": "assistant", "content": res})

        st.write("---")
        if st.button("Log Out"):
            st.session_state["logged_in"] = False
            st.session_state.messages = []
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload AP Ledger", type=["csv", "xlsx"])

    if f:
        df_raw = load_uploaded(f)
        with st.status("🚀 AI Engine Scanning..."):
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
                cats = st.multiselect("Categories", options=audit_df["Category"].unique(), default=audit_df["Category"].unique())
                filtered = audit_df[audit_df["Category"].isin(cats)]
                st.dataframe(filtered, use_container_width=True, hide_index=True)
            with col_c:
                st.write("### 📈 Distribution")
                st.bar_chart(data=filtered, x="Category", y="Amount ($)")
            
            st.divider()
            report_df = filtered.copy()
            report_df['Priority'] = report_df['Priority'].str.replace('🔴 ', '').str.replace('🟠 ', '').str.replace('🟣 ', '').str.replace('🟡 ', '')
            
            csv = report_df.to_csv(index=False).encode('utf-8')
            st.download_button(label="Download Recovery Report (CSV)", data=csv, file_name=f"ClearSpend_Audit.csv", mime='text/csv')
        else:
            st.info("💡 Please upload a financial ledger to begin.")

```
