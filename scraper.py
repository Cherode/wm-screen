#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""银行理财 R1/R2 短持有期产品抓取器（v1 本地版）
数据源：招商银行 APP 代销理财列表公开 API（mobile.cmbchina.com，含招银理财/工银理财等全理财子公司产品）
口径：风险等级 R1/R2 + 流动性（每日可赎 / 最短持有 <=30 天）
输出：webapp/data.json + webapp/data.js（供网页读取） + history.csv（收益趋势存档）
仅抓取公开披露信息，频率建议每日 1 次。不构成投资建议。
"""
import csv
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import os
BASE = Path(__file__).resolve().parent
WEB = Path(os.environ.get("WM_OUT", str(BASE / "webapp")))
WEB.mkdir(parents=True, exist_ok=True)

CMB_URL = "https://mobile.cmbchina.com/IEntrustFinance/financelist/alllist"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://mobile.cmbchina.com/IEntrustFinance/financeproduct/financelist.html?PrdTyp=B",
}

MAX_HOLD_DAYS = 30
RISK_KEEP = {"R1", "R2"}


def post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_hold_days(ter):
    """从赎回条款解析最短持有天数；每日可赎=0；无法解析=None"""
    if not ter:
        return None
    m = re.search(r"持[有满]\s*(\d+)\s*[天后日]", ter)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*天开放|每\s*(\d+)\s*天开放", ter)
    if m:
        return int(m.group(1) or m.group(2))
    if "每日可赎" in ter or "下个工作日到账" in ter or "实时到账" in ter:
        return 0
    return None


def parse_risk(inf):
    m = re.search(r"(R[1-5])", inf or "")
    return m.group(1) if m else None


def parse_min_buy(inf):
    m = re.search(r"(\d+)\s*元起购", inf or "")
    return m.group(1) if m else None


CURSOR_KEYS = ("yDalCod", "yPagCnt", "yRipCod", "ySaaCod")


def fetch_cmb_paged(endpoint, max_pages=80):
    """主机式游标翻页：把上一响应里的 y* 游标字段回传取下一页"""
    out, seen = [], set()
    body = {"pageNum": 1, "pageSize": 10}
    for _ in range(max_pages):
        data = post_json(CMB_URL.rsplit("/", 1)[0] + "/" + endpoint, body)
        d = (data.get("bizResult") or {}).get("data") or {}
        pl = d.get("prdList") or []
        if not pl:
            break
        new = 0
        for p in pl:
            code = p.get("ripCod") or p.get("fndNbr")
            if code in seen:
                continue
            seen.add(code)
            new += 1
            p["_src"] = endpoint
            out.append(p)
        nxt = {k: d.get(k, "") for k in CURSOR_KEYS}
        if new == 0 or not any(nxt.values()):
            break
        body = {"pageNum": body["pageNum"] + 1, "pageSize": 10}
        body.update(nxt)
    return out


def fetch_cmb():
    products = []
    for ep in ("alllist", "cashlistnew"):
        try:
            products.extend(fetch_cmb_paged(ep))
        except Exception as e:
            sys.stderr.write("warn: %s failed: %s\n" % (ep, e))
    for p in products:
        p["_risk"] = parse_risk(p.get("prdInf"))
    return products


def main():
    now = datetime.now()
    raw_api = fetch_cmb()
    raw = []
    for p in raw_api:
        issuer = (p.get("zylTag") or "").replace("I 代销", "").replace("代销", "").strip()
        raw.append({
            "code": p.get("ripCod") or p.get("fndNbr"),
            "name": p.get("ripSnm", ""),
            "issuer": issuer or "招银理财",
            "channel": "招商银行APP",
            "risk": p.get("_risk") or ("R1" if p.get("_src") == "cashlistnew" else None),
            "yield_pct": p.get("prdRat", ""),
            "yield_type": p.get("ratDes", ""),
            "holding_desc": p.get("terDay", ""),
            "holding_days": parse_hold_days(p.get("terDay")),
            "min_buy": parse_min_buy(p.get("prdInf")),
            "note": p.get("prdInf", ""),
            "tags": [t.get("tagName") for t in (p.get("prdTags") or []) if t.get("tagName")],
        })
    kept = [p for p in raw
            if p["risk"] in RISK_KEEP
            and p["holding_days"] is not None
            and p["holding_days"] <= MAX_HOLD_DAYS]

    def yv(p):
        try:
            return float(p["yield_pct"].replace("%", ""))
        except Exception:
            return -1.0
    kept.sort(key=yv, reverse=True)

    # 与上一次快照对比收益变化
    prev = {}
    if (WEB / "data.json").exists():
        try:
            prev = {q["code"]: q.get("yield_pct") for q in json.loads((WEB / "data.json").read_text(encoding="utf-8")).get("products", [])}
        except Exception:
            prev = {}
    for p in kept:
        p["prev_yield_pct"] = prev.get(p["code"])

    # 历史存档
    hist = BASE / "history.csv"
    write_head = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_head:
            w.writerow(["date", "code", "name", "issuer", "risk", "yield_pct", "yield_type", "holding_days"])
        for p in kept:
            w.writerow([now.strftime("%Y-%m-%d"), p["code"], p["name"], p["issuer"], p["risk"], p["yield_pct"], p["yield_type"], p["holding_days"]])

    # 历史序列（供网页画图）：code -> [{d,name,risk,y,t}]
    hmap = {}
    if hist.exists():
        with hist.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hmap.setdefault(row["code"], []).append(
                    {"d": row["date"], "name": row["name"], "risk": row["risk"],
                     "y": row["yield_pct"], "t": row["yield_type"]})
    history = {k: v[-90:] for k, v in hmap.items()}
    (WEB / "history.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")

    # 市场概览序列：每个快照日 R1/R2 中位数收益
    import statistics
    by_date = {}
    for rows in history.values():
        for r in rows:
            by_date.setdefault(r["d"], []).append(r)
    market = {"dates": [], "r1": [], "r2": []}
    for d in sorted(by_date):
        a1, a2 = [], []
        for r in by_date[d]:
            try:
                y = float(r["y"].replace("%", ""))
            except Exception:
                continue
            if r["risk"] == "R1":
                a1.append(y)
            elif r["risk"] == "R2":
                a2.append(y)
        market["dates"].append(d)
        market["r1"].append(round(statistics.median(a1), 3) if a1 else None)
        market["r2"].append(round(statistics.median(a2), 3) if a2 else None)

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "招商银行APP代销理财列表公开接口（含招银理财/工银理财等理财子公司产品）；工行直销渠道接口待 v2 接入",
        "criteria": "风险等级 R1/R2；流动性：每日可赎或最短持有≤30天；按收益率降序",
        "count_total_raw": len(raw),
        "count_kept": len(kept),
        "products": kept,
        "market": market,
    }
    (WEB / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (WEB / "data.js").write_text("window.WM_DATA = " + json.dumps(payload, ensure_ascii=False) + ";", encoding="utf-8")
    print("OK raw=%d kept=%d top=%s" % (len(raw), len(kept), kept[0]["name"] if kept else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
