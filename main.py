# ----------------------------
# 4) Chatbot Logic (5 Leaks + General Info)
# ----------------------------
def forensic_bot(query):
    query = query.lower()
    
    # General Site Purpose
    if "site" in query or "what does this do" in query or "clearspend" in query:
        return (
            "**ClearSpend Analytics** is a forensic audit platform designed to identify 'financial leaks' "
            "in Accounts Payable data. It scans your ledgers to recover lost capital by catching "
            "billing errors, vendor overcharges, and duplicate payments that standard software misses."
        )
    
    # 1. Math Integrity
    elif "math" in query or "integrity" in query:
        return (
            "**1. Math Integrity Check:** Acts as a 'Digital Receipt Validator.' It independently recalculates "
            "the sum of all itemized charges (Line Amounts) and compares them against the final billed amount "
            "(Invoice Total) to uncover hidden fees or manual entry errors."
        )
    
    # 2. Duplicate Invoice
    elif "duplicate" in query:
        return (
            "**2. Duplicate Invoice:** Scans for identical Invoice IDs. This validation prevents 'Double Dipping,' "
            "where a company accidentally pays the exact same bill twice due to clerical errors."
        )
    
    # 3. Price Creep
    elif "creep" in query or "price increase" in query:
        return (
            "**3. Price Creep:** Monitors unit price changes over time. It flags vendors who gradually "
            "increase costs for the same item across different months, allowing you to renegotiate and stop "
            "unauthorized price hikes."
        )
    
    # 4. Negative Leak
    elif "negative" in query or "leak" in query:
        return (
            "**4. Negative Leak:** Identifies negative values or credits in the ledger. These represent "
            "money owed back to your company (like returns) that may have never been successfully collected."
        )
    
    # 5. Pricing Inconsistency
    elif "inconsistency" in query or "variation" in query:
        return (
            "**5. Pricing Inconsistency:** Checks if a vendor is charging different prices for the same "
            "service across different departments or plants. It ensures you are always paying the lowest "
            "contracted rate."
        )
    
    # Fallback
    return (
        "I can explain all 5 forensic leaks: **Math Integrity, Duplicates, Price Creep, "
        "Negative Leaks, or Pricing Inconsistency.** What would you like to know more about?"
    )
