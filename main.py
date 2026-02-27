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
[data-testid="stChatMessage"] p { 
    color: #000000 !important; 
    font-weight: 700 !important; 
    font-size: 1.05rem;
}
</style>
""", unsafe_allow_html=True)

# ----------------------------
# 2) Persistence Logic (File DB)
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
# 3) State Management (The Sticky Fix)
# ----------------------------
# We check if 'logged_in' exists, but we DON'T force it to False on rerun
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "messages" not in st.session_state:
    st.session_state.messages = []
if "audit_data" not in st.session_state:
    st.session_state.audit_data = None

# ----------------------------
# 4) Forensic Engine
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

    c_amt, c_tot, c_id = find_col(df, ["Line_Amount", "Amount"]), find_col(df, ["Invoice_Total", "Total"]), find_col(df, ["Invoice_ID", "InvoiceID"])
    c_unit, c_ven, c_date = find_col(df, ["Unit_Price", "Price"]), find_col(df, ["Vendor_Name", "Vendor"]), find_col(df, ["Invoice_Date", "Date"])

    if not c_amt or not c_tot: return pd.DataFrame()

    df["__L"], df["__T"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0), pd.to_numeric(df[c_tot], errors='coerce').fillna(0)
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"], df["__V"] = df[c_id].astype(str) if c_id else "N/A", df[c_ven].astype(str) if c_ven else "N/A"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    # 1. Math Integrity
    mm = df[df["__L"] != df["__T"]]
    if not mm.empty: issues.append({"Category": "Math Integrity Check", "Amount ($)": float((mm["__T"] - mm["__L"]).abs().sum()), "Priority": "🔴 Critical"})
    # 2. Duplicate
    dups = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dups.empty and (df["__ID"] != "N/A").any(): issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(df[df["__ID"].isin(dups.unique())]["__T"].sum()), "Priority": "🔴 Critical"})
    # 3. Price Creep
    creep = sum((g["__U"].iloc[-1] - g["__U"].iloc[0]) * len(g) for v, g in df.sort_values("__D").groupby("__V") if len(g) > 1 and (g["__U"].iloc[-1] > g["__U"].iloc[0]))
    if creep > 0: issues.append({"Category": "Price Creep", "Amount ($)": float(creep), "Priority": "🟠 High"})
    # 4. Negative Leak
    negs = df[df["__L"] < 0]
    if not negs.empty: issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__L"].abs().sum()), "Priority": "🟣 High"})
    # 5. Pricing Inconsistency
    if c_unit:
        inc = (df.groupby("__V")["__U"].nunique() > 1).sum()
        if inc > 0: issues.append({"Category": "Pricing Inconsistency", "Amount ($)": float(inc * 500), "Priority": "🟡 Medium"})

    return pd.DataFrame(issues)

def forensic_bot(query):
    q = query.lower()
    if "site" in q or "do" in q or "clearspend" in q:
        return "**ClearSpend Analytics is a forensic audit platform designed to identify financial leaks, recover lost capital, and ensure 100% vendor compliance.**"
    elif "math" in q or "integrity" in q:
        return "**Math Integrity Check: Functions as a Digital Receipt Validator cross-referencing Line Amounts with Totals to catch shadow fees.**"
    elif "duplicate" in q:
        return "**Duplicate Invoice: Scans for identical Invoice IDs to prevent paying the same obligation twice.**"
    elif "creep" in q:
        return "**Price Creep: Monitors unit pricing trends over time to flag unauthorized price increases.**"
    elif "negative" in q:
        return "**Negative Leak: Identifies negative entries and credits that have never been successfully recovered.**"
    elif "inconsistency" in q:
        return "**Pricing Inconsistency: Detects when a vendor charges varying rates for the same SKU across departments.**"
    return "**I am the ClearSpend AI Assistant. Ask me about our forensic checks!**"

# ----------------------------
# 5) UI FLOW
# ----------------------------

with st.sidebar:
    st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
    
    # If not logged in, show Login/Signup
    if not st.session_state["logged_in"]:
        st.subheader("🛡️ Security Portal")
        tab_login, tab_signup = st.tabs(["Login", "Signup"])
        with tab_login:
            u_in = st.text_input("Username", key="l_u")
            p_in = st.text_input("Password", type="password", key="l_p")
            if st.button("Log In", use_container_width=True):
                accounts = load_accounts()
                if u_in in accounts and accounts[u_in]["pw"] == p_in:
                    st.session_state["logged_in"] = True
                    st.session_state["user_name"] = accounts[u_in]["name"]
                    st.session_state["org_name"] = accounts[u_in].get("org", "UIC")
                    st.rerun()
                else:
                    st.error("Invalid Credentials")
        with tab_signup:
            new_u = st.text_input("Username", key="s_u")
            new_p = st.text_input("Password", type="password", key="s_p")
            new_n = st.text_input("Full Name", key="s_n")
            if st.button("Create Account", use_container_width=True):
                if new_u in load_accounts():
                    st.error("Account already exists!")
                elif new_u and new_p:
                    save_account(new_u, {"pw": new_p, "name": new_n, "org": "UIC"})
                    st.balloons()
                    st.success("Account created! Log in now.")
    
    # If logged in, show User Info and AI Bot
    else:
        st.info(f"👤 **{st.session_state['user_name']}** | 🏢 **{st.session_state['org_name']}**")
        st.subheader("🤖 AI Assistant")
        with st.container():
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask a question..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                st.session_state.messages.append({"role": "assistant", "content": forensic_bot(pr)})
                st.rerun()
        
        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
            # Clear chat history on explicit logout
            st.session_state.messages = []
            st.rerun()

# Main Page
st.title("📊 ClearSpend Recovery Dashboard")

if st.session_state["logged_in"]:
    f = st.file_uploader("Upload AP Ledger", type=["csv", "xlsx"])
    if f:
        df_raw = pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)
        st.session_state.audit_data = build_audit(df_raw)

    if st.session_state.audit_data is not None:
        audit_df = st.session_state.audit_data
        if not audit_df.empty:
            st.metric("Total Recoverable Cash Found", f"${audit_df['Amount ($)'].sum():,.2f}")
            c1, c2 = st.columns([1, 1.5])
            with c1:
                st.write("### 🔍 Risk Findings")
                opts = audit_df["Category"].unique().tolist()
                sel = st.multiselect("Filter Security Categories", opts, default=opts)
                filt = audit_df[audit_df["Category"].isin(sel)]
                st.dataframe(filt, use_container_width=True, hide_index=True)
            with c2:
                st.write("### 📈 Exposure Distribution")
                st.bar_chart(data=filt, x="Category", y="Amount ($)")
            st.divider()
            csv_data = filt.to_csv(index=False).encode('utf-8-sig')
            st.download_button("Download Secure Audit Report", csv_data, "ClearSpend_Report.csv")
else:
    st.warning("⚠️ Please log in via the sidebar to access the Recovery Dashboard.")
