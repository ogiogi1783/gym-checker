# 大和市 体育館キャンセル待ち通知ツール
# 空きが新しく出たら LINE に通知します。

import asyncio
import datetime as dt
import json
import os
import urllib.request

# ===== 設定（ここを書き換えれば条件を変えられます） =====
TARGETS = [
    {"label": "スポセン 第1体育室", "fc": "1001", "rc": "101",
     "rn": "三機MIRAIアリーナ（第1体育室）", "weekday": ("18:00", "21:00")},
    {"label": "スポセン 第3体育室", "fc": "1001", "rc": "111",
     "rn": "第3体育室", "weekday": ("18:00", "21:00")},
    {"label": "ポラリス アリーナ", "fc": "2002", "rc": "161",
     "rn": "アリーナ", "weekday": ("19:30", "21:30")},
]
WEEKDAYS = (2, 3, 4)     # 平日で通知する曜日（0=月 1=火 2=水 3=木 4=金）
DAYS_AHEAD = 60          # 何日先までチェックするか
MAX_DAY_CHECKS = 30      # 1施設あたり1回で調べる日数の上限
FAIL_ALERT_AFTER = 6     # 連続で失敗したらLINEでお知らせ（10分おきなら約1時間）
# ======================================================

BASE = "https://s-yoyaku.e-kanagawa.lg.jp/yamato"
LGC = "142131"
JST = dt.timezone(dt.timedelta(hours=9))
STATE_FILE = "state.json"
WEEK = "月火水木金土日"


def is_open(status):
    return bool(status) and status.startswith("A")


def hm(s):
    h, m = s[:5].split(":")
    return int(h) * 60 + int(m)


def wanted(target, day, start, end, holidays):
    """その日・その時間帯が通知対象かどうか"""
    if day.weekday() >= 5 or day.isoformat() in holidays:
        return True  # 土日祝は時間指定なし
    if day.weekday() not in WEEKDAYS:
        return False  # 対象外の平日
    ws, we = target["weekday"]
    return hm(start) < hm(we) and hm(end) > hm(ws)  # 時間帯が少しでも重なればOK


def open_days(calendar_json):
    days, holidays = [], set()
    for h in calendar_json.get("holidays") or []:
        holidays.add(h["holidayDate"][:10])
    for d in calendar_json.get("dayBooks") or []:
        if is_open(d.get("statusType")):
            days.append(d["usageDate"][:10])
    return days, holidays


def open_slots(target, day_json, holidays):
    """GetDay の結果から、通知対象の空き枠を取り出す"""
    frames = {}
    for ts in day_json.get("timeFrames") or []:
        for f in ts.get("usageTimeFrames") or []:
            frames[f["usageTimeFrameId"]] = f
    slots = []
    for room in day_json.get("rooms") or []:
        courts = room.get("courts") or []
        for court in courts:
            name = court.get("courtName") or ""
            if len(courts) > 1 and "全面" in name:
                continue  # 区画ごとに見るので「全面」は除く
            for book in court.get("dayBooks") or []:
                day = dt.date.fromisoformat(book["usageDate"][:10])
                for u in book.get("usageTimes") or []:
                    f = frames.get(u["usageTimeFrameId"])
                    if not f or not is_open(u.get("statusType")):
                        continue
                    s, e = f["usageStartTime"][:5], f["usageEndTime"][:5]
                    if not wanted(target, day, s, e, holidays):
                        continue
                    slots.append({
                        "key": f'{target["fc"]}-{target["rc"]}-{court.get("courtCode")}-{day}-{u["usageTimeFrameId"]}',
                        "date": day.isoformat(),
                        "text": f'{day.month}/{day.day}({WEEK[day.weekday()]}) {s}-{e} {target["label"]}'
                                + ("" if len(courts) == 1 else f" {name}"),
                        "url": f'{BASE}/FacilityAvailability/Index/{LGC}/{target["fc"]}?rc={target["rc"]}'
                               f'&ptn=2&d={day}&sd={day}&ed={day}',
                    })
    return slots


def body(target, start, end, period=None):
    b = {"startDate": start, "endDate": end, "roomCode": None, "courtSize": None,
         "utilizationPurpose": None, "usePeople": None,
         "room": {"roomCode": target["rc"], "roomName": target["rn"]},
         "toggleTimeType": False, "usageTimes": None, "usagePeriodOfTime": None,
         "allowInternetRequest": None, "requestId": None}
    if period:
        b["period"] = period
    return b


async def check_target(ctx, target, today):
    page = await ctx.new_page()
    captured = {}

    async def on_request(req):
        if "GetCalendar" in req.url and req.method == "POST" and not captured:
            captured.update(await req.all_headers())

    page.on("request", on_request)
    await page.goto(f'{BASE}/FacilityAvailability/Index/{LGC}/{target["fc"]}?rc={target["rc"]}&ptn=1',
                    wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)
    await page.close()
    headers = {k: v for k, v in captured.items()
               if k.lower() not in ("content-length", "cookie", "host") and not k.startswith(":")}
    headers.setdefault("content-type", "application/json")

    async def post(api, data):
        url = f'{BASE}/FacilityAvailability/{api}/{LGC}/{target["fc"]}'
        r = await ctx.request.post(url, data=json.dumps(data), headers=headers, timeout=30000)
        if not r.ok:
            raise RuntimeError(f"{api} HTTP {r.status}")
        return await r.json()

    days, holidays = [], set()
    start = today
    while start <= today + dt.timedelta(days=DAYS_AHEAD):
        end = start + dt.timedelta(days=34)
        d, h = open_days(await post("GetCalendar", body(target, start.isoformat(), end.isoformat(), 35)))
        days += d
        holidays |= h
        start = end + dt.timedelta(days=1)
        await asyncio.sleep(1)

    days = sorted({d for d in days if d >= today.isoformat()})[:MAX_DAY_CHECKS]
    slots = []
    for d in days:
        slots += open_slots(target, await post("GetDay", body(target, d, d)), holidays)
        await asyncio.sleep(1)
    return slots


def send_line(text):
    token = os.environ.get("LINE_TOKEN")
    print("----- LINE -----\n" + text)
    if not token:
        print("(LINE_TOKEN が未設定なので送信しません)")
        return
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=json.dumps({"messages": [{"type": "text", "text": text[:4900]}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print("LINE送信:", r.status)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"open": [], "fails": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def build_message(new_slots):
    lines = [f"🏸 体育館に空きが出ました！（{len(new_slots)}件）", ""]
    for s in new_slots[:15]:
        lines += [f"・{s['text']}", s["url"]]
    if len(new_slots) > 15:
        lines.append(f"…ほか{len(new_slots) - 15}件")
    return "\n".join(lines)


async def main():
    if os.environ.get("MODE") == "test":
        send_line("✅ テスト通知です。体育館の空き通知はこのLINEに届きます。")
        return
    if not os.environ.get("LINE_TOKEN"):
        print("LINE_TOKEN が未設定のため、確認だけして状態は保存しません。")

    from playwright.async_api import async_playwright
    today = dt.datetime.now(JST).date()
    state = load_state()
    slots, errors = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo")
        for t in TARGETS:
            try:
                found = await check_target(ctx, t, today)
                print(f'{t["label"]}: 対象の空き {len(found)}件')
                slots += found
            except Exception as e:
                errors.append(f'{t["label"]}: {e}')
                print("エラー", errors[-1])
        await browser.close()

    if len(errors) == len(TARGETS):
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] == FAIL_ALERT_AFTER:
            send_line("⚠️ 体育館の空き確認が1時間ほど失敗し続けています。サイトの仕様が変わった可能性があります。\n"
                      + "\n".join(errors))
        if os.environ.get("LINE_TOKEN"):
            save_state(state)
        return
    if state.get("fails", 0) >= FAIL_ALERT_AFTER:
        send_line("✅ 体育館の空き確認が復旧しました。")
    state["fails"] = 0

    prev = set(state.get("open", []))
    # 失敗した施設の分は前回の状態を引き継ぐ（復旧時に二重通知しないため）
    failed = [t for t in TARGETS if any(e.startswith(t["label"] + ":") for e in errors)]
    keep = {k for k in prev for t in failed if k.startswith(f'{t["fc"]}-{t["rc"]}-')}
    new = [s for s in slots if s["key"] not in prev]
    new.sort(key=lambda s: s["key"].split("-", 3)[3])
    if new:
        send_line(build_message(new))
    else:
        print("新しい空きはありません")
    state["open"] = sorted({s["key"] for s in slots} | keep)
    if os.environ.get("LINE_TOKEN"):
        save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
