"""iCloudカレンダーから今日・明日・明後日の予定を読む（CalDAV）

必要な環境変数（Railwayに設定済み）:
  ICLOUD_ID            Apple IDのメールアドレス
  ICLOUD_APP_PASSWORD  アプリ用パスワード

requirements.txt に `caldav` を1行追加すること。
"""
import os
import datetime as dt
from zoneinfo import ZoneInfo

import caldav

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"


def _client():
    return caldav.DAVClient(
        url="https://caldav.icloud.com/",
        username=os.environ["ICLOUD_ID"],
        password=os.environ["ICLOUD_APP_PASSWORD"],
    )


def _search(cal, start, end):
    try:
        return cal.search(start=start, end=end, event=True, expand=True)
    except AttributeError:
        return cal.date_search(start, end, expand=True)


def get_events(days=3):
    """今日から days 日分の予定を [(開始, 終日か, 件名, 場所), ...] で返す（開始順）"""
    now = dt.datetime.now(JST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=days)

    items = []
    principal = _client().principal()
    for cal in principal.calendars():
        try:
            events = _search(cal, start, end)
        except Exception:
            continue  # リマインダー等、予定でないカレンダーは飛ばす
        for ev in events:
            try:
                comp = ev.icalendar_component
                dtstart = comp.get("dtstart").dt
                title = " ".join(str(comp.get("summary", "（無題）")).split())
                place_raw = str(comp.get("location", "") or "").strip()
                # 住所は改行で複数行になるので、1行目（施設名）だけ使う
                place = place_raw.splitlines()[0].strip() if place_raw else ""
            except Exception:
                continue
            all_day = not isinstance(dtstart, dt.datetime)
            if all_day:
                dtstart = dt.datetime.combine(dtstart, dt.time.min, tzinfo=JST)
            elif dtstart.tzinfo is None:
                dtstart = dtstart.replace(tzinfo=JST)
            else:
                dtstart = dtstart.astimezone(JST)
            items.append((dtstart, all_day, title, place))

    items.sort(key=lambda x: (x[0], not x[1]))
    return items


def format_briefing(days=3):
    """朝ブリーフィング用のテキストを返す"""
    labels = ["今日", "明日", "明後日"]
    today = dt.datetime.now(JST).date()
    items = get_events(days)
    lines = []
    for i in range(days):
        d = today + dt.timedelta(days=i)
        label = labels[i] if i < len(labels) else f"{i}日後"
        lines.append(f"■ {label} {d.month}/{d.day}({WEEKDAYS[d.weekday()]})")
        todays = [x for x in items if x[0].date() == d]
        if not todays:
            lines.append("  予定なし")
        for start, all_day, title, place in todays:
            when = "終日" if all_day else start.strftime("%H:%M")
            tail = f"（{place}）" if place else ""
            lines.append(f"  {when} {title}{tail}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_briefing())
