"""
抓取 Dota 2 官方补丁列表，维护各版本号与更新日期 data/patch_date.json。

数据源：
    https://www.dota2.com/datafeed/patchnoteslist
    返回 patches 数组，每项含 patch_number（版本号）与 patch_timestamp（发布时间），
    一次性返回全部历史版本，无需分页。

输出：data/patch_date.json，{版本号: 更新日期}，按日期倒序。
      日期取补丁发布时间的北京时间（UTC+8）日期；
      与存量合并：同一版本保留首次记录的日期，历史版本不会丢失。

联动：
    - 检测到最新版本号相对上次变化时，向 GITHUB_OUTPUT 输出 new_patch=true，
      由 patch_date.yml 触发一次 talents_cn.yml。
    - 输出 latest / latest_date / days_since 供工作流日志与后续节奏判断使用。
      d2pt_build.yml 的构建节奏由 fetch_d2pt_build.py 读取本文件决定。
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(ROOT, "data", "patch_date.json")

PATCHES_API = "https://www.dota2.com/datafeed/patchnoteslist"
# 项目数据统一使用北京时间
BEIJING_TZ = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# 版本号：7.42、7.41f、7.13b
VERSION_RE = re.compile(r"\d\.\d{2}[a-z]?")


def fetch_patches():
    """请求补丁列表。"""
    req = urllib.request.Request(PATCHES_API, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as response:
        data = json.loads(response.read())
    patches = data.get("patches") if isinstance(data, dict) else None
    if not isinstance(patches, list) or not patches:
        raise RuntimeError("补丁列表响应格式异常")
    return patches


def parse_patches(items):
    """解析 {版本号: 更新日期(UTC+8)}，同一版本保留最早日期。"""
    patches = {}
    for item in items:
        version = str(item.get("patch_number") or "").strip()
        ts = item.get("patch_timestamp")
        if not VERSION_RE.fullmatch(version) or not isinstance(ts, (int, float)):
            continue
        date = datetime.fromtimestamp(ts, BEIJING_TZ).strftime("%Y-%m-%d")
        if version not in patches or date < patches[version]:
            patches[version] = date
    return patches


def _version_key(version):
    """版本号排序键（同日期时按版本号大小排序）。"""
    match = re.fullmatch(r"(\d)\.(\d{2})([a-z]?)", version)
    if not match:
        return (0, 0, "")
    return (int(match.group(1)), int(match.group(2)), match.group(3))


def load_patches():
    """读取存量数据；缺失或损坏时返回空字典。"""
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def merge_patches(existing, fetched):
    """合并新抓取的版本：存量优先（版本日期以首次记录为准），按日期倒序。"""
    merged = dict(existing)
    for version, date in fetched.items():
        merged.setdefault(version, date)
    return dict(sorted(
        merged.items(),
        key=lambda kv: (kv[1], _version_key(kv[0])),
        reverse=True,
    ))


def _latest(patches):
    """返回 (最新版本号, 日期)，空数据返回 None。"""
    if not patches:
        return None
    return max(patches.items(), key=lambda kv: (kv[1], _version_key(kv[0])))


def save_patches(patches):
    """原子写入 data/patch_date.json。"""
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    tmp_file = OUTPUT_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(patches, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_file, OUTPUT_FILE)
    print(f"已保存 {len(patches)} 个版本到 {OUTPUT_FILE}")


def _write_github_output(**values):
    """写入 GitHub Actions 步骤输出（本地运行无 GITHUB_OUTPUT 时忽略）。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def main():
    existing = load_patches()
    previous_latest = _latest(existing)

    fetched = parse_patches(fetch_patches())
    if not fetched:
        raise SystemExit("补丁列表中未解析出任何版本号，保留原数据")

    merged = merge_patches(existing, fetched)
    save_patches(merged)

    latest_version, latest_date = _latest(merged)
    new_patch = bool(previous_latest and latest_version != previous_latest[0])
    days_since = (datetime.now(BEIJING_TZ).date()
                  - datetime.strptime(latest_date, "%Y-%m-%d").date()).days

    added = {v: d for v, d in fetched.items() if v not in existing}
    print(f"最新版本: {latest_version}（{latest_date}，距今 {days_since} 天）")
    print(f"本次新增版本: {added or '无'}")
    print(f"新版本标记: {new_patch}")

    _write_github_output(
        new_patch="true" if new_patch else "false",
        latest=latest_version,
        latest_date=latest_date,
        days_since=days_since,
    )


if __name__ == "__main__":
    main()
