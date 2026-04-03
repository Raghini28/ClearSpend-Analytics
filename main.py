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
# 4) Forensic Engine (The 5 Leaks)
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

    c_amt = find_col(df, ["Line_Amount", "Amount"])
    c_tot = find_col(df, ["Invoice_Total", "Total"])
    c_id = find_col(df, ["Invoice_ID", "InvoiceID"])
    c_unit = find_col(df, ["Unit_Price", "Price"])
    c_ven = find_col(df, ["Vendor_Name", "Vendor"])
    c_date = find_col(df, ["Invoice_Date", "Date"])

    if not c_amt or not c_tot: return pd.DataFrame()

    df["__L"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[c_tot], errors='coerce').fillna(0)
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"] = df[c_id].astype(str) if c_id else "N/A"
    df["__V"] = df[c_ven].astype(str) if c_ven else "N/A"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    
    # 1. Math Integrity
    mm = df[df["__L"] != df["__T"]]
    if not mm.empty:
        issues.append({"Category": "Math Integrity Check", "Amount ($)": float((mm["__T"] - mm["__L"]).abs().sum()), "Priority": "🔴 Critical"})
    
    # 2. Duplicate Invoice
    dup_ids = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__ID"] != "N/A").any():
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(df[df["__ID"].isin(dup_ids.unique())]["__T"].sum()), "Priority": "🔴 Critical"})
    
    # 3. Price Creep
    creep_amt = 0
    for v, group in df.sort_values("__D").groupby("__V"):
        if len(group) > 1:
            diff = group["__U"].iloc[-1] - group["__U"].iloc[0]
            if diff > 0: creep_amt += diff * len(group)
    if creep_amt > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})
    
    # 4. Negative Leak
    negs = df[df["__L"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__L"].abs().sum()), "Priority": "🟣 High"})
    
    # 5. Pricing Inconsistency
    if c_unit:
        inc_count = (df.groupby("__V")["__U"].nunique() > 1).sum()
        if inc_count > 0:
            issues.append({"Category": "Pricing Inconsistency", "Amount ($)": float(inc_count * 500), "Priority": "🟡 Medium"})

    return pd.DataFrame(issues)

# ----------------------------
# 5) Chatbot Logic
# ----------------------------
def forensic_bot(query):
    query = query.lower()
    if "site" in query or "do" in query or "clearspend" in query:
        return "**ClearSpend Analytics is a high-level forensic audit platform designed to identify hidden financial leaks, recover lost capital, and ensure 100% vendor compliance.**"
    elif "math" in query or "integrity" in query:
        return "**Math Integrity Check: This functions as a Digital Receipt Validator. It cross-references itemized Line Amounts with the final Invoice Total to catch shadow fees.**"
    elif "duplicate" in query:
        return "**Duplicate Invoice: This validation scans for identical Invoice IDs across the entire dataset to prevent paying the same obligation twice.**"
    elif "creep" in query:
        return "**Price Creep: This monitors unit pricing trends over time to flag unauthorized price increases.**"
    elif "negative" in query or "leak" in query:
        return "**Negative Leak: This identifies credits and negative entries that have never been successfully recovered.**"
    elif "inconsistency" in query:
        return "**Pricing Inconsistency: This detects when a single vendor charges varying rates for the same SKU across departments.**"
    return "**I am the ClearSpend AI Assistant. Ask me about Math Integrity, Price Creep, or Duplicates!**"

# ----------------------------
# 6) UI Flow: Login & Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    tab_login, tab_signup = st.tabs(["Login", "Create Account"])
    
with tab_login:
        u_in = st.text_input("Username", key="l_u")
        p_in = st.text_input("Password", type="password", key="l_p")
        
        # This 'if' must line up perfectly with the 'u_in' above it
        if st.button("Log In", use_container_width=True):
            accounts = load_accounts() # THE FIX: Added this line only
            if u_in in accounts and accounts[u_in]["pw"] == p_in:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = accounts[u_in]["name"]
                st.balloons()
                st.rerun()
            else:
                st.error("❌ Account not recognized or wrong password.")
                
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
            # Clear data on manual logout too
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
                st.write("### 🔍 Risk Findings")
                opts = audit_df["Category"].unique().tolist()
                sel = st.multiselect("Filter Security Categories", opts, default=opts)
                filt = audit_df[audit_df["Category"].isin(sel)]
                st.dataframe(filt, use_container_width=True, hide_index=True)
            with col2:
                st.write("### 📈 Exposure Distribution")
                st.bar_chart(data=filt, x="Category", y="Amount ($)")
            
            st.divider()
            csv_data = filt.to_csv(index=False).encode('utf-8-sig')
            st.download_button("Download Secure Audit Report", csv_data, "ClearSpend_Report.csv")
