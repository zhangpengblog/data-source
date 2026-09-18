#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人投资理财看板 - 数据抓取与计算脚本
读取 data/ 下的底表 CSV，抓取公开金融数据，产出 metrics.csv / positions_detail.json 等

数据源（全部免费公开，无需鉴权）：
  - 场外基金：天天基金 pingzhongdata（全历史净值、分红、经理、规模、阶段收益）
  - 场内股票/ETF：腾讯行情 + 新浪行情（实时价）+ 腾讯 K 线（历史序列）
  - 场内分红：天天基金分红送配页（基金）/ 腾讯行情股息率（股票）
"""
import csv
import io
import json
import math
import os
import re
import ssl
import statistics
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Referer": "https://finance.sina.com.cn"}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

WARNINGS = []   # 空数据红线：如实记录，不编造


def http_get(url, timeout=20, encoding=None, retries=2):
    """取文本。网络类故障（超时 / SSL 握手失败 / 连接重置）会重试，短退避。

    为什么必须重试：实测过 024564 的 pingzhongdata 出现
    `_ssl.c:1015: The handshake operation timed out`，当场连测 4 次全部成功 ——
    是瞬时故障。没有重试的话，一次抖动就会让整只标的的数据缺失，
    而且要等下一个 TTL 才可能恢复（实测 TTL 3 小时）。

    ⚠️ 4xx/5xx（如 403 / 501）**不重试**：那是服务端明确拒绝，重试没有意义，
    还会加重风控。
    """
    req = urllib.request.Request(url, headers=UA)
    raw = None
    last_err = None
    for attempt in range(retries + 1):
        try:
            raw = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
            break
        except urllib.error.HTTPError as e:
            last_err = e          # 服务端明确拒绝，重试无意义
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))    # 0.8s / 1.6s
                continue
    if raw is None:
        WARNINGS.append("请求失败 %s: %s" % (url[:90], last_err))
        return None
    if encoding:
        return raw.decode(encoding, "ignore")
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def load_csv(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


# ------------------------------------------------------------ 底表远端来源（可选）
# 配置后，每次跑 main() 之前会先尝试从远端拉最新底表；**留空则完全维持本地文件行为**，
# 本地调试不会被线上那份覆盖。
#   KANBAN_BASE_URL       主来源，形如  https://gitee.com/<owner>/<repo>/raw/<branch>/<目录>
#   KANBAN_BASE_FALLBACK  兜底来源，形如 https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/<目录>
# 顺序：主来源 → 兜底来源 → 保留本地副本。
BASE_SRC = os.environ.get("KANBAN_BASE_URL", "").strip().rstrip("/")
BASE_SRC_FALLBACK = os.environ.get("KANBAN_BASE_FALLBACK", "").strip().rstrip("/")

# 每张底表至少必须含有的列名 —— 挡住坏上传（HTML 错误页 / 空文件 / 传错表）
BASE_TABLE_REQUIRED = {
    "holdings.csv": ["type", "code", "shares", "cost_price"],
    "dca_plan.csv": ["code", "frequency", "amount", "start_date"],
    "dca_actual.csv": ["code", "deal_date"],
    "dividend_records.csv": ["code", "ex_date"],
    "dividend_policy.csv": ["code", "dividend_mode"],
}


def _safe_url(u):
    """对 URL 路径里的非 ASCII 字符做百分号编码；已有的 %xx 与合法 pchar 不再编码。

    ⚠️ urllib 不会自动处理非 ASCII 路径 —— 直接传会抛
    UnicodeEncodeError: 'ascii' codec can't encode characters in position ...
    本项目的 Gitee 底表目录名含中文（理财看板/data），所以这一步是必需的。

    注意 safe 集合里必须保留 `@`：服务端分页/版本类 URL 常用 `repo@version` 形式，
    quote() 默认会把 `@` 编成 %40，把 URL 改坏。
    """
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((
        p.scheme, p.netloc,
        urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=~"),
        urllib.parse.quote(p.query, safe="=&%:@!$'()*+,;~"),
        p.fragment))


def _http_get_bytes(url, timeout=20):
    """取原始字节。底表要比对与落盘，不能用会解码的 http_get（会丢 BOM）。"""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.read()
    except Exception:
        return None


def _valid_base_table(name, raw):
    """远端内容能否安全替代本地底表。返回 (ok, 说明)。"""
    if not raw:
        return False, "空响应"
    if raw.lstrip()[:1] == b"<":
        return False, "不是 CSV（像是 HTML 错误页）"
    try:
        txt = raw.decode("utf-8-sig")
    except Exception as e:
        return False, "非 UTF-8 文本: %s" % e
    try:
        rows = list(csv.reader(io.StringIO(txt)))
    except Exception as e:
        return False, "CSV 解析失败: %s" % e
    if len(rows) < 2:
        return False, "只有表头或空文件（共 %d 行）" % len(rows)
    header = [c.strip() for c in rows[0]]
    missing = [c for c in BASE_TABLE_REQUIRED.get(name, []) if c not in header]
    if missing:
        return False, "缺少必需列 %s（实际表头: %s）" % (missing, header)
    return True, "%d 行 × %d 列" % (len(rows) - 1, len(header))


def pull_base_tables():
    """按需从远端拉底表覆盖本地副本，返回摘要（便于打印与排错）。

    **先校验再落盘**：任何一项不过就保留本地旧文件并记 WARNING，
    不让一次坏上传把好底表覆盖成半残。
    """
    if not BASE_SRC:
        return {"enabled": False, "updated": [], "unchanged": [], "failed": []}
    bases = [(BASE_SRC, "主")]
    if BASE_SRC_FALLBACK:
        bases.append((BASE_SRC_FALLBACK, "兜底"))
    updated, unchanged, failed = [], [], []
    for name in BASE_TABLE_REQUIRED:
        path = os.path.join(DATA, name)
        local = open(path, "rb").read() if os.path.exists(path) else b""
        done = False
        for base, tag in bases:
            raw = _http_get_bytes(_safe_url("%s/%s" % (base, name)))
            if raw is None:
                continue
            ok, why = _valid_base_table(name, raw)
            if not ok:
                failed.append("%s(%s校验不过: %s)" % (name, tag, why))
                continue
            if raw == local:
                unchanged.append(name)
                done = True
                break
            with open(path, "wb") as f:
                f.write(raw)
            updated.append("%s(%s, %s)" % (name, tag, why))
            done = True
            break
        if not done:
            failed.append("%s(两源均失败，保留本地)" % name)
            WARNINGS.append("底表 %s 远端拉取失败，继续用本地副本" % name)
    return {"enabled": True, "updated": updated, "unchanged": unchanged, "failed": failed}


def to_float(v, default=None):
    try:
        s = str(v).strip().replace(",", "").replace("%", "")
        if s in ("", "-", "--", "None", "null"):
            return default
        return float(s)
    except Exception:
        return default


# ---------------------------------------------------------------- 场外基金
def _ms_to_nav_date(ms):
    """天天基金净值时间戳 -> 净值所属日期

    【重要】天天基金 pingzhongdata 的 Data_netWorthTrend 时间戳为「UTC 零点」，
    即北京时间当天 08:00，但该净值实际是【前一交易日收盘后公布、归属当日】的净值。
    实测：最后一个点 utcfromtimestamp -> 2026-09-10，而基金页显示的
    「最新净值日期」为 2026-09-11（周五），净值 1.0879。

    因此这里统一 +1 天，把净值挂到正确的日期上。
    若不做这个校正，所有净值日期会系统性提前 1 天，进而导致：
      - 定投扣款日命中的净值错位（本该取当日净值却取到前一日）
      - 除息日/确认日与净值序列对不上
    """
    try:
        return (datetime.utcfromtimestamp(ms / 1000) + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def norm_fund_code(code):
    """把场外基金代码补足 6 位。

    底表里可能写成 20603（Excel 把前导 0 吃掉了），
    而天天基金只认 020603。不补零会拿到 301 重定向，
    urllib 不跟随重定向 → 净值抓取全部失败。
    """
    c = (code or "").strip()
    if not c.isdigit():
        return c
    return c.zfill(6) if len(c) < 6 else c


def fetch_fund(code, display_code=None):
    """天天基金：净值/分红/经理/规模/阶段收益

    code        用于请求的规范化代码（6 位）
    display_code 用于回填输出的原始代码（保持底表口径）
    """
    out = {"code": display_code or code, "source": "天天基金"}
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js" % code
    txt = http_get(url)
    if not txt or "fS_name" not in txt:
        WARNINGS.append("基金 %s 净值数据获取失败" % code)
        return out

    def var_str(key):
        m = re.search(r'var\s+%s\s*=\s*"?([^";]*)"?' % re.escape(key), txt)
        return m.group(1).strip() if m else None

    def var_json(key):
        """按括号配平提取 var X = <json>; 兼容数组与嵌套对象"""
        m = re.search(r'var\s+%s\s*=\s*' % re.escape(key), txt)
        if not m:
            return None
        i = m.end()
        while i < len(txt) and txt[i] in " \t\r\n":
            i += 1
        if i >= len(txt) or txt[i] not in "[{":
            return None
        open_ch = txt[i]
        close_ch = "]" if open_ch == "[" else "}"
        depth, in_str, esc = 0, False, False
        for j in range(i, len(txt)):
            ch = txt[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(txt[i:j + 1])
                    except Exception:
                        return None
        return None

    out["name"] = var_str("fS_name")
    out["syl_1y"] = to_float(var_str("syl_1y"))    # 近1月
    out["syl_3y"] = to_float(var_str("syl_3y"))    # 近3月
    out["syl_6y"] = to_float(var_str("syl_6y"))    # 近6月
    out["syl_1n"] = to_float(var_str("syl_1n"))    # 近1年
    out["fund_rate"] = var_str("fund_Rate")
    out["min_buy"] = var_str("fund_minsg")

    # 历史净值（复权=累计净值口径）
    trend = var_json("Data_netWorthTrend") or []
    ac = var_json("Data_ACWorthTrend") or []
    nav_series = []
    for item in trend:
        try:
            d = _ms_to_nav_date(item["x"])
            nav_series.append({"date": d, "nav": float(item["y"]),
                               "pct": item.get("equityReturn"),
                               "bonus": item.get("unitMoney") or ""})
        except Exception:
            continue
    out["nav_series"] = nav_series
    out["nav"] = nav_series[-1]["nav"] if nav_series else None
    out["nav_date"] = nav_series[-1]["date"] if nav_series else None
    if len(nav_series) >= 2:
        prev = nav_series[-2]["nav"]
        out["day_pct"] = round((nav_series[-1]["nav"] / prev - 1) * 100, 4) if prev else None
    # 累计净值（用于分红复投口径）
    acc_series = []
    for it in ac:
        try:
            acc_series.append({"date": _ms_to_nav_date(it[0]),
                               "acc": float(it[1])})
        except Exception:
            continue
    out["acc_series"] = acc_series
    out["acc_nav"] = acc_series[-1]["acc"] if acc_series else None

    # 基金经理 / 规模
    mgr = var_json("Data_currentFundManager")
    if isinstance(mgr, list) and mgr:
        m0 = mgr[0]
        out["manager"] = m0.get("name")
        out["manager_star"] = m0.get("star")
        out["manager_worktime"] = m0.get("workTime")
        out["manager_fundsize"] = m0.get("fundSize")
        pw = m0.get("power") or {}
        out["manager_power"] = pw.get("avr")
        out["manager_power_cats"] = pw.get("categories")
        out["manager_power_data"] = pw.get("data")
    # 规模：{"categories":[...],"series":[{"y":349.43,"mom":"-10.19%"},...]}
    fs = var_json("Data_fluctuationScale")
    if isinstance(fs, dict):
        ser = fs.get("series") or []
        cats = fs.get("categories") or []
        if ser:
            out["scale"] = to_float(str(ser[-1].get("y", "")).replace("亿元", ""))
            out["scale_date"] = cats[-1] if cats else None
            out["scale_mom"] = ser[-1].get("mom")
            out["scale_series"] = [{"date": c, "y": s.get("y")}
                                   for c, s in zip(cats, ser)]
    elif isinstance(fs, list) and fs:
        out["scale"] = to_float(str(fs[-1].get("y", "")).replace("亿元", ""))
        out["scale_date"] = fs[-1].get("x")

    # 分红记录（天天基金 fhsp 页）
    out["dividends"] = fetch_fund_dividends(code)
    return out


def fetch_fund_dividends(code):
    """基金分红送配：'每10份派现金0.4500元' -> per_share = 0.4500/10"""
    html = http_get("https://fundf10.eastmoney.com/fhsp_%s.html" % code)
    if not html:
        return []
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    res = []
    for r in rows:
        cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if len(cells) == 5 and cells[0].endswith("年"):
            # 仅接受“派现金”类分红，排除拆分折算行
            if "派现金" not in cells[3] and "派现" not in cells[3]:
                continue
            m = re.search(r"派现金?\s*([0-9]+(?:\.[0-9]+)?)\s*元", cells[3])
            if not m:
                continue
            per10 = float(m.group(1))
            res.append({"year": cells[0], "ex_date": cells[2], "per10": per10,
                        "per_share": round(per10 / 10, 6), "pay_date": cells[4],
                        "raw": cells[3]})
    res.sort(key=lambda x: x.get("ex_date") or "", reverse=True)
    return res


# ---------------------------------------------------------------- 场内股票/ETF
def norm_stock_code(code):
    """把沪深/北交所代码补足 6 位。

    底表是 Excel 导出的，前导 0 会被吃掉：776 → 000776、512400 保持 6 位。
    不补零会导致 _tx_prefix 判错交易所（776 → sh776 查不到），
    行情、K线、技术位全线拿不到。
    """
    c = (code or "").strip()
    if not c.isdigit():
        return c
    return c.zfill(6) if len(c) < 6 else c


def _tx_prefix(code):
    """判断交易所前缀。注意：沪市除 60/68 外，还包括 5xx（基金/ETF，如 510300/512400）、9xx（B股）"""
    c = norm_stock_code(str(code))
    if c.startswith(("60", "68", "9", "5")):
        return "sh" + c
    if c.startswith(("00", "30", "20", "15", "16", "18")):
        return "sz" + c
    if c.startswith(("83", "87", "43")):
        return "bj" + c
    return "sh" + c


def guess_type(code, name=""):
    """按代码段与名称推断品种类型：股票 / ETF / 基金"""
    c = str(code)
    n = name or ""
    # 场内 ETF / LOF：15x/16x/5xx 且名称含 ETF
    if "ETF" in n:
        return "ETF"
    if c.startswith(("51", "52", "56", "58", "159", "16")):
        return "ETF"
    # 场外基金：0xx/1xx/2xx/3xx/4xx 且非股票段
    if c.startswith(("0", "1", "2", "3", "4", "5")) and not c.startswith(("00", "30")):
        return "基金"
    return "股票"


def _kline_sina(sym, n):
    """新浪日线（不复权）。返回 [{date,open,close,low,high,volume,amount}]，未排序。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, n))
    txt = http_get(url)
    if not txt:
        return []
    try:
        j = json.loads(txt)
    except Exception as e:
        WARNINGS.append("%s 新浪K线解析异常: %s" % (sym, e))
        return []
    out = []
    for r in (j if isinstance(j, list) else []):
        try:
            out.append({"date": r["day"], "open": to_float(r.get("open")),
                        "close": to_float(r.get("close")), "low": to_float(r.get("low")),
                        "high": to_float(r.get("high")), "volume": to_float(r.get("volume")),
                        "amount": None})
        except Exception:
            continue
    return out


def _kline_tencent(sym, n):
    """腾讯日线（不复权：param 末位不传 fq）。含当日未收盘的半根K线，仅作兜底。"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d," % (sym, n)
    txt = http_get(url)
    if not txt:
        return []
    try:
        j = json.loads(txt)
    except Exception as e:
        WARNINGS.append("%s 腾讯K线解析异常: %s" % (sym, e))
        return []
    node = ((j.get("data") or {}).get(sym)) or {}
    rows = node.get("day") or node.get("qfqday") or []
    out = []
    for r in rows:
        # [日期, 开, 收, 高, 低, 成交量(手)]
        try:
            out.append({"date": r[0], "open": to_float(r[1]), "close": to_float(r[2]),
                        "high": to_float(r[3]), "low": to_float(r[4]),
                        "volume": to_float(r[5]), "amount": None})
        except Exception:
            continue
    return out


def _kline_sohu(code, days):
    """搜狐日线（原唯一来源，现降为最后兜底；在沙箱内间歇性 503）。"""
    mk = "cn_" + norm_stock_code(str(code))
    end = date.today().strftime("%Y%m%d")
    start = (date.today().replace(year=date.today().year - 2)).strftime("%Y%m%d")
    url = ("https://q.stock.sohu.com/hisHq?code=%s&start=%s&end=%s&stat=0&order=D&period=d"
           % (mk, start, end))
    txt = http_get(url)
    if not txt:
        return []
    try:
        j = json.loads(txt)
    except Exception as e:
        WARNINGS.append("%s 搜狐K线解析异常: %s" % (code, e))
        return []
    rows = (j[0].get("hq") if j and isinstance(j, list) else None) or []
    out = []
    for r in rows:
        # [日期, 开盘, 收盘, 涨跌额, 涨跌幅, 最低, 最高, 成交量(手), 成交额(万), 换手率, 后一]
        try:
            out.append({"date": r[0], "open": to_float(r[1]), "close": to_float(r[2]),
                        "low": to_float(r[5]), "high": to_float(r[6]),
                        "volume": to_float(r[7]), "amount": to_float(r[8])})
        except Exception:
            continue
    return out


def fetch_daily_kline(code, days=400):
    """股票/ETF 历史日线，不复权口径，用于支撑压力位/波动率/量价配合。

    返回 [{date,open,close,low,high,volume,amount}]，按日期升序，取最后 days 根。

    多源兜底（2026-09-17 起）：新浪 → 腾讯 → 搜狐。
    起因：旧唯一来源搜狐在 WorkBuddy 沙箱里**间歇性 HTTP 503**
    （3 次采样 2 次失败；DNS 与 UA/Referer 均相同，判定为对数据中心出口 IP 的限流），
    而新浪/腾讯在沙箱与本地均稳定。

    替换口径核对（2026-09-17，标的 601628，与搜狐逐日比对）：
      · 新浪 交集 485 日、最大偏差 0.000；腾讯 交集 485 日、最大偏差 0.000 —— 收盘价完全一致
      · `amount` 仅搜狐提供，但下游（评分卡）与前端（蜡烛图只用 open/close/low/high/date）
        均未使用，故统一置 None，不影响任何结果
      · volume 单位差异（手 / 股）无需换算：score_volume_price 只用 v5/v20 比值，单位自然抵消
      · 注意：腾讯含当日未收盘的半根 K 线，新浪与搜狐都不含 —— 仅在回落到腾讯时才存在此差异
    """
    sym = _tx_prefix(code)
    n = days + 100
    out = _kline_sina(sym, n) or _kline_tencent(sym, n)
    if not out:
        out = _kline_sohu(code, days)
    if not out:
        WARNINGS.append("%s 日线三源均失败（新浪/腾讯/搜狐）" % code)
        return []
    out.sort(key=lambda x: x["date"])
    return out[-days:]


def fetch_stock(code):
    """腾讯行情 + 新浪行情 + 腾讯K线"""
    out = {"code": str(code), "source": "腾讯/新浪"}
    sym = _tx_prefix(code)

    # 腾讯实时
    txt = http_get("https://qt.gtimg.cn/q=%s" % sym, encoding="gbk")
    if txt and "~" in txt:
        f = txt.split('="')[1].split("~") if '="' in txt else txt.split("~")
        try:
            out["name"] = f[1]
            out["now"] = to_float(f[3])
            out["prev_close"] = to_float(f[4])
            out["open"] = to_float(f[5])
            out["volume"] = to_float(f[6])
            out["high"] = to_float(f[33]) if len(f) > 33 else None
            out["low"] = to_float(f[34]) if len(f) > 34 else None
            out["change"] = to_float(f[31]) if len(f) > 31 else None
            out["pct"] = to_float(f[32]) if len(f) > 32 else None
            out["amount"] = to_float(f[37]) if len(f) > 37 else None
            out["turnover"] = to_float(f[38]) if len(f) > 38 else None
            out["pe"] = to_float(f[39]) if len(f) > 39 else None
            out["amplitude"] = to_float(f[43]) if len(f) > 43 else None
            out["circ_mv"] = to_float(f[44]) if len(f) > 44 else None
            out["total_mv"] = to_float(f[45]) if len(f) > 45 else None
            out["pb"] = to_float(f[46]) if len(f) > 46 else None
            out["limit_up"] = to_float(f[47]) if len(f) > 47 else None
            out["limit_down"] = to_float(f[48]) if len(f) > 48 else None
            out["volume_ratio"] = to_float(f[49]) if len(f) > 49 else None
            out["avg_price"] = to_float(f[51]) if len(f) > 51 else None
            out["pe_static"] = to_float(f[53]) if len(f) > 53 else None
        except Exception as e:
            WARNINGS.append("%s 腾讯行情解析异常: %s" % (code, e))

    # 新浪兜底（拿股息率需要另一套字段，这里主要兜价格）
    if not out.get("now"):
        s = http_get("https://hq.sinajs.cn/list=%s" % sym, encoding="gbk")
        if s and "=" in s and len(s.split('"')) > 1:
            p = s.split('"')[1].split(",")
            if len(p) > 4:
                out["name"] = out.get("name") or p[0]
                out["now"] = to_float(p[3], 0) or None
                out["prev_close"] = to_float(p[2])

    # K线（近400日，用于支撑压力位/波动率/量价配合）——新浪主源，腾讯、搜狐依次兜底
    out["kline"] = fetch_daily_kline(code)

    # 股息率：从腾讯扩展字段不易取，用公开分红接口兜
    out["dividends"] = fetch_stock_dividends(code)
    return out


def fetch_stock_dividends(code):
    """A股历史分红（每10股派息）

    注：原先这里会抓 data.eastmoney.com/stockdata/{code}.html，但该页返回的是
    整页 HTML、解析出的结果从未被使用（死代码），且经常 500 报错污染 warnings。
    已移除该无效请求，只保留 F10 接口。
    """
    res = []
    # 东财 F10 分红页
    h2 = http_get("https://emweb.securities.eastmoney.com/PC_HSF10/BonusFinancing/PageAjax"
                  "?code=%s" % _tx_prefix(code).upper())
    if h2:
        try:
            j = json.loads(h2)
            for item in (j.get("fh") or j.get("data") or []):
                pass
        except Exception:
            pass
    return res


# ---------------------------------------------------------------- 计算
def calc_support_resistance(kline, now, n=60):
    """支撑位/压力位：近期波段高低点 + 关键均线 + 布林带 + 近一年价格分位锚点"""
    if not kline or len(kline) < 20:
        return {}
    closes = [k["close"] for k in kline if k.get("close")]
    if not closes:
        return {}
    r = {}
    recent = kline[-n:] if len(kline) >= n else kline
    r["swing_high_60"] = max(x["high"] for x in recent if x.get("high"))
    r["swing_low_60"] = min(x["low"] for x in recent if x.get("low"))

    # 中期波段（近 250 交易日高低）
    mid = kline[-250:] if len(kline) >= 250 else kline
    r["swing_high_250"] = max(x["high"] for x in mid if x.get("high"))
    r["swing_low_250"] = min(x["low"] for x in mid if x.get("low"))

    for p in (5, 10, 20, 60, 120):
        if len(closes) >= p:
            r["ma%d" % p] = round(sum(closes[-p:]) / p, 3)
    # 布林带(20,2)
    if len(closes) >= 20:
        w = closes[-20:]
        ma = sum(w) / 20
        sd = (sum((c - ma) ** 2 for c in w) / 20) ** 0.5
        r["boll_mid"], r["boll_up"], r["boll_low"] = round(ma, 3), round(ma + 2 * sd, 3), round(ma - 2 * sd, 3)

    p = now or closes[-1]
    # 用近一年日线收盘构建价格分位锚点（成交密集区），避免均线贴价导致支撑压力过近
    yr = sorted(c for c in (closes[-250:] if len(closes) > 250 else closes))
    if yr:
        import math
        for q, key in ((0.10, "pct10"), (0.25, "pct25"), (0.50, "pct50"), (0.75, "pct75"), (0.90, "pct90")):
            idx = min(len(yr) - 1, max(0, int(math.floor(q * (len(yr) - 1)))))
            r[key] = round(yr[idx], 3)
        r["pos_pct_1y"] = round(sum(1 for c in yr if c <= p) / len(yr) * 100, 1)

    cand = [v for k, v in r.items() if k in
            ("ma20", "ma60", "ma120", "boll_low", "boll_up", "swing_low_60", "swing_high_60",
             "swing_low_250", "swing_high_250", "pct10", "pct25", "pct50", "pct75", "pct90")
            and v and v > 0]
    # 支撑：低于现价、且与现价至少有 1% 缓冲，取最高的两个
    sup = sorted([v for v in cand if v < p * 0.99], reverse=True)
    # 压力：高于现价、且至少有 1% 空间，取最低的两个
    res = sorted([v for v in cand if v > p * 1.01])
    if not sup:
        sup = sorted([v for v in cand if v < p], reverse=True)
    if not res:
        res = sorted([v for v in cand if v > p])
    r["support1"] = round(sup[0], 3) if sup else None
    r["support2"] = round(sup[1], 3) if len(sup) > 1 else None
    r["resistance1"] = round(res[0], 3) if res else None
    r["resistance2"] = round(res[1], 3) if len(res) > 1 else None
    r["support_dist"] = round((p / sup[0] - 1) * 100, 2) if sup else None
    r["resistance_dist"] = round((res[0] / p - 1) * 100, 2) if res else None

    # 安全度评分：距支撑的缓冲 + 位置分位（越低越安全）
    safety = 50.0
    if r["support_dist"] is not None:
        safety += min(25, r["support_dist"] * 2.5)
    if r["pos_pct_1y"] is not None:
        safety += (50 - r["pos_pct_1y"]) / 4
    if r["resistance_dist"] is not None and r["resistance_dist"] < 3:
        safety -= 8
    r["safety_score"] = max(0, min(100, round(safety, 1)))
    return r


def calc_volatility(kline, n=60):
    if not kline or len(kline) < 10:
        return None
    closes = [k["close"] for k in kline if k.get("close")]
    w = closes[-(n + 1):]
    rets = [(w[i] / w[i - 1] - 1) for i in range(1, len(w)) if w[i - 1]]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return round((var ** 0.5) * (250 ** 0.5) * 100, 2)


def calc_rsi(kline, n=14):
    closes = [k["close"] for k in kline if k.get("close")]
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 2)


def calc_macd(kline):
    closes = [k["close"] for k in kline if k.get("close")]
    if len(closes) < 35:
        return {}
    def ema(arr, n):
        k = 2 / (n + 1)
        e = arr[0]
        out = [e]
        for v in arr[1:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    macd = [(a - b) * 2 for a, b in zip(dif, dea)]
    return {"dif": round(dif[-1], 3), "dea": round(dea[-1], 3), "macd": round(macd[-1], 3),
            "cross": "金叉" if dif[-1] > dea[-1] and dif[-2] <= dea[-2] else
                     ("死叉" if dif[-1] < dea[-1] and dif[-2] >= dea[-2] else
                      ("多头" if dif[-1] > dea[-1] else "空头"))}


def calc_kdj(kline, n=9):
    if not kline or len(kline) < n:
        return {}
    k, d = 50.0, 50.0
    for i in range(n - 1, len(kline)):
        w = kline[i - n + 1:i + 1]
        hi = max(x["high"] for x in w)
        lo = min(x["low"] for x in w)
        c = kline[i]["close"]
        rsv = 50 if hi == lo else (c - lo) / (hi - lo) * 100
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    j = 3 * k - 2 * d
    return {"k": round(k, 2), "d": round(d, 2), "j": round(j, 2)}


def score_technical(kline, now):
    """技术面 0-100"""
    if not kline or len(kline) < 30:
        return None, {}
    closes = [x["close"] for x in kline]
    det = {}
    s = 50.0
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    det["ma20"] = round(ma20, 3)
    det["ma60"] = round(ma60, 3) if ma60 else None
    p = now or closes[-1]
    if p > ma20:
        s += 8
    else:
        s -= 8
    if ma60:
        if p > ma60:
            s += 7
        else:
            s -= 7
        if ma20 > ma60:
            s += 5
        else:
            s -= 5
    md = calc_macd(kline)
    det.update(md)
    if md.get("cross") == "金叉":
        s += 10
    elif md.get("cross") == "死叉":
        s -= 10
    elif md.get("cross") == "多头":
        s += 5
    else:
        s -= 5
    rsi = calc_rsi(kline)
    det["rsi14"] = rsi
    if rsi is not None:
        if 45 <= rsi <= 65:
            s += 8
        elif rsi > 75:
            s -= 8
        elif rsi < 30:
            s += 4
    kd = calc_kdj(kline)
    det.update(kd)
    if kd.get("k") and kd.get("d"):
        if kd["k"] > kd["d"]:
            s += 5
        else:
            s -= 5
    # 趋势斜率
    if len(closes) >= 20:
        slope = (closes[-1] / closes[-20] - 1) * 100
        det["ret20_pct"] = round(slope, 2)
        s += max(-10, min(10, slope / 2))
    return max(0, min(100, round(s, 1))), det


def score_valuation(pe, pe_static, pb, dividend_yield, pos_pct):
    """基本面-估值合理性 0-100（可解释：绝对估值 + 历史分位）"""
    if pe is None and pb is None:
        return None, {}
    s = 50.0
    det = {"pe": pe, "pb": pb, "pe_static": pe_static, "pos_pct_1y": pos_pct}
    if pe is not None and pe > 0:
        if pe < 10:
            s += 20
        elif pe < 15:
            s += 14
        elif pe < 20:
            s += 8
        elif pe < 30:
            s += 0
        elif pe < 45:
            s -= 8
        elif pe < 70:
            s -= 15
        else:
            s -= 22
    if pb is not None and pb > 0:
        if pb < 1:
            s += 12
        elif pb < 2:
            s += 8
        elif pb < 4:
            s += 2
        elif pb < 8:
            s -= 6
        else:
            s -= 12
    if dividend_yield:
        det["dividend_yield"] = dividend_yield
        if dividend_yield >= 5:
            s += 12
        elif dividend_yield >= 3:
            s += 8
        elif dividend_yield >= 1.5:
            s += 4
    if pos_pct is not None:
        s += (50 - pos_pct) / 8
    return max(0, min(100, round(s, 1))), det


def score_volume_price(kline, now, prev_close):
    """量价配合 0-100"""
    if not kline or len(kline) < 25:
        return None, {}
    det = {}
    vols = [x["volume"] for x in kline if x.get("volume")]
    closes = [x["close"] for x in kline]
    v5 = sum(vols[-5:]) / 5
    v20 = sum(vols[-20:]) / 20
    vr = v5 / v20 if v20 else 1
    det["vol_ratio_5_20"] = round(vr, 3)
    ret5 = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
    ret20 = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0
    det["ret5_pct"], det["ret20_pct"] = round(ret5, 2), round(ret20, 2)
    s = 50.0
    # 价涨量增 / 价跌量缩 = 健康
    if ret5 > 0 and vr > 1:
        s += 18
        det["pattern"] = "价涨量增(健康)"
    elif ret5 > 0 and vr < 1:
        s -= 6
        det["pattern"] = "价涨量缩(动能不足)"
    elif ret5 <= 0 and vr > 1:
        s -= 16
        det["pattern"] = "价跌量增(抛压)"
    else:
        s += 8
        det["pattern"] = "价跌量缩(惜售)"
    if ret20 > 8:
        s += 6
    elif ret20 < -8:
        s -= 6
    # 换手活跃度
    return max(0, min(100, round(s, 1))), det


def score_fundamental_quality(fin, fund_meta):
    """基本面-盈利质量/成长性 0-100（基金用阶段收益+规模，股票用ROE等）"""
    s = 50.0
    det = {}
    if fund_meta:
        for k, lbl, w in (("syl_1y", "近1月", 0.5), ("syl_3y", "近3月", 0.8),
                          ("syl_6y", "近6月", 1.0), ("syl_1n", "近1年", 1.5)):
            v = fund_meta.get(k)
            if v is not None:
                det[lbl] = v
                s += max(-18, min(18, v * w / 3))
    if fin:
        det.update(fin)
    return max(0, min(100, round(s, 1))), det


# ---------------------------------------------------------------- 股息/分红
def estimate_stock_dividend(code, name, now, shares):
    """股票股息：优先取公开股息率，取不到则如实标注"""
    sym = _tx_prefix(code)
    info = {}
    txt = http_get("https://qt.gtimg.cn/q=%s" % sym, encoding="gbk")
    # 腾讯行情无股息率字段，改从新浪/东财口径取；取不到则为 None
    info["dividend_yield"] = None
    info["per_share"] = None
    info["annual_income"] = None
    info["note"] = "公开源未直接返回股息率，需人工核对或后续接入分红明细接口"
    return info


# ---------------------------------------------------------------- 定投计算
def _parse_day_rule(day_rule):
    """把 day_rule 解析成 (mode, value)。支持 '每周四'/'周四'/'每周'/'每月8日' 等"""
    s = (day_rule or "").strip()
    # 先找「周X」——注意必须跳过「每周」中的「每」，故用 周[一二三四五六日天]
    m = re.search(r"周\s*([一二三四五六日天])", s)
    if m:
        ch = m.group(1)
        wd = 6 if ch == "天" else "一二三四五六日".find(ch)
        if 0 <= wd <= 6:
            return ("weekday", wd)
    m = re.search(r"(\d+)\s*[日号]", s)
    if m:
        return ("monthday", int(m.group(1)))
    return (None, None)


def calc_dca(plan, nav_series, today=None):
    """按历史净值回算定投累计份额

    支持两种锚定：
      - 每周：锚定每周几（day_rule='每周四'），当天无净值则顺延到下一个有净值的交易日
      - 每月：锚定每月第几号，无则顺延；未指定则按 start_date 的「日」每月同一天
    """
    if not plan or not nav_series:
        return {}
    freq = (plan.get("frequency") or "月").strip()
    amount = to_float(plan.get("amount"), 0)
    start = (plan.get("start_date") or "").strip()
    fee_rate = to_float(plan.get("fee_rate"), 0)   # 单笔申购费率，如 0.0012 = 0.12%
    if not start or not amount:
        return {}
    try:
        sd = datetime.strptime(start, "%Y-%m-%d").date()
    except Exception:
        return {}

    navs = [(datetime.strptime(x["date"], "%Y-%m-%d").date(), x["nav"])
            for x in nav_series if x.get("nav")]
    navs.sort(key=lambda x: x[0])
    if not navs:
        return {"periods": 0, "shares": 0, "invested": 0, "plan": plan}
    last_day = navs[-1][0]

    dates = [d for d, _ in navs]
    def hit_of(target):
        """从 target 起顺延，找到第一个 >= target 且有净值的交易日"""
        for d, v in navs:
            if d >= target:
                return d, v
        return None

    mode, val = _parse_day_rule(plan.get("day_rule"))

    # 生成扣款日列表
    buys = []
    is_week = ("周" in freq) or (mode == "weekday")   # 兼容「每周」「周」「每周四」等写法
    if is_week:
        wd = val if (mode == "weekday" and val is not None) else sd.weekday()
        # 从 sd 所在周起，逐周推进
        cur = sd - timedelta(days=(sd.weekday() - wd) % 7)
        if cur < sd:
            cur = cur + timedelta(days=7)
        while cur <= last_day:
            buys.append(cur)
            cur = cur + timedelta(days=7)
    else:
        day = val if (mode == "monthday" and val) else sd.day
        y, mo = sd.year, sd.month
        while True:
            try:
                cur = date(y, mo, min(day, 28) if day > 28 else day)
            except ValueError:
                cur = date(y, mo, 28)
            if cur > last_day:
                break
            if cur >= sd:
                buys.append(cur)
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1

    total_shares = total_amount = total_fee = 0.0
    detail = []
    for b in buys:
        h = hit_of(b)
        if not h:
            continue
        d, v = h
        if v <= 0:
            continue
        # 外扣法：投入 amount 元，先扣申购费，余额再按当日净值换份额
        fee = amount * fee_rate
        net = amount - fee
        sh = net / v
        total_shares += sh
        total_amount += amount
        total_fee += fee
        detail.append({"plan_date": b.isoformat(), "deal_date": d.isoformat(),
                       "nav": v, "amount": amount, "fee": round(fee, 4),
                       "net": round(net, 4), "shares": round(sh, 2)})

    periods = len(detail)
    if periods == 0:
        return {"periods": 0, "shares": 0, "invested": 0, "plan": plan, "detail": []}

    last_nav = navs[-1][1]
    return {"periods": periods, "shares": round(total_shares, 2),
            "invested": round(total_amount, 2),
            "total_fee": round(total_fee, 2),
            "fee_rate": fee_rate,
            "value": round(total_shares * last_nav, 2),
            "profit": round(total_shares * last_nav - total_amount, 2),
            "ret_pct": round((total_shares * last_nav / total_amount - 1) * 100, 2) if total_amount else None,
            "avg_cost": round(total_amount / total_shares, 4) if total_shares else None,
            "last_nav": last_nav, "plan": plan, "detail": detail}


def calc_dividend_reinvest(divs, shares, policy, shares_incl_reinvest=True, since=None):
    """分红与再投汇总

    重要口径说明：
      用户填报的 shares 通常【已包含历史红利再投得到的份额】（如 020602 的 930.49
      份已含 9/11 再投的 8.11 份）。因此这里【不再叠加】历史再投份额，
      只统计历史每份累计分红金额、按当前份额折算的「累计分红现金」，
      以及面向未来的年化分红率估算，避免重复放大份额。

    since: 建仓日（YYYY-MM-DD）。仅统计【除息日 >= 建仓日】的分红，
           因为建仓之前的分红与本人无关（例：024564 于 2026-07-20 建仓，
           2026-07-14 的那笔分红不参与统计）。
    """
    if not divs:
        return {"count": 0, "total_per_share": 0, "cash": 0, "mode": policy,
                "note": "无历史分红记录"}
    mode = (policy or "红利再投").strip()

    # 建仓日过滤：只保留建仓之后（含当日）的除息记录
    excluded = []
    if since:
        kept = []
        for d in divs:
            ex = (d.get("ex_date") or "")
            if ex and ex < since:
                excluded.append(d)
            else:
                kept.append(d)
        divs = kept
        if not divs:
            return {"count": 0, "total_per_share": 0, "cash": 0,
                    "cash_recent_12m": 0, "recent_count_12m": 0,
                    "recent_per_share_12m": 0, "mode": mode,
                    "shares_basis": round(shares, 2),
                    "shares_incl_reinvest": shares_incl_reinvest,
                    "since": since, "excluded_count": len(excluded),
                    "note": "建仓日 %s 之前无分红记录（已排除 %d 笔建仓前分红）"
                            % (since, len(excluded))}

    total_per = sum(d.get("per_share", 0) or 0 for d in divs)
    # 近 12 个月分红
    from datetime import date as _d
    try:
        today = _d.today()
    except Exception:
        today = None
    recent, recent_per = [], 0.0
    for d in divs:
        try:
            ex = datetime.strptime(d.get("ex_date"), "%Y-%m-%d").date()
        except Exception:
            continue
        if today and (today - ex).days <= 366:
            recent.append(d)
            recent_per += d.get("per_share") or 0
    cash_recent = round(recent_per * shares, 2)
    out = {"count": len(divs), "total_per_share": round(total_per, 4),
           "cash": cash_recent, "cash_recent_12m": cash_recent,
           "recent_count_12m": len(recent),
           "recent_per_share_12m": round(recent_per, 4),
           "mode": mode, "shares_basis": round(shares, 2),
           "shares_incl_reinvest": shares_incl_reinvest,
           "note": "份额已含历史再投，未重复叠加；cash 为近12个月分红现金(按当前份额折算)",
           "last": max(divs, key=lambda x: x.get("ex_date") or "") if divs else None}
    if since:
        out["since"] = since
        out["excluded_count"] = len(excluded)
        if excluded:
            out["note"] += "；已排除建仓日 %s 之前的 %d 笔分红" % (since, len(excluded))
    return out


def reconcile_dividend_record(rec, divs, dca_plan, current_shares):
    """还原一次实际分红再投的「派息基数」，并校验是否与净值/份额自洽。

    关键业务规则（用户确认）：
      权益登记日当天，T+1 才确认的申购份额（如当天定投买入的份额）
      【不计入派息基数】。因此：
        派息基数 = 当前份额 - 再投份额(本笔) - 登记日当天未确认的定投份额

    返回校验详情，用于页面展示与对账。
    """
    out = {"code": rec.get("code"), "ex_date": rec.get("ex_date"),
           "confirm_date": rec.get("confirm_date")}
    try:
        nav_reinv = float(rec.get("reinvest_nav"))
        reinv_shares = float(rec.get("reinvest_shares"))
    except Exception:
        # 历史记录可能只有份额、没有净值（截图未提供）——仍计入份额统计
        try:
            reinv_shares = float(rec.get("reinvest_shares"))
        except Exception:
            return out
        out["reinvest_shares"] = reinv_shares
        out["note"] = "仅有确认份额，无再投净值，未做基数校验"
        out["consistent"] = None
        return out
    cash = reinv_shares * nav_reinv
    out["reinvest_nav"] = nav_reinv
    out["reinvest_shares"] = reinv_shares
    out["dividend_cash"] = round(cash, 4)

    # 找除息日对应的每份分红
    per_share = None
    for d in divs:
        if d.get("ex_date") == rec.get("ex_date"):
            per_share = d.get("per_share")
            break
    if not per_share:
        out["note"] = "未找到该除息日的分红公告，无法还原派息基数"
        return out
    out["per_share"] = per_share
    base = cash / per_share
    out["ex_dividend_base"] = round(base, 2)

    # 登记日当天的定投份额（T+1 确认，不计入基数）
    same_day_dca = 0.0
    # 除息日之后（不含当日）的定投份额——这些发生在分红之后，不算进派息基数，
    # 但已经体现在「当前份额」里，校验时必须补回来。
    after_dca = 0.0
    if dca_plan:
        plan = dca_plan
        exd = (rec.get("ex_date") or "")
        for det in (plan.get("detail") or []):
            dd = det.get("deal_date") or ""
            sh = det.get("shares") or 0
            if dd == exd:
                same_day_dca = sh
            elif dd > exd:
                after_dca += sh
    out["same_day_dca_shares"] = round(same_day_dca, 2)
    out["after_ex_dca_shares"] = round(after_dca, 2)

    # 校验：派息基数 + 再投 + 当日定投 + 除息后定投 == 当前份额 ?
    implied_current = base + reinv_shares + same_day_dca + after_dca
    out["implied_current_shares"] = round(implied_current, 2)
    out["current_shares"] = round(current_shares, 2)
    out["diff_vs_current"] = round(implied_current - current_shares, 2)
    out["consistent"] = abs(implied_current - current_shares) < 1.0
    if out["consistent"]:
        out["note"] = ("自洽：派息基数 %.2f（已排除登记日 T+1 未确认的定投 %.2f 份）"
                       "+ 再投 %.2f + 当日定投 %.2f + 除息后定投 %.2f = 当前 %.2f"
                       % (base, same_day_dca, reinv_shares, same_day_dca, after_dca, current_shares))
    else:
        out["note"] = ("存在 %.2f 份差异，可能含非定投买入或净值口径不同"
                       % out["diff_vs_current"])
    return out


def project_longterm(dca, divs, years=10, since=None, rates=None, rate_meta=None):
    """定投长期预测（三情景），按【周】递推。

    起点资金以「实际持仓市值」为准（有则用 actual_value，退化到回放估值）；
    每期投入取计划里的周金额，一年 52 期（非周投计划折算为周口径）。

    ⚠️ 三档年化回报由 derive_scenario_rates() 按【该基金自身历史】推导后传入（rates），
    不再是全站写死的整数；拿不到 rates 就直接返回 {}（宁可不出预测也不编造）。

    本函数只是「余额按 r 复利 + 每期投入」，**不单独建模分红** —— 分红已隐含在总回报 r 里
    （r 是含分红的总回报）。要单独看每年分到多少现金，见 build_dividend_projection()。
    since: 建仓日，早于此日的分红不计入历史分红统计。
    """
    if not dca or not dca.get("periods") or not rates:
        return {}
    plan = dca.get("plan") or {}
    amt = to_float(plan.get("amount"), 0) or 0
    freq = (plan.get("frequency") or "月").strip()
    weekly = amt if "周" in freq else amt * 12 / 52     # 统一折算成周口径
    start_bal = dca.get("actual_value") or dca.get("value") or 0
    hist = [d for d in (divs or [])
            if not since or (d.get("ex_date") or "") >= since]
    total_div = sum(d.get("per_share", 0) or 0 for d in hist)
    scen = {}
    for label in SCEN_LABELS:
        r = rates.get(label)
        r_w = (1 + r / 12) ** (12.0 / 52) - 1 if r is not None else 0
        bal = start_bal
        for _ in range(years * 52):
            bal = bal * (1 + r_w) + weekly
        scen[label] = round(bal, 2)
    out = {"years": years, "weekly": round(weekly, 2), "weeks_per_year": 52,
           "monthly": round(weekly * 52 / 12, 2),
           "start_balance": round(start_bal, 2),
           "dividend_history_per_share": round(total_div, 4),
           "invested_total": round(weekly * 52 * years + start_bal, 2),
           "rates": {k: rates.get(k) for k in SCEN_LABELS},
           "scenarios": scen}
    if rate_meta:
        out["rate_meta"] = rate_meta
    if since:
        out["since"] = since
    return out


# ---------------------------------------------------------------- 年度分红推演
# 按定投计划推演未来若干年「每年可得分红」（毛额，即红利再投之前的金额）。
DIVPROJ_YEARS = 30

# ---------------------------------------------------------------- 三情景假设
# 【口径 —— 2026-09-16 起改为数据驱动，此前是三档写死的整数（-2% / 6% / 14%）】
# 【2026-09-18 再调：差价倍率由 1σ 改为 0.2σ，用户拍板，理由见下】
# 三档年化回报由【每只基金自身的历史】推导，不再全站统一：
#     中性     = 该基金历史实现年化总回报（累计净值口径，含分红，避免除息造成的假跌幅）
#     σ        = 年化波动率（日对数收益的总体标准差 × √252，同一口径）
#     悲观/乐观 = 中性 ∓ K × σ × √(1/投影年数 + 1/历史年数)
#                两项分别是「投影均值自身的抽样波动」与「历史均值的估计误差」。
# ⚠️ 为什么把 K 从 1.0 降到 0.2：这批基金的历史只有 1.1~2.4 年，
#    σ/√历史年数（历史均值的估计误差）高达 8.9~11.4pp，占了总差价的 92~96%，
#    于是 1σ 档位跑出「悲观 -2.54% / 乐观 +15.92%」这种跨度过大、没法当规划输入用的区间。
#    实测 K=1.0 → 差价 ±9.2~11.6pp；K=0.2 → ±1.8~2.3pp。
# ⚠️ K=0.2σ 意味着这【不再是 1σ 置信区间】，只是「围绕历史均值的窄幅情景带」；
#    它是【本看板自定的口径】，不是统计推断的最优解，样本越短越不可靠。
#    所有中间量（含 spread_k）都会随数据下发并在页面上标注，便于复核。
SCEN_LABELS = ("悲观", "中性", "乐观")
MIN_SCEN_DAYS = 180          # 少于此交易日数就不推导，宁可不出预测也不编造
SCEN_SPREAD_K = 0.2          # 差价倍率（σ 的倍数）—— 改这里就改了三档的宽窄


def derive_scenario_rates(nav_series, acc_series, years=DIVPROJ_YEARS):
    """由基金自身历史推导三情景年化回报。

    返回 (rates_dict, meta)。样本不足时返回 (None, meta)，调用方据此不出预测。
    """
    use_acc = bool(acc_series) and len(acc_series) > MIN_SCEN_DAYS
    src, key = (acc_series, "acc") if use_acc else (nav_series, "nav")
    vals = [x.get(key) for x in (src or []) if x.get(key)]
    if len(vals) < MIN_SCEN_DAYS + 1:
        return None, {"usable": False,
                      "reason": "有效历史不足 %d 个交易日" % MIN_SCEN_DAYS}
    rets = [math.log(vals[i + 1] / vals[i])
            for i in range(len(vals) - 1) if vals[i] > 0 and vals[i + 1] > 0]
    if len(rets) < MIN_SCEN_DAYS:
        return None, {"usable": False, "reason": "有效收益样本不足 %d 个" % MIN_SCEN_DAYS}
    n_days = len(rets)
    n_years = n_days / 252.0
    mu = (1 + statistics.mean(rets)) ** 252 - 1
    sigma = statistics.pstdev(rets) * math.sqrt(252)
    spread = SCEN_SPREAD_K * sigma * math.sqrt(1.0 / years + 1.0 / n_years)
    rates = {"悲观": round(mu - spread, 4), "中性": round(mu, 4), "乐观": round(mu + spread, 4)}
    meta = {"usable": True, "basis": "累计净值" if use_acc else "单位净值",
            "sample_days": n_days, "history_years": round(n_years, 2),
            "start": src[0].get("date"), "end": src[-1].get("date"),
            "realized_annual": round(mu, 4), "volatility_annual": round(sigma, 4),
            "spread": round(spread, 4), "spread_k": SCEN_SPREAD_K, "project_years": years,
            "formula": "悲观/乐观 = 中性 ∓ %sσ×√(1/投影年数 + 1/历史年数)" % SCEN_SPREAD_K,
            "note": ("中性为该基金历史实现年化总回报；σ 为年化波动率；两者均由%s日序列算出，"
                     "样本 %d 个交易日（约 %.2f 年）。差价倍率 %sσ（已不是 1σ 置信区间，"
                     "因样本短、1σ 区间过宽而收窄），为本看板自定口径，非统计推断最优解，"
                     "样本越长越可靠。"
                     % ("累计净值" if use_acc else "单位净值", n_days, n_years, SCEN_SPREAD_K))}
    return rates, meta


def annual_dividend_yield(dividends, nav):
    """由公开分红全历史推算【年度分红率】。

    口径：平均每份派发额 × 年派发频次 ÷ 当前净值。
    频次由相邻派发日的中位间隔换算（次/年 = 365.25 / 中位间隔天数）。

    为什么不直接取「近 12 个月派发合计」：新基金上市可能不足 12 个月
    （如 024564 只有 2 笔），直接取合计会把年化水平严重低估。
    """
    if not nav or not dividends or len(dividends) < 2:
        return None
    ds = sorted([d for d in dividends
                 if d.get("ex_date") and d.get("per_share") is not None],
                key=lambda d: d["ex_date"])
    if len(ds) < 2:
        return None
    try:
        dates = [datetime.strptime(str(d["ex_date"])[:10], "%Y-%m-%d").date() for d in ds]
    except Exception:
        return None
    gaps = sorted((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1))
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    n = len(gaps)
    median = float(gaps[n // 2]) if n % 2 else (gaps[n // 2 - 1] + gaps[n // 2]) / 2.0
    if median <= 0:
        return None
    per_year = 365.25 / median
    avg_ps = sum(float(d["per_share"]) for d in ds) / len(ds)
    annual_ps = avg_ps * per_year
    return {"count": len(ds), "median_gap_days": round(median, 1),
            "per_year": round(per_year, 2), "avg_per_share": round(avg_ps, 4),
            "annual_per_share": round(annual_ps, 4),
            "yield": round(annual_ps / nav, 4)}


def project_dividends(shares0, nav0, weekly, y, rate, years=None, weeks_per_year=52, nav_growth=True):
    """按【周】推演未来每年可得分红（毛额，红利再投前）。

    【为什么不能拿既有的 r 直接当净值增速】
    既有三情景的 r 是【总回报】（含分红）。分红型基金除息后净值会掉，
    若再用 r 去推净值，等于把分红算了两遍。正确做法是把 r 拆开：

        周利率 r_w = (1 + r/12)^(12/52) - 1   ← 52 周累计恰好等于 12 个月的累计
        周分红率 y_w = (1 + y)^(1/52) - 1
        净值周增速 g_w = (1 + r_w) / (1 + y_w) - 1

    于是 (1 + y_w)(1 + g_w) = 1 + r_w —— 总市值按 r_w 逐周复利，
    与既有 project_longterm() 的月复利终值对齐，两边自洽、不重复计算。

    每周（顺序很关键）：
        当前净值 = 当前净值 × (1 + g_w)
        当周分红 = 持有份额 × 当前净值 × y_w              （毛额，红利再投前）
        持有份额 = 持有份额 × (1 + y_w) + 周定投额 / 当前净值  （× (1+y_w) 即红利再投）

    ⚠️ 【这个口径等价于什么】y_w 为常量，意味着「每份分红额 = 当前净值 × 分红率」——
    即**假设每份分红额与净值同比例增长**。基金实际宣告的是每份金额（如 0.011 元），
    所以本口径隐含了「每份分红额会跟着净值涨」这一假设，**故逐年输出 per_share 以便核对**。
    若改为「每份分红额固定」，把 y_w 换成 q_t/净值 即可（q_t = 当期每份分红额），
    总回报恒等式 (1+q/nav)(1+g)=1+r 依然精确成立。

    顺序为什么是「先涨净值、再分红、最后定投」：既有的 project_longterm() 是
    bal = bal*(1+r/12) + monthly，即当月投入的钱当月不产生收益。按上面的顺序，
    市值递推恰好是 V_w = V_{w-1}(1 + r_w) + 周定投额。顺序反过来会让定投多赚一期，
    出现系统性的小幅高估。

    y 由 annual_dividend_yield() 从公开分红全历史推算；
    定投额直接取计划里的每周金额（不再折成月），年投入 = 周金额 × 52。

    【nav_growth=False —— 纯现金流口径（用户 2026-09-18 要求）】
    净值固定在当前值（g_w = 0），不掺任何净值涨跌，于是：
        每周分红 = 份额 × nav0 × y_w     ← 每份分红额 = nav0 × y_w，恒定
        份额    += 份额 × y_w + 周定投额 / nav0
        → 年末份额 × nav0 ≡ 期初市值 + 累计投入 + 累计分红   （恒等，因为所有新份额都按 nav0 买入）
    此时 rate 参数不参与计算，三档情景结果完全相同——表格只用中性那一份。
    """
    years = years or DIVPROJ_YEARS
    wpy = int(weeks_per_year)
    weekly = float(weekly)
    r_w = (1 + rate / 12) ** (12.0 / wpy) - 1
    y_w = (1 + y) ** (1.0 / wpy) - 1
    g_w = ((1 + r_w) / (1 + y_w) - 1) if nav_growth else 0.0
    shares, nav = float(shares0), float(nav0)
    rows, cum = [], 0.0
    for yr in range(1, years + 1):
        div_year, ps_year = 0.0, 0.0
        for _ in range(wpy):
            nav = nav * (1 + g_w)
            div_year += shares * nav * y_w
            ps_year += nav * y_w                       # 当年每份分红额（元/份）
            shares = shares * (1 + y_w) + (weekly / nav if nav else 0)
        cum += div_year
        rows.append({"year": yr, "nav": round(nav, 4), "shares": round(shares, 2),
                     "dividend": round(div_year, 2), "cum_dividend": round(cum, 2),
                     "per_share": round(ps_year, 4)})
    return {"weekly": round(weekly, 2), "weeks_per_year": wpy,
            "annual_contrib": round(weekly * wpy, 2),
            "cum_contrib": round(weekly * wpy * years, 2), "yearly": rows}


def build_dividend_projection(shares0, nav0, dca, dividends, years=None, rates=None, rate_meta=None):
    """组装某只基金的年度分红推演（三情景）。

    没有定投计划、算不出分红率、或拿不到三档年化回报（rates）→ 返回 {}，前端据此不渲染该区块。
    rates 由 derive_scenario_rates() 按该基金自身历史推导，随数据一并下发，前端不再写死。
    """
    plan = (dca or {}).get("plan") or {}
    if not plan or not shares0 or not nav0 or not rates:
        return {}
    weekly = to_float(plan.get("amount"), 0) or 0
    if weekly <= 0:
        return {}
    dy = annual_dividend_yield(dividends, nav0)
    if not dy or not dy.get("yield"):
        return {}
    years = years or DIVPROJ_YEARS
    # 【纯现金流口径】净值固定不涨跌 → 三档结果完全相同，表格只用中性那一份；
    # 三档年化只服务于「持仓金额」那张图（数据来自 project_longterm 的 projection）。
    scen = {label: project_dividends(shares0, nav0, weekly, dy["yield"], rates[label], years,
                                     nav_growth=False)
            for label in SCEN_LABELS if rates.get(label) is not None}
    # 把「每份分红额固定」这个口径显式写出来
    mid_yr = ((scen.get("中性") or {}).get("yearly") or [])
    ps_note, ps_mult = "", None
    if mid_yr and mid_yr[0].get("per_share"):
        ps_mult = 1.0
        ps_note = ("　本表为【纯现金流口径】：净值固定为当前值、不涨跌，"
                   "每份分红额固定为「当前净值 × 股息率」= %.4f 元/年（不再假设它随净值增长）。"
                   "因此表中资产恒等于「期初市值 + 累计投入 + 累计分红」，不含任何净值收益；"
                   "分红为毛额（再投前），未计申赎费与税费。" % mid_yr[0]["per_share"])
    return {
        "years": years,
        "assumptions": {
            "dividend_yield": dy["yield"],
            "dividend_basis": dy,
            "rates": {k: rates.get(k) for k in SCEN_LABELS},
            "rate_meta": rate_meta or {},
            "reinvest": True,
            "nav_growth": False,
            "weekly": round(weekly, 2),
            "weeks_per_year": 52,
            "annual_contrib": round(weekly * 52, 2),
            "per_share_first": (mid_yr[0].get("per_share") if mid_yr else None),
            "per_share_last": (mid_yr[-1].get("per_share") if mid_yr else None),
            "per_share_multiple": ps_mult,
            "note": ("按每周 ¥%.2f 的定投计划推演，红利全部再投；金额为毛额（红利再投前）。"
                     "分红率由公开分红全历史推算（平均每份 × 年派发频次 ÷ 当前净值）。%s"
                     % (weekly, ps_note)),
        },
        "scenarios": scen,
    }


# ---------------------------------------------------------------- 五维评分卡
def five_dimension_score(kind, data):
    """五维：基本面 / 技术面 / 量价配合 / 资金面 / 信息面
    权重默认等权；每维由子指标归一到 0-100，取不到的子指标标 暂无"""
    dims = {}
    det = {}

    if kind == "场内":
        t, td = score_technical(data.get("kline"), data.get("now"))
        dims["技术面"] = t
        det["技术面"] = td
        v, vd = score_valuation(data.get("pe"), data.get("pe_static"), data.get("pb"),
                                data.get("dividend_yield"), (data.get("support") or {}).get("pos_pct_1y"))
        dims["基本面"] = v
        det["基本面"] = vd
        vp, vpd = score_volume_price(data.get("kline"), data.get("now"), data.get("prev_close"))
        dims["量价配合"] = vp
        det["量价配合"] = vpd
        # 资金面：需 MCP 深度资料，公开源缺 -> 如实标注
        dims["资金面"] = None
        det["资金面"] = {"note": "暂无数据：需接入通达信资金流向接口"}
        dims["信息面"] = None
        det["信息面"] = {"note": "暂无数据：需接入通达信研报/题材接口"}
    else:
        q, qd = score_fundamental_quality(None, data.get("fund"))
        dims["基本面"] = q
        det["基本面"] = qd
        # 基金技术面：用净值序列
        navs = (data.get("fund") or {}).get("nav_series") or []
        kl = [{"date": x["date"], "open": x["nav"], "close": x["nav"],
               "high": x["nav"], "low": x["nav"], "volume": 1} for x in navs[-300:]]
        t, td = score_technical(kl, data.get("now"))
        dims["技术面"] = t
        det["技术面"] = td
        dims["量价配合"] = None
        det["量价配合"] = {"note": "场外基金无成交量数据，不适用"}
        dims["资金面"] = None
        det["资金面"] = {"note": "暂无数据"}
        dims["信息面"] = None
        det["信息面"] = {"note": "暂无数据"}

    valid = {k: v for k, v in dims.items() if v is not None}
    overall = round(sum(valid.values()) / len(valid), 1) if valid else None
    return dims, det, overall


# ---------------------------------------------------------------- 主流程
def downsample(seq, target=120, keep_tail=0):
    """等步长下采样，保留首尾与整体形状；序列短于 target 时原样返回。

    用于压缩导出体积（手机图表 120 个点已足够，原 500 点属过剩）。
    keep_tail>0 时强制保留最后 keep_tail 条（如最近净值/K线）。
    """
    if not seq or len(seq) <= target:
        return seq or []
    n = len(seq)
    step = n / float(target)
    out, seen = [], set()
    i = 0.0
    while int(i) < n:
        idx = int(i)
        if idx not in seen:
            seen.add(idx)
            out.append(seq[idx])
        i += step
    if n - 1 not in seen:
        out.append(seq[-1])
    if keep_tail:
        tail = seq[-keep_tail:]
        have = set(id(x) for x in out[-keep_tail:])
        for x in tail:
            if x not in out[-keep_tail:]:
                out.append(x)
    return out


def main():
    # 先取最新底表。未配置 KANBAN_BASE_URL 时这一步什么都不做（本地行为不变）。
    _src = pull_base_tables()
    if _src.get("enabled"):
        print("=== 底表来源（远端）===")
        print("  已更新  : %s" % (", ".join(_src["updated"]) or "无"))
        print("  未变化  : %s" % (", ".join(_src["unchanged"]) or "无"))
        print("  失败保留本地: %s" % (", ".join(_src["failed"]) or "无"))
        print()

    holdings = load_csv("holdings.csv")
    dca_plans = {r["code"]: r for r in load_csv("dca_plan.csv")}
    # 用户确认的实际定投成交记录（覆盖回放值）
    dca_actual = {}
    for r in load_csv("dca_actual.csv"):
        dca_actual.setdefault(r.get("code", ""), []).append(r)
    div_policies = {r["code"]: r.get("dividend_mode", "") for r in load_csv("dividend_policy.csv")}
    # 用户确认的实际分红再投记录（优先于公开数据估算）
    div_records = {}
    for r in load_csv("dividend_records.csv"):
        div_records.setdefault(r.get("code", ""), []).append(r)
    today = date.today()

    rows, detail = [], {}
    total_mv = total_cost = total_day = 0.0
    pending = []          # 缺成本价的持仓
    fund_mv = stock_mv = etf_mv = 0.0

    for h in holdings:
        code = (h.get("code") or "").strip()
        typ = (h.get("type") or "").strip()
        shares = to_float(h.get("shares"), 0)
        cost = to_float(h.get("cost_price"), 0)
        rec = {"type": typ, "code": code, "name": h.get("name"), "shares": shares,
               "cost_price": cost, "account": h.get("account", ""), "cost": round(shares * cost, 2)}

        if typ == "基金":
            f = fetch_fund(norm_fund_code(code), display_code=code)
            nav = f.get("nav")
            rec.update({"source": f.get("source"), "nav": nav, "nav_date": f.get("nav_date"),
                        "now": nav, "day_pct": f.get("day_pct"), "manager": f.get("manager"),
                        "manager_worktime": f.get("manager_worktime"),
                        "manager_fundsize": f.get("manager_fundsize"),
                        "manager_power": f.get("manager_power"),
                        "manager_power_cats": f.get("manager_power_cats"),
                        "manager_power_data": f.get("manager_power_data"),
                        "scale": f.get("scale"), "scale_date": f.get("scale_date"),
                        "scale_mom": f.get("scale_mom"), "acc_nav": f.get("acc_nav"),
                        "syl_1y": f.get("syl_1y"), "syl_3y": f.get("syl_3y"),
                        "syl_6y": f.get("syl_6y"), "syl_1n": f.get("syl_1n"),
                        "min_buy": f.get("min_buy"), "fund_rate": f.get("fund_rate")})
            rec["name"] = rec["name"] or f.get("name")
            rec["market_value"] = round(nav * shares, 2) if nav else None
            rec["profit"] = round((nav - cost) * shares, 2) if (nav and cost) else None
            rec["ret_pct"] = round((nav / cost - 1) * 100, 2) if nav and cost else None
            rec["day_pnl"] = round(nav * (f.get("day_pct") or 0) / 100 * shares, 2) if nav else None
            d = calc_dca(dca_plans.get(code), f.get("nav_series"))
            # 用用户确认的实际成交覆盖回放值（真实净值/份额优先）
            acts = dca_actual.get(code) or []
            if d and acts:
                by_date = {a.get("deal_date"): a for a in acts}
                applied = []
                for det in d.get("detail", []):
                    a = by_date.get(det.get("deal_date"))
                    if a:
                        det["replay_nav"] = det.get("nav")
                        det["replay_shares"] = det.get("shares")
                        det["nav"] = to_float(a.get("nav"), det.get("nav"))
                        det["shares"] = to_float(a.get("shares"), det.get("shares"))
                        det["confirmed"] = True
                        det["source"] = a.get("source")
                        applied.append(det.get("deal_date"))
                if applied:
                    # 用覆盖后的明细重算合计
                    d["shares"] = round(sum(x["shares"] for x in d["detail"]), 2)
                    inv = sum((x.get("net") or x.get("amount") or 0) for x in d["detail"])
                    d["invested"] = round(sum(x.get("amount") or 0 for x in d["detail"]), 2)
                    ln = d.get("last_nav") or 0
                    d["value"] = round(d["shares"] * ln, 2)
                    d["profit"] = round(d["value"] - d["invested"], 2)
                    d["ret_pct"] = round((d["value"] / d["invested"] - 1) * 100, 2) if d["invested"] else None
                    d["avg_cost"] = round(d["invested"] / d["shares"], 4) if d["shares"] else None
                    d["confirmed_dates"] = applied
                    d["note"] = "含 %d 笔用户确认的实际成交（已覆盖回放值）" % len(applied)
            # 以用户实际填报的份额/成本为准（定投回放仅作规划参考）
            if d and shares:
                actual_cost = to_float(cost, 0)
                d["actual_shares"] = round(shares, 2)
                # 定投开始前的存量份额（用于核对：存量 + 定投回算 = 建议份额）
                d["base_shares"] = round(to_float((dca_plans.get(code) or {}).get("base_shares"), 0), 2)
                d["actual_cost_price"] = round(actual_cost, 4)
                d["actual_invested"] = round(shares * actual_cost, 2)
                d["actual_value"] = round(shares * (nav or 0), 2)
                d["actual_profit"] = round(shares * ((nav or 0) - actual_cost), 2)
                d["actual_ret_pct"] = round((nav / actual_cost - 1) * 100, 2) if nav and actual_cost else None
                if d.get("periods"):
                    d["replay_shares"] = d.get("shares")
                    d["replay_invested"] = d.get("invested")
                    d["share_diff"] = round(d["replay_shares"] - shares, 2)
            rec["dca"] = d
            open_date = (h.get("open_date") or "").strip() or None   # 建仓日，早于此日的分红不计入
            dv = calc_dividend_reinvest(f.get("dividends") or [], shares,
                                        div_policies.get(code), since=open_date)
            # 叠加用户确认的实际再投记录（权威值，优先展示）
            recs = div_records.get(code) or []
            if recs:
                # 用完整净值序列建日期索引（不用被下采样的序列）
                nav_by_date = {}
                for x in (f.get("nav_series") or []):
                    if x.get("nav"):
                        nav_by_date[x["date"]] = x["nav"]
                div_by_ex = {x.get("ex_date"): x for x in (f.get("dividends") or [])}

                confirmed_shares = 0.0     # 累计再投份额
                confirmed_cost = 0.0       # 累计再投成本（按当时净值）
                enriched = []
                for r in recs:
                    sh = to_float(r.get("reinvest_shares"), 0)
                    # 优先用记录里的净值；否则依次尝试 确认日 / 除息日 / 除息日前一交易日
                    nav_at = to_float(r.get("reinvest_nav"), 0) or None
                    src = "记录值" if nav_at else None
                    if not nav_at:
                        for probe in (r.get("confirm_date"), r.get("ex_date")):
                            if probe and nav_by_date.get(probe):
                                nav_at = nav_by_date[probe]; src = "确认日净值" if probe == r.get("confirm_date") else "除息日净值"
                                break
                    if not nav_at:
                        # 找除息日之前最近的一个交易日净值
                        exd = r.get("ex_date") or ""
                        prev = [d for d in nav_by_date if d <= exd]
                        if prev:
                            nav_at = nav_by_date[max(prev)]; src = "除息前最近净值"
                    cost = round(sh * nav_at, 2) if nav_at else None
                    confirmed_shares += sh
                    if cost:
                        confirmed_cost += cost
                    enriched.append({**r, "nav_at_reinvest": nav_at,
                                     "nav_source": src, "cost_at_reinvest": cost})
                dv["confirmed_records"] = enriched
                dv["confirmed_reinvest_shares"] = round(confirmed_shares, 2)
                dv["confirmed_reinvest_cost"] = round(confirmed_cost, 2)
                dv["confirmed_cost_covered"] = round(
                    sum(1 for x in enriched if x.get("cost_at_reinvest")) / len(enriched) * 100, 0)
                dv["confirmed_fee"] = round(sum(to_float(r.get("fee"), 0) for r in recs), 2)
                # 逐笔还原派息基数并校验自洽性
                dv["reconcile"] = [
                    reconcile_dividend_record(r, f.get("dividends") or [], d, shares)
                    for r in recs
                ]
                ok = all(x.get("consistent") for x in dv["reconcile"])
                dv["note"] = ("份额已含历史再投；%d 条用户确认的实际再投记录（+%.2f 份，成本 ¥%.2f，%.0f%% 有净值）"
                              % (len(recs), confirmed_shares, confirmed_cost,
                                 dv["confirmed_cost_covered"]))
            rec["dividend"] = dv
            # 三情景年化回报：由该基金自身历史推导（不再是全站统一的写死整数）
            scen_rates, scen_meta = derive_scenario_rates(
                f.get("nav_series"), f.get("acc_series"))
            if not scen_rates:
                WARNINGS.append("%s %s 未生成长期预测：%s"
                                % (code, rec.get("name") or "", (scen_meta or {}).get("reason")))
            # 图上的「预计持仓金额」曲线与推演表对齐，都是未来 30 年（原为函数默认的 10 年，
            # 用户 2026-09-18 要求改 30 年；三档差价收窄到 0.2σ 后，30 年也不会把纵轴撑废）
            rec["projection"] = project_longterm(d, f.get("dividends") or [],
                                                 years=DIVPROJ_YEARS,
                                                 since=open_date, rates=scen_rates,
                                                 rate_meta=scen_meta)
            # 年度分红推演（未来 30 年）。只进 detail，不进 positions.json，
            # 免得把行数据撑大——前端本来就从 detail 取图表数据。
            div_proj = build_dividend_projection(rec.get("shares"), nav, d,
                                                 f.get("dividends") or [],
                                                 rates=scen_rates, rate_meta=scen_meta)
            dims, det, overall = five_dimension_score("场外", {"fund": f, "now": nav})
            rec["dims"], rec["overall_score"] = dims, overall
            detail[code] = {"fund": {k: v for k, v in f.items() if k not in ("nav_series", "acc_series")},
                            "nav_series": downsample(f.get("nav_series", [])[-500:], 120),
                            "dims_detail": det, "dims": dims, "dividends": f.get("dividends", []),
                            "dividend": dv,
                            "dca": d, "projection": rec["projection"],
                            "dividend_projection": div_proj, "type": typ}
            fund_mv += rec["market_value"] or 0
        else:
            s = fetch_stock(code)
            now = s.get("now")
            rec.update({"source": s.get("source"), "now": now, "prev_close": s.get("prev_close"),
                        "pct": s.get("pct"), "pe": s.get("pe"), "pb": s.get("pb"),
                        "pe_static": s.get("pe_static"), "turnover": s.get("turnover"),
                        "volume_ratio": s.get("volume_ratio"), "total_mv": s.get("total_mv"),
                        "amplitude": s.get("amplitude"), "open": s.get("open"),
                        "high": s.get("high"), "low": s.get("low")})
            rec["name"] = rec["name"] or s.get("name")
            rec["market_value"] = round(now * shares, 2) if now else None
            rec["profit"] = round((now - cost) * shares, 2) if (now and cost) else None
            rec["ret_pct"] = round((now / cost - 1) * 100, 2) if now and cost else None
            rec["day_pnl"] = round((now - s.get("prev_close", now)) * shares, 2) if now else None
            sr = calc_support_resistance(s.get("kline"), now)
            rec.update({"support": sr, "volatility": calc_volatility(s.get("kline")),
                        "rsi": calc_rsi(s.get("kline"))})
            dv = {"count": 0, "cash": 0, "mode": div_policies.get(code, ""), "note": "股息明细待接入"}
            rec["dividend"] = dv
            dims, det, overall = five_dimension_score("场内", {
                "kline": s.get("kline"), "now": now, "prev_close": s.get("prev_close"),
                "pe": s.get("pe"), "pe_static": s.get("pe_static"), "pb": s.get("pb"),
                "support": sr, "dividend_yield": None})
            rec["dims"], rec["overall_score"] = dims, overall
            detail[code] = {"kline": downsample(s.get("kline", [])[-300:], 120),
                            "dims_detail": det, "dims": dims, "support": sr, "type": typ,
                            "dividends": s.get("dividends", [])}
            if typ == "ETF":
                etf_mv += rec["market_value"] or 0
            else:
                stock_mv += rec["market_value"] or 0

        total_mv += rec["market_value"] or 0
        # 缺少成本价的持仓（如用户未提供），其市值计入总市值，但不计入成本/收益，
        # 避免把市值当成收益。单独记录以便页面提示。
        if rec.get("cost"):
            total_cost += rec["cost"]
        else:
            pending.append({"code": rec.get("code"), "name": rec.get("name"),
                            "market_value": rec.get("market_value")})
        total_day += rec["day_pnl"] or 0
        rows.append(rec)
        time.sleep(0.3)

    pending_mv = round(sum(x.get("market_value") or 0 for x in pending), 2)
    summary = {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "total_mv": round(total_mv, 2), "total_cost": round(total_cost, 2),
               "total_profit": round(total_mv - pending_mv - total_cost, 2),
               "total_ret_pct": round(((total_mv - pending_mv) / total_cost - 1) * 100, 2) if total_cost else None,
               "pending_cost": pending,
               "pending_cost_mv": pending_mv,
               "total_day_pnl": round(total_day, 2),
               "total_day_pct": round(total_day / (total_mv - total_day) * 100, 2) if total_mv else None,
               "alloc": {"基金": round(fund_mv, 2), "股票": round(stock_mv, 2), "ETF": round(etf_mv, 2)},
               "warnings": WARNINGS}

    # 写 metrics.csv
    cols = ["type", "code", "name", "shares", "cost_price", "cost", "now", "nav_date", "day_pct",
            "market_value", "profit", "ret_pct", "day_pnl", "pe", "pb", "overall_score",
            "s1_基本面", "s2_技术面", "s3_量价配合", "s4_资金面", "s5_信息面",
            "support1", "resistance1", "volatility", "dividend_mode"]
    with open(os.path.join(DATA, "metrics.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            d = r.get("dims") or {}
            sr = r.get("support") or {}
            w.writerow({"type": r["type"], "code": r["code"], "name": r["name"],
                        "shares": r["shares"], "cost_price": r["cost_price"], "cost": r["cost"],
                        "now": r.get("now"), "nav_date": r.get("nav_date"), "day_pct": r.get("day_pct"),
                        "market_value": r.get("market_value"), "profit": r.get("profit"),
                        "ret_pct": r.get("ret_pct"), "day_pnl": r.get("day_pnl"),
                        "pe": r.get("pe"), "pb": r.get("pb"),
                        "overall_score": r.get("overall_score"),
                        "s1_基本面": d.get("基本面"), "s2_技术面": d.get("技术面"),
                        "s3_量价配合": d.get("量价配合"), "s4_资金面": d.get("资金面"),
                        "s5_信息面": d.get("信息面"),
                        "support1": sr.get("support1"), "resistance1": sr.get("resistance1"),
                        "volatility": r.get("volatility"),
                        "dividend_mode": (r.get("dividend") or {}).get("mode")})

    with open(os.path.join(DATA, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA, "positions_detail.json"), "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    with open(os.path.join(DATA, "positions.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=2)

    print("=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n=== 持仓 ===")
    for r in rows:
        print("%-4s %-8s %-22s 现价%-9s 市值%-14s 收益%-10s %s%%  综合分%s" % (
            r["type"], r["code"], (r["name"] or "")[:20], r.get("now"), r.get("market_value"),
            r.get("profit"), r.get("ret_pct"), r.get("overall_score")))
    if WARNINGS:
        print("\n=== 警告（空数据，未编造）===")
        for w_ in WARNINGS:
            print(" -", w_)


if __name__ == "__main__":
    main()
