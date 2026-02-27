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
[data-testid="stChatMessage"] p { color: #000000 !important; }
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
# 3) Forensic Engine (Math Integrity Logic)
# ----------------------------
def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    
    # Generic mapping logic
    line_col = next((c for c in df.columns if "Line" in c or "Amount" in c), None)
    total_col = next((c for c in df.columns if "Total" in c), None)

    if not line_col or not total_col:
        return pd.DataFrame()

    df["__L"] = pd.to_numeric(df[line_col], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[total_col], errors='coerce').fillna(0)

    # Flagging mismatches
    mismatch = df[df["__L"] != df["__T"]]
    issues = []
    if not mismatch.empty:
        amt = (mismatch["__T"] - mismatch["__L"]).abs().sum()
        issues.append({"Category": "Math Integrity Check", "Amount ($)": float(amt), "Priority": "🔴 Critical"})
    
    return pd.DataFrame(issues)

# ----------------------------
# 4) Chatbot Logic (General Definition)
# ----------------------------
def forensic_bot(query):
    query = query.lower()
    if "math" in query or "integrity" in query:
        return (
            "**Math Integrity Check:** A forensic validation process that ensures a financial document is "
            "internally consistent. It independently recalculates the sum of all itemized charges (Line Amounts) "
            "and compares them against the final billed amount (Invoice Total) to uncover hidden fees, "
            "data entry errors, or financial discrepancies."
        )
    return "I am the ClearSpend AI. Ask me about Math Integrity!"

# ----------------------------
# 5) UI: Login / Signup
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")
    t1, t2 = st.tabs(["Login", "Create Account"])
    
    with t1:
        u = st.text_input("Username", key="l_u")
        p = st.text_input("Password", type="password", key="l_p")
        
        if st.button("Log In", use_container_width=True):
            if u in st.session_state["accounts"] and st.session_state["accounts"][u]["pw"] == p:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = st.session_state["accounts"][u]["name"]
                st.session_state["org_name"] = st.session_state["accounts"][u]["org"]
                st.rerun()
            else:
                # FIXED: Error message now triggers properly
                st.error("❌ Invalid Username or Password. Please try again.")
                
    with t2:
        nu = st.text_input("Username", key="s_u")
        np = st.text_input("Password", type="password", key="s_p")
        nn = st.text_input("Full Name", key="s_n")
        no = st.text_input("Company", key="s_o")
        
        if st.button("Sign Up"):
            if nu and np:
                st.session_state["accounts"][nu] = {"pw": np, "name": nn, "org": no}
                st.balloons()
                st.success("✅ Account created! Please switch to the Login tab.")

# ----------------------------
# 6) Main Dashboard
# ----------------------------
else:
    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 {st.session_state['user_name']} | 🏢 {st.session_state['org_name']}")
        
        # Chatbot
        st.subheader("🤖 AI Assistant")
        with st.expander("Chat with Bot", expanded=True):
            for m in st.session_state.messages:
                with st.chat_message(m["role"]): st.markdown(m["content"])
            if pr := st.chat_input("Ask a question..."):
                st.session_state.messages.append({"role": "user", "content": pr})
                with st.chat_message("user"): st.markdown(pr)
                res = forensic_bot(pr)
                with st.chat_message("assistant"): st.markdown(res)
                st.session_state.messages.append({"role": "assistant", "content": res})

        if st.button("Log Out"):
            st.session_state["logged_in"] = False
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Dashboard")
    f = st.file_uploader("Upload Ledger", type=["csv", "xlsx"])

    if f:
        df_raw = pd.read_csv(f) if f.name.endswith('.csv') else pd.read_excel(f)
        audit_df = build_audit(df_raw)
        
        if not audit_df.empty:
            st.metric("Recoverable Cash", f"${audit_df['Amount ($)'].sum():,.2f}")
            st.dataframe(audit_df, use_container_width=True, hide_index=True)
            
            csv = audit_df.to_csv(index=False).encode('utf-8')
            st.download_button("Download Report", data=csv, file_name="Audit.csv")
