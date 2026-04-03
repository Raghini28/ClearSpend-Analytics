# ----------------------------
# 4) Forensic Engine (Smart Backup Plan & Company Data Ready)
# ----------------------------
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in str(s).strip() if ch.isalnum())

def find_col(df: pd.DataFrame, candidates: list[str]):
    # 1. Try Exact/Strict Match
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    
    # 2. BACKUP PLAN: Fuzzy Keyword Match (for Ref_Num, GlobalID, etc.)
    for col in df.columns:
        c_low = col.lower()
        if any(word in c_low for word in ["id", "key", "ref", "num", "code"]):
            return col
    return None

def find_amt_col(df: pd.DataFrame, candidates: list[str]):
    # 1. Try Exact/Strict Match
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        if _norm(cand) in norm_map: return norm_map[_norm(cand)]
    
    # 2. BACKUP PLAN: Numeric Profile (Guesses based on highest values)
    numeric_cols = df.select_dtypes(include=['number']).columns
    if not numeric_cols.empty:
        return df[numeric_cols].mean().idxmax()
    return None

def build_audit(df_raw: pd.DataFrame):
    if df_raw.empty: return pd.DataFrame()
    df = df_raw.copy()
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].astype(str).str.replace(r'[$,]', '', regex=True)

    # UPDATED MAPPING: Now detects Snowflake, chatgpt.csv, and Fuzzy Headers
    c_amt = find_amt_col(df, ["BOOKING_VALUE_USD", "AMOUNT_USD", "Line_Amount", "Amount"])
    c_tot = find_col(df, ["NET_CASH_IMPACT_USD", "Invoice_Total", "Total"])
    c_id = find_col(df, ["BOOKING_KEY", "TRANSACTION_ID", "Invoice_ID", "InvoiceID"])
    c_unit = find_col(df, ["FX_RATE_TO_USD", "Unit_Price", "Price"])
    c_ven = find_col(df, ["SUPPLIER_KEY", "Vendor_Name", "Vendor"])
    c_date = find_col(df, ["TRANSACTION_TS", "BOOKING_TS", "Invoice_Date", "Date"])

    # If no ID found even after backup plan, use row index to ensure audit runs
    if not c_id:
        df["__VIRTUAL_ID"] = range(len(df))
        c_id = "__VIRTUAL_ID"

    # Critical Check: Audit only fails if NO numeric data exists
    if not c_amt: return pd.DataFrame()

    df["__L"] = pd.to_numeric(df[c_amt], errors='coerce').fillna(0)
    df["__T"] = pd.to_numeric(df[c_tot], errors='coerce').fillna(0) if c_tot else df["__L"]
    df["__U"] = pd.to_numeric(df[c_unit], errors='coerce').fillna(0) if c_unit else 0
    df["__ID"] = df[c_id].astype(str)
    df["__V"] = df[c_ven].astype(str) if c_ven else "N/A"
    df["__D"] = pd.to_datetime(df[c_date], errors='coerce')

    issues = []
    
    # 1. Math Integrity
    mm = df[abs(df["__L"] - df["__T"]) > 0.05]
    if not mm.empty:
        issues.append({"Category": "Math Integrity Check", "Amount ($)": float(abs(mm["__T"] - mm["__L"]).sum()), "Priority": "🔴 Critical"})
    
    # 2. Duplicate Invoice
    dup_ids = df["__ID"][df["__ID"].duplicated(keep=False)]
    if not dup_ids.empty and (df["__ID"] != "N/A").any():
        issues.append({"Category": "Duplicate Invoice", "Amount ($)": float(df[df["__ID"].isin(dup_ids.unique())]["__L"].sum()), "Priority": "🔴 Critical"})
    
    # 3. Price Creep
    creep_amt = 0
    for v, group in df.sort_values("__D").groupby("__V"):
        if len(group) > 1:
            diff = group["__U"].iloc[-1] - group["__U"].iloc[0]
            if diff > 0: creep_amt += diff * len(group)
    if creep_amt > 0:
        issues.append({"Category": "Price Creep", "Amount ($)": float(creep_amt), "Priority": "🟠 High"})
    
    # 4. Negative Leak (Catches unclaimed credits in Snowflake files)
    negs = df[df["__L"] < 0]
    if not negs.empty:
        issues.append({"Category": "Negative Leak", "Amount ($)": float(negs["__L"].abs().sum()), "Priority": "🟣 High"})
    
    # 5. Pricing Inconsistency
    if c_unit:
        inc_count = (df.groupby("__V")["__U"].nunique() > 1).sum()
        if inc_count > 0:
            issues.append({"Category": "Pricing Inconsistency", "Amount ($)": float(inc_count * 500), "Priority": "🟡 Medium"})

    return pd.DataFrame(issues)
