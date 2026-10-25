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


def parse_confirm_tx(note, confirm_rule):
    """购入确认 T+X：优先文案口径，其次从确认规则的两个日期算工作日差"""
    note = note or ""
    if "下个工作日享收益" in note or "下一个工作日享收益" in note or "次工作日确认" in note:
        return 1
    if "购买当日享收益" in note or "当日确认" in note or "实时确认" in note:
        return 0
    dates = re.findall(r"(\d{1,2})月(\d{1,2})日", confirm_rule or "")
    if len(dates) >= 2:
        from datetime import date as _dd, timedelta as _td
        y = _dd.today().year
        try:
            d1 = _dd(y, int(dates[-2][0]), int(dates[-2][1]))
            d2 = _dd(y, int(dates[-1][0]), int(dates[-1][1]))
            if d2 < d1:
                d1 = _dd(y + 1, int(dates[-2][0]), int(dates[-2][1]))
            x, cur = 0, d1
            while cur < d2:
                cur += _td(days=1)
                if cur.weekday() < 5:
                    x += 1
            return x if 0 <= x <= 15 else None
        except Exception:
            return None
    return None


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


RILE_MAP = (("00001", "R1"), ("00002", "R2"))


BOC_PAGES = ["https://www.bankofchina.com/sourcedb/srfd6_2024/index.html",
             "https://www.bankofchina.com/sourcedb/srfd6_2024/index_1.html"]


def _norm_name(n):
    import re as _r
    n = _r.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", n or "")
    for pre in ("机构专属", "中银理财", "中银"):
        if n.startswith(pre):
            n = n[len(pre):]
    return n


def fetch_boc(max_pages=80):
    """中行代销净值表：只扫最新截止日期的页，返回 规范名 -> 行信息"""
    import re as _r
    import urllib.request
    out = {}
    latest = None
    url = BOC_PAGES[0]
    for pg in range(max_pages):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        except Exception as e:
            sys.stderr.write("warn: boc page %s failed: %s\n" % (url, e))
            break
        rows = _r.findall(r"<tr[^>]*>(.*?)</tr>", html, _r.S)
        parsed = []
        for r in rows:
            cells = [_r.sub(r"<[^>]+>", "", c).strip() for c in _r.findall(r"<td[^>]*>(.*?)</td>", r, _r.S)]
            if len(cells) < 8 or not cells[0] or not _r.match(r"\d{4}/\d{2}/\d{2}", cells[7] or ""):
                continue
            parsed.append((cells, cells[7][:10]))
        if not parsed:
            break
        page_max = max(d for _, d in parsed)
        if latest is None:
            latest = page_max
        # 只扫最近10天披露窗口：每产品首次出现即其最新净值
        if page_max < latest and (int(latest[8:10]) - int(page_max[8:10]) > 10 or page_max < latest[:8] + "01"):
            break
        got = 0
        for cells, date in parsed:
            if _norm_name(cells[1]) in out:
                continue
            def f(x):
                try:
                    return float(x)
                except Exception:
                    return None
            out[_norm_name(cells[1])] = {
                "boc_code": cells[0], "boc_name": cells[1], "boc_nav": f(cells[2]),
                "boc_wan": f(cells[4]), "boc_7d": f(cells[5]), "boc_growth": f(cells[6]),
                "boc_date": date,
            }
            got += 1
        if got == 0:
            break
        url = "https://www.bankofchina.com/sourcedb/srfd6_2024/index_%d.html" % (pg + 1)
    return out


def fetch_icbc():
    """工行官方移动端接口：按风险等级(rile)拉取 R1/R2 全量，含募集档期"""
    import time
    from datetime import date as _date
    products, upcoming = [], []
    today = _date.today()
    for rile, risk in RILE_MAP:
        sys.stderr.write("fetching ICBC rile=%s\n" % rile)
        for page in range(1, 400):
            try:
                data = icbc_post(ICBC_LIST_URL, {"searchInfo": "", "curr": "", "term": "",
                                                 "type": "", "rile": rile, "utty": "",
                                                 "pageIndex": page, "pageSize": 20})
            except Exception as e:
                sys.stderr.write("warn: icbc page %d failed: %s\n" % (page, e))
                break
            resp = data.get("data") or {}
            items = resp.get("list") or []
            if not items:
                break
            for item in items:
                pid = item.get("productId") or ""
                if not pid:
                    continue
                term = item.get("productTerm") or ""
                hold = parse_icbc_term_days(term)
                yld, ytype = parse_icbc_yield(item.get("yieldValue"), item.get("yieldType"))
                op = item.get("offerPeriod") or ""
                is_off = item.get("isOfferPeriod")
                rec = {
                    "code": pid,
                    "name": item.get("productName") or "",
                    "issuer": extract_issuer_from_name(item.get("productName") or "") or "工银理财",
                    "channel": "工商银行官方",
                    "risk": risk,
                    "yield_pct": yld,
                    "yield_type": ytype,
                    "holding_desc": term,
                    "saa": "",
                    "holding_days": hold,
                    "min_buy": parse_icbc_min_buy(item.get("issuMin")),
                    "note": "工行官方数据",
                    "tags": [],
                    "_src": "icbc",
                    "offer_period": op,
                }
                if is_off and op and "-" in op:
                    s0, e0 = op.split("-", 1)
                    try:
                        d0 = _date(int(s0[:4]), int(s0[4:6]), int(s0[6:8]))
                        d1 = _date(int(e0[:4]), int(e0[4:6]), int(e0[6:8]))
                    except Exception:
                        d0 = d1 = None
                    if d1 and d1 >= today:
                        upcoming.append({
                            "code": pid, "name": rec["name"], "issuer": rec["issuer"],
                            "risk": risk,
                            "sale_text": "募集期 %s~%s" % (s0, d1.strftime("%m-%d")) if d0 and d0 > today else "募集中 至%s" % e0[4:],
                            "sale_date": d0.isoformat() if d0 and d0 > today else _date.today().isoformat(),
                            "holding_desc": term, "min_buy": rec["min_buy"],
                            "yield_pct": yld, "yield_type": ytype,
                        })
                        continue  # 募集中的不算在售入选
                products.append(rec)
            if page >= (resp.get("pages") or page):
                break
            time.sleep(0.1)
    # 募集期/首发产品只在无筛选列表头部：扫前若干页直至连续2页无募集项
    RILE2RISK = {"00001": "R1", "00002": "R2", "00003": "R3", "00004": "R4", "00005": "R5"}
    dry = 0
    for page in range(1, 15):
        try:
            data = icbc_post(ICBC_LIST_URL, {"searchInfo": "", "curr": "", "term": "",
                                             "type": "", "rile": "", "utty": "",
                                             "pageIndex": page, "pageSize": 20})
        except Exception:
            break
        items = (data.get("data") or {}).get("list") or []
        if not items:
            break
        got = 0
        for item in items:
            if not item.get("isOfferPeriod"):
                continue
            got += 1
            pid = item.get("productId") or ""
            op = item.get("offerPeriod") or ""
            if not op or "-" not in op:
                continue
            s0, e0 = op.split("-", 1)
            try:
                d0 = _date(int(s0[:4]), int(s0[4:6]), int(s0[6:8]))
                d1 = _date(int(e0[:4]), int(e0[4:6]), int(e0[6:8]))
            except Exception:
                continue
            if d1 < today:
                continue
            risk = None
            try:
                det = icbc_post(ICBC_DETAIL_URL, {"keyword_objID": pid}).get("data") or {}
                if isinstance(det, list):
                    det = det[0] if det else {}
                risk = RILE2RISK.get(str(det.get("lowestBuyLevel") or ""))
            except Exception:
                pass
            yld, ytype = parse_icbc_yield(item.get("yieldValue"), item.get("yieldType"))
            upcoming.append({
                "code": pid, "name": item.get("productName") or "",
                "issuer": extract_issuer_from_name(item.get("productName") or "") or "工银理财",
                "risk": risk,
                "kind": "offering" if d0 <= today else "upcoming",
                "end_date": d1.isoformat(),
                "sale_text": "募集期 %s~%s" % (d0.strftime("%m-%d"), d1.strftime("%m-%d")),
                "sale_date": d0.isoformat(),
                "holding_desc": item.get("productTerm") or "",
                "min_buy": parse_icbc_min_buy(item.get("issuMin")),
                "yield_pct": yld, "yield_type": ytype,
            })
        if got == 0:
            dry += 1
            if dry >= 2:
                break
        else:
            dry = 0
        time.sleep(0.1)
    return products, upcoming


def fetch_icbc_detail_extra(pid):
    """工行详情：区间收益 + 周频净值历史"""
    d = icbc_post(ICBC_DETAIL_URL, {"keyword_objID": pid}).get("data") or {}
    if isinstance(d, list):
        d = d[0] if d else {}
    wins = {}
    for y in (d.get("historicalYieldList") or []):
        wins[y.get("timeInterval") or ""] = y.get("yeildValue") or ""
    rows = []
    for v in (d.get("historicalValueList") or []):
        ds = (v.get("date") or "").replace(".", "-")
        try:
            nav = float(v.get("value"))
        except Exception:
            nav = None
        if ds and nav:
            rows.append({"d": ds, "nav": nav, "w": None, "s7": None})
    rows.sort(key=lambda r: r["d"])
    return wins, rows[-90:]


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
    icbc_raw, icbc_upcoming = [], []
    if os.environ.get("WM_NOICBC") != "1":
        try:
            icbc_raw, icbc_upcoming = fetch_icbc()
            sys.stderr.write("ICBC raw: %d, upcoming: %d\n" % (len(icbc_raw), len(icbc_upcoming)))
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
            if p["risk"] in RISK_KEEP
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

    # ── 中行代销净值表：交叉校验 + 净值截止日 ──
    boc = {}
    try:
        boc = fetch_boc()
        sys.stderr.write("BOC latest-date rows: %d\n" % len(boc))
    except Exception as e:
        sys.stderr.write("warn: BOC fetch failed: %s\n" % e)
    boc_hit = 0
    for p in kept:
        info = boc.get(_norm_name(p["name"]))
        if info:
            p.update(info)
            boc_hit += 1
    sys.stderr.write("BOC matched: %d/%d\n" % (boc_hit, len(kept)))

    for p in kept:
        if p.get("confirm_tx") is None:
            p["confirm_tx"] = parse_confirm_tx(p.get("note"), p.get("confirm_rule"))

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
        if not u.get("kind"):
            u["kind"] = "offering" if (u.get("sale_date") or "9999") <= now.strftime("%Y-%m-%d") else "upcoming"
    upcoming.extend(icbc_upcoming)
    upcoming.sort(key=lambda u: (0 if u.get("kind") == "upcoming" else 1, u.get("sale_date") or "9999"))

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
                try:
                    wins, rows = fetch_icbc_detail_extra(p["code"])
                    p["win_yields"] = wins
                    if rows:
                        hist_api[p["code"]] = rows
                except Exception as e:
                    sys.stderr.write("warn: icbc detail %s: %s\n" % (p["code"], e))
            else:
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
