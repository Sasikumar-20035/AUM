import sys
import time
import calendar
import re
from typing import Optional

import gspread

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# =========================
# GOOGLE SHEETS CONFIG
# =========================
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
SHEET_ID = "16soY3eRQdOqqZxEmlchJUuEcOlNO3pibSId7B_d13Hc"


# =========================
# SHEET NAME FROM PMS NAME
# =========================
def sheet_name_from_pms(pms_name: Optional[str]) -> str:
    """
    Map PMS provider name to worksheet/tab name.

    Rules:
      - "Capitalmind Financial Services Private Limited" -> "Capitalmind"
      - "Dezerv Investments Private Limited"             -> "Dezerv"
      - "Wealth Managers (India) Pvt. Ltd."             -> "Scripbox"
      - If PMS is None / "-" / "all"                    -> fallback "Sheet3"
      - Otherwise                                       -> fallback to pms_name as sheet name
    """
    if not pms_name:
        return "Sheet3"  # fallback for ALL PMS mode

    name_norm = pms_name.strip().lower()

    if name_norm == "capitalmind financial services private limited":
        return "Capitalmind(Automatic)"
    if name_norm == "dezerv investments private limited":
        return "Dezerv(Automatic)"
    if name_norm == "wealth managers (india) pvt. ltd.":
        return "Scripbox(Automatic)"

    # Fallback: try use PMS name directly as sheet name
    return pms_name


# =========================
# IA NAME CLEANING
# =========================
def clean_ia_name(ia_name: str, pms_name: Optional[str]) -> str:
    if not pms_name:
        return ia_name.strip()

    original = ia_name
    ia = ia_name

    pattern_full = re.compile(re.escape(pms_name), flags=re.IGNORECASE)
    ia = pattern_full.sub("", ia)

    tokens = [tok for tok in re.split(r"[\s\.]+", pms_name) if len(tok) >= 4]
    for tok in tokens:
        pattern_tok = re.compile(re.escape(tok), flags=re.IGNORECASE)
        ia = pattern_tok.sub("", ia)

    ia = re.sub(r"[-–—]+", " ", ia)
    ia = " ".join(ia.split())
    ia = ia.strip(" -–—")

    if not ia:
        return original.strip()

    return ia


# =========================
# GOOGLE SHEET UPDATE LOGIC
# =========================
def update_pms_sheet(data: dict, worksheet_name: str):
    gc = gspread.oauth(
        credentials_filename=CREDENTIALS_FILE,
        authorized_user_filename=TOKEN_FILE,
    )
    sh = gc.open_by_key(SHEET_ID)
    sheet = sh.worksheet(worksheet_name)

    month_num = int(data["month"])
    year_num = int(data["year"])
    month_abbr = calendar.month_abbr[month_num]
    year_short = str(year_num)[-2:]
    target_month = f"{month_abbr} '{year_short}"

    IA_COL = 2
    IA_COL_IDX = IA_COL - 1

    all_rows = sheet.get_all_values()
    if not all_rows:
        raise Exception(f"Sheet '{worksheet_name}' is empty!")

    rscr_row_idx = None
    for i, row in enumerate(all_rows):
        if len(row) > 1 and row[1].strip().lower() == "rs. cr":
            rscr_row_idx = i
            break

    if rscr_row_idx is None:
        raise Exception(f"Could not find a row where column B is 'Rs. Cr' in sheet '{worksheet_name}'.")

    rscr_row = all_rows[rscr_row_idx]

    if target_month in rscr_row:
        month_col_index = rscr_row.index(target_month) + 1
    else:
        last_used_col = 0
        for idx, val in enumerate(rscr_row, start=1):
            if str(val).strip():
                last_used_col = idx
        if last_used_col > 0:
            month_col_index = last_used_col + 1
        else:
            month_col_index = 3

        sheet.update_cell(rscr_row_idx + 1, month_col_index, target_month)
        print(f"[SHEET:{worksheet_name}] Added month '{target_month}' at column {month_col_index}")

    print(f"[SHEET:{worksheet_name}] Using month = {target_month}, column index = {month_col_index}")

    def is_strategy_marker(text: str) -> bool:
        t = " ".join(text.lower().split())
        return (
            t.startswith("equity aum total")
            or t == "debt"
            or t.startswith("debt ")
            or t == "hybrid"
            or t.startswith("hybrid ")
            or t.startswith("multi-asset")
            or t.startswith("multi asset")
        )

    def is_service_marker(text: str) -> bool:
        t = " ".join(text.lower().split())
        return ("aum total" in t) and ("discretionary" in t)

    strategy_name = data["strategy"].strip().lower()
    if strategy_name == "multi":
        strategy_name = "multi-asset"

    service_raw = data["service_type"].strip().lower()
    is_non_disc = service_raw.startswith("non")

    print(f"\n[DEBUG:{worksheet_name}] Looking for STRATEGY row for '{strategy_name}'")
    strat_idx = -1
    for i, row in enumerate(all_rows):
        if len(row) > 1:
            raw_b = row[1]
            cell = " ".join(raw_b.lower().split())
            print(f"   [DEBUG][STRAT] row {i+1}: {repr(raw_b)}  ->  {cell}")
            if cell.startswith(strategy_name):
                strat_idx = i
                print(f"   [DEBUG][STRAT] >>> MATCH strategy '{strategy_name}' at row {i+1}")
                break

    if strat_idx == -1:
        print(f"[SHEET:{worksheet_name}] Strategy '{strategy_name}' not found in column B. Skipping whole block.")
        return

    print(
        f"[DEBUG:{worksheet_name}] Looking for SERVICE header for '{data['service_type']}' "
        f"under strategy row {strat_idx+1}"
    )
    serv_idx = -1
    for i in range(strat_idx + 1, len(all_rows)):
        row = all_rows[i]
        if len(row) <= 1:
            continue

        raw_b = row[1]
        cell = " ".join(raw_b.lower().split())
        print(f"   [DEBUG][SERV] row {i+1}: {repr(raw_b)}  ->  {cell}")

        if is_strategy_marker(cell):
            print("   [DEBUG][SERV] --- hit next strategy marker; stop searching service header here ---")
            break

        if is_service_marker(cell):
            if is_non_disc:
                if "non" in cell:
                    serv_idx = i
                    print(f"   [DEBUG][SERV] >>> MATCH Non-Discretionary header at row {i+1}")
                    break
                else:
                    print("   [DEBUG][SERV] contains 'discretionary' but NOT 'non' (need non-discretionary) – skip")
            else:
                if "non" not in cell:
                    serv_idx = i
                    print(f"   [DEBUG][SERV] >>> MATCH Discretionary header at row {i+1}")
                    break
                else:
                    print("   [DEBUG][SERV] has 'non' (this is non-discretionary) – skip for pure discretionary")

    if serv_idx == -1:
        print(
            f"[SHEET:{worksheet_name}] Service type row for '{data['service_type']}' "
            f"not found under strategy '{strategy_name}'. Skipping."
        )
        return

    next_block = len(all_rows)
    for i in range(serv_idx + 1, len(all_rows)):
        row = all_rows[i]
        if len(row) <= 1:
            continue
        cell = " ".join(row[1].lower().split())

        if is_strategy_marker(cell) or is_service_marker(cell):
            next_block = i
            break

    print(
        f"[DEBUG:{worksheet_name}] IA rows for this block will be searched "
        f"between rows {serv_idx+2} and {next_block} (1-based)"
    )

    for res in data["results"]:
        raw_ia_name = res["ia_name"].strip()
        raw_ia_name = raw_ia_name.lstrip(". ").strip()
        ia_name = clean_ia_name(raw_ia_name, data.get("pms_name"))
        aum_value = res["aum"]

        print(f"\n[DEBUG:{worksheet_name}][IA] Processing IA '{ia_name}' (raw: {res['ia_name']})")

        all_rows = sheet.get_all_values()

        ia_idx = -1
        for i in range(serv_idx + 1, next_block):
            row = all_rows[i]
            if len(row) > IA_COL_IDX:
                cell_ia = row[IA_COL_IDX].strip()
                cell_ia_norm = cell_ia.lstrip(". ").strip().lower()
                print(
                    f"   [DEBUG:{worksheet_name}][IA-MATCH] row {i+1}: sheet IA '{cell_ia}' "
                    f"-> norm '{cell_ia_norm}' vs target '{ia_name.lower()}'"
                )
                if cell_ia_norm == ia_name.lower():
                    ia_idx = i
                    print(f"   [DEBUG:{worksheet_name}][IA-MATCH] >>> MATCH at row {i+1}")
                    break

        if ia_idx != -1:
            sheet.update_cell(ia_idx + 1, month_col_index, aum_value)
            print(
                f"[SHEET:{worksheet_name}] Updated IA '{ia_name}' "
                f"at row {ia_idx+1}, col {month_col_index} = {aum_value}"
            )
        else:
            newrow_len = max(len(rscr_row), month_col_index, IA_COL)
            newrow = [''] * newrow_len
            newrow[IA_COL_IDX] = ia_name
            newrow[month_col_index - 1] = aum_value

            insert_at = next_block + 1
            sheet.insert_row(newrow, insert_at)
            print(
                f"[SHEET:{worksheet_name}] Inserted IA '{ia_name}' "
                f"at row {insert_at}, col {month_col_index} = {aum_value}"
            )
            next_block += 1


# =========================
# TOTALS RECOMPUTE
# =========================
def recompute_totals_for_month(month_value: str, year_value: str, worksheet_name: str):
    gc = gspread.oauth(
        credentials_filename=CREDENTIALS_FILE,
        authorized_user_filename=TOKEN_FILE,
    )
    sh = gc.open_by_key(SHEET_ID)
    sheet = sh.worksheet(worksheet_name)

    month_num = int(month_value)
    year_num = int(year_value)
    month_abbr = calendar.month_abbr[month_num]
    year_short = str(year_num)[-2:]
    target_month = f"{month_abbr} '{year_short}"

    all_rows = sheet.get_all_values()
    if not all_rows:
        print(f"[TOTALS:{worksheet_name}] Sheet empty.")
        return

    rscr_row_idx = None
    for i, row in enumerate(all_rows):
        if len(row) > 1 and row[1].strip().lower() == "rs. cr":
            rscr_row_idx = i
            break

    if rscr_row_idx is None:
        print(f"[TOTALS:{worksheet_name}] No 'Rs. Cr' row.")
        return

    rscr_row = all_rows[rscr_row_idx]
    if target_month not in rscr_row:
        print(f"[TOTALS:{worksheet_name}] Month '{target_month}' not found.")
        return

    month_col_index = rscr_row.index(target_month) + 1
    print(f"[TOTALS:{worksheet_name}] Recomputing totals for {target_month} (col {month_col_index})")

    def is_strategy_marker(text: str) -> bool:
        t = " ".join(text.lower().split())
        return (
            t.startswith("equity aum total")
            or t == "debt"
            or t.startswith("debt ")
            or t == "hybrid"
            or t.startswith("hybrid ")
            or t.startswith("multi-asset")
            or t.startswith("multi asset")
        )

    def is_service_marker(text: str) -> bool:
        t = " ".join(text.lower().split())
        return ("aum total" in t) and ("discretionary" in t)

    strategy_totals = {}
    current_strategy_row = None

    for i, row in enumerate(all_rows):
        if len(row) <= 1:
            continue

        col_b = row[1]
        col_b_norm = " ".join(col_b.lower().split())

        if is_strategy_marker(col_b_norm):
            current_strategy_row = i
            strategy_totals.setdefault(i, 0.0)
            continue

        if is_service_marker(col_b_norm):
            if current_strategy_row is None:
                continue

            service_row_idx = i

            block_end = len(all_rows)
            for j in range(service_row_idx + 1, len(all_rows)):
                if len(all_rows[j]) <= 1:
                    continue
                b2 = " ".join(all_rows[j][1].lower().split())
                if is_service_marker(b2) or is_strategy_marker(b2):
                    block_end = j
                    break

            subtotal = 0.0
            for r in range(service_row_idx + 1, block_end):
                row2 = all_rows[r]
                if len(row2) < month_col_index:
                    continue
                val_str = row2[month_col_index - 1].replace(",", "").strip()
                if not val_str:
                    continue
                try:
                    subtotal += float(val_str)
                except ValueError:
                    continue

            sheet.update_cell(service_row_idx + 1, month_col_index, f"{subtotal:.2f}")
            print(f"[TOTALS:{worksheet_name}] Service row '{col_b}' row {service_row_idx+1}: {subtotal:.2f}")

            strategy_totals[current_strategy_row] = strategy_totals.get(current_strategy_row, 0.0) + subtotal

    for strat_row_idx, tot in strategy_totals.items():
        sheet.update_cell(strat_row_idx + 1, month_col_index, f"{tot:.2f}")
        print(f"[TOTALS:{worksheet_name}] Strategy total row {strat_row_idx+1}: {tot:.2f}")

    total_row_idx = None
    for i, row in enumerate(all_rows):
        if len(row) > 1 and row[1].strip().lower() in ("total pms aum", "total aum", "total"):
            total_row_idx = i
            break

    if total_row_idx is not None and strategy_totals:
        grand = sum(strategy_totals.values())
        sheet.update_cell(total_row_idx + 1, month_col_index, f"{grand:.2f}")
        print(f"[TOTALS:{worksheet_name}] Total PMS AUM row {total_row_idx+1}: {grand:.2f}")
    else:
        print(f"[TOTALS:{worksheet_name}] No 'Total PMS AUM' row found, or no strategy totals.")


# =========================
# SCRAPE ONE COMBINATION
# =========================
def scrape_one_combo(driver, strategy_value, service_code, month_value, year_value, pms_name):
    service_label = 'discretionary' if service_code.upper() == 'D' else 'non discretionary'

    driver.get("https://www.apmiindia.org/apmi/welcomeiaperformance.htm?action=PMSmenu")
    wait = WebDriverWait(driver, 20)
    time.sleep(1.0)

    valid_strategies = {"equity", "debt", "hybrid", "multi"}
    strat_id = strategy_value if strategy_value in valid_strategies else "equity"
    strat_radio = driver.find_element(By.ID, strat_id)
    driver.execute_script("arguments[0].click();", strat_radio)
    print(f"[SCRAPE] Strategy: {strat_id}, Service: {service_label}, Month-Year: {month_value}-{year_value}")
    time.sleep(0.2)

    radio_id = "servicetypeN" if service_code.upper() == "N" else "servicetypeD"
    service_radio = driver.find_element(By.ID, radio_id)
    driver.execute_script("arguments[0].click();", service_radio)
    time.sleep(0.2)

    if pms_name:
        select_elem = wait.until(EC.element_to_be_clickable((By.ID, "pmsProvideName")))
        try:
            Select(select_elem).select_by_visible_text(pms_name)
            print(f"[SCRAPE] PMS Provider: {pms_name}")
            time.sleep(0.2)
        except Exception:
            print(f"[WARNING] PMS Provider '{pms_name}' NOT FOUND. Using ALL.")
            pms_name = None
    else:
        print("[SCRAPE] PMS Provider: ALL")

    Select(driver.find_element(By.ID, "fromYears")).select_by_visible_text(year_value)
    time.sleep(0.4)
    Select(driver.find_element(By.ID, "fromMonth")).select_by_visible_text(month_value.zfill(2))
    time.sleep(0.6)

    selected_month = Select(driver.find_element(By.ID, "fromMonth")).first_selected_option.text.strip()
    selected_year = Select(driver.find_element(By.ID, "fromYears")).first_selected_option.text.strip()
    print(f"[SCRAPE] Dropdown now set to: {selected_month}-{selected_year}")

    submit_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'Submit')]")))
    submit_btn.click()
    time.sleep(1.0)

    ia_results = []
    page_num = 1
    while True:
        wait.until(EC.visibility_of_element_located((By.XPATH, "//table//tbody//tr")))
        time.sleep(0.2)
        rows = driver.find_elements(By.XPATH, "//table//tbody/tr")
        for row in rows:
            tds = row.find_elements(By.TAG_NAME, "td")
            if len(tds) < 3:
                continue
            ia_name_raw = tds[1].text.strip()
            aum_raw = tds[2].text.strip()
            aum = aum_raw.replace("₹", "").strip()

            ia_name_raw = ia_name_raw.lstrip(". ").strip()
            ia_name = clean_ia_name(ia_name_raw, pms_name)

            ia_results.append({"ia_name": ia_name, "aum": aum})
        print(f"[SCRAPE] Page {page_num}: total records so far = {len(ia_results)}")

        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.2)
            next_btn = driver.find_element(By.XPATH, "//a[normalize-space(text())='Next']")
            is_disabled = (
                "disabled" in (next_btn.get_attribute("class") or "").lower()
                or next_btn.get_attribute("aria-disabled") == "true"
            )
            if is_disabled:
                print("[SCRAPE] Next disabled. End of pages.")
                break
            driver.execute_script("arguments[0].click();", next_btn)
            page_num += 1
            wait.until(EC.text_to_be_present_in_element((By.XPATH, "//a[@aria-current='page']"), str(page_num)))
            time.sleep(0.5)
        except Exception as e:
            print(f"[SCRAPE] Pagination finished or error: {e}")
            break

    print(f"[SCRAPE] Finished: {len(ia_results)} IA records.")
    print("--------------- SCRAPED VALUES ---------------")
    print(f"Strategy     : {strategy_value}")
    print(f"Service Type : {service_label}")
    print(f"Month-Year   : {month_value}-{year_value}")
    print(f"PMS Provider : {pms_name if pms_name else 'ALL'}")
    print("----------------------------------------------")
    max_preview = 20
    for idx, item in enumerate(ia_results):
        if idx >= max_preview:
            print(f"... ({len(ia_results) - max_preview} more rows)")
            break
        print(f"IA: {item['ia_name']} | AUM: {item['aum']}")
    print("------------------------------------------------\n")

    return ia_results, service_label


# =========================
# MAIN (CLI ONLY)
# =========================
def print_usage():
    print(
        "Usage:\n"
        "  python3 data.py <PMS_NAME_OR_-> <MM-YYYY> [<MM-YYYY> ...]\n\n"
        "Examples:\n"
        '  python3 data.py "Capitalmind Financial Services Private Limited" 06-2024 07-2024\n'
        "  python3 data.py - 06-2024 07-2024 08-2025   # '-' = ALL PMS\n"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2:
        print_usage()
        sys.exit(1)

    raw_pms = args[0].strip()
    if raw_pms == "-" or raw_pms.lower() == "all":
        pms_name = None
    else:
        pms_name = raw_pms

    worksheet_name = sheet_name_from_pms(pms_name)
    print(f"[MAIN] Using worksheet/tab: '{worksheet_name}'")

    month_year_pairs = []
    for token in args[1:]:
        token = token.strip()
        if "-" not in token:
            print(f"[ERROR] Invalid month-year '{token}'. Expected MM-YYYY.")
            print_usage()
            sys.exit(1)
        m, y = token.split("-", 1)
        m = m.strip()
        y = y.strip()
        if len(m) != 2 or not m.isdigit():
            print(f"[ERROR] Invalid month '{m}'. Use 2 digits, e.g. 06.")
            sys.exit(1)
        if len(y) != 4 or not y.isdigit():
            print(f"[ERROR] Invalid year '{y}'. Use 4 digits, e.g. 2025.")
            sys.exit(1)
        month_year_pairs.append((m, y))

    strategies = ["equity", "debt", "hybrid", "multi"]
    service_codes = ["D", "N"]

    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--headless=new")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        for (month_value, year_value) in month_year_pairs:
            for strategy_value in strategies:
                for service_code in service_codes:
                    print(
                        f"\n=== RUN: {strategy_value.upper()} | {service_code} | "
                        f"{month_value}-{year_value} | Sheet: {worksheet_name} ==="
                    )

                    ia_results, service_label = scrape_one_combo(
                        driver=driver,
                        strategy_value=strategy_value,
                        service_code=service_code,
                        month_value=month_value,
                        year_value=year_value,
                        pms_name=pms_name,
                    )

                    if not ia_results:
                        print("[INFO] No IA results for this combo; skipping.")
                        continue

                    data_for_sheet = {
                        "strategy": strategy_value,
                        "service_type": service_label,
                        "month": month_value,
                        "year": year_value,
                        "pms_name": pms_name,
                        "results": ia_results,
                    }

                    update_pms_sheet(data_for_sheet, worksheet_name)

            recompute_totals_for_month(month_value, year_value, worksheet_name)
    finally:
        driver.quit()
        time.sleep(1)

