"""Build one plain-language market observation per symbol, then one daily email."""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STOCKS = {
    "002594": "比亚迪", "601398": "工商银行", "601288": "农业银行",
    "601988": "中国银行", "601939": "建设银行", "600900": "长江电力",
    "600941": "中国移动", "600887": "伊利股份",
}


def observe(code, now):
    db = sqlite3.connect("file:data/stock_analysis.db?mode=ro", uri=True)
    bars = db.execute(
        "SELECT date, close FROM stock_daily WHERE code=? AND close>0 "
        "ORDER BY date DESC LIMIT 21", (code,)
    ).fetchall()
    report = db.execute(
        "SELECT operation_advice, raw_result FROM analysis_history "
        "WHERE code=? ORDER BY id DESC LIMIT 1", (code,)
    ).fetchone()
    db.close()
    session_closed = now.hour >= 15 and bars and bars[0][0] == now.date().isoformat()
    if len(bars) < 21 or not session_closed:
        return "待确认", "还没有核实今天的完整收盘数据，等收盘更新。"
    price = bars[0][1]
    avg = sum(row[1] for row in bars[:20]) / 20
    yesterday_avg = sum(row[1] for row in bars[1:21]) / 20
    prefix = f"收盘 {price:.2f} 元，近20天平均 {avg:.2f} 元。"
    if not report:
        return "待确认", prefix + "当天模型报告缺失。"
    raw = json.loads(report[1] or "{}")
    dashboard = raw.get("dashboard") or {}
    core = dashboard.get("core_conclusion") or {}
    advice = (core.get("position_advice") or {}).get("no_position") or ""
    context = " ".join(str(item or "") for item in (
        advice, core.get("one_sentence"), (dashboard.get("phase_decision") or {}).get("immediate_action")
    ))
    wait = any(term in context for term in ("等待","观望","观察","禁止","谨慎","不宜","不建议","暂不","不要","避免","不买","不建仓","不入场"))
    supports_entry = any(term in str(advice) for term in ("买入","建仓","介入","入场"))
    if report[0] in ("卖出", "减仓", "止损") or price < avg:
        return "先观察", prefix + "等股价收回均价并企稳，或等模型风险提示解除。"
    if (price >= avg >= yesterday_avg and supports_entry and not wait
            and report[0] in ("买入","强烈买入","建仓","加仓")
            and str(raw.get("action") or raw.get("decision_type")).lower() in ("buy","strong_buy","add")):
        return "值得复核", prefix + "均价未下滑，模型对首次买入的判断也一致。"
    return "先观察", prefix + "价格和模型意见还没同时确认买点。"


def digest(now):
    rows = []
    for code, name in STOCKS.items():
        path = Path("briefs", code + ".json")
        status, detail = json.loads(path.read_text()) if path.exists() else ("待确认", "本次数据未取齐。")
        rows.append((name, code, status, detail))
    candidates = [name for name, _, status, _ in rows if status == "值得复核"]
    if candidates:
        lead = "值得复核：" + "、".join(candidates)
    elif all(status == "待确认" for _, _, status, _ in rows):
        lead = "数据待确认，先等收盘更新"
    else:
        lead = "暂无明确买点，先观察"
    period = "收盘" if os.environ["GITHUB_EVENT_NAME"] == "schedule" else "测试"
    title = f"自选股提醒｜{now:%m月%d日} {period}｜{lead}"
    rows.sort(key=lambda row: row[2] != "值得复核")
    lines = [f"@{os.environ['GITHUB_REPOSITORY_OWNER']}", f"**{lead}**",
             "以下是首次买入的观察名单。四家银行受相似因素影响，可以放在一起比较。"]
    lines.extend(f"- **{name}（{code}）｜{status}**　{detail}" for name, code, status, detail in rows)
    lines.append("这封简报在交易日收盘后更新。盘中若出现经确认的价格突破，会另发提醒；收到时请先核对券商实时报价。")
    Path("brief-title.txt").write_text(title, encoding="utf-8")
    Path("brief-body.md").write_text("\n\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if sys.argv[1] == "digest":
        digest(now)
    else:
        code = sys.argv[1]
        if code not in STOCKS:
            raise ValueError("Unknown symbol")
        Path(code + ".json").write_text(json.dumps(observe(code, now), ensure_ascii=False), encoding="utf-8")
