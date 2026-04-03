import streamlit as st
import pandas as pd
import json
import os
import time

# --- 1. PERSISTENT STORAGE SETUP ---
DB_FILE = "users_db.json"

def load_accounts():
    """Fresh load from the JSON file every time this is called."""
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_account(username, password, name, org):
    """Saves new user data permanently to the JSON file."""
    accounts = load_accounts()
    accounts[username] = {"pw": password, "name": name, "org": org}
    with open(DB_FILE, "w") as f:
        json.dump(accounts, f)

# --- 2. INITIALIZE SESSION STATE ---
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "user_name" not in st.session_state:
    st.session_state["user_name"] = ""
if "org_name" not in st.session_state:
    st.session_state["org_name"] = ""

# --- 3. LOGIN & SIGNUP UI ---
if not st.session_state["logged_in"]:
    st.title("ClearSpend Analytics 🛡️")
    tab_login, tab_signup = st.tabs(["Log In", "Create Account"])

    with tab_login:
        u_in = st.text_input("Username", key="l_u")
        p_in = st.text_input("Password", type="password", key="l_p")
        
        if st.button("Log In", use_container_width=True):
            # THE MEMORY FIX: Force a fresh read of the file right now
            accounts = load_accounts()
            
            if u_in in accounts and accounts[u_in]["pw"] == p_in:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = accounts[u_in]["name"]
                # THE KEYERROR FIX: Set org_name during login
                st.session_state["org_name"] = accounts[u_in].get("org", "ClearSpend Partner")
                st.rerun()
            else:
                st.error("❌ Account not recognized or wrong password.")

    with tab_signup:
        new_u = st.text_input("New Username", key="reg_u")
        new_p = st.text_input("New Password", type="password", key="reg_p")
        new_n = st.text_input("Full Name", key="reg_n")
        new_o = st.text_input("Organization Name", key="reg_o")
        
        if st.button("Sign Up", use_container_width=True):
            if new_u and new_p:
                save_account(new_u, new_p, new_n, new_o)
                # BALLOONS ONLY HERE:
                st.balloons()
                st.success(f"✅ Account created for {new_n}! You can now log in.")
            else:
                st.warning("Please fill in all fields.")
    st.stop()

# --- 4. MAIN DASHBOARD (Only visible after login) ---
st.set_page_config(page_title="ClearSpend Analytics", layout="wide")

# Sidebar
st.sidebar.title("ClearSpend Menu")
# SAFE HEADER: Uses .get() to prevent crashing if data is missing
st.sidebar.info(f"👤 **{st.session_state.get('user_name', 'User')}**\n\n🏢 **{st.session_state.get('org_name', 'ClearSpend Partner')}**")

if st.sidebar.button("Log Out"):
    st.session_state["logged_in"] = False
    st.rerun()

st.title("Financial Data Viewer")
uploaded_file = st.file_uploader("Upload Finance Data (CSV)", type=["csv"])

if uploaded_file:
    df = pd.read_csv(uploaded_file)
    st.write("### Data Preview", df)
    
    st.divider()
    st.subheader("Data Summary")
    st.write(f"Total Records: {len(df)}")
    st.write(f"Columns Detected: {', '.join(df.columns.tolist())}")

# --- 5. CHATBOT INTERFACE ---
st.divider()
st.subheader("ClearSpend AI Support")
if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("Ask about your data..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response = f"I'm currently processing your request about: '{prompt}'. How else can I help?"
        st.markdown(response)
    st.session_state.messages.append({"role": "assistant", "content": response})
