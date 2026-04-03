import streamlit as st
import pandas as pd
import json
import os
import time  # Added for the balloon delay

# --- 1. PERSISTENT STORAGE (Required for the fix) ---
DB_FILE = "users_db.json"

def load_accounts():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        return json.load(f)

# --- [KEEP ALL YOUR CUSTOM CSS/STYLING/GRAPHICS CODE HERE] ---

# --- 2. THE UPDATED LOGIN LOGIC (The only part we change) ---
if not st.session_state.get("logged_in", False):
    st.title("ClearSpend Analytics 🛡️")
    tab_login, tab_signup = st.tabs(["Log In", "Create Account"])

    with tab_login:
        u_in = st.text_input("Username", key="l_u")
        p_in = st.text_input("Password", type="password", key="l_p")
        
        if st.button("Log In", use_container_width=True):
            # FIX: Force load from disk right now so it recognizes old accounts
            accounts = load_accounts()
            
            if u_in in accounts and accounts[u_in]["pw"] == p_in:
                st.session_state["logged_in"] = True
                st.session_state["user_name"] = accounts[u_in]["name"]
                
                # THE BALLOONS: Show them, wait, then enter the app
                st.balloons()
                st.success(f"Welcome back, {st.session_state['user_name']}!")
                time.sleep(2) 
                st.rerun()
            else:
                st.error("❌ Account not recognized or wrong password.")

    # --- [KEEP YOUR SIGNUP TAB EXACTLY AS YOU HAD IT] ---
    with tab_signup:
        # ... your existing signup code ...
        pass
    st.stop()

# --- [KEEP ALL YOUR DASHBOARD/SIDEBAR/CHART CODE BELOW] ---
