"""
โมดูลตอบคำถามข้อมูลยอดขายจาก Sales Analysis Dashboard (https://sales-dashboard-40q.pages.dev/)

หน้าเว็บ Dashboard ดึงข้อมูลจาก Google Sheet สาธารณะ บอทจึงอ่านจากชีทเดียวกันโดยตรง (ไม่ต้องเปิดเว็บ)
ขั้นตอนการตอบคำถาม:
  1. ให้ Gemini เขียนโค้ด pandas สั้นๆ จากคำถาม (Text-to-Pandas)
  2. ตรวจโค้ดด้วย AST ให้ปลอดภัย แล้วรันกับข้อมูลจริง
  3. ให้ Gemini (กัญญ์วรา) สรุปผลลัพธ์เป็นภาษาไทย
"""
import io
import re
import ast
import json
import time
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from google.genai import types

import agent

GOOGLE_SHEET_ID = "1YOsuTlk48K0Sz7M4bWbqsYSH5lc6THE5qXTwd4faZ7s"
STOCK_SHEET_ID = "1coWEi-XkpOvj4Rqg88CA1PY6v5d53lA8rJH07rAXjvw"
DASHBOARD_URL = "https://sales-dashboard-40q.pages.dev/"
CACHE_SECONDS = 10 * 60  # โหลดข้อมูลชีทใหม่ทุก 10 นาที

SALES_KEYWORDS = (
    "ยอดขาย", "ยอด", "ขายได้", "ขายดี", "ออเดอร์", "order", "invoice", "อินวอยซ์", "บิล",
    "ลูกค้า", "สินค้า", "incentive", "อินเซนทีฟ", "อินเซน", "เป้า", "achievement", "cycle",
    "dashboard", "แดชบอร์ด", "sales", "กำไร", "profit", "คืนสินค้า", "ใบลดหนี้",
    "สั่ง", "ซื้อ", "แถม", "นพ.", "พญ.", "ภก.", "หมอ", "คลินิก", "คลีนิค", "รพ.", "โรงพยาบาล",
    "ร้านยา", "เภสัช", "หจก", "บริษัท", "สต็อก", "stock", "ดีล", "โน้ต", "ราคา", "โปร",
    "exp", "หมดอายุ", "ล็อต", "batch", "lot", "ยา", "mg", "ตารางเวร", "งาน", "task",
    "meithromax", "esloray", "reminisin", "reinisin", "meigabalin", "meicoxia", "meipixa", "meipazin",
    "diclex", "meixil", "gastach", "ampicaban", "proctase", "acofide", "cefazolin", "meiphox",
)

# ---------------------------------------------------------------------------
# 1. โหลดและเตรียมข้อมูลจาก Google Sheet
# ---------------------------------------------------------------------------

def _sheet_url(sheet: str | None = None) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/gviz/tq?tqx=out:csv"
    if sheet:
        url += "&sheet=" + urllib.parse.quote(sheet)
    return url

def _download(sheet: str | None = None, timeout: int = 40) -> bytes:
    req = urllib.request.Request(_sheet_url(sheet), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def _num(series: pd.Series) -> pd.Series:
    """แปลงข้อความตัวเลข เช่น '1,700.00' หรือ '(313,803)' เป็นตัวเลข"""
    s = series.astype(str).str.replace(",", "", regex=False).str.strip()
    s = s.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(s, errors="coerce").fillna(0)

def _be_short_to_ad(series: pd.Series) -> pd.Series:
    """ปี พ.ศ. แบบย่อ (เช่น 69) -> ค.ศ. (2026)"""
    y = pd.to_numeric(series, errors="coerce")
    return (y.where(y >= 2000, y + 2500) - 543).astype("Int64")

def _prepare_sales(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), dtype=str)
    raw_qty = _num(df["Quantity"])
    raw_sales = _num(df["Sales Amt."])
    is_free = (raw_sales == 0) & (raw_qty > 0)
    out = pd.DataFrame({
        "customer": df["Customer name"].fillna(df["Name Thai"]).str.strip(),
        "cust_no": df["Cust. No."],
        "province": df["Address"],
        "customer_type": df["SUB TYPE"],
        "channel": df["CHANNEL"],
        "company": df["COMPANY"],
        "product": df["Product"].str.strip(),
        "product_name": df["Full name"].str.strip(),
        "group": df["Group product"],
        "subgroup": df["SubGroup product"],
        "qty": raw_qty.where(~is_free, 0),
        "free_qty": raw_qty.where(is_free, 0),
        "sales": raw_sales,
        "cost": _num(df["Cost Amt."]),
        "profit": _num(df["Profit"]),
        "inv_no": df["Inv. No."],
        "order_no": df["Order No."],
        "cn_reason": df["CN_REASON"],
        "year": _be_short_to_ad(df["Inv. Year"]),
        "month": pd.to_numeric(df["Inv. Month"], errors="coerce").astype("Int64"),
        "fy": _be_short_to_ad(df["Year_FY"]),
        "fy_month_code": df["MONTH_FY"],
    })
    # วันที่ใบแจ้งหนี้รูปแบบ YYMMDD (ปี พ.ศ. ย่อ) เช่น 690915
    d = df["Inv. Date"].astype(str).str.extract(r"^(\d{2})(\d{2})(\d{2})$")
    out["inv_date"] = pd.to_datetime(
        dict(year=pd.to_numeric(d[0], errors="coerce") + 2500 - 543,
             month=pd.to_numeric(d[1], errors="coerce"),
             day=pd.to_numeric(d[2], errors="coerce")),
        errors="coerce",
    )
    out = out[out["customer"].notna() & out["year"].notna()]
    out = out[~((out["sales"] == 0) & (out["qty"] + out["free_qty"] <= 0))]  # ตัดแถวว่างแบบเดียวกับหน้า Dashboard
    return out.reset_index(drop=True)

def _prepare_invoice(raw: bytes) -> pd.DataFrame:
    if not raw.strip():
        return pd.DataFrame()
    df = pd.read_csv(io.BytesIO(raw), dtype=str)
    return pd.DataFrame({
        "billing_month": df.get("Billing Month (Calendar)"),
        "date": pd.to_datetime(df.get("Billing Date"), errors="coerce"),
        "invoice_no": df.get("Invoice No"),
        "customer": df.get("customer name").fillna(df.get("Sold To Customer Name")),
        "customer_group": df.get("Customer Local Group 1 Desc"),
        "district": df.get("Sold To District"),
        "product": df.get("item"),
        "product_name": df.get("item full name").fillna(df.get("Material Desc")),
        "value": _num(df.get("Value - Transaction")),
        "qty": _num(df.get("Qty - Transaction")),
        "bonus_qty": _num(df.get("Qty Bonus Net Returns")),
        "price": _num(df.get("Selling Price")),
        "expiry": df.get("Expiration Date"),
    })

def _prepare_orders(raw: bytes) -> pd.DataFrame:
    if not raw.strip():
        return pd.DataFrame()
    df = pd.read_csv(io.BytesIO(raw), dtype=str)
    return pd.DataFrame({
        "date": pd.to_datetime(df.get("Datetime"), errors="coerce"),
        "customer": df.get("Customer - Sold To Customer Name"),
        "po_no": df.get("Customer Purchase Order Number"),
        "order_no": df.get("Order Number"),
        "billing_doc": df.get("Billing Document"),
        "status": df.get("OMR - Status Description (EZRX)"),
        "purchase_type": df.get("Purchase Type"),
        "product_name": df.get("Material Desc"),
        "order_qty": _num(df.get("Order Quantity")),
        "billing_qty": _num(df.get("Billing Quantity")),
        "net_amount": _num(df.get("Net Amount")),
        "billing_amount": _num(df.get("Billing Amount")),
    })

def _prepare_notes(raw: bytes) -> pd.DataFrame:
    if not raw.strip():
        return pd.DataFrame(columns=["customer", "note", "timestamp"])
    df = pd.read_csv(io.BytesIO(raw), dtype=str, header=None)  # ชีทนี้ไม่มีหัวตาราง
    df = df.iloc[:, :3]
    df.columns = ["customer", "note", "timestamp"][: df.shape[1]]
    df["note"] = df["note"].str.replace("\\n", "\n", regex=False)
    return df

def _download_url(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def _prepare_stock(raw: bytes) -> pd.DataFrame:
    """แปลงชีทสต็อกคลังและวันหมดอายุ (Stock & Expiry Date) จาก StockView"""
    if not raw.strip():
        return pd.DataFrame()
    df = pd.read_csv(io.BytesIO(raw), dtype=str)
    out = pd.DataFrame({
        "snapshot_date": df.get("Snapshot Date (End of)"),
        "location_code": df.get("Storage Location Code"),
        "location_desc": df.get("Storage Location Desc"),
        "product_group": df.get("Product Group"),
        "material_code": df.get("Material Code"),
        "product_name": df.get("Material Desc", pd.Series(dtype=str)).str.strip(),
        "batch_no": df.get("Batch No", pd.Series(dtype=str)).str.strip(),
        "expiry_str": df.get("Expiry Date", pd.Series(dtype=str)).str.strip(),
        "expiry_date": pd.to_datetime(df.get("Expiry Date"), errors="coerce"),
        "stock_on_hand": _num(df.get("Stock on Hand", pd.Series(dtype=str))),
        "stock_available": _num(df.get("Stock Available", pd.Series(dtype=str))),
    })
    return out[out["product_name"].notna() & (out["product_name"] != "")].reset_index(drop=True)

def _prepare_prices(raw: bytes) -> pd.DataFrame:
    """แปลงข้อมูล Price List & Promotion Deals จาก /api/price-list"""
    if not raw.strip():
        return pd.DataFrame()
    obj = json.loads(raw.decode("utf-8", errors="ignore"))
    rows = obj.get("data", []) if isinstance(obj, dict) else obj
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)

def _prepare_doctors(raw: bytes) -> pd.DataFrame:
    """แปลงข้อมูลรายชื่อแพทย์และตารางเวรจาก /api/doctors"""
    if not raw.strip():
        return pd.DataFrame()
    rows = json.loads(raw.decode("utf-8", errors="ignore"))
    parsed = []
    for r in rows if isinstance(rows, list) else []:
        info = {}
        try:
            info = json.loads(r.get("data") or "{}")
        except Exception:
            pass
        parsed.append({
            "hospital": r.get("customer_name") or info.get("customer_name"),
            "doctor_name": info.get("fullName") or info.get("name"),
            "specialty": info.get("specialty"),
            "tier": info.get("tier"),
            "is_ptc": info.get("isPtc"),
            "target_products": ", ".join(info.get("targetProducts") or []),
            "prescribe_status": info.get("prescribeStatus"),
            "best_time": info.get("bestTime"),
            "room": info.get("room"),
            "note": info.get("personalNote") or info.get("roleNote"),
            "schedules": json.dumps(info.get("schedules") or [], ensure_ascii=False),
        })
    return pd.DataFrame(parsed)

def _prepare_tasks(raw: bytes) -> pd.DataFrame:
    """แปลงข้อมูลรายการงาน (Task Manager) จาก /api/tasks"""
    if not raw.strip():
        return pd.DataFrame()
    rows = json.loads(raw.decode("utf-8", errors="ignore"))
    parsed = []
    for r in rows if isinstance(rows, list) else []:
        info = {}
        try:
            info = json.loads(r.get("data") or "{}")
        except Exception:
            pass
        parsed.append({
            "title": info.get("title"),
            "scope": r.get("scope") or info.get("scope"),
            "due_date": r.get("due_date") or info.get("dueDate"),
            "month_key": r.get("month_key") or info.get("monthKey"),
            "priority": info.get("priority"),
            "status": r.get("status") or info.get("status"),
            "customer": info.get("customerName"),
            "product": info.get("productName"),
            "notes": info.get("notes"),
        })
    return pd.DataFrame(parsed)

def _prepare_stock_checks(raw: bytes) -> pd.DataFrame:
    """แปลงข้อมูลบันทึกเช็คสต็อกหน้าร้านลูกค้าจาก /api/stock-checks"""
    if not raw.strip():
        return pd.DataFrame()
    rows = json.loads(raw.decode("utf-8", errors="ignore"))
    parsed = []
    for r in rows if isinstance(rows, list) else []:
        cust = r.get("customer_name")
        cdate = r.get("check_date")
        for it in r.get("items") or []:
            parsed.append({
                "customer": cust,
                "check_date": cdate,
                "product": it.get("product"),
                "subgroup": it.get("subGroup"),
                "qty": it.get("qty"),
                "prev_qty": it.get("prevQty"),
                "avg_per_month": it.get("avgPerMonth"),
                "exp_date": it.get("expDate"),
                "note": it.get("note"),
            })
    return pd.DataFrame(parsed)

def _prepare_incentive(raw: bytes) -> str:
    """ชีท Incentive เป็นตารางสรุปแบบกว้าง ส่งให้ AI อ่านเป็นข้อความ CSV ที่ตัดช่องว่างออกแล้ว"""
    text = raw.decode("utf-8", errors="ignore")
    lines = []
    for line in text.splitlines():
        cells = [c.strip().strip('"').strip() for c in line.split('","')]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)[:12000]

CYCLES = [("C1", "เม.ย.-พ.ค.", (4, 5)), ("C2", "มิ.ย.-ก.ค.", (6, 7)), ("C3", "ส.ค.-ก.ย.", (8, 9)),
          ("C4", "ต.ค.-พ.ย.", (10, 11)), ("C5", "ธ.ค.-ม.ค.", (12, 1)), ("C6", "ก.พ.-มี.ค.", (2, 3))]

def current_cycle_id() -> str:
    m = agent.now_th().month
    return next(cid for cid, _, months in CYCLES if m in months)

def _cell_num(val) -> float | None:
    s = str(val or "").replace(",", "").replace("%", "").strip()
    if s in ("", "-"):
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None

def _prepare_incentive_table(raw: bytes) -> tuple[pd.DataFrame, pd.DataFrame]:
    """แปลงชีท Incentive (ตารางกว้าง ไม่มีหัวคอลัมน์ชัดเจน) เป็นตาราง 2 ชุด โดยตรวจตำแหน่งคอลัมน์แบบเดียวกับหน้า Dashboard:
    - incentive: ยอดขาย/เป้า/% achievement ของแต่ละกลุ่มสินค้า แยกตาม Cycle (C1-C6) + YTD
    - incentive_gap: Cycle ปัจจุบัน ยังขาดยอดอีกเท่าไหร่ถึงจะถึงขั้น 85%-110% และเงิน incentive ที่ได้
    """
    import csv
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8", errors="ignore"))))
    if len(rows) < 4:
        return pd.DataFrame(), pd.DataFrame()

    gap85_col = next((c for c, v in enumerate(rows[2]) if "85%" in v), None)
    pct_cols = [c for c, v in enumerate(rows[3]) if "%" in v]
    cur = current_cycle_id()

    perf, gaps = [], []
    for row in rows[3:]:
        prod = (row[1] if len(row) > 1 else "").strip()
        if not prod or prod in ("Product", "Item"):
            continue
        get = lambda c: _cell_num(row[c]) if c is not None and 0 <= c < len(row) else None
        for idx, (cid, period, _) in enumerate(CYCLES):
            if idx >= len(pct_cols):
                break
            ach = pct_cols[idx]
            sales_total, target = get(ach - 2), get(ach - 1)
            perf.append({
                "product_group": prod, "annual_target": get(2), "cycle": cid, "period": period,
                "is_current_cycle": cid == cur, "sales": sales_total, "target": target,
                "achievement_pct": round(sales_total / target * 100, 2) if sales_total is not None and target else get(ach),
            })
        if len(pct_cols) > 6:
            ytd = pct_cols[6]
            perf.append({
                "product_group": prod, "annual_target": get(2), "cycle": "YTD", "period": "สะสมทั้งปีถึงปัจจุบัน",
                "is_current_cycle": False, "sales": get(ytd - 2), "target": get(ytd - 1), "achievement_pct": get(ytd),
            })
        if gap85_col is not None:
            gaps.append({
                "product_group": prod, "cycle": cur,
                **{f"gap_{p}": get(gap85_col + i) for i, p in enumerate((85, 90, 95, 100, 105, 110))},
                "incentive_amount": get(gap85_col + 7),
            })
    return pd.DataFrame(perf), pd.DataFrame(gaps)

_cache = {"data": None, "at": 0.0}
_cache_lock = threading.Lock()

def load_data(force: bool = False) -> dict:
    """โหลดข้อมูลทุกชีทพร้อมกัน (Parallel) และ cache ไว้ 10 นาที"""
    with _cache_lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]

        sheets = {"sales": None, "invoice": "invoice", "orders": "order tracking",
                  "notes": "Sheet4", "incentive": "incentive"}
        extra_urls = {
            "stock": f"https://docs.google.com/spreadsheets/d/{STOCK_SHEET_ID}/gviz/tq?tqx=out:csv",
            "prices": f"{DASHBOARD_URL}api/price-list",
            "doctors": f"{DASHBOARD_URL}api/doctors",
            "tasks": f"{DASHBOARD_URL}api/tasks",
            "stock_checks": f"{DASHBOARD_URL}api/stock-checks",
        }
        with ThreadPoolExecutor(max_workers=len(sheets) + len(extra_urls)) as pool:
            futures = {k: pool.submit(_download, v) for k, v in sheets.items()}
            for k, u in extra_urls.items():
                futures[k] = pool.submit(_download_url, u)
            raws = {}
            for k, f in futures.items():
                try:
                    raws[k] = f.result()
                except Exception as e:
                    print(f"Sheet/API download error ({k}): {e}")
                    raws[k] = b""

        if not raws["sales"]:
            if _cache["data"]:
                return _cache["data"]  # ใช้ข้อมูลเก่าไปก่อนถ้าโหลดใหม่ไม่ได้
            raise RuntimeError("โหลดข้อมูลยอดขายจาก Google Sheet ไม่สำเร็จ")

        incentive_df, incentive_gap_df = (_prepare_incentive_table(raws["incentive"])
                                          if raws["incentive"] else (pd.DataFrame(), pd.DataFrame()))
        data = {
            "sales": _prepare_sales(raws["sales"]),
            "invoice": _prepare_invoice(raws["invoice"]),
            "orders": _prepare_orders(raws["orders"]),
            "notes": _prepare_notes(raws["notes"]),
            "incentive": incentive_df,
            "incentive_gap": incentive_gap_df,
            "incentive_text": _prepare_incentive(raws["incentive"]) if raws["incentive"] else "",
            "stock": _prepare_stock(raws["stock"]),
            "prices": _prepare_prices(raws["prices"]),
            "doctors": _prepare_doctors(raws["doctors"]),
            "tasks": _prepare_tasks(raws["tasks"]),
            "stock_checks": _prepare_stock_checks(raws["stock_checks"]),
        }
        _cache["data"] = data
        _cache["at"] = time.time()
        return data

# ---------------------------------------------------------------------------
# 2. คำอธิบายข้อมูลให้ AI เขียนโค้ด
# ---------------------------------------------------------------------------

def _top_values(series: pd.Series, n: int = 12) -> str:
    return ", ".join(str(v) for v in series.dropna().value_counts().head(n).index)

def build_schema_text(data: dict) -> str:
    s = data["sales"]
    latest = s.sort_values(["year", "month"]).iloc[-1]
    inv, orders = data["invoice"], data["orders"]
    return f"""ตัวแปร DataFrame ที่ใช้ได้ (pandas อยู่ในตัวแปร pd):

1) sales = ประวัติยอดขายรายบรรทัดใบแจ้งหนี้ของเขต U7 (ทีม TEAM_A) ตั้งแต่ปี {int(s['year'].min())} ถึงเดือน {int(latest['month'])}/{int(latest['year'])} ({len(s):,} แถว)
   คอลัมน์: customer (ชื่อลูกค้า), cust_no, province (จังหวัด: {_top_values(s['province'])}),
   customer_type ({_top_values(s['customer_type'])}), channel ({_top_values(s['channel'])}), company ({_top_values(s['company'])}),
   product (รหัสสินค้าย่อ เช่น {_top_values(s['product'], 8)}), product_name (ชื่อเต็มสินค้า เช่น {_top_values(s['product_name'], 5)}),
   group (กลุ่มสินค้า: {_top_values(s['group'])}), subgroup (กลุ่มย่อย เช่น {_top_values(s['subgroup'], 8)}),
   qty (จำนวนสั่งซื้อจริง), free_qty (จำนวนแถมฟรีในบิลนั้น), sales (ยอดขายบาท, ติดลบ = ใบลดหนี้/CN), cost, profit, inv_no, order_no, cn_reason (เหตุผลการคืน/CN),
   year (ปี ค.ศ. ของใบแจ้งหนี้), month (1-12), inv_date (datetime),
   fy (ปีงบประมาณ ค.ศ. เริ่ม 1 เม.ย. เช่น fy=2026 คือ เม.ย. 2026 - มี.ค. 2027), fy_month_code (A-04..A-12, B-01..B-03)
   หมายเหตุ:
   - ปีงบประมาณแบ่งเป็น Cycle ละ 2 เดือน: C1=เม.ย.-พ.ค., C2=มิ.ย.-ก.ค., C3=ส.ค.-ก.ย., C4=ต.ค.-พ.ย., C5=ธ.ค.-ม.ค., C6=ก.พ.-มี.ค.
   - ในการสั่งซื้อ 1 ครั้ง มักแยกเป็น 2 แถวในวันเดียวกัน (แถวซื้อจริง qty>0 และแถมฟรี free_qty>0) ดังนั้นเวลาดูประวัติการสั่งซื้อหรือการสั่งล่าสุด ให้ groupby(['inv_date', 'customer', 'product', 'product_name'], as_index=False)[['qty', 'free_qty', 'sales']].sum() เสมอ จะได้เห็นทั้งจำนวนซื้อ จำนวนแถม และยอดเงินครบในบรรทัดเดียว (ห้ามใช้ .iloc[-1] หรือ .tail(1) กับแถวดิบโดยไม่ groupby เพราะจะได้แค่แถวของแถม!)

2) invoice = ใบแจ้งหนี้ของเดือนปัจจุบัน (ยังไม่รวมใน sales) {len(inv)} แถว
   คอลัมน์: billing_month, date, invoice_no, customer, customer_group, district, product, product_name, value (บาท), qty, bonus_qty (ของแถม), price, expiry

3) orders = ติดตามสถานะออเดอร์ล่าสุด {len(orders)} แถว
   คอลัมน์: date, customer, po_no, order_no, billing_doc, status (เช่น {_top_values(orders['status'], 6) if len(orders) else '-'}), purchase_type, product_name, order_qty, billing_qty, net_amount, billing_amount

4) notes = บันทึกโน้ตลูกค้า คอลัมน์: customer, note, timestamp

5) incentive = ผลงานเทียบเป้าของแต่ละกลุ่มสินค้า (ปีงบประมาณปัจจุบัน) แยกตาม Cycle — ตอนนี้อยู่ Cycle {current_cycle_id()}
   คอลัมน์: product_group (เช่น {_top_values(data['incentive']['product_group'], 6) if len(data['incentive']) else '-'}; แถว Total/Grand Total = รวมทุกกลุ่ม),
   annual_target (เป้าทั้งปี), cycle (C1..C6 หรือ YTD = สะสมทั้งปี), period, is_current_cycle (bool), sales (ยอดขายของ cycle), target (เป้าของ cycle), achievement_pct (%)

6) incentive_gap = Cycle ปัจจุบัน แต่ละกลุ่มสินค้ายังขาดยอดอีกเท่าไหร่ถึงจะถึงขั้น achievement
   คอลัมน์: product_group, cycle, gap_85, gap_90, gap_95, gap_100, gap_105, gap_110 (ค่าติดลบ = ยังขาดอีกกี่บาท, ค่าบวก = เกินแล้ว), incentive_amount (เงิน incentive ที่ได้ตอนนี้)

7) stock = สต็อกสินค้าคงคลังและวันหมดอายุ (Expiry Date / EXP) ของยาแต่ละล็อต ({len(data['stock'])} แถว)
   คอลัมน์: snapshot_date, location_code (1001=Commercial Goods ขายได้ปกติ, 10RS=Cust.Reservation, 1004=RUD, 10PR=PRN), location_desc, product_group, material_code,
   product_name (ชื่อยา เช่น {', '.join(data['stock']['product_name'].dropna().unique()[:25]) if len(data['stock']) else '-'}),
   batch_no (เลขล็อต), expiry_str (วันหมดอายุ เช่น '5/15/2029'), expiry_date (datetime วันหมดอายุ), stock_on_hand (จำนวนในคลัง), stock_available (จำนวนพร้อมขาย)

8) prices = ตารางราคายาและโปรโมชันของแถม (Price List) ({len(data['prices'])} แถว)
   คอลัมน์: category, code, name, pack_size, channel (HOS / Clinic / Drugstore), net_price, ltd_price, deal_1, deal_2, deal_3, deal_4, min_box_price

9) doctors = ข้อมูลแพทย์ประจำ รพ./คลินิก ({len(data['doctors'])} แถว)
   คอลัมน์: hospital, doctor_name, specialty, tier, is_ptc, target_products, prescribe_status, best_time, room, note, schedules

10) tasks = รายการงาน/นัดหมายของเซลส์ ({len(data['tasks'])} แถว) คอลัมน์: title, scope, due_date, month_key, priority, status, customer, product, notes
11) stock_checks = บันทึกเช็คสต็อกหน้าร้านลูกค้า ({len(data['stock_checks'])} แถว) คอลัมน์: customer, check_date, product, subgroup, qty, prev_qty, avg_per_month, exp_date, note

ข้อควรรู้:
- หากผู้ใช้พิมพ์ชื่อยาสะกดผิดเล็กน้อย (เช่น พิมพ์ 'reinisin' แทนที่จะเป็น 'reminisin') ให้เทียบกับชื่อยาจริงในระบบแล้วใช้คำค้นที่สะกดถูกต้อง (เช่น 'REMINISIN')
- เวลาถามว่ายาตัวไหน exp (หมดอายุ) เมื่อไหร่ หรือเหลือสต็อกเท่าไหร่ ให้ดูจากตาราง stock โดยแสดงครบทุกคอลัมน์ ['product_name', 'batch_no', 'location_desc', 'expiry_str', 'expiry_date', 'stock_available']
- ชื่อลูกค้าในตารางมักมีเว้นวรรคหลังคำนำหน้า เช่น "นพ. โกวิทย์ ธาราศักดิ์" เวลาค้นหาชื่อลูกค้าให้ตัดคำนำหน้า (นพ./พญ./ภก./รพ./หจก./บริษัท) ออกแล้วค้นเฉพาะชื่อจริง เช่น .str.contains('โกวิทย์', case=False, na=False, regex=False)
- ชื่อสินค้าเดียวกันอาจมีหลายรูปแบบ (เช่น MEITHROMAX มีทั้งแบบ Capsule และ Oral Suspension) ถ้าถามว่าสั่งล่าสุดเมื่อไหร่ ให้แสดงวันที่สั่งล่าสุดของทุกรูปแบบสินค้าที่ตรงกับชื่อนั้น พร้อมรายการล่าสุด 5 รายการเรียงตาม inv_date"""

# ---------------------------------------------------------------------------
# 3. ตรวจความปลอดภัยของโค้ดที่ AI เขียน แล้วรัน
# ---------------------------------------------------------------------------

_FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "help", "exit", "quit", "memoryview", "type", "object",
    "classmethod", "staticmethod", "property", "super", "os", "sys", "subprocess", "agent",
}
_FORBIDDEN_ATTRS = {"to_csv", "to_excel", "to_pickle", "to_parquet", "to_json", "to_sql", "to_hdf",
                    "to_clipboard", "to_feather", "to_stata", "to_html", "to_latex", "to_markdown", "to_xml",
                    "read_csv", "read_excel", "read_pickle", "read_parquet", "read_json", "read_sql",
                    "read_html", "read_table", "read_fwf", "read_xml", "read_clipboard", "eval", "query",
                    "pipe", "system", "popen", "os", "sys", "io", "subprocess", "builtins", "importlib",
                    "load", "loads", "options", "set_option"}

_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in ("len", "sum", "min", "max", "round", "sorted", "abs", "list", "dict", "str", "int",
                 "float", "bool", "range", "enumerate", "zip", "set", "tuple", "any", "all",
                 "isinstance", "print", "reversed", "map", "filter")
}

def _validate_code(code: str):
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.With,
                             ast.AsyncWith, ast.AsyncFunctionDef, ast.ClassDef, ast.While, ast.Try)):
            raise ValueError(f"ไม่อนุญาตให้ใช้ {type(node).__name__}")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in _FORBIDDEN_NAMES):
            raise ValueError(f"ไม่อนุญาตให้ใช้ชื่อ {node.id}")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRS):
            raise ValueError(f"ไม่อนุญาตให้ใช้ .{node.attr}")

def _run_code(code: str, data: dict):
    _validate_code(code)
    env = {
        "__builtins__": _SAFE_BUILTINS,
        "pd": pd,
        "sales": data["sales"].copy(),
        "invoice": data["invoice"].copy(),
        "orders": data["orders"].copy(),
        "notes": data["notes"].copy(),
        "incentive": data["incentive"].copy(),
        "incentive_gap": data["incentive_gap"].copy(),
        "stock": data["stock"].copy(),
        "prices": data["prices"].copy(),
        "doctors": data["doctors"].copy(),
        "tasks": data["tasks"].copy(),
        "stock_checks": data["stock_checks"].copy(),
        "today": pd.Timestamp(agent.now_th().replace(tzinfo=None)).normalize(),
    }
    exec(compile(code, "<sales_query>", "exec"), env)
    if "result" not in env:
        raise ValueError("โค้ดไม่ได้กำหนดตัวแปร result")
    return env["result"]

def _format_result(result) -> str:
    with pd.option_context("display.max_rows", 60, "display.max_columns", 20, "display.width", 200,
                           "display.float_format", lambda v: f"{v:,.2f}"):
        if isinstance(result, pd.DataFrame):
            text = result.head(60).to_string()
            if len(result) > 60:
                text += f"\n... (ทั้งหมด {len(result):,} แถว แสดง 60 แถวแรก)"
        elif isinstance(result, pd.Series):
            text = result.head(60).to_string()
            if len(result) > 60:
                text += f"\n... (ทั้งหมด {len(result):,} รายการ แสดง 60 รายการแรก)"
        elif isinstance(result, float):
            text = f"{result:,.2f}"
        else:
            text = str(result)
    return text[:6000]

# ---------------------------------------------------------------------------
# 4. ถาม-ตอบ
# ---------------------------------------------------------------------------

# จำคำถาม-คำตอบเรื่องยอดขายล่าสุดของแต่ละคน เพื่อรองรับคำถามต่อเนื่อง เช่น "แล้วเดือนก่อนล่ะ"
_recent_qa: dict[int, list[tuple[str, str]]] = {}
_last_sales_at: dict[int, float] = {}

def is_sales_question(user_id: int, text: str) -> bool:
    lower = text.lower()
    if any(k in lower for k in SALES_KEYWORDS):
        return True
    # ถามต่อเนื่องภายใน 5 นาทีหลังคุยเรื่องยอดขาย (ถ้าไม่ใช่คำถามข้อมูล AI จะตอบ NOT_DATA_QUESTION แล้วไปคุยปกติ)
    return time.time() - _last_sales_at.get(user_id, 0) < 300

def _generate(prompt: str, json_mode: bool = False, system: str | None = None) -> str:
    last_err = None
    for model_name in agent.FAST_MODELS:
        try:
            cfg = {}
            if json_mode:
                cfg["response_mime_type"] = "application/json"
            if system:
                cfg["system_instruction"] = system
            res = agent.client.models.generate_content(
                model=model_name, contents=prompt, config=types.GenerateContentConfig(**cfg)
            )
            if res.text:
                return res.text
        except Exception as e:
            last_err = e
            print(f"Sales generate error on {model_name}: {e}")
    raise RuntimeError(f"AI ไม่ตอบกลับ: {last_err}")

def _ask_for_code(question: str, schema: str, history: list, previous_error: str | None = None) -> dict:
    hist_text = "\n".join(f"- ถาม: {q}\n  ตอบ: {a[:300]}" for q, a in history[-3:]) or "-"
    err_text = f"\n\nโค้ดรอบก่อนรันแล้ว error: {previous_error}\nช่วยแก้โค้ดใหม่" if previous_error else ""
    now = agent.now_th()
    prompt = f"""คุณคือนักวิเคราะห์ข้อมูลยอดขายที่เขียน Python pandas เก่งมาก
วันนี้คือ {now.strftime('%Y-%m-%d')} (ตัวแปร today เป็น pd.Timestamp ของวันนี้)

{schema}

บทสนทนาเรื่องยอดขายก่อนหน้า (ใช้ตีความคำถามต่อเนื่อง):
{hist_text}

คำถามใหม่จากผู้ใช้: "{question}"

กติกา:
- เขียนโค้ด pandas ที่คำนวณคำตอบ แล้วเก็บผลไว้ในตัวแปร result (เป็นตัวเลข, ข้อความ, dict, Series หรือ DataFrame ขนาดไม่เกิน 30 แถว)
- ห้าม import, ห้ามอ่าน/เขียนไฟล์, ห้ามใช้ while/class/try (ใช้ฟังก์ชัน pandas และ lambda ได้ตามปกติ)
- ถ้าเป็นรายการจัดอันดับ ให้เรียงจากมากไปน้อยและจำกัดจำนวนแถวด้วย .head()
- ถ้าเป็นการเปรียบเทียบ (เทียบเดือนก่อน/ปีก่อน) ให้ใส่ตัวเลขทั้งสองฝั่งและ % การเปลี่ยนแปลงไว้ใน result
- คำถามเรื่อง incentive / เป้า / achievement ให้ใช้ตาราง incentive และ incentive_gap
- ถ้าคำถาม incentive ซับซ้อนจนตารางไม่พอ ให้ตอบ "use_incentive": true เพิ่มด้วย (ระบบจะแนบตารางดิบให้ AI อ่าน)
- ถ้าข้อความนี้ไม่ใช่คำถามเกี่ยวกับข้อมูลยอดขาย/ลูกค้า/สินค้า/ออเดอร์ (เช่น ทักทาย ขอบคุณ คุยเล่น) ให้ตอบ "not_data_question": true

ตอบเป็น JSON เท่านั้น: {{"not_data_question": false, "use_incentive": false, "code": "โค้ด python"}}{err_text}"""
    raw = _generate(prompt, json_mode=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)

def answer_sales_question(user_id: int, question: str) -> str | None:
    """ตอบคำถามเรื่องยอดขาย คืนค่า None ถ้าไม่ใช่คำถามข้อมูล (ให้ไปคุยแบบปกติแทน)"""
    data = load_data()
    schema = build_schema_text(data)
    history = _recent_qa.get(user_id, [])

    plan = _ask_for_code(question, schema, history)
    if plan.get("not_data_question"):
        _last_sales_at.pop(user_id, None)
        return None

    code = plan.get("code") or "result = None"
    result_text, error = "", None
    for attempt in range(2):
        try:
            result_text = _format_result(_run_code(code, data))
            error = None
            break
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            print(f"Sales code error (attempt {attempt + 1}): {error}\n{code}")
            if attempt == 0:
                try:
                    plan = _ask_for_code(question, schema, history, previous_error=f"{error}\nโค้ดเดิม:\n{code}")
                    code = plan.get("code") or "result = None"
                except Exception as e2:
                    error = str(e2)
                    break

    incentive_part = ""
    if plan.get("use_incentive") and data["incentive_text"]:
        incentive_part = f"\n\n[ตาราง Sale Performance by Cycle / Incentive (เขต U7)]:\n{data['incentive_text']}"

    if error and not incentive_part:
        return (f"แงง กัญญ์คำนวณข้อมูลนี้ไม่สำเร็จค่ะ 🥺 ลองถามใหม่ให้เจาะจงขึ้นอีกนิดได้ไหมคะ "
                f"เช่น ระบุชื่อลูกค้า สินค้า หรือเดือนที่ต้องการ|||MSG|||(รายละเอียด: {error[:200]})")

    answer_prompt = f"""ผู้ใช้ถามเกี่ยวกับข้อมูลยอดขายใน Sales Dashboard ของเขาว่า: "{question}"

ผลการคำนวณจากข้อมูลจริง:
{result_text or '-'}{incentive_part}

ช่วยตอบผู้ใช้เป็นภาษาไทยในบทบาท "กัญญ์วรา" โดย:
- ตอบตรงคำถามก่อน ใช้ตัวเลขจากผลการคำนวณเท่านั้น ห้ามแต่งตัวเลขเอง
- จัดรูปแบบตัวเลขเงินให้มีคอมม่าและหน่วย "บาท" ถ้าเป็นรายการให้ทำเป็น bullet อ่านง่าย
- ถ้าผลลัพธ์ว่างหรือไม่พบข้อมูล ให้บอกตรงๆ และแนะนำให้ลองระบุชื่อให้ชัดขึ้น
- ถ้าเห็น insight จากตัวเลขในผลการคำนวณ (เช่น โตขึ้น/ลดลง) เสริมสั้นๆ ได้ 1 ประโยค แต่ห้ามพูดถึงแนวโน้มหรือการเปรียบเทียบที่ไม่มีตัวเลขรองรับในผลการคำนวณ
- แบ่งเป็น 1-2 กล่องข้อความคั่นด้วย |||MSG|||"""
    answer = _generate(answer_prompt, system=agent.get_system_prompt(user_id))

    _recent_qa.setdefault(user_id, []).append((question, answer))
    _recent_qa[user_id] = _recent_qa[user_id][-5:]
    _last_sales_at[user_id] = time.time()
    return answer
