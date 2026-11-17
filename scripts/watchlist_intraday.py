"""Watch fresh A-share quotes during one trading session (Beijing time)."""
import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
import re
import subprocess
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from watchlist_brief import STOCKS

TZ = ZoneInfo("Asia/Shanghai")
WINDOW = timedelta(minutes=10)
MAX_GAP = timedelta(seconds=90)


@dataclass(frozen=True)
class Quote:
    timestamp: datetime
    price: Decimal
    yesterday: Decimal
    volume: int


def quotes(now):
    codes = ",".join(("sz" if c.startswith("0") else "sh") + c for c in STOCKS)
    request = Request("https://qt.gtimg.cn/q=" + codes, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=12) as response:
        payload = response.read(100000).decode("gb18030", errors="replace")
    result = {}
    for match in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)"', payload):
        code, value = match.groups()
        fields = value.split("~")
        try:
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
            age = (now - stamp).total_seconds()
            quote = Quote(stamp, Decimal(fields[3]), Decimal(fields[4]), int(fields[6]))
            if (code in STOCKS and fields[2] == code and -5 <= age <= 90
                    and quote.price.is_finite() and quote.yesterday.is_finite()
                    and min(quote.price, quote.yesterday, quote.volume) > 0):
                result[code] = quote
        except (IndexError, ValueError, InvalidOperation):
            continue
    return result


class Tracker:
    def __init__(self):
        self.history = deque()
        self.level = None

    def update(self, q):
        h = self.history
        if h and q.timestamp <= h[-1].timestamp:
            return None
        if h and (q.timestamp - h[-1].timestamp > MAX_GAP or q.volume < h[-1].volume):
            h.clear()
            self.level = None
        while len(h) > 1 and h[1].timestamp <= q.timestamp - WINDOW:
            h.popleft()
        confirmed = None
        if self.level is not None:
            if q.price > self.level and q.price > q.yesterday and q.volume > h[-1].volume:
                confirmed = self.level
            self.level = None
        if confirmed is None and len(h) >= 15 and q.timestamp - h[0].timestamp >= WINDOW:
            peak = max(item.price for item in h)
            if q.price > peak and q.price > q.yesterday and q.volume > h[-1].volume:
                self.level = peak
        h.append(q)
        return confirmed


def gh(*args):
    return subprocess.check_output(["gh", *args], text=True, timeout=35)


def notify(code, q, level):
    day = q.timestamp.date().isoformat()
    title = f"盘中观察｜{day}｜{STOCKS[code]} {code}｜价格突破后再次确认"
    # Match bot-authored notifications only. Query directly to avoid search indexing delay.
    repo = os.environ["GITHUB_REPOSITORY"]
    since = q.timestamp.replace(hour=0, minute=0, second=0).isoformat()
    issues = gh("api", "--method", "GET", f"repos/{repo}/issues", "-f", "state=all",
                "-f", f"since={since}", "-f", "per_page=100", "--paginate", "--slurp")
    if any(i["title"] == title and i["user"]["login"] == "github-actions[bot]"
           for page in json.loads(issues) for i in page):
        return
    body = (f"@{os.environ['GITHUB_REPOSITORY_OWNER']}\n\n"
            f"**{STOCKS[code]}（{code}）出现值得复核的价格变化。**\n\n"
            f"北京时间 {q.timestamp:%H:%M:%S} 报 {q.price:.2f} 元，高于昨收 {q.yesterday:.2f} 元。"
            f"此前十分钟采样的最高价是 {level:.2f} 元，连续两个新报价都高于该价，而且两次之间有新增成交。\n\n"
            f"收到邮件时，先在券商 App 看现价和时间。若已跌回 {level:.2f} 元或以下，"
            "这次突破条件已失效；如果价格已经明显走高，也不能把邮件价当成交价。"
            "这条提醒只识别一种价格走势，无法判断之后一定上涨。")
    print(gh("issue", "create", "--repo", repo, "--title", title, "--body", body), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    now = datetime.now(TZ)
    if args.check:
        batch = quotes(now)
        print("Fresh quotes:", ", ".join(batch), flush=True)
        return
    if now.weekday() >= 5:
        return
    schedule = os.environ.get("SESSION_CRON", "")
    morning = schedule in ("10 1 * * 1-5", "15 2 * * 1-5") if schedule else now.time() <= wall_time(11, 30)
    start = datetime.combine(now.date(), wall_time(9, 30) if morning else wall_time(13), TZ)
    end = datetime.combine(now.date(), wall_time(11, 30) if morning else wall_time(14, 57), TZ)
    if now < start - timedelta(minutes=40) or now >= end:
        print("Outside the selected market session.", flush=True)
        return
    while datetime.now(TZ) < start:
        time.sleep(min(30, max(0, (start - datetime.now(TZ)).total_seconds())))
    trackers = {code: Tracker() for code in STOCKS}
    sent = set()
    tick = misses = 0
    while datetime.now(TZ) < end:
        began = time.monotonic()
        try:
            batch = quotes(datetime.now(TZ))
        except OSError as exc:
            print(f"Quote source temporarily unavailable: {type(exc).__name__}", flush=True)
            batch = {}
        misses = 0 if batch else misses + 1
        if tick % 10 == 0:
            print(f"{datetime.now(TZ):%H:%M:%S} fresh {len(batch)}/{len(STOCKS)}: {','.join(batch)}", flush=True)
        if misses >= 20:
            raise RuntimeError("No fresh quotes for ten minutes; stopping this session.")
        for code, q in batch.items():
            if q.timestamp < start or q.timestamp >= end or code in sent:
                continue
            level = trackers[code].update(q)
            if level is not None:
                notify(code, q, level)
                sent.add(code)
        tick += 1
        time.sleep(max(0, 30 - (time.monotonic() - began)))


if __name__ == "__main__":
    main()
