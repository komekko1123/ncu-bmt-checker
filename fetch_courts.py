import os, re, json, time, calendar, requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # CI 上沒有 .env，直接吃環境變數

ACCOUNT  = os.environ["FIT17_ACCOUNT"]
PASSWORD = os.environ["FIT17_PASSWORD"]

# 一律以台灣時間為基準。GitHub runner 是 UTC，用 datetime.today() 會在
# 台灣時間 00:00~08:00 抓到前一天，導致整個 18 天視窗往前偏一天。
TZ = ZoneInfo("Asia/Taipei")

# 查詢天數：從今天起往後幾天（NCU 最多開放約 15 天，18 保留餘裕）
DAYS = 18

COURTS = {
    "50116": {"name": "羽球場01 近講臺右", "member_id": "735523"},
    "50117": {"name": "羽球場02 近講臺中", "member_id": "735525"},
    "50118": {"name": "羽球場03 近講臺左", "member_id": "735529"},
    "50119": {"name": "羽球場04 近門口右", "member_id": "735531"},
    "50121": {"name": "羽球場06 近門口左", "member_id": "735536"},
}

BASE = "https://17fit.com"


def now_tw():
    return datetime.now(TZ)


def log(msg):
    print(f"[{now_tw():%H:%M:%S}] {msg}", flush=True)


# ---------- 排程校正 ----------

def wait_until(hhmmss):
    """睡到今天的指定台灣時刻。用來抵銷 GitHub Actions cron 的發車延遲：
    workflow 提早開跑，job 自己等到真正的目標時間。
    目標時間已過（漂移超過提前量）就不等，直接往下跑。"""
    parts = [int(x) for x in hhmmss.split(":")]
    while len(parts) < 3:
        parts.append(0)
    now = now_tw()
    target = now.replace(hour=parts[0], minute=parts[1],
                         second=parts[2], microsecond=0)
    delta = (target - now).total_seconds()
    if delta <= 0:
        log(f"目標時間 {hhmmss} 已過 {-delta:.0f}s（cron 漂移），直接執行")
        return
    if delta > 3600:
        log(f"距離 {hhmmss} 還有 {delta / 60:.0f} 分鐘，超過 1 小時，不等待")
        return
    log(f"等待至 {hhmmss}（{delta:.0f}s）…")
    # 分段睡，最後 5 秒改用短間隔逼近，避免單次長睡的累積誤差
    while True:
        remain = (target - now_tw()).total_seconds()
        if remain <= 0:
            break
        time.sleep(min(remain, 5) if remain <= 5 else min(remain - 5, 30))
    log(f"到點，開始抓取")


def _shift_to_workday(d):
    """遇週末提前至前一個平日。（不含國定假日，需要的話自行補一份清單）"""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def is_open_day(d):
    """是否為開放預約日：每月 15 日、每月最後工作日（遇假日提前）。"""
    fifteenth = _shift_to_workday(date(d.year, d.month, 15))
    last = _shift_to_workday(
        date(d.year, d.month, calendar.monthrange(d.year, d.month)[1]))
    return d in (fifteenth, last)


# ---------- 17fit API ----------

def _csrf(text):
    m = re.search(r'name="csrf_token" content="([^"]+)"', text)
    return m.group(1) if m else ""


def _post(s, url, tries=3, **kw):
    """開放瞬間伺服器最忙，單次失敗就整輪報銷，所以帶重試。"""
    for i in range(tries):
        try:
            r = s.post(url, timeout=15, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"{url} 失敗（{e}），{2 ** i}s 後重試")
            time.sleep(2 ** i)


def setup():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"

    r = s.get(f"{BASE}/service-flow-dt", timeout=15)
    csrf = _csrf(r.text)

    resp = _post(s, f"{BASE}/webapi/account/login",
        headers={"X-CSRF-TOKEN": csrf, "Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
        json={"account": ACCOUNT, "password": PASSWORD, "location": "TW",
              "third_party_type": None, "third_party_authorization": None})
    assert resp.json()["code"] == 0, f"登入失敗：{resp.text}"

    s.post(f"{BASE}/service-flow-sp",
        headers={"Referer": f"{BASE}/service-list/1090",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"_token": csrf, "currency": "NT$", "studio_id": "1090",
              "branch_id": "1275", "selected_services": "28055",
              "selected_services_namelist": "羽球館線上預約，現場付費",
              "selected_services_timetotal": "60",
              "selected_services_pricetotal": "250",
              "service_url": f"{BASE}/service-list/1090?tab=appointments"},
        timeout=15, allow_redirects=False)

    return s, csrf


def select_court(s, csrf, court_id, info):
    r = _post(s, f"{BASE}/service-flow-dt",
        headers={"Referer": f"{BASE}/service-flow-sp",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"member_id": info["member_id"], "role_relationships_id": court_id,
              "member_name": info["name"], "level_price": "0", "_token": csrf})
    return _csrf(r.text) or csrf


def fetch(s, csrf, date_str):
    r = _post(s, f"{BASE}/getServiceProviderDateTimeApi",
        headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"date": date_str})
    return sorted({item["time"] for item in r.json() if item.get("time")})


# ---------- 主流程 ----------

def collect(s, csrf):
    today = now_tw()
    dates = [(today + timedelta(days=i)).strftime("%Y/%m/%d") for i in range(DAYS)]
    availability = {cid: {} for cid in COURTS}

    for cid, info in COURTS.items():
        csrf = select_court(s, csrf, cid, info)
        for d in dates:
            availability[cid][d] = fetch(s, csrf, d)

    result = {
        "updated_at": now_tw().strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
        "data": [
            {
                "date": d,
                "weekday": datetime.strptime(d, "%Y/%m/%d").strftime("%a"),
                "courts": [
                    {"name": COURTS[cid]["name"], "id": cid,
                     "available_slots": availability[cid].get(d, [])}
                    for cid in COURTS
                ]
            }
            for d in dates
        ]
    }
    return result, csrf


def open_dates(result):
    """有開出任何時段的日期集合。開放瞬間會突然多出一批新日期。"""
    return {d["date"] for d in result["data"]
            if any(c["available_slots"] for c in d["courts"])}


def save(result):
    with open("slots.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main():
    # 只在開放日執行（給 booking-window workflow 用）
    if os.environ.get("ONLY_ON_OPEN_DAY") == "1" and not is_open_day(now_tw().date()):
        log(f"{now_tw():%Y-%m-%d} 不是開放預約日，跳過")
        return

    target = os.environ.get("TARGET_TIME")
    if target:
        wait_until(target)

    s, csrf = setup()
    result, csrf = collect(s, csrf)
    save(result)
    baseline = open_dates(result)
    log(f"首輪完成，{len(baseline)} 天有空場")

    # 輪詢模式：開放瞬間官方後台可能慢幾十秒才真的放票，
    # 所以持續重抓直到出現新日期，或到達截止時間。
    poll_until = os.environ.get("POLL_UNTIL")      # 例 "17:10"
    interval = int(os.environ.get("POLL_INTERVAL", "20"))
    if not poll_until:
        return

    parts = [int(x) for x in poll_until.split(":")]
    deadline = now_tw().replace(hour=parts[0], minute=parts[1],
                                second=0, microsecond=0)
    while now_tw() < deadline:
        time.sleep(interval)
        try:
            result, csrf = collect(s, csrf)
        except Exception as e:
            log(f"輪詢抓取失敗：{e}")
            continue
        current = open_dates(result)
        save(result)
        new = current - baseline
        if new:
            log(f"偵測到新開放日期：{sorted(new)}")
            return
        log(f"尚無新日期，{interval}s 後再試")
    log("輪詢逾時，以最後一次結果為準")


if __name__ == "__main__":
    main()
