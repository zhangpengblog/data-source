#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""理财看板 · 数据同步器

====================================================================
设计目标：定时任务只更新数据，绝不重建、绝不碰前端
====================================================================

【两条铁律】

1. 定时任务不依赖任何可能拿不到的工具，只用命令行与标准库。
   旧版让定时任务用 MCP（网盘 tdrive）取配置，但网盘连接器不在定时任务
   可挂载的连接器白名单里，那个沙箱永远拿不到工具。模型拿不到工具，就会
   自己重建项目、重写 index.html、把数据算错 —— 这是旧版所有故障的总根源。

2. index.html 从头到尾不参与流程 —— 结构性地不可能被写坏。
   底层表与产出只经手 data/ 与脚本，前端读的路径永远不变，一个字都不用改。

【底表来源】—— 待重新确定

  download 步骤需要一个能取到人维护底表（holdings.csv 等）的来源，
  由环境变量 KANBAN_BASE 指定。

  该来源与刷新方式目前尚未确定。未配置 KANBAN_BASE 时，download 会直接
  失败并给出提示，**不会去访问任何遗留地址**。在来源确定前，若底表已在
  项目目录内，可跳过 download，直接跑 fetch / validate / push。

【三层守卫】
  第 1 层：拿到旧数据（download）—— 既是基线，也是回滚材料
  第 2 层：跑 fetch_data.py 生成新数据（fetch）
  第 3 层：新旧合理性对比（validate）—— 不过就拒绝写入，宁可这轮不刷新

用法：
  python3 sync.py all        # 一键跑完整流程（定时任务就用这个）
  python3 sync.py download   # 只从底表来源拉底表 + 旧数据
  python3 sync.py fetch      # 只跑 fetch_data.py
  python3 sync.py validate   # 只做合理性校验
  python3 sync.py push       # 只打印写入清单
  python3 sync.py selftest   # 自检：确认前端文件不在任何写入路径上

环境变量：
  KANBAN_BASE   底表来源根地址。未设置时 download 步骤直接失败并提示。
"""
import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime

# ------------------------------------------------------------------ 配置
# 底表来源根地址。刻意不设默认值：未配置时 download 直接失败并提示，
# 而不是回落到任何历史地址上。
BASE_URL = os.environ.get("KANBAN_BASE", "").rstrip("/")

# 人类维护的底表：从 BASE_URL 下的 assets/ 取（源名 → 落地相对路径）
# 这些是【输入】，只读不写
SOURCE_FILES = [
    ("holdings.csv", "data/holdings.csv"),
    ("dca_plan.csv", "data/dca_plan.csv"),
    ("dividend_policy.csv", "data/dividend_policy.csv"),
    ("dividend_records.csv", "data/dividend_records.csv"),
    ("dca_actual.csv", "data/dca_actual.csv"),
    ("fetch_data.py", "fetch_data.py"),
]

# 脚本产出的数据文件：这是唯一的【输出】，要写回数据目录
OUTPUT_FILES = [
    "data/summary.json",
    "data/positions.json",
    "data/positions_detail.json",
    "data/metrics.csv",
]

# 旧数据基线：从底表来源的 data/ 取，用于新旧对比 + 回滚
BASELINE_FILES = ["summary.json", "positions.json"]

# 永久黑名单：这些是前端与人类资产，任何情况下都不写
# 注意 index.html 不在此列表里，因为 write_outputs() 只认 OUTPUT_FILES 白名单，
# 黑名单是第二道保险
FORBIDDEN_OUTPUTS = ["index.html", "index.htm", "README.md"]

# 合理性校验阈值
MAX_MV_DRIFT = 0.25        # 总市值相对旧值的最大偏离（25%）
MAX_DAY_PNL_RATIO = 0.15   # 当日盈亏绝对值不得超过总市值的 15%
MIN_HOLDINGS = 1           # 至少要有 1 条持仓


def log(m):
    print("[sync] %s" % m, flush=True)


def die(m, code=1):
    log("失败：%s" % m)
    print("SYNC_FAILED: %s" % m)
    sys.exit(code)


def http_get(url, timeout=30, binary=True):
    """GET 一个 URL。返回 bytes 或 None（失败时记录而不抛异常）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "kanban-sync/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        log("  取数失败 %s → %s" % (url, e))
        return None


def workdir(arg=""):
    if arg:
        os.makedirs(arg, exist_ok=True)
        return arg
    d = tempfile.mkdtemp(prefix="kanban-")
    log("工作目录 %s" % d)
    return d


# ------------------------------------------------------------------ 第 1 步
def download(wd):
    """从底表来源取底表 + 旧数据基线。

    底表来自 <BASE_URL>/assets/（人类维护的只读快照）
    旧数据来自 <BASE_URL>/data/（上一版产出，用于对比与回滚）
    """
    if not BASE_URL:
        die("未配置底表来源（环境变量 KANBAN_BASE 为空），download 步骤无法执行。\n"
            "来源确定后设置 KANBAN_BASE 即可；\n"
            "若底表已在项目目录内，可跳过本步，直接跑 fetch / validate / push。")

    log("第 1 步 · 从底表来源取底表与旧数据   %s" % BASE_URL)

    missing = []
    for src, rel in SOURCE_FILES:
        url = "%s/assets/%s" % (BASE_URL, src)
        raw = http_get(url)
        if raw is None or len(raw) == 0:
            missing.append(src)
            continue
        p = os.path.join(wd, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(raw)
        log("  ✅ %-22s %6d 字节" % (rel, len(raw)))

    if missing:
        die("以下底表在 %s/assets/ 取不到：%s\n"
            "请先确认底表来源的 assets/ 已就位（这是本步骤的唯一输入源）。"
            % (BASE_URL, ", ".join(missing)))

    # 旧数据基线：取不到不算致命（首次运行就是这种情况）
    os.makedirs(os.path.join(wd, "baseline"), exist_ok=True)
    for name in BASELINE_FILES:
        raw = http_get("%s/data/%s" % (BASE_URL, name))
        if raw:
            with open(os.path.join(wd, "baseline", name), "wb") as f:
                f.write(raw)
            log("  ✅ baseline/%-16s %6d 字节" % (name, len(raw)))
        else:
            log("  ⚠️  baseline/%-16s 取不到（首次运行属正常，跳过对比）" % name)

    return True


# ------------------------------------------------------------------ 第 2 步
def fetch(wd):
    """跑 fetch_data.py 生成新数据。脚本是确定性的，模型不得改写它。"""
    log("第 2 步 · 执行 fetch_data.py 抓数")
    r = subprocess.run([sys.executable, "fetch_data.py"], cwd=wd,
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        log("--- fetch_data.py stdout 末尾 ---")
        log((r.stdout or "")[-2000:])
        log("--- stderr 末尾 ---")
        log((r.stderr or "")[-1000:])
        die("fetch_data.py 退出码 %s" % r.returncode)

    got = []
    for rel in OUTPUT_FILES:
        p = os.path.join(wd, rel)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            got.append((rel, os.path.getsize(p)))
    if len(got) != len(OUTPUT_FILES):
        die("产出不完整：期望 %d 个，实得 %d 个" % (len(OUTPUT_FILES), len(got)))
    for rel, size in got:
        log("  ✅ %-28s %7d 字节" % (rel, size))
    return True


# ------------------------------------------------------------------ 第 3 步
def load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def validate(wd):
    """新旧数据合理性对比。任何一项不过 → 拒绝写入。

    这是整套设计里最关键的守卫：定时任务无人值守，一旦代码逻辑飘了、
    或者数据源返回了离谱的数字，宁可这轮不刷新，也不能把错数据推上线。
    """
    log("第 3 步 · 新旧数据合理性校验")

    new = load_json(os.path.join(wd, "data", "summary.json"))
    if not new:
        die("新产出的 summary.json 不可读")
    if not isinstance(new.get("total_mv"), (int, float)):
        die("summary.json 缺 total_mv，产出异常")

    problems = []

    # --- 3.1 结构完整性 ---
    pos = load_json(os.path.join(wd, "data", "positions.json"))
    rows = pos.get("rows") if isinstance(pos, dict) else pos
    n_rows = len(rows or [])
    if n_rows < MIN_HOLDINGS:
        problems.append("持仓条数为 %d，少于下限 %d" % (n_rows, MIN_HOLDINGS))

    detail = load_json(os.path.join(wd, "data", "positions_detail.json"))
    if not detail:
        problems.append("positions_detail.json 不可读或为空")

    # --- 3.2 与旧数据对比 ---
    old = load_json(os.path.join(wd, "baseline", "summary.json"))
    if old:
        old_mv = old.get("total_mv")
        old_n = None
        old_pos = load_json(os.path.join(wd, "baseline", "positions.json"))
        if old_pos is not None:
            o_rows = old_pos.get("rows") if isinstance(old_pos, dict) else old_pos
            old_n = len(o_rows or [])

        if isinstance(old_mv, (int, float)) and old_mv > 0:
            drift = abs(new["total_mv"] - old_mv) / old_mv
            log("  总市值：旧 %.2f → 新 %.2f（偏离 %.2f%%，上限 %.0f%%）"
                % (old_mv, new["total_mv"], drift * 100, MAX_MV_DRIFT * 100))
            if drift > MAX_MV_DRIFT:
                problems.append("总市值偏离旧值 %.1f%%，超过 %.0f%% 阈值"
                                % (drift * 100, MAX_MV_DRIFT * 100))
        else:
            log("  旧 summary 无有效 total_mv，跳过市值对比")

        if old_n is not None:
            log("  持仓条数：旧 %d → 新 %d" % (old_n, n_rows))
            if old_n and n_rows != old_n:
                # 条数变化是"提示"而非"否决"——用户可能真的调仓了
                log("  ⚠️  持仓条数发生变化（若为人工调仓则正常）")
    else:
        log("  无旧数据基线，跳过对比（首次运行属正常）")

    # --- 3.3 内部自洽性 ---
    day_pnl = new.get("total_day_pnl")
    if isinstance(day_pnl, (int, float)) and new["total_mv"] > 0:
        ratio = abs(day_pnl) / new["total_mv"]
        log("  当日盈亏 %.2f，占总市值 %.2f%%（上限 %.0f%%）"
            % (day_pnl, ratio * 100, MAX_DAY_PNL_RATIO * 100))
        if ratio > MAX_DAY_PNL_RATIO:
            problems.append("当日盈亏占总市值 %.1f%%，异常偏高"
                            % (ratio * 100))

    # 成本与市值都非空的持仓要能算出收益
    bad_rows = [r for r in (rows or [])
                if r.get("market_value") is None and r.get("cost")]
    if bad_rows:
        log("  ⚠️  %d 条持仓无市值（数据源可能暂不可用）" % len(bad_rows))

    if problems:
        log("校验未通过：")
        for p in problems:
            log("  ❌ %s" % p)
        die("数据合理性校验未通过，本轮拒绝写入。\n"
            "数据保持上一版不变，页面不受影响。\n"
            "这通常意味着上游数据源异常或代码逻辑有问题，请人工确认后再重试。", code=3)

    log("校验通过 ✅")
    return True


# ------------------------------------------------------------------ 第 4 步
def build_pending(wd):
    """生成 pending 标记（前端不读它，仅供汇报与前端新鲜度提示）"""
    summary = load_json(os.path.join(wd, "data", "summary.json")) or {}
    pending = {
        "pending": True,
        "ok": True,
        "at": summary.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_mv": summary.get("total_mv"),
        "total_profit": summary.get("total_profit"),
        "total_ret_pct": summary.get("total_ret_pct"),
        "warnings": summary.get("warnings") or [],
    }
    with open(os.path.join(wd, "data", "pending.json"), "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)
    return pending


def push(wd):
    """把产出写回数据目录。只会写 OUTPUT_FILES 白名单里的文件。

    写之前先做一次黑名单自检：白名单里若混入前端文件，直接拒绝。
    """
    log("第 4 步 · 写回数据目录")

    # 双重保险：白名单里绝不能出现前端/人类文件
    leaked = [f for f in OUTPUT_FILES
              if os.path.basename(f) in FORBIDDEN_OUTPUTS]
    if leaked:
        die("白名单混入受保护文件，拒绝写入：%s" % leaked)

    # 产出必须存在
    for rel in OUTPUT_FILES:
        p = os.path.join(wd, rel)
        if not (os.path.exists(p) and os.path.getsize(p) > 0):
            die("产出缺失，无法写入：%s" % rel)

    log("  待写入文件（共 %d 个）：" % len(OUTPUT_FILES))
    for rel in OUTPUT_FILES:
        log("    %-28s %7d 字节" % (rel, os.path.getsize(os.path.join(wd, rel))))
    log("  未触碰：index.html、README.md、底表 CSV（结构与权限上就不在写入路径上）")

    # 本步骤只打印清单，不动盘：实际落盘由调用方执行。
    manifest = {
        "action": "write",
        "target_dir": "data/",
        "files": [
            {"local_path": os.path.join(wd, rel),
             "remote_name": os.path.basename(rel),
             "size": os.path.getsize(os.path.join(wd, rel)),
             "md5": hashlib.md5(open(os.path.join(wd, rel), "rb").read()).hexdigest()}
            for rel in OUTPUT_FILES
        ],
    }
    log("写入清单：")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


# ------------------------------------------------------------------ selftest
def selftest():
    """自检：确认前端文件永远不在任何写入路径上。"""
    leaked = [f for f in OUTPUT_FILES if os.path.basename(f) in FORBIDDEN_OUTPUTS]
    if leaked:
        die("白名单混入受保护文件：%s" % leaked)
    log("自检通过 ✅")
    log("  可写入（白名单 %d 个）：%s" % (len(OUTPUT_FILES), ", ".join(OUTPUT_FILES)))
    log("  只读输入（%d 个）：%s" % (len(SOURCE_FILES), ", ".join(s for s, _ in SOURCE_FILES)))
    log("  永不触碰：%s" % ", ".join(FORBIDDEN_OUTPUTS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["all", "download", "fetch", "validate",
                                      "push", "selftest"])
    ap.add_argument("--work", default="")
    a = ap.parse_args()

    if a.stage == "selftest":
        selftest()
        return

    wd = workdir(a.work)
    if a.stage in ("all", "download"):
        download(wd)
    if a.stage in ("all", "fetch"):
        fetch(wd)
    if a.stage in ("all", "validate"):
        validate(wd)
    if a.stage in ("all", "push"):
        pending = build_pending(wd)
        push(wd)
        print()
        log("汇总：")
        print(json.dumps(pending, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
