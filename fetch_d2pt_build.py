"""
GitHub Actions 自动化抓取脚本：定期更新 d2pt_core_build.json。

设计要点：
    - 全量更新：每次运行都重新抓取所有英雄所有位置
    - 仅覆盖有效数据：新数据有效（cb 非空且无 err）才覆盖原数据
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
import random
import sys
import threading
import time

from curl_cffi import requests as cffi_requests

from fetch_d2pt_build_api import (
    BASE,
    _get_proxy,
    get_hero_builds,
    get_item_mapping,
    get_positions_info,
    build_core_items,
    build_start_items,
    build_talents,
    build_abilities_new,
)

DATABASE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "d2pt_core_build.json")
# 最大并发请求数（CI 环境降低并发以减少 Cloudflare 拦截风险）
CONCURRENCY = 3
# 单个请求超时（秒）
REQUEST_TIMEOUT = 10
# 单个位置最大重试次数
MAX_RETRIES = 3
# 位置请求前的随机小延迟范围（秒），错开发起时刻以降低风控风险
REQUEST_DELAY_RANGE = (0.2, 0.8)
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


def _hero_id(hero_info: dict) -> int | None:
    """从英雄信息提取 hero_id。"""
    return hero_info.get("hero_id") or hero_info.get("id")


def _hero_display_name(hero_info: dict) -> str:
    """从英雄信息提取展示名称（兜底为 hero_{id}）。"""
    return hero_info.get("displayName", f"hero_{_hero_id(hero_info)}")


def _format_win_rate(rate: float) -> str:
    """将胜率小数转为百分比字符串，如 0.5517 -> "55%"。"""
    return f"{round(rate * 100)}%"


def _is_position_valid(pos_data: dict | None) -> bool:
    """检查位置数据是否有效（有 cb 且无 err）。"""
    if not isinstance(pos_data, dict):
        return False
    if pos_data.get("err"):
        return False
    return bool(pos_data.get("cb"))


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
    """保存数据库到文件（原子写入：先写临时文件再重命名）。"""
    os.makedirs(os.path.dirname(DATABASE_FILE), exist_ok=True)
    tmp_file = DATABASE_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_file, DATABASE_FILE)


def _sync_get_hero_builds(hero_slug: str, hero_id: int, position: str) -> dict:
    """在线程中调用共享 API 获取 hero builds（使用线程私有 Session）。"""
    return get_hero_builds(_get_thread_session(), hero_slug, hero_id, position)


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
    async with sem:
        return await asyncio.to_thread(
            get_item_mapping, cffi_requests.Session(impersonate="chrome")
        )


async def fetch_core_build_async(
    hero_slug: str,
    hero_id: int,
    position: str,
    item_mapping: dict,
    sem: asyncio.Semaphore,
) -> dict:
    """异步获取指定英雄指定位置的 Core Item Build 及相关数据（带重试）。

    返回（缩写字段名以压缩 JSON 体积）：
        {
            "cb": [...],      # core_build 核心物品列表
            "si": [...],      # start_items 开局物品
            "lg": [...],      # lategame_inventories 后期出装
            "tl": [...],      # talents 天赋选择
            "ab": [...]       # abilities_new 技能加点序列
        }
    """
    # 随机小延迟，错开同一英雄内各位置的请求发起时刻
    await asyncio.sleep(random.uniform(*REQUEST_DELAY_RANGE))
    build_entry = await _fetch_hero_builds_with_retry(hero_slug, hero_id, position, sem)
    build_data = build_entry.get("build_data", {})

    return {
        "cb": build_core_items(build_entry, item_mapping),
        "si": build_start_items(build_data),
        "lg": build_data.get("anchor_lategame_inventories", []),
        "tl": build_talents(build_data),
        "ab": build_abilities_new(build_data),
    }


def _position_entry_from_result(result: dict, info: dict) -> dict:
    """从成功抓取的结果构造位置条目。"""
    return {
        "cb": result["cb"],
        "si": result.get("si", []),
        "lg": result.get("lg", []),
        "tl": result.get("tl", []),
        "ab": result.get("ab", []),
        "mc": info["match_count"],
        "wr": _format_win_rate(info["win_rate"]),
    }


def _position_placeholder(info: dict, error: str | None = None) -> dict:
    """构造无有效数据时的占位条目（失败时可附带 error 信息）。"""
    entry: dict = {
        "cb": [],
        "mc": info["match_count"],
        "wr": _format_win_rate(info["win_rate"]),
    }
    if error is not None:
        entry["err"] = error
    else:
        entry["si"] = []
        entry["lg"] = []
        entry["tl"] = []
        entry["ab"] = []
    return entry


async def build_hero_entry_async(
    hero_info: dict,
    item_mapping: dict,
    existing_entry: dict | None,
    sem: asyncio.Semaphore,
) -> tuple[dict, int, int]:
    """异步抓取单个英雄所有位置的数据（位置间并发）。

    全量更新模式：
        - 新数据有效（cb 非空）→ 覆盖原数据
        - 新数据无效/抓取失败 → 保留原数据（如果有）
        - 原数据也没有 → 记录 err 占位

    返回 (entry, success_count, failed_count)
    """
    hero_id = _hero_id(hero_info)
    hero_slug = _hero_slug_from_info(hero_info)
    display_name = _hero_display_name(hero_info)
    positions_info = get_positions_info(hero_info)

    existing = existing_entry or {}
    entry: dict = {"n": display_name}

    success_count = 0
    failed_count = 0

    # 全量更新：所有位置都重新抓取，并发请求
    positions_to_fetch = list(positions_info.items())
    tasks = [
        fetch_core_build_async(hero_slug, hero_id, pos, item_mapping, sem)
        for pos, _ in positions_to_fetch
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for (pos, info), result in zip(positions_to_fetch, results):
        if isinstance(result, Exception):
            # 抓取失败：保留原数据（如果有效），否则记录 error 占位
            if _is_position_valid(existing.get(pos)):
                entry[pos] = existing[pos]
                print(f"    {pos}: 抓取失败，保留原数据 ({result})")
            else:
                entry[pos] = _position_placeholder(info, str(result))
                print(f"    {pos}: 抓取失败，无原数据 ({result})")
            failed_count += 1
        elif result and result.get("cb"):
            # 新数据有效：覆盖原数据
            entry[pos] = _position_entry_from_result(result, info)
            success_count += 1
            print(
                f"    {pos}: {len(result['cb'])} 物品, "
                f"{info['match_count']} 场, 胜率 {_format_win_rate(info['win_rate'])}"
            )
        else:
            # cb 为空（位置比赛数太少）：保留原数据，否则空占位
            if _is_position_valid(existing.get(pos)):
                entry[pos] = existing[pos]
                print(f"    {pos}: 新数据为空，保留原数据")
            else:
                entry[pos] = _position_placeholder(info)
                print(f"    {pos}: 新数据为空，无原数据")
            failed_count += 1

    # 自动计算 "Most Played"：比赛数量最多的位置
    best = max(
        (
            (key, val)
            for key, val in entry.items()
            if key not in ("n", "mp")
            and isinstance(val, dict) and "mc" in val
        ),
        key=lambda kv: kv[1]["mc"],
        default=None,
    )
    if best:
        entry["mp"] = best[0]

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
        r = session.get(f"{BASE}/api/heroes/list", proxies=_get_proxy(), timeout=REQUEST_TIMEOUT)
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
        hero_id = _hero_id(hero_info)
        display_name = _hero_display_name(hero_info)
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
