import streamlit as st
import pandas as pd
import json
import hashlib
import secrets
import hmac
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

# ----------------------------
# 1) Page Configuration
# ----------------------------
st.set_page_config(
    page_title="ClearSpend Analytics",
    layout="wide",
    initial_sidebar_state="expanded"
)

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
# 2) Authentication / User DB
# ----------------------------
DB_FILE = Path("clearspend_auth.db")
SESSION_TIMEOUT_MINUTES = 30
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, salt: str | None = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        200_000
    ).hex()
    return f"{salt}${pwd_hash}"


def verify_password(password: str, stored_value: str) -> bool:
    try:
        salt, saved_hash = stored_value.split("$", 1)
        test_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            200_000
        ).hex()
        return hmac.compare_digest(test_hash, saved_hash)
    except Exception:
        return False


def init_auth_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            organization TEXT,
            role TEXT DEFAULT 'user',
            failed_attempts INTEGER DEFAULT 0,
            locked_until TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()

    cur.execute("SELECT username FROM users WHERE username = ?", ("admin",))
    if cur.fetchone() is None:
        cur.execute("""
            INSERT INTO users (
                username, password_hash, full_name, organization, role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            "admin",
            hash_password("uic2026"),
            "Raghini Kumar",
            "UIC",
            "admin",
            datetime.utcnow().isoformat()
        ))
        conn.commit()

    conn.close()


def get_user(username: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return row


def create_user(username: str, password: str, full_name: str, organization: str, role: str = "user"):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT username FROM users WHERE username = ?", (username,))
    if cur.fetchone():
        conn.close()
        return False, "Username already exists."

    cur.execute("""
        INSERT INTO users (
            username, password_hash, full_name, organization, role, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (
        username,
        hash_password(password),
        full_name,
        organization,
        role,
        datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()
    return True, "Account created successfully."


def is_locked(user_row) -> tuple[bool, str]:
    locked_until = user_row["locked_until"]
    if not locked_until:
        return False, ""

    try:
        until_dt = datetime.fromisoformat(locked_until)
        if datetime.utcnow() < until_dt:
            mins = int((until_dt - datetime.utcnow()).total_seconds() // 60) + 1
            return True, f"Account locked. Try again in {mins} minute(s)."
    except Exception:
        pass

    return False, ""


def reset_failed_attempts(username: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET failed_attempts = 0,
            locked_until = NULL
        WHERE username = ?
    """, (username,))
    conn.commit()
    conn.close()


def record_failed_attempt(username: str, current_failed_attempts: int):
    conn = get_db()
    cur = conn.cursor()

    new_attempts = current_failed_attempts + 1
    locked_until = None

    if new_attempts >= MAX_FAILED_ATTEMPTS:
        locked_until = (datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()

    cur.execute("""
        UPDATE users
        SET failed_attempts = ?,
            locked_until = ?
        WHERE username = ?
    """, (new_attempts, locked_until, username))
    conn.commit()
    conn.close()


def authenticate_user(username: str, password: str):
    user = get_user(username)
    if not user:
        return False, "Invalid username or password.", None

    locked, msg = is_locked(user)
    if locked:
        return False, msg, None

    if verify_password(password, user["password_hash"]):
        reset_failed_attempts(username)
        return True, "Login successful.", user

    record_failed_attempt(username, user["failed_attempts"])
    return False, "Invalid username or password.", None


def start_session(user_row):
    st.session_state["logged_in"] = True
    st.session_state["user_name"] = user_row["full_name"]
    st.session_state["org_name"] = user_row["organization"] or "N/A"
    st.session_state["role"] = user_row["role"]
    st.session_state["username"] = user_row["username"]
    st.session_state["session_expires_at"] = (
        datetime.utcnow() + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
    ).isoformat()


def logout_user():
    for key in [
        "logged_in", "user_name", "org_name", "role", "username",
        "session_expires_at", "messages", "audit_data",
        "audit_detail", "recovery_tracker"
    ]:
        if key in st.session_state:
            del st.session_state[key]

    st.session_state.setdefault("logged_in", False)
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("audit_data", None)
    st.session_state.setdefault("audit_detail", {})
    st.session_state.setdefault("recovery_tracker", pd.DataFrame())


def enforce_session_timeout():
    if not st.session_state.get("logged_in"):
        return

    expires = st.session_state.get("session_expires_at")
    if not expires:
        return

    try:
        if datetime.utcnow() > datetime.fromisoformat(expires):
            logout_user()
            st.warning("Your session expired. Please log in again.")
            st.rerun()
    except Exception:
        logout_user()
        st.rerun()


def refresh_session():
    if st.session_state.get("logged_in"):
        st.session_state["session_expires_at"] = (
            datetime.utcnow() + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
        ).isoformat()


# ----------------------------
# 3) Session State
# ----------------------------
st.session_state.setdefault("logged_in", False)
st.session_state.setdefault("messages", [])
st.session_state.setdefault("audit_data", None)
st.session_state.setdefault("audit_detail", {})
st.session_state.setdefault("recovery_tracker", pd.DataFrame())

init_auth_db()
enforce_session_timeout()

# ----------------------------
# 4) Forensic Helpers
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())


def find_col(df: pd.DataFrame, candidates: list[str], fallback_keywords: list[str] | None = None):
    norm_map = {_norm(c): c for c in df.columns}

    for cand in candidates:
        if _norm(cand) in norm_map:
            return norm_map[_norm(cand)]

    if fallback_keywords:
        for col in df.columns:
            c_low = col.lower()
            if any(word in c_low for word in fallback_keywords):
                return col
    return None


def find_amt_col(df: pd.DataFrame, candidates: list[str]):
    norm_map = {_norm(c): c for c in df.columns}

    for cand in candidates:
        if _norm(cand) in norm_map:
            return norm_map[_norm(cand)]

    nums = df.select_dtypes(include=["number"]).columns.tolist()
    if nums:
        means = df[nums].apply(pd.to_numeric, errors="coerce").fillna(0).abs().mean()
        return means.idxmax()
    return None


def clean_money_series(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(r"[$,]", "", regex=True)
        .str.replace(r"\((.*?)\)", r"-\1", regex=True)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def add_row_reference(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.insert(0, "Row Ref", range(1, len(out) + 1))
    return out


def summarize_issue(category: str, amount: float, priority: str, action: str, row_count: int, confidence: str):
    return {
        "Category": category,
        "Amount ($)": round(float(amount), 2),
        "Priority": priority,
        "Confidence": confidence,
        "Action": action,
        "Rows Flagged": int(row_count),
        "Recovery Status": "Not Reviewed",
    }


# ----------------------------
# 5) Fast Duplicate Detection
# ----------------------------
def detect_duplicates_fast(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = df.copy()
    work["Date Only"] = pd.to_datetime(work["__D"], errors="coerce").dt.date
    work["Amount Rounded"] = work["__L"].round(2)

    exact = work[
        work["__ID"].notna() &
        work["__ID"].ne("N/A") &
        work["__ID"].ne("") &
        work["__ID"].duplicated(keep=False)
    ].copy()

    if not exact.empty:
        exact["Confidence"] = "High"
        exact["Why Flagged"] = "Same transaction ID appears multiple times"
        exact["Estimated Exposure"] = exact["__L"].abs()

    potential = work[
        work.duplicated(subset=["__V", "Amount Rounded", "Date Only"], keep=False)
    ].copy()

    if not exact.empty:
        exact_refs = set(exact["Row Ref"].tolist())
        potential = potential[~potential["Row Ref"].isin(exact_refs)].copy()

    if not potential.empty:
        potential["Confidence"] = "Medium"
        potential["Why Flagged"] = "Same vendor, same amount, and same transaction date"
        potential["Estimated Exposure"] = potential["__L"].abs() * 0.5

    return exact, potential


# ----------------------------
# 6) Main Audit Logic
# ----------------------------
def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty:
        return pd.DataFrame(), {}, pd.DataFrame()

    df = add_row_reference(df_raw.copy())

    c_amt = find_amt_col(df, ["BOOKING_VALUE_USD", "AMOUNT_USD", "Line_Amount", "Amount"])
    c_tot = find_col(df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total"], ["total", "cash", "impact"])
    c_id = find_col(df, ["TRANSACTION_ID", "BOOKING_KEY", "Invoice_ID", "InvoiceID"], ["id", "key", "ref", "num"])
    c_unit = find_col(df, ["FX_RATE_TO_USD", "Unit_Price", "Price"], ["fx", "rate", "unit", "price"])
    c_ven = find_col(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"], ["supplier", "vendor"])
    c_date = find_col(df, ["TRANSACTION_TS", "Invoice_Date", "Date"], ["date", "time", "timestamp"])
    c_qty = find_col(df, ["QUANTITY", "QTY", "Units"], ["qty", "quantity", "units"])
    c_prod = find_col(df, ["ITEM_ID", "SKU", "PRODUCT_CODE", "PRODUCT", "ITEM"], ["item", "sku", "product"])
    c_desc = find_col(df, ["DESCRIPTION", "MEMO", "NOTES", "COMMENT"], ["description", "memo", "note", "comment", "reason"])

    if not c_amt:
        return pd.DataFrame(), {"error": "No amount column could be detected."}, pd.DataFrame()

    if not c_id:
        df["__ID"] = range(len(df))
        c_id = "__ID"

    df["__L"] = clean_money_series(df[c_amt]).fillna(0)
    df["__T"] = clean_money_series(df[c_tot]).fillna(0) if c_tot else df["__L"]
    df["__U"] = clean_money_series(df[c_unit]).fillna(0) if c_unit else 0
    df["__Q"] = clean_money_series(df[c_qty]).fillna(1) if c_qty else 1
    df["__ID"] = df[c_id].astype(str).fillna("N/A")
    df["__V"] = df[c_ven].astype(str).fillna("N/A") if c_ven else "N/A"
    df["__D"] = pd.to_datetime(df[c_date], errors="coerce") if c_date else pd.NaT
    df["__P"] = df[c_prod].astype(str).fillna("UNKNOWN") if c_prod else "UNKNOWN"
    df["__DESC"] = df[c_desc].astype(str).fillna("") if c_desc else ""

    issues = []
    details = {}

    # 1. Calculation Variance
    mm = df[(df["__L"] - df["__T"]).abs() > 0.05].copy()
    if not mm.empty:
        mm["Variance"] = (mm["__T"] - mm["__L"]).abs()
        denom = mm["__L"].replace(0, pd.NA).abs()
        mm["Variance %"] = ((mm["Variance"] / denom) * 100).fillna(0).round(2)
        mm["Why Flagged"] = "Net cash impact differs from itemized booking amount"
        mm["Confidence"] = mm["Variance %"].apply(lambda x: "High" if x >= 5 else "Medium")

        issues.append(
            summarize_issue(
                "Calculation Variance",
                mm["Variance"].sum(),
                "🔴 Critical",
                "Audit line-item math and payment totals",
                len(mm),
                "High",
            )
        )

        details["Calculation Variance"] = mm[
            ["Row Ref", "__ID", "__V", "__L", "__T", "Variance", "Variance %", "Confidence", "Why Flagged"]
        ]

    # 2. Duplicate Detection
    exact_dups, potential_dups = detect_duplicates_fast(df)

    if not exact_dups.empty:
        issues.append(
            summarize_issue(
                "Exact Duplicate ID",
                exact_dups["Estimated Exposure"].sum(),
                "🔴 Critical",
                "Verify whether duplicate transaction IDs were paid twice",
                len(exact_dups),
                "High",
            )
        )
        details["Exact Duplicate ID"] = exact_dups[
            ["Row Ref", "__ID", "__V", "__L", "__D", "Confidence", "Why Flagged", "Estimated Exposure"]
        ]

    if not potential_dups.empty:
        issues.append(
            summarize_issue(
                "Potential Duplicate Pattern",
                potential_dups["Estimated Exposure"].sum(),
                "🟠 High",
                "Review same-vendor same-amount records before recovery claim",
                len(potential_dups),
                "Medium",
            )
        )
        details["Potential Duplicate Pattern"] = potential_dups[
            ["Row Ref", "__ID", "__V", "__L", "__D", "Confidence", "Why Flagged", "Estimated Exposure"]
        ]

    # 3. Trend Price Creep
    creep_records = []
    if c_unit and c_date and c_ven:
        for keys, group in df.sort_values("__D").groupby(["__V", "__P"]):
            group = group.dropna(subset=["__D"]).copy()
            group = group[group["__U"] > 0]
            if len(group) < 3:
                continue

            baseline = group["__U"].iloc[:-1].median() if len(group) > 3 else group["__U"].mean()
            latest = group["__U"].iloc[-1]
            latest_qty = max(group["__Q"].iloc[-1], 1)
            pct_change = ((latest - baseline) / baseline * 100) if baseline > 0 else 0

            if baseline > 0 and pct_change >= 5:
                creep_records.append({
                    "Vendor": keys[0],
                    "Product": keys[1],
                    "Baseline Unit Price": round(float(baseline), 4),
                    "Latest Unit Price": round(float(latest), 4),
                    "Price Increase %": round(float(pct_change), 2),
                    "Latest Quantity": round(float(latest_qty), 2),
                    "Estimated Overcharge": round(float((latest - baseline) * latest_qty), 2),
                    "Why Flagged": "Latest unit price exceeds historical baseline by at least 5%",
                })

    if creep_records:
        creep_df = pd.DataFrame(creep_records)
        issues.append(
            summarize_issue(
                "Trend Price Creep",
                creep_df["Estimated Overcharge"].sum(),
                "🟠 High",
                "Review vendor rate changes against prior baseline pricing",
                len(creep_df),
                "Medium",
            )
        )
        details["Trend Price Creep"] = creep_df

    # 4. Negative Leak
    negs = df[df["__L"] < 0].copy()
    if not negs.empty:
        negs["Negative Type"] = "Unclassified Negative"

        if c_desc:
            lower_desc = negs["__DESC"].str.lower()
            negs.loc[lower_desc.str.contains("refund|rebate", na=False), "Negative Type"] = "Refund"
            negs.loc[lower_desc.str.contains("credit", na=False), "Negative Type"] = "Credit Memo"
            negs.loc[lower_desc.str.contains("reversal|reverse", na=False), "Negative Type"] = "Reversal"
            negs.loc[lower_desc.str.contains("adjustment|adj", na=False), "Negative Type"] = "Adjustment"

        negs["Why Flagged"] = "Negative transaction should be verified against actual cash recovery"

        issues.append(
            summarize_issue(
                "Negative Leak",
                negs["__L"].abs().sum(),
                "🟣 High",
                "Confirm refund or credit was actually recovered in cash",
                len(negs),
                "Medium",
            )
        )

        details["Negative Leak"] = negs[
            ["Row Ref", "__ID", "__V", "__L", "Negative Type", "__DESC", "Why Flagged"]
        ]

    # 5. Contract Variance
    if c_unit and c_ven:
        valid = df[df["__U"] > 0].copy()
        if not valid.empty:
            group_keys = ["__V", "__P"]
            valid["Best Observed Unit Price"] = valid.groupby(group_keys)["__U"].transform("min")
            valid["Estimated Variance Loss"] = (valid["__U"] - valid["Best Observed Unit Price"]) * valid["__Q"]
            contract_var = valid[valid["Estimated Variance Loss"] > 0.01].copy()

            if not contract_var.empty:
                contract_var["Why Flagged"] = "Observed unit price exceeds best observed rate for same vendor and product"

                issues.append(
                    summarize_issue(
                        "Contract Variance",
                        contract_var["Estimated Variance Loss"].sum(),
                        "🟡 Medium",
                        "Compare department pricing against best observed vendor rate",
                        len(contract_var),
                        "Medium",
                    )
                )

                details["Contract Variance"] = contract_var[
                    ["Row Ref", "__ID", "__V", "__P", "__U", "Best Observed Unit Price", "__Q",
                     "Estimated Variance Loss", "Why Flagged"]
                ]

    result = pd.DataFrame(issues)

    if not result.empty:
        result = result.sort_values("Amount ($)", ascending=False).reset_index(drop=True)
        recovery_tracker = result[["Category", "Recovery Status", "Action", "Confidence", "Rows Flagged"]].copy()
    else:
        recovery_tracker = pd.DataFrame(
            columns=["Category", "Recovery Status", "Action", "Confidence", "Rows Flagged"]
        )

    return result, details, recovery_tracker


# ----------------------------
# 7) Chatbot Logic
# ----------------------------
def forensic_bot(query):
    query = query.lower()

    if "site" in query or "do" in query or "clearspend" in query:
        return (
            "**ClearSpend Analytics is a forensic AP audit platform that uses modular validation logic, "
            "trend analysis, confidence scoring, and recovery-focused workflows to identify hidden capital leaks.**"
        )
    elif "duplicate" in query:
        return (
            "**We detect exact duplicate IDs and rule-based duplicate patterns using vendor, amount, and date keys "
            "to keep the scan scalable for large ledgers.**"
        )
    elif "price" in query or "creep" in query:
        return (
            "**We compare current pricing against historical vendor-product baselines to flag upward drift "
            "and contract variance.**"
        )
    elif "negative" in query or "refund" in query or "credit" in query:
        return (
            "**We isolate negative ledger entries and classify likely refunds, credits, reversals, and adjustments "
            "for recovery review.**"
        )
    elif "math" in query or "variance" in query:
        return (
            "**We compare line-level booking amounts against net cash impact and quantify both dollar variance "
            "and variance percentage.**"
        )

    return "**I am the ClearSpend AI Assistant. Ask me about duplicates, price creep, contract variance, or refund recovery.**"


# ----------------------------
# 8) Login / Signup UI
# ----------------------------
if not st.session_state["logged_in"]:
    st.title("🛡️ ClearSpend Security Portal")

    tab_login, tab_signup = st.tabs(["Login", "Create Account"])

    with tab_login:
        u = st.text_input("Username", key="l_user")
        p = st.text_input("Password", type="password", key="l_pass")

        if st.button("Log In", use_container_width=True):
            ok, msg, user = authenticate_user(u.strip(), p)
            if ok and user is not None:
                start_session(user)
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

    with tab_signup:
        new_u = st.text_input("Choose Username", key="s_user")
        new_p = st.text_input("Choose Password", type="password", key="s_pass")
        new_n = st.text_input("Full Name", key="s_name")
        new_o = st.text_input("Organization", key="s_org")

        if st.button("Create Account", use_container_width=True):
            if not new_u or not new_p or not new_n:
                st.warning("Please fill in all required fields.")
            elif len(new_p) < 8:
                st.warning("Password must be at least 8 characters.")
            else:
                ok, msg = create_user(
                    username=new_u.strip(),
                    password=new_p,
                    full_name=new_n.strip(),
                    organization=new_o.strip()
                )
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

# ----------------------------
# 9) Dashboard UI
# ----------------------------
else:
    refresh_session()

    with st.sidebar:
        st.markdown('<p class="brand-text">💎 ClearSpend</p>', unsafe_allow_html=True)
        st.info(f"👤 **{st.session_state['user_name']}** | 🏢 **{st.session_state['org_name']}**")

        st.subheader("🤖 AI Assistant")
        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])

        prompt = st.chat_input("Ask a forensic question...")
        if prompt:
            st.session_state.messages.append({"role": "user", "content": prompt})
            reply = forensic_bot(prompt)
            st.session_state.messages.append({"role": "assistant", "content": reply})
            st.rerun()

        if st.button("Log Out", use_container_width=True):
            logout_user()
            st.rerun()

    st.title(f"📊 {st.session_state['org_name']} Recovery Dashboard")
    uploaded_file = st.file_uploader("Upload AP Ledger (CSV or XLSX)", type=["csv", "xlsx"])

    if uploaded_file:
        try:
            if uploaded_file.name.lower().endswith(".csv"):
                df_raw = pd.read_csv(uploaded_file)
            else:
                df_raw = pd.read_excel(uploaded_file)

            audit_df, detail_map, recovery_df = build_audit(df_raw)
            st.session_state.audit_data = audit_df
            st.session_state.audit_detail = detail_map
            st.session_state.recovery_tracker = recovery_df

        except Exception as exc:
            st.error(f"Unable to process file: {exc}")

    if st.session_state.audit_data is not None:
        res = st.session_state.audit_data

        if isinstance(st.session_state.audit_detail, dict) and "error" in st.session_state.audit_detail:
            st.error(st.session_state.audit_detail["error"])

        elif not res.empty:
            st.metric("Total Recoverable Cash Found", f"${res['Amount ($)'].sum():,.2f}")

            col1, col2 = st.columns([1, 1.5])

            with col1:
                st.write("### 🔍 Risk Findings")
                categories = res["Category"].tolist()
                selected = st.multiselect("Filter categories", categories, default=categories)
                view = res[res["Category"].isin(selected)]
                st.dataframe(view, use_container_width=True, hide_index=True)

            with col2:
                st.write("### 📈 Exposure Distribution")
                st.bar_chart(data=view, x="Category", y="Amount ($)")

            st.divider()

            st.write("### 📁 Flag Review Detail")
            available_categories = view["Category"].tolist()

            if available_categories:
                selected_category = st.selectbox("Inspect finding details", available_categories)
                detail_df = st.session_state.audit_detail.get(selected_category)

                if isinstance(detail_df, pd.DataFrame) and not detail_df.empty:
                    st.dataframe(detail_df, use_container_width=True, hide_index=True)
                else:
                    st.info("No detail rows available for this category.")

            st.divider()

            st.write("### ✅ Recovery Workflow Tracker")
            st.dataframe(st.session_state.recovery_tracker, use_container_width=True, hide_index=True)

            csv_report = view.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "Download Secure Audit Report",
                csv_report,
                "ClearSpend_Report.csv",
                mime="text/csv"
            )

        else:
            st.success("No material leak patterns were detected in the uploaded file.")
