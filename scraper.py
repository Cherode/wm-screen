#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""银行理财 R1/R2 短持有期产品抓取器（v2 多源版）
数据源：
  1. 招商银行 APP 代销理财列表公开 API（mobile.cmbchina.com，含招银理财/工银理财等全理财子公司产品）
  2. 工商银行官方理财 API（papi.icbc.com.cn，工银理财全量产品）
口径：风险等级 R1/R2 + 流动性（每日可赎 / 最短持有 <=30 天）
输出：webapp/data.json + webapp/data.js（供网页读取） + history.csv（收益趋势存档）
仅抓取公开披露信息，频率建议每日 1 次。不构成投资建议。
"""
import csv
import json
import re
import ssl
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

# 理财子公司 → 银行全称映射
ISSUER_MAP = {
    "工银理财": "工商银行",
    "招银理财": "招商银行",
    "中银理财": "中国银行",
    "建信理财": "建设银行",
    "农银理财": "农业银行",
    "交银理财": "交通银行",
    "中邮理财": "邮储银行",
    "信银理财": "中信银行",
    "光大理财": "光大银行",
    "兴银理财": "兴业银行",
    "华夏理财": "华夏银行",
    "平安理财": "平安银行",
    "民生理财": "民生银行",
    "浦银理财": "浦发银行",
    "杭银理财": "杭州银行",
    "宁银理财": "宁波银行",
    "上银理财": "上海银行",
    "北银理财": "北京银行",
    "苏银理财": "江苏银行",
    "高盛工银理财": "工商银行",
}


def extract_issuer_from_name(name):
    """从产品名称中提取理财子公司→银行全称"""
    if not name:
        return None
    # 优先匹配较长的名称（如"高盛工银理财"在"工银理财"之前）
    for wm in sorted(ISSUER_MAP.keys(), key=len, reverse=True):
        if wm in name:
            return ISSUER_MAP[wm]
    return None

# 置顶银行（按展示顺序）
PRIORITY_BANKS = ["工商银行", "招商银行"]


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
BCD_URL = "https://mobile.cmbchina.com/IEntrustFinance/financelist/bcdlist"
PRDINFO_URL = "https://mobile.cmbchina.com/IEntrustFinance/sa-finance-detail/prd-info"


def fetch_upcoming():
    """bcdlist 中带「X月X日开售」的首发/募集产品"""
    import re as _re
    from datetime import date as _date
    out, seen = [], set()
    body = {"pageNum": 1, "pageSize": 10}
    for _ in range(60):
        try:
            data = post_json(BCD_URL, body)
        except Exception:
            break
        d = (data.get("bizResult") or {}).get("data") or {}
        pl = d.get("prdList") or []
        if not pl:
            break
        for p in pl:
            st = (p.get("salTim") or "").strip()
            code = p.get("ripCod") or p.get("fndNbr")
            if st and code not in seen:
                seen.add(code)
                m = _re.search(r"(\d{1,2})月(\d{1,2})日", st)
                sd = None
                if m:
                    mm, dd = int(m.group(1)), int(m.group(2))
                    today = _date.today()
                    yy = today.year if (mm, dd) >= (today.month, today.day) else today.year + 1
                    sd = "%04d-%02d-%02d" % (yy, mm, dd)
                issuer = (p.get("zylTag") or "").replace("I 代销", "").replace("代销", "").strip()
                out.append({
                    "code": code, "name": p.get("ripSnm", ""), "issuer": issuer or "招银理财",
                    "risk": parse_risk(p.get("prdInf")), "sale_text": st, "sale_date": sd,
                    "holding_desc": p.get("terDay", ""), "min_buy": parse_min_buy(p.get("prdInf")),
                    "yield_pct": p.get("prdRat", ""), "yield_type": p.get("ratDes", ""),
                })
        cur = {k: d.get(k, "") for k in CURSOR_KEYS}
        if not any(cur.values()):
            break
        body = {"pageNum": body["pageNum"] + 1, "pageSize": 10}
        body.update(cur)
    return out


def fetch_trade_rules(code, saa):
    """prd-info：购入/确认/赎回到账 T+X 与费用规则"""
    import re as _re
    try:
        data = post_json(PRDINFO_URL, {"prdCode": code, "saCode": saa or "D07"})
        d = (data.get("bizResult") or {}).get("data") or {}
    except Exception:
        return {}
    pay = d.get("payTimeRule") or ""
    m = _re.search(r"T\s*\+\s*(\d+)", pay)
    redeem_tx = int(m.group(1)) if m else None
    tips = ((d.get("timeLineData") or {}).get("confirmDayTips")) or ""
    return {
        "buy_rule": d.get("buyTimeRule") or "",
        "confirm_rule": d.get("confirmTimeRule") or tips,
        "redeem_rule": pay,
        "redeem_tx": redeem_tx,
        "fees": " / ".join(x for x in [d.get("buyFeeRule"), d.get("redeemFeeRule")] if x),
    }


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


HIST_URL = "https://mobile.cmbchina.com/ientrustfinance/product-statistics/get-history-value"


def fetch_history(code, saa):
    """招行渠道官方净值历史：日频，含七日年化/万份收益/单位净值"""
    import time
    body = {"saaCode": saa or "D07", "ripInn": code, "yDalCod": "N",
            "ySaaCod": "", "yFndInn": "", "yNavDat": "0"}
    try:
        data = post_json(HIST_URL, body)
        rows = ((data.get("bizResult") or {}).get("data") or {}).get("historyValueLists") or []
    except Exception:
        return []
    out = []
    for r in rows[:90]:  # 接口倒序，取最近90天
        def f(k):
            try:
                return float(r.get(k))
            except Exception:
                return None
        out.append({"d": r.get("date"), "nav": f("unitNetValue"), "w": f("tenThousandProfit"), "s7": f("sevenDaysAnnualProfit")})
    out.reverse()
    time.sleep(0.12)
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


# ──────────────────── 工商银行官方 API ────────────────────
ICBC_LIST_URL = "https://papi.icbc.com.cn/finance/financeWap/searchList"
ICBC_DETAIL_URL = "https://papi.icbc.com.cn/icbc/iepa/portalinfo/recommend/finance/queryFinanceDetailInfoN"
ICBC_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://m.icbc.com.cn/mpage/finance/list",
}


def _icbc_ssl_ctx():
    """ICBC 服务器使用旧版 SSL 握手，需要开启 legacy renegotiate"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
    return ctx


def icbc_post(url, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=ICBC_HEADERS, method="POST")
    ctx = _icbc_ssl_ctx()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_icbc_product_list(max_pages=900):
    """翻页获取工行理财全量产品列表（移动端 searchList 接口）"""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        body = {"pageIndex": page, "pageSize": 20}
        try:
            data = icbc_post(ICBC_LIST_URL, body)
        except Exception as e:
            sys.stderr.write("warn: icbc list page %d failed: %s\n" % (page, e))
            break
        resp = data.get("data") or {}
        items = resp.get("list") or []
        if not items:
            break
        new = 0
        for item in items:
            pid = item.get("productId") or ""
            if not pid or pid in seen:
                continue
            seen.add(pid)
            new += 1
            out.append(item)
        # 到达最后一页
        total = resp.get("total") or 0
        pages_total = resp.get("pages") or 0
        if page >= pages_total:
            break
        if new == 0:
            break
    return out


def parse_icbc_term_days(product_term):
    """解析 productTerm（如 '345天'、'193天'、'每日开放'）为持有天数"""
    if not product_term:
        return None
    s = str(product_term)
    if "每日" in s or "天天" in s or "活期" in s:
        return 0
    m = re.search(r"(\d+)\s*天", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*个月", s)
    if m:
        return int(m.group(1)) * 30
    m = re.search(r"(\d+)\s*年", s)
    if m:
        return int(m.group(1)) * 365
    return None


def parse_icbc_yield(yield_value, yield_type_raw):
    """解析 yieldValue（如 '2.1500%-2.3500%'）→ (展示收益率, 类型描述)"""
    if not yield_value:
        return "", yield_type_raw or ""
    s = str(yield_value).strip()
    # 区间收益率：取上限
    m = re.search(r"([\d.]+)%\s*[-~]\s*([\d.]+)%", s)
    if m:
        return m.group(2) + "%", yield_type_raw or "预期收益率"
    # 单一百分比
    m = re.search(r"([\d.]+)%", s)
    if m:
        return m.group(1) + "%", yield_type_raw or "预期收益率"
    return s, yield_type_raw or ""


def parse_icbc_min_buy(issu_min):
    """解析 issuMin（如 '1人民币起购'）→ 起购金额字符串"""
    if not issu_min:
        return None
    m = re.search(r"(\d+)", str(issu_min))
    return m.group(1) if m else None


def fetch_icbc_detail(prod_id):
    """获取单个工行产品详情（含收益率），用于补充列表缺失字段"""
    body = {"keyword_objID": prod_id}
    try:
        data = icbc_post(ICBC_DETAIL_URL, body)
        return data.get("data") or {}
    except Exception as e:
        sys.stderr.write("warn: icbc detail %s failed: %s\n" % (prod_id, e))
        return {}


def fetch_icbc():
    """抓取工银理财全量产品，返回标准化列表
    注意：ICBC searchList 接口不返回风险等级，需从产品名推断或标记为未知。
    """
    import time
    sys.stderr.write("fetching ICBC product list...\n")
    items = fetch_icbc_product_list()
    sys.stderr.write("ICBC list: %d products\n" % len(items))

    products = []
    for i, item in enumerate(items):
        pid = item.get("productId") or ""
        name = item.get("productName") or ""
        term = item.get("productTerm") or ""
        hold_days = parse_icbc_term_days(term)
        yield_pct, yield_type = parse_icbc_yield(
            item.get("yieldValue"), item.get("yieldType"))
        min_buy = parse_icbc_min_buy(item.get("issuMin"))

        # 风险等级：列表接口不返回，尝试从名称推断
        risk = parse_risk(name)
        # 如果名称里没有风险标识，标记为 None（后续筛选时特殊处理）

        products.append({
            "code": pid,
            "name": name,
            "issuer": extract_issuer_from_name(name) or "工商银行",
            "channel": "工商银行官方",
            "risk": risk,
            "yield_pct": yield_pct,
            "yield_type": yield_type,
            "holding_desc": term,
            "saa": "",
            "holding_days": hold_days,
            "min_buy": min_buy,
            "note": "工行官方数据（风险等级未披露）" if not risk else "",
            "tags": [],
            "_src": "icbc",
        })

        if (i + 1) % 200 == 0:
            sys.stderr.write("ICBC parsed %d/%d\n" % (i + 1, len(items)))

    return products


def main():
    now = datetime.now()

    # ── 1. 招行渠道 ──
    raw_api = fetch_cmb()
    raw = []
    for p in raw_api:
        raw_issuer = (p.get("zylTag") or "").replace("I 代销", "").replace("代销", "").strip() or "招银理财"
        issuer = ISSUER_MAP.get(raw_issuer, raw_issuer)
        raw.append({
            "code": p.get("ripCod") or p.get("fndNbr"),
            "name": p.get("ripSnm", ""),
            "issuer": issuer,
            "channel": "招商银行APP",
            "risk": p.get("_risk") or ("R1" if p.get("_src") == "cashlistnew" else None),
            "yield_pct": p.get("prdRat", ""),
            "yield_type": p.get("ratDes", ""),
            "holding_desc": p.get("terDay", ""),
            "saa": p.get("saaCod", ""),
            "holding_days": parse_hold_days(p.get("terDay")),
            "min_buy": parse_min_buy(p.get("prdInf")),
            "note": p.get("prdInf", ""),
            "tags": [t.get("tagName") for t in (p.get("prdTags") or []) if t.get("tagName")],
        })
    sys.stderr.write("CMB raw: %d\n" % len(raw))

    # ── 2. 工行官方渠道 ──
    icbc_raw = []
    if os.environ.get("WM_NOICBC") != "1":
        try:
            icbc_raw = fetch_icbc()
            sys.stderr.write("ICBC raw: %d\n" % len(icbc_raw))
        except Exception as e:
            sys.stderr.write("warn: ICBC fetch failed: %s\n" % e)
    else:
        sys.stderr.write("skip ICBC (WM_NOICBC=1)\n")

    # ── 3. 合并去重（工行官方优先覆盖招行渠道的工行产品） ──
    merged_map = {}
    for p in raw:
        key = p["code"]
        merged_map[key] = p
    for p in icbc_raw:
        key = p["code"]
        prev = merged_map.get(key)
        # 工行官方数据覆盖，但风险等级/销售代码从招行渠道回填（工行接口不披露风险）
        if prev and not p.get("risk"):
            p["risk"] = prev.get("risk")
            p["risk_src"] = "招商银行渠道披露"
            p["saa"] = prev.get("saa", "")
        merged_map[key] = p
    all_products = list(merged_map.values())
    sys.stderr.write("merged total: %d (CMB %d + ICBC %d, dedup by code)\n" % (
        len(all_products), len(raw), len(icbc_raw)))

    # ── 4. 筛选 R1/R2 + ≤30天 ──
    # ICBC 官方接口不返回风险等级，只要持有期满足就纳入（note 里标注了风险未披露）
    kept = [p for p in all_products
            if (p["risk"] in RISK_KEEP or p.get("_src") == "icbc")
            and p["holding_days"] is not None
            and p["holding_days"] <= MAX_HOLD_DAYS]
    sys.stderr.write("after filter (R1/R2 or ICBC) ≤30d: %d\n" % len(kept))

    def yv(p):
        try:
            return float(p["yield_pct"].replace("%", ""))
        except Exception:
            return -1.0
    # 置顶银行优先（按 PRIORITY_BANKS 顺序），其余按收益率降序
    priority = [p for p in kept if p["issuer"] in PRIORITY_BANKS]
    priority.sort(key=lambda p: (PRIORITY_BANKS.index(p["issuer"]), -yv(p)))
    others = [p for p in kept if p["issuer"] not in PRIORITY_BANKS]
    others.sort(key=yv, reverse=True)
    kept = priority + others

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

    upcoming = fetch_upcoming()
    for u in upcoming:
        u.update(fetch_trade_rules(u["code"], u.get("saa") or "D07"))

    # 交易规则（购入/确认/赎回 T+X）
    if os.environ.get("WM_NODETAIL") != "1":
        for i, p in enumerate(kept):
            p.update(fetch_trade_rules(p["code"], p.get("saa")))
            if (i + 1) % 80 == 0:
                sys.stderr.write("rules %d/%d\n" % (i + 1, len(kept)))
            import time as _t
            _t.sleep(0.1)

    # 官方净值历史（逐产品，日频）
    hist_api = {}
    if os.environ.get("WM_NOHIST") == "1" and (WEB / "history.json").exists():
        hist_api = json.loads((WEB / "history.json").read_text(encoding="utf-8"))
    elif os.environ.get("WM_NOHIST") != "1":
        for i, p in enumerate(kept):
            if p.get("_src") == "icbc":
                continue  # ICBC 产品不走招行历史接口
            rows = fetch_history(p["code"], p.get("saa"))
            if rows:
                hist_api[p["code"]] = rows
            if (i + 1) % 60 == 0:
                sys.stderr.write("history %d/%d\n" % (i + 1, len(kept)))
    (WEB / "history.json").write_text(json.dumps(hist_api, ensure_ascii=False), encoding="utf-8")

    # 快照存档序列（兜底）：code -> [{d,name,risk,y,t}]
    hmap = {}
    if hist.exists():
        with hist.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hmap.setdefault(row["code"], []).append(
                    {"d": row["date"], "name": row["name"], "risk": row["risk"],
                     "y": row["yield_pct"], "t": row["yield_type"]})

    # 市场概览序列：基于官方历史，每个日期 R1/R2 中位数七日年化（无则净值日涨跌年化）
    import statistics
    by_date = {}
    risk_of_code = {p["code"]: p["risk"] for p in kept}
    for code, rows in hist_api.items():
        risk = risk_of_code.get(code)
        navs = [r.get("nav") for r in rows]
        for i, r in enumerate(rows):
            v = r.get("s7") or None
            if not v:
                # 净值型：按 30/14/7 天自适应窗口的净值涨幅年化
                for win in (30, 14, 7):
                    if i >= win and navs[i] and navs[i - win]:
                        v = round((navs[i] / navs[i - win] - 1.0) * 365.0 / win * 100.0, 3)
                        break
            if v:
                by_date.setdefault(r["d"], {}).setdefault(risk, []).append(v)
    market = {"dates": [], "r1": [], "r2": []}
    for d in sorted(by_date)[-90:]:
        market["dates"].append(d)
        market["r1"].append(round(statistics.median(by_date[d]["R1"]), 3) if by_date[d].get("R1") else None)
        market["r2"].append(round(statistics.median(by_date[d]["R2"]), 3) if by_date[d].get("R2") else None)

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "招商银行APP代销理财公开接口 + 工商银行官方理财API",
        "source_url": "https://www.icbc.com.cn/column/1438058341489590354.html",
        "disclaimer": "数据来自银行公开披露的代销理财产品信息（招行渠道+工行官方），收益率等数据由各家理财子公司官方披露，本工具仅做聚合展示，不构成投资建议。理财非存款，产品有风险，投资需谨慎。",
        "criteria": "风险等级 R1/R2；流动性：每日可赎或最短持有≤30天；工商银行、招商银行置顶，其余按收益率降序",
        "count_total_raw": len(all_products),
        "count_cmb": len(raw),
        "count_icbc": len(icbc_raw),
        "count_kept": len(kept),
        "products": kept,
        "market": market,
        "upcoming": upcoming,
    }
    (WEB / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (WEB / "data.js").write_text("window.WM_DATA = " + json.dumps(payload, ensure_ascii=False) + ";", encoding="utf-8")
    print("OK raw=%d (CMB=%d ICBC=%d) kept=%d top=%s" % (
        len(all_products), len(raw), len(icbc_raw), len(kept),
        kept[0]["name"] if kept else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
