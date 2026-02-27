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
# 2) Persistence Logic (The Fix)
# ----------------------------
USER_FILE = "users_db.json"

def load_accounts():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            return json.load(f)
    # Default admin if no file exists
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

# Load accounts from file into session
st.session_state["accounts"] = load_accounts()

# ----------------------------
# 4) Forensic Engine & Bot (Condensed for space)
# ----------------------------
def build_audit(df_raw: pd.DataFrame):
    # (Same 5-validation logic from previous steps)
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)
    # ... logic for math, duplicates, creep, negative, inconsistency ...
    # (Keeping this brief so you can focus on the Login fix)
    return pd.DataFrame([{"Category": "Audit Complete", "Amount ($)": 0.0, "Priority": "🟢 Low"}])

def forensic_bot(query):
    # (Same bold responses from previous steps)
    return "**Ask me about our forensic checks!**"

# ----------------------------
# 5) UI Flow: Login & Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    tab_login, tab_signup = st.tabs(["Login", "Create Account"])
    
    with tab_login:
        u = st.text_input("Username", key="l_user")
        p = st.text_input("Password", type="password", key="l_pass")
        if st.button("Log In", use_container_width=True):
            accounts = load_accounts() # Refresh from file
            if u in accounts and accounts[u]["pw"] == p:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = accounts[u]["name"]
                st.session_state["org_name"] = accounts[u].get("org", "UIC")
                st.rerun()
            else:
                st.error("❌ **Invalid Username or Password.**")
                
    with tab_signup:
        st.subheader("Register New Account")
        new_u = st.text_input("Choose Username", key="s_user")
        new_p = st.text_input("Choose Password", type="password", key="s_pass")
        new_n = st.text_input("Full Name", key="s_name")
        new_o = st.text_input("Organization", key="s_org")
        
        if st.button("Create Account", use_container_width=True):
            accounts = load_accounts()
            if new_u in accounts:
                st.error("⚠️ **Username already exists!**")
            elif new_u and new_p:
                # FIXED: This saves to your hard drive
                save_account(new_u, {"pw": new_p, "name": new_n, "org": new_o})
                st.balloons()
                st.success(f"**Account for {new_n} saved to database! You can now refresh the page and still log in.**")
            else:
                st.warning("⚠️ **Please fill in the fields.**")

# ----------------------------
# 6) Dashboard UI
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 **{st.session_state['user_name']}** | 🏢 **{st.session_state['org_name']}**")
        if st.button("Log Out", use_container_width=True):
            st.session_state["logged_in"] = False
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    f = st.file_uploader("Upload AP Ledger", type=["csv", "xlsx"])
    if f:
        st.write("File uploaded successfully! (Audit running...)")
