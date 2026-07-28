"""
GitHub Actions 自动化抓取脚本：定期更新 d2pt_core_build.json。

设计要点：
    - 全量更新：每次运行都重新抓取所有英雄所有位置
    - 仅覆盖有效数据：新数据有效（core_build 非空且无 error）才覆盖原数据
    - 错误保留原数据：新抓取失败时保留数据库中原有的有效数据
    - 增量保存：每完成一个英雄即保存一次，避免中途崩溃丢失进度
    - 重试机制：单个位置最多重试 3 次，指数退避
    - 无代理模式：通过环境变量 D2PT_PROXY="" 禁用 SOCKS5 代理（用于 CI 环境）
    - 容错退出：致命错误（如英雄列表获取失败）时正常退出（exit 0），保留原数据

依赖安装：
    pip install curl_cffi

用法：
    python fetch_d2pt_build.py
"""

import asyncio
import json
import os
import sys
import threading
import time
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

from scrape_d2pt_api import (
    BASE,
    OPENDOTA_ITEMS,
    get_positions_info,
    build_core_items,
    FALLBACK_ITEMS,
)

DATABASE_FILE = "d2pt_core_build.json"
# 最大并发请求数（CI 环境降低并发以减少 Cloudflare 拦截风险）
CONCURRENCY = 4
# 单个请求超时（秒）
REQUEST_TIMEOUT = 30
# 单个位置最大重试次数
MAX_RETRIES = 3
# 代理配置：通过环境变量 D2PT_PROXY 控制
# - 未设置 / 空字符串 → 不使用代理（CI 环境直连）
# - 设置为有效 URL（如 socks5://127.0.0.1:1080）→ 使用该代理（本地开发）
_proxy_url = os.environ.get("D2PT_PROXY", "").strip()
_PROXY = {"https": _proxy_url, "http": _proxy_url} if _proxy_url else None
# 线程局部存储：每个线程独立的同步 Session（线程安全）
_thread_local = threading.local()


def _get_thread_session() -> cffi_requests.Session:
    """获取当前线程的 Session（惰性创建）。"""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = cffi_requests.Session(impersonate="chrome")
    return _thread_local.session


def _hero_slug_from_info(hero_info: dict) -> str:
    """从英雄信息生成 URL slug。

    注意：dota2protracker.com 使用空格（URL编码为 %20），不是连字符。
    """
    name = hero_info.get("displayName") or hero_info.get("npc") or ""
    return name.strip()


def _format_win_rate(rate: float) -> str:
    """将胜率小数转为百分比字符串，如 0.5517 -> "55%"。"""
    return f"{round(rate * 100)}%"


def _is_position_valid(pos_data: dict | None) -> bool:
    """检查位置数据是否有效（有 core_build 且无 error）。"""
    if not isinstance(pos_data, dict):
        return False
    if pos_data.get("error"):
        return False
    return bool(pos_data.get("core_build"))


def load_database() -> dict:
    """加载已有数据库。"""
    if os.path.exists(DATABASE_FILE):
        try:
            with open(DATABASE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  警告: 加载已有数据库失败 ({e})，将重新创建")
    return {}


def save_database(db: dict) -> None:
    """保存数据库到文件（压缩格式，原子写入：先写临时文件再重命名）。"""
    tmp_file = DATABASE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_file, DATABASE_FILE)


def _sync_get_hero_builds(hero_slug: str, hero_id: int, position: str) -> dict:
    """同步获取 hero builds 数据（在线程中执行）。

    流程：
        1. 先访问英雄页面获取 cookie（绕过 Cloudflare）
        2. 请求 /api/hero/{hero_id}/builds?position={position}（带 Referer）
    """
    session = _get_thread_session()
    page_url = f"{BASE}/hero/{quote(hero_slug, safe='-')}"

    # 先访问页面获取 cookie
    session.get(page_url, proxies=_PROXY, timeout=REQUEST_TIMEOUT)

    # 请求 build API（带 Referer）
    pos_enc = position.replace(" ", "+")
    url = f"{BASE}/api/hero/{hero_id}/builds?position={pos_enc}"
    headers = {
        "Referer": page_url,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    r = session.get(url, proxies=_PROXY, timeout=REQUEST_TIMEOUT, headers=headers)
    r.raise_for_status()
    data = r.json()

    # API 返回列表，取第一个（最常见 build）
    # 某些位置比赛数太少时返回空列表，此时返回空结构
    if isinstance(data, list):
        return data[0] if data else {"build_data": {}}
    return data


async def _fetch_hero_builds(hero_slug: str, hero_id: int, position: str,
                             sem: asyncio.Semaphore) -> dict:
    """异步获取 hero builds 数据。"""
    async with sem:
        return await asyncio.to_thread(_sync_get_hero_builds, hero_slug, hero_id, position)


async def _fetch_hero_builds_with_retry(
    hero_slug: str, hero_id: int, position: str, sem: asyncio.Semaphore,
) -> dict:
    """带重试的获取 hero builds 数据（指数退避）。"""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await _fetch_hero_builds(hero_slug, hero_id, position, sem)
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                print(f"      重试 {attempt + 1}/{MAX_RETRIES}（{wait}s 后）: {e}")
                await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def get_item_mapping_async(sem: asyncio.Semaphore) -> dict:
    """异步获取物品映射（OpenDota，无 Cloudflare）。"""
    try:
        async with sem:
            data = await asyncio.to_thread(
                lambda: cffi_requests.Session(impersonate="chrome").get(
                    OPENDOTA_ITEMS, timeout=REQUEST_TIMEOUT
                ).json()
            )
        mapping = {}
        for slug, info in data.items():
            item_id = info.get("id")
            if item_id is not None:
                mapping[item_id] = {
                    "name": info.get("dname") or slug.replace("_", " ").title(),
                    "image": f"/static/items/{slug}.png",
                }
        if mapping:
            print(f"  从 OpenDota 获取 {len(mapping)} 个物品")
            return mapping
    except Exception as e:
        print(f"  OpenDota API 不可用 ({e})，使用内置映射")
    return {
        iid: {"name": name, "image": f"/static/items/{slug}.png"}
        for iid, (name, slug) in FALLBACK_ITEMS.items()
    }


async def fetch_core_build_async(
    hero_slug: str,
    hero_id: int,
    position: str,
    item_mapping: dict,
    sem: asyncio.Semaphore,
) -> dict:
    """异步获取指定英雄指定位置的 Core Item Build 及相关数据（带重试）。

    返回：
        {
            "core_build": [...],       # 核心物品列表（与原来一致）
            "start_items": [...],      # anchor_start_items_new（开局物品）
            "lategame_inventories": [...]  # anchor_lategame_inventories（后期出装）
        }
    """
    build_entry = await _fetch_hero_builds_with_retry(hero_slug, hero_id, position, sem)
    build_data = build_entry.get("build_data", {})

    return {
        "core_build": build_core_items(build_entry, item_mapping),
        "start_items": build_data.get("anchor_start_items_new", []),
        "lategame_inventories": build_data.get("anchor_lategame_inventories", []),
    }


async def build_hero_entry_async(
    hero_info: dict,
    item_mapping: dict,
    existing_entry: dict | None,
    sem: asyncio.Semaphore,
) -> tuple[dict, int, int]:
    """异步抓取单个英雄所有位置的数据（位置间并发）。

    全量更新模式：
        - 新数据有效（core_build 非空）→ 覆盖原数据
        - 新数据无效/抓取失败 → 保留原数据（如果有）
        - 原数据也没有 → 记录 error 占位

    返回 (entry, success_count, failed_count)
    """
    hero_id = hero_info.get("hero_id") or hero_info.get("id")
    hero_slug = _hero_slug_from_info(hero_info)
    display_name = hero_info.get("displayName", f"hero_{hero_id}")
    positions_info = get_positions_info(hero_info)

    existing = existing_entry or {}
    entry: dict = {"displayName": display_name}

    success_count = 0
    failed_count = 0

    # 全量更新：所有位置都重新抓取
    positions_to_fetch = list(positions_info.items())

    # 并发请求所有位置
    tasks = [
        fetch_core_build_async(hero_slug, hero_id, pos, item_mapping, sem)
        for pos, _ in positions_to_fetch
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for (pos, info), result in zip(positions_to_fetch, results):
        if isinstance(result, Exception):
            # 抓取失败：保留原数据（如果有效）
            if _is_position_valid(existing.get(pos)):
                entry[pos] = existing[pos]
                print(f"    {pos}: 抓取失败，保留原数据 ({result})")
            else:
                # 原数据也无效，记录 error 占位
                entry[pos] = {
                    "core_build": [],
                    "match_count": info["match_count"],
                    "win_rate": _format_win_rate(info["win_rate"]),
                    "error": str(result),
                }
                print(f"    {pos}: 抓取失败，无原数据 ({result})")
            failed_count += 1
        elif result and result.get("core_build"):
            # 新数据有效：覆盖原数据
            entry[pos] = {
                "core_build": result["core_build"],
                "start_items": result.get("start_items", []),
                "lategame_inventories": result.get("lategame_inventories", []),
                "match_count": info["match_count"],
                "win_rate": _format_win_rate(info["win_rate"]),
            }
            success_count += 1
            print(f"    {pos}: {len(result['core_build'])} 物品, {info['match_count']} 场, 胜率 {_format_win_rate(info['win_rate'])}")
        else:
            # core_build 为空（位置比赛数太少）：保留原数据
            if _is_position_valid(existing.get(pos)):
                entry[pos] = existing[pos]
                print(f"    {pos}: 新数据为空，保留原数据")
            else:
                entry[pos] = {
                    "core_build": [],
                    "start_items": [],
                    "lategame_inventories": [],
                    "match_count": info["match_count"],
                    "win_rate": _format_win_rate(info["win_rate"]),
                }
                print(f"    {pos}: 新数据为空，无原数据")
            failed_count += 1

    # 自动计算 "Most Played"：比赛数量最多的位置
    best_pos = None
    best_matches = -1
    for key, val in entry.items():
        if key in ("displayName", "Most Played"):
            continue
        if isinstance(val, dict) and "match_count" in val:
            if val["match_count"] > best_matches:
                best_matches = val["match_count"]
                best_pos = key
    if best_pos:
        entry["Most Played"] = best_pos

    return entry, success_count, failed_count


async def main_async() -> int:
    """主函数。返回退出码（0=正常，1=致命错误）。

    即使发生致命错误也返回 0，以确保 workflow 的 push 步骤能执行（保留原数据）。
    仅当无法获取英雄列表且无原数据时返回 1。
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    start_time = time.time()

    # 1. 加载已有数据库（先加载，确保任何错误都能保留原数据）
    db = load_database()
    if db:
        print(f"已加载 {len(db)} 个英雄的已有数据")

    # 2. 获取所有英雄
    print("获取英雄列表 ...")
    try:
        session = cffi_requests.Session(impersonate="chrome")
        r = session.get(f"{BASE}/api/heroes/list", proxies=_PROXY, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        heroes_data = r.json()
        heroes = heroes_data if isinstance(heroes_data, list) else heroes_data.get("heroes", heroes_data)
        print(f"  共 {len(heroes)} 个英雄")
    except Exception as e:
        print(f"获取英雄列表失败: {e}")
        if db:
            print("保留原数据，正常退出")
            return 0
        print("无原数据可用，退出")
        return 1

    # 3. 获取物品映射
    print("获取物品映射 ...")
    item_mapping = await get_item_mapping_async(sem)

    # 4. 遍历抓取（英雄间串行，位置间并发）
    total = len(heroes)
    total_success = 0
    total_failed = 0
    heroes_updated = 0

    for i, hero_info in enumerate(heroes, 1):
        hero_id = hero_info.get("hero_id") or hero_info.get("id")
        display_name = hero_info.get("displayName", f"hero_{hero_id}")
        existing_entry = db.get(str(hero_id))

        print(f"[{i}/{total}] {display_name} (id={hero_id})")

        try:
            entry, success, failed = await build_hero_entry_async(
                hero_info, item_mapping, existing_entry, sem
            )
            db[str(hero_id)] = entry
            save_database(db)
            total_success += success
            total_failed += failed
            if success > 0:
                heroes_updated += 1
        except Exception as e:
            print(f"  英雄 {display_name} 整体抓取失败: {e}")
            # 保留原数据
            if existing_entry:
                db[str(hero_id)] = existing_entry
                save_database(db)
            total_failed += 1

    # 5. 最终保存
    save_database(db)
    elapsed = time.time() - start_time

    print(f"\n{'=' * 64}")
    print(f"完成: {total} 个英雄, {heroes_updated} 个有更新")
    print(f"  成功位置: {total_success}")
    print(f"  失败位置: {total_failed}")
    print(f"  数据库: {len(db)} 个英雄")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  已保存到 {DATABASE_FILE}")
    print(f"{'=' * 64}")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main_async())
    sys.exit(exit_code)
