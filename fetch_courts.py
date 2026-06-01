import os, re, json, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

ACCOUNT  = os.environ["FIT17_ACCOUNT"]
PASSWORD = os.environ["FIT17_PASSWORD"]

# 查詢天數：從今天起往後幾天（NCU 最多開放約 15 天，18 保留餘裕）
# 若要縮短，直接改這個數字，例如 DAYS = 7
DAYS = 18

# 場地清單：可在個別場地加 "days": N 覆蓋全域 DAYS
# 例如 "50116": {"name": "...", "member_id": "...", "days": 7}
COURTS = {
    "50116": {"name": "羽球場01 近講臺右", "member_id": "735523"},
    "50117": {"name": "羽球場02 近講臺中", "member_id": "735525"},
    "50118": {"name": "羽球場03 近講臺左", "member_id": "735529"},
    "50119": {"name": "羽球場04 近門口右", "member_id": "735531"},
    "50121": {"name": "羽球場06 近門口左", "member_id": "735536"},
}

BASE = "https://17fit.com"


def _csrf(text):
    m = re.search(r'name="csrf_token" content="([^"]+)"', text)
    return m.group(1) if m else ""


def setup():
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"

    r = s.get(f"{BASE}/service-flow-dt", timeout=15)
    csrf = _csrf(r.text)

    resp = s.post(f"{BASE}/webapi/account/login",
        headers={"X-CSRF-TOKEN": csrf, "Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
        json={"account": ACCOUNT, "password": PASSWORD, "location": "TW",
              "third_party_type": None, "third_party_authorization": None},
        timeout=15)
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
    r = s.post(f"{BASE}/service-flow-dt",
        headers={"Referer": f"{BASE}/service-flow-sp",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"member_id": info["member_id"], "role_relationships_id": court_id,
              "member_name": info["name"], "level_price": "0", "_token": csrf},
        timeout=15)
    return _csrf(r.text) or csrf


def fetch(s, csrf, date_str):
    r = s.post(f"{BASE}/getServiceProviderDateTimeApi",
        headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"date": date_str}, timeout=15)
    r.raise_for_status()
    return sorted({item["time"] for item in r.json() if item.get("time")})


def main():
    s, csrf = setup()
    today = datetime.today()

    court_dates = {
        cid: [(today + timedelta(days=i)).strftime("%Y/%m/%d")
              for i in range(info.get("days", DAYS))]
        for cid, info in COURTS.items()
    }
    all_dates = sorted(set(d for dates in court_dates.values() for d in dates))
    availability = {cid: {} for cid in COURTS}

    for cid, info in COURTS.items():
        csrf = select_court(s, csrf, cid, info)
        for d in court_dates[cid]:
            availability[cid][d] = fetch(s, csrf, d)

    result = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
            for d in all_dates
        ]
    }

    with open("slots.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
