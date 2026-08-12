"""
通过 API 请求抓取 dota2protracker.com 的 Core Item Build 数据（不打开浏览器）。

数据源：
    /api/hero/{hero_id}/builds?position={position}
    - 返回 build_data.anchor_build[0]（Core Item Build 物品 ID 数组）
    - build_data.anchor_item_stats（物品统计：avg_minute, pr）
    - build_data.anchor_items（CORE 物品列表，含 raw_item_id）
    - build_data.talents（天赋选择：每级 left/right，含 displayName/pr/win_rate）
    - build_data.abilities_new（最常见的技能加点序列，按 pr 降序）
    - build_data.abilities（ability_id -> displayName 映射）

CORE 判定：
    物品在 anchor_items 中 → CORE；只在 anchor_build[0] 中 → situational
    过滤：anchor_item_stats[item_id].pr > 5%

物品映射：OpenDota /api/constants/items（无 Cloudflare）

依赖安装：
    pip install curl_cffi

用法：
    python fetch_d2pt_build_api.py              # 默认 Snapfire，自动选择比赛最多的位置
    python fetch_d2pt_build_api.py Anti-Mage    # 指定英雄，自动选择位置
    python fetch_d2pt_build_api.py Snapfire "pos 4"  # 指定英雄和位置（pos 1~5）

注意：
    - build API 需要先访问英雄页面获取 cookie，并带 Referer header
    - 默认使用 SOCKS5 代理 socks5://127.0.0.1:1080 绕过 Cloudflare；
      可通过环境变量 D2PT_PROXY 覆盖，设为空字符串可禁用代理（CI 直连）。
"""

import json
import os
import sys
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

BASE = "https://dota2protracker.com"
OPENDOTA_ITEMS = "https://api.opendota.com/api/constants/items"
DEFAULT_PROXY = "socks5://127.0.0.1:1080"

# 显示物品的最低购买率（过滤噪音，与网页端一致）
MIN_DISPLAY_RATE = 5.0
# CORE 判定阈值：pr > 80% → CORE（与网页端 InventoryV2 组件: n[6].pr>80 一致）
CORE_PR_THRESHOLD = 80.0

# 所有位置
POSITIONS = ["pos 1", "pos 2", "pos 3", "pos 4", "pos 5"]

# 当 OpenDota 不可用时的备用物品映射（item_id -> (名称, 图片slug)）
FALLBACK_ITEMS = {
    1: ("Blink Dagger", "blink"),
    36: ("Magic Wand", "magic_wand"),
    37: ("Ghost Scepter", "ghost"),
    41: ("Bottle", "bottle"),
    48: ("Boots of Travel", "travel_boots"),
    96: ("Scythe of Vyse", "sheepstick"),
    110: ("Refresher Orb", "refresher"),
    116: ("Black King Bar", "black_king_bar"),
    119: ("Shiva's Guard", "shivas_guard"),
    178: ("Soul Ring", "soul_ring"),
    220: ("Boots of Travel 2", "travel_boots_2"),
    235: ("Octarine Core", "octarine_core"),
    259: ("Kaya", "kaya"),
    277: ("Yasha and Kaya", "yasha_and_kaya"),
    600: ("Overwhelming Blink", "overwhelming_blink"),
    609: ("Aghanim's Shard", "aghanims_shard"),
}


def _get_proxy() -> dict | None:
    """获取代理配置。

    优先级：环境变量 D2PT_PROXY > 默认 SOCKS5 代理。
    显式将 D2PT_PROXY 设为空字符串可禁用代理（用于 CI 直连）。
    """
    proxy = os.environ.get("D2PT_PROXY")
    if proxy is None:
        proxy = DEFAULT_PROXY
    if not proxy:
        return None
    return {"https": proxy, "http": proxy}


def _request(url: str, session: cffi_requests.Session, use_proxy: bool = True,
             headers: dict | None = None, timeout: int = 30) -> dict | list:
    """发送 GET 请求，必要时使用代理。"""
    proxies = _get_proxy() if use_proxy else None
    r = session.get(url, proxies=proxies, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.json()


def create_session() -> cffi_requests.Session:
    """创建模拟 Chrome 的会话以绕过 Cloudflare。"""
    return cffi_requests.Session(impersonate="chrome")


def get_item_mapping(session: cffi_requests.Session) -> dict:
    """获取 item_id -> {name, image} 映射。

    优先从 OpenDota 公共 API 获取（无 Cloudflare），失败时使用内置映射。
    """
    try:
        # OpenDota 无 Cloudflare，不需要代理
        data = _request(OPENDOTA_ITEMS, session, use_proxy=False)
        mapping = {}
        for slug, info in data.items():
            item_id = info.get("id")
            if item_id is not None:
                mapping[item_id] = {
                    "name": info.get("dname") or slug.replace("_", " ").title(),
                    "image": f"{slug}.png",
                }
        if mapping:
            print(f"  从 OpenDota 获取 {len(mapping)} 个物品")
            return mapping
    except Exception as e:
        print(f"  OpenDota API 不可用 ({e})，使用内置映射")

    return {
        iid: {"name": name, "image": f"{slug}.png"}
        for iid, (name, slug) in FALLBACK_ITEMS.items()
    }


def get_hero_info(session: cffi_requests.Session, hero_name: str) -> dict | None:
    """从 /api/heroes/list 获取英雄完整信息。

    返回英雄字典（含 hero_id、displayName、各位置的 matches/winrate 等），
    未找到返回 None。
    """
    data = _request(f"{BASE}/api/heroes/list", session)
    heroes = data if isinstance(data, list) else data.get("heroes", data)

    # 归一化：去掉连字符、空格、下划线，统一小写比较
    # 例： "Anti-Mage" -> "antimage"，匹配 displayName "Anti-Mage" 和 npc "npc_dota_hero_antimage"
    def _norm(s: str) -> str:
        return s.lower().replace("-", "").replace("_", "").replace(" ", "").strip()

    target = _norm(hero_name)
    # 第一轮：精确匹配 displayName 或 npc（避免子字符串误匹配，如 "Io" 匹配到 "Lion"）
    for hero in heroes:
        display = _norm(hero.get("displayName") or "")
        npc = _norm(hero.get("npc") or "")
        if target == display or target == npc:
            return hero
    # 第二轮：仅当 target 较长时，才在 npc 中做子字符串匹配（短名如 "Io" 容易误匹配）
    if len(target) >= 4:
        for hero in heroes:
            npc = _norm(hero.get("npc") or "")
            if target in npc:
                return hero
    return None


def get_hero_id(session: cffi_requests.Session, hero_name: str) -> int | None:
    """从 /api/heroes/list 获取英雄 ID。"""
    hero = get_hero_info(session, hero_name)
    return hero and (hero.get("hero_id") or hero.get("id"))


def get_positions_info(hero_info: dict) -> dict[str, dict]:
    """从英雄信息中提取各位置的比赛数量和胜率。

    返回 {"pos 1": {"match_count": 85, "win_rate": 0.5294}, ...}
    仅包含有比赛数据的位置。
    """
    result = {}
    for pos in POSITIONS:
        matches = hero_info.get(f"{pos} matches", 0) or 0
        winrate = hero_info.get(f"{pos} winrate", 0) or 0
        if matches > 0:
            result[pos] = {"match_count": matches, "win_rate": winrate}
    return result


def select_default_position(hero_info: dict) -> str | None:
    """选择比赛数量最多的位置。"""
    positions = get_positions_info(hero_info)
    if not positions:
        return None
    return max(positions.items(), key=lambda x: x[1]["match_count"])[0]


def get_hero_builds(session: cffi_requests.Session, hero_slug: str,
                    hero_id: int, position: str) -> dict:
    """获取 hero builds 数据（Core Item Build 的真正数据源）。

    流程：
        1. 先访问英雄页面获取 cookie（绕过 Cloudflare）
        2. 请求 /api/hero/{hero_id}/builds?position={position}（带 Referer）
    """
    page_url = f"{BASE}/hero/{quote(hero_slug, safe='-')}"
    # 先访问页面获取 cookie
    session.get(page_url, proxies=_get_proxy(), timeout=30)

    # 请求 build API（带 Referer）
    pos_enc = position.replace(" ", "+")
    url = f"{BASE}/api/hero/{hero_id}/builds?position={pos_enc}"
    headers = {
        "Referer": page_url,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    data = _request(url, session, headers=headers)

    # API 返回列表，取第一个（最常见 build）
    # 某些位置比赛数太少时返回空列表，此时返回空结构
    if isinstance(data, list):
        return data[0] if data else {"build_data": {}}
    return data


def build_core_items(build_entry: dict, item_mapping: dict) -> list[dict]:
    """根据 builds API 返回的数据构建 Core Item Build 列表。

    参数：
        build_entry: builds API 返回的字典（含 build_data）
        item_mapping: item_id -> {name, image}（当前未使用，保留以兼容调用方）

    返回列表格式：
        {
            "id": 41,
            "avg_time": "1m",
            "is_core": True
        }

    数据源：
        - anchor_build[0]: Core Item Build 区域显示的物品 ID 列表
        - anchor_item_stats: 所有物品的统计数据（dict，键为物品 ID 字符串）
          * pr 为百分比 (0-100)
          * avg_minute 为平均购买时间（分钟）

    CORE 判定（与网页端 InventoryV2 组件一致）：
        pr > 80 → CORE（网页端 Yl 组件: n[5]&&n[6].pr>80&&S()）
    """
    build_data = build_entry.get("build_data", {})

    # anchor_build[0] 是 Core Item Build 区域显示的物品 ID 列表
    anchor_build = build_data.get("anchor_build", [])
    if not anchor_build or not anchor_build[0]:
        return []
    item_ids = anchor_build[0]

    # anchor_item_stats 是所有物品的统计数据（dict，键为物品 ID 字符串）
    # 注意：pr 为百分比 (0-100)，与 anchor_items/items_mid_late 的小数 (0-1) 不同
    anchor_item_stats = build_data.get("anchor_item_stats", {})

    # 构建物品列表（判定规则见常量 MIN_DISPLAY_RATE / CORE_PR_THRESHOLD 注释）
    display: list[tuple[int, float, bool]] = []
    seen_ids: set[int] = set()  # anchor_build[0] 可能含重复 ID，需去重
    for iid in item_ids:
        if iid in seen_ids:
            continue
        seen_ids.add(iid)

        stats = anchor_item_stats.get(str(iid))
        if not stats:
            continue  # 该物品无统计数据，跳过

        pr = stats.get("pr", 0) or 0
        avg = stats.get("avg_minute", 0) or 0

        if pr <= MIN_DISPLAY_RATE:
            continue  # 购买率过低，过滤噪音

        # CORE 判定：购买率超过阈值即为核心物品
        is_core = pr > CORE_PR_THRESHOLD
        display.append((iid, avg, is_core))

    # 按 avg_minute 升序排序
    display.sort(key=lambda x: x[1])

    # 转换为输出格式
    # 注意：网页端 InventoryV2 组件对 avg_minute<=0 显示 "-"（不匹配时间正则），
    # 所以数据库中 avg_minute<=0 时 avg_time 设为 None，与网页端一致
    return [
        {
            "id": iid,
            "avg_time": f"{round(avg)}m" if avg > 0 else None,
            "is_core": is_core,
        }
        for iid, avg, is_core in display
    ]


def build_start_items(build_data: dict) -> list[list]:
    """根据 builds API 返回的数据构建开局物品列表。

    数据源：build_data.anchor_start_items_new
    每项为 [item_id 数组, {count, win_rate}]，win_rate 保留 4 位小数。
    """
    result = []
    for item_ids, stats in build_data.get("anchor_start_items_new", []):
        result.append([
            item_ids,
            {
                "count": stats.get("count", 0),
                "win_rate": round(stats.get("win_rate", 0), 4),
            },
        ])
    return result


def build_talents(build_data: dict) -> list[dict]:
    """根据 builds API 返回的数据构建天赋选择列表。

    数据源：build_data.talents（每级一个条目，含 left/right 两个可选天赋）。

    输出格式：
        {
            "lvl": 10,
            "left": {"name": "...", "pr": 0.176},
            "right": {"name": "...", "pr": 0.824},
            "choice": "rt",   # 更常用一侧："lf" 或 "rt"
            "win_rate": 0.504
        }
    """
    def _side(side: dict | None) -> dict | None:
        if not side:
            return None
        return {
            "name": side.get("name"),
            "pr": round(side.get("pr", 0), 3),
            "win_rate": round(side.get("win_rate", 0), 3),
        }

    result = []
    for talent in build_data.get("talents", []):
        result.append({
            "lvl": talent.get("lvl"),
            "left": _side(talent.get("left")),
            "right": _side(talent.get("right")),
            "choice": talent.get("choice"),
            "win_rate": round(talent.get("win_rate", 0), 3),
        })
    return result


def build_abilities_new(build_data: dict) -> list[dict]:
    """根据 builds API 返回的数据构建技能加点序列列表。

    数据源：
        - build_data.abilities_new：最常见的技能加点模式列表，每项为
          [ability_id 数组(0~9级), {pr, wins, count, win_rate}]
        - build_data.abilities：ability_id -> displayName 映射

    输出（按使用率降序，展示最常见的前几种加点序列）：
        {
            "pr": 0.0778,
            "win_rate": 0.524,
            "build": ["Bramble Maze", "Shadow Realm", ...]   # 0~9 级依次加点
        }
    """
    name_of = {
        a.get("ability_id"): a.get("displayName")
        for a in build_data.get("abilities", [])
        if a.get("ability_id") is not None
    }
    result = []
    for pattern, stats in build_data.get("abilities_new", []):
        result.append({
            "pr": round(stats.get("pr", 0), 4),
            "win_rate": round(stats.get("win_rate", 0), 4),
            "build": [name_of.get(iid, f"ability_{iid}") for iid in pattern],
        })
    return result


def print_table(hero: str, position: str, items: list[dict]) -> None:
    """以表格形式打印 Core Item Build 数据。"""
    print(f"\n{'=' * 64}")
    print(f"英雄: {hero} | 位置: {position} | Core Item Build")
    print(f"{'=' * 64}")
    header = f"{'#':<4} {'物品ID':<10} {'平均时间':<10} {'核心':<6}"
    print(header)
    print(f"{'-' * 4} {'-' * 10} {'-' * 10} {'-' * 6}")

    for i, it in enumerate(items, 1):
        core_str = "是" if it["is_core"] else "否"
        print(f"{i:<4} {it['id']:<10} {it['avg_time'] or '-':<10} {core_str:<6}")


def fetch_core_build(
    session: cffi_requests.Session,
    hero_slug: str,
    hero_id: int,
    position: str,
    item_mapping: dict,
) -> list[dict]:
    """获取指定英雄指定位置的 Core Item Build（可复用）。

    流程：
        1. 从 /api/hero/{hero_id}/builds API 获取数据
        2. 构建 core build 列表
    """
    build_entry = get_hero_builds(session, hero_slug, hero_id, position)
    return build_core_items(build_entry, item_mapping)


def main() -> None:
    hero = sys.argv[1] if len(sys.argv) > 1 else "Snapfire"
    # 位置参数可选：未指定时自动选择比赛数量最多的位置
    position = sys.argv[2] if len(sys.argv) > 2 else None

    # 英雄名转为 URL 友好格式
    # 注意：dota2protracker.com 使用空格（URL编码为 %20），不是连字符
    hero_slug = hero.strip()
    hero_slug = " ".join(part.capitalize() for part in hero_slug.split())

    session = create_session()

    # 1. 获取英雄完整信息
    print(f"获取英雄信息: {hero} ...")
    hero_info = get_hero_info(session, hero)
    if not hero_info:
        print(f"错误: 未找到英雄 '{hero}'", file=sys.stderr)
        sys.exit(1)
    hero_id = hero_info.get("hero_id") or hero_info.get("id")
    print(f"  hero_id = {hero_id}, displayName = {hero_info.get('displayName')}")

    # 2. 确定位置
    positions_info = get_positions_info(hero_info)
    if not position:
        position = select_default_position(hero_info)
        if not position:
            print(f"错误: 英雄 '{hero}' 没有任何位置的比赛数据", file=sys.stderr)
            sys.exit(1)
        print(f"  未指定位置，自动选择比赛最多的位置: {position}")
    print(f"  各位置比赛数: { {p: v['match_count'] for p, v in positions_info.items()} }")

    # 3. 获取物品 ID→名称映射
    print("获取物品映射 ...")
    item_mapping = get_item_mapping(session)

    # 4. 获取 Core Item Build
    print(f"获取 builds 数据 (position={position}) ...")
    items = fetch_core_build(session, hero_slug, hero_id, position, item_mapping)

    # 5. 输出
    print_table(hero, position, items)

    # 保存 JSON
    output_file = f"{hero.lower().replace(' ', '_')}_core_build_api.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"\n数据已保存到: {output_file}")


if __name__ == "__main__":
    main()
