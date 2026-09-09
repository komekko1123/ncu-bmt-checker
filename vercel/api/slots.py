"""
即時查詢單日空場。

  GET /api/slots?date=YYYY/MM/DD
  → { date, weekday, courts: [{id, name, available_slots}], fetched_at, cached }

為什麼放 Vercel 而不是 Cloudflare Workers：17fit 封鎖 Cloudflare 的出口網段
（實測所有 17fit 主機名一律逾時，而 example.com/google.com 正常）。
Vercel 的 hnd1（東京）出口實測 200 / 248ms，正常。

流程與 fetch_courts.py 相同，差別是這裡 5 個場地各自獨立 session 平行跑
—— 選場（service-flow-dt）是寫在 session 狀態上的，共用會互相蓋掉。
"""

from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import json, os, re, time

import requests

TZ = timezone(timedelta(hours=8))
BASE = "https://17fit.com"
UA = "Mozilla/5.0"

COURTS = {
    "50116": {"name": "羽球場01 近講臺右", "member_id": "735523"},
    "50117": {"name": "羽球場02 近講臺中", "member_id": "735525"},
    "50118": {"name": "羽球場03 近講臺左", "member_id": "735529"},
    "50119": {"name": "羽球場04 近門口右", "member_id": "735531"},
    "50121": {"name": "羽球場06 近門口左", "member_id": "735536"},
}

SLOT_TTL = 60           # 同一日期的結果重用幾秒
SESSION_TTL = 6 * 3600  # session 重用多久
MAX_DAYS_AHEAD = 30     # 只接受這個範圍內的日期，避免被當成任意 proxy

# Vercel 的容器在連續請求間會保留（warm start），所以這兩個 dict 能跨請求重用。
# 冷啟動時會清空，屆時重新登入即可 —— 不是快取失效，只是多花 1 秒。
_sessions = {}     # court_id -> {"s": Session, "csrf": str, "born": float}
_slot_cache = {}   # date -> {"body": dict, "ts": float}


class SessionExpired(Exception):
    pass


def _csrf(text):
    m = re.search(r'name="csrf_token" content="([^"]+)"', text)
    return m.group(1) if m else ""


def establish(court_id):
    """建立單一場地的 session：取 csrf → 登入 → 設定服務 → 選場。"""
    info = COURTS[court_id]
    s = requests.Session()
    s.headers["User-Agent"] = UA

    r = s.get(f"{BASE}/service-flow-dt", timeout=15)
    csrf = _csrf(r.text)

    r = s.post(f"{BASE}/webapi/account/login",
        headers={"X-CSRF-TOKEN": csrf, "Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
        json={"account": os.environ["FIT17_ACCOUNT"],
              "password": os.environ["FIT17_PASSWORD"],
              "location": "TW", "third_party_type": None,
              "third_party_authorization": None},
        timeout=15)
    if r.json().get("code") != 0:
        raise RuntimeError(f"登入失敗：{r.text[:200]}")

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

    r = s.post(f"{BASE}/service-flow-dt",
        headers={"Referer": f"{BASE}/service-flow-sp",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"member_id": info["member_id"], "role_relationships_id": court_id,
              "member_name": info["name"], "level_price": "0", "_token": csrf},
        timeout=15)
    csrf = _csrf(r.text) or csrf

    return {"s": s, "csrf": csrf, "born": time.time()}


def fetch_slots(sess, date):
    r = sess["s"].post(f"{BASE}/getServiceProviderDateTimeApi",
        headers={"X-CSRF-TOKEN": sess["csrf"], "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"date": date}, timeout=15, allow_redirects=False)
    # session 過期時會 302 回登入頁或吐 HTML，不會是 JSON
    if r.status_code != 200 or "json" not in r.headers.get("Content-Type", ""):
        raise SessionExpired()
    return sorted({x["time"] for x in r.json() if x.get("time")})


def court_slots(court_id, date):
    """先用既有 session，失效才重新登入。"""
    sess = _sessions.get(court_id)
    if sess and time.time() - sess["born"] < SESSION_TTL:
        try:
            return fetch_slots(sess, date)
        except SessionExpired:
            pass
    sess = establish(court_id)
    _sessions[court_id] = sess
    return fetch_slots(sess, date)


def collect(date):
    ids = list(COURTS)
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        results = list(pool.map(lambda c: court_slots(c, date), ids))
    dt = datetime.strptime(date, "%Y/%m/%d")
    return {
        "date": date,
        "weekday": dt.strftime("%a"),
        "courts": [{"id": c, "name": COURTS[c]["name"], "available_slots": r}
                   for c, r in zip(ids, results)],
        "fetched_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
    }


def valid_date(date):
    if not re.fullmatch(r"\d{4}/\d{2}/\d{2}", date or ""):
        return False
    try:
        target = datetime.strptime(date, "%Y/%m/%d").date()
    except ValueError:
        return False
    today = datetime.now(TZ).date()
    return 0 <= (target - today).days <= MAX_DAYS_AHEAD


def cors_for(origin):
    allowed = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]
    if "*" in allowed or origin in allowed:
        return origin or "*"
    return "null"


def probe():
    """診斷用：確認這台機器的出口連不連得到 17fit。"""
    import urllib.request, urllib.error
    out = []
    for url in ("https://example.com/", "https://17fit.com/service-flow-dt"):
        t0 = time.time()
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(rq, timeout=8) as r:
                out.append({"target": url, "status": r.status,
                            "ms": int((time.time() - t0) * 1000),
                            "server": r.headers.get("Server")})
        except Exception as e:
            out.append({"target": url, "failed": type(e).__name__,
                        "ms": int((time.time() - t0) * 1000)})
    return {"region": os.environ.get("VERCEL_REGION"), "results": out}


class handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin",
                         cors_for(self.headers.get("Origin", "")))
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",
                         cors_for(self.headers.get("Origin", "")))
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        # Vercel 新版 Python runtime 只認一個 entrypoint，所以路由在這裡分派
        path = urlparse(self.path).path
        if path.endswith("/health"):
            return self._send(200, {"ok": True})
        if path.endswith("/probe"):
            return self._send(200, probe())

        date = (parse_qs(urlparse(self.path).query).get("date") or [""])[0]
        if not valid_date(date):
            return self._send(400, {"error": f"date 需為 YYYY/MM/DD 且在今天起 {MAX_DAYS_AHEAD} 天內"})

        hit = _slot_cache.get(date)
        if hit and time.time() - hit["ts"] < SLOT_TTL:
            return self._send(200, {**hit["body"], "cached": True})

        try:
            body = collect(date)
        except Exception as e:
            return self._send(502, {"error": f"{type(e).__name__}: {e}"[:300]})

        _slot_cache[date] = {"body": body, "ts": time.time()}
        self._send(200, {**body, "cached": False})
