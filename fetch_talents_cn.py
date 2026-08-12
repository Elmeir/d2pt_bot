"""
抓取英雄天赋的中文名称。

数据源：
    - 英雄 ID 列表：OpenDota /api/heroes（无 Cloudflare）
    - 天赋详情：https://www.dota2.com/datafeed/herodata?language=schinese&hero_id={hero_id}

name_loc 占位符替换规则：
    - {s:value} → 取天赋自身 special_values 中 name="value" 的 values_float[0]
    - {s:bonus_X} → 遍历所有技能的 special_values，找到 name="X" 且 bonuses
      中存在 name 等于当前天赋 name 的条目，取该 bonus 的 value

输出：talents_cn.json，扁平的 {name: name_loc} 映射（不区分英雄）。
"""

import json
import os
import re
import time
import urllib.request

OPENDOTA_HEROES = "https://api.opendota.com/api/heroes"
DOTA2_HERODATA = "https://www.dota2.com/datafeed/herodata?language=schinese&hero_id={hero_id}"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "talents_cn.json")

# 请求间隔（秒），避免对 dota2.com 请求过于频繁
REQUEST_DELAY = 0.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 匹配 name_loc 中的占位符，如 {s:value}、{s:bonus_mana_void_damage_per_mana}
PLACEHOLDER_RE = re.compile(r"\{s:([^}]+)\}")


def _fetch_json(url: str, timeout: int = 30) -> dict | list:
    """发送 GET 请求并返回 JSON 数据。"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def get_hero_ids() -> list[int]:
    """从 OpenDota API 获取所有英雄 ID。"""
    data = _fetch_json(OPENDOTA_HEROES)
    return [hero["id"] for hero in data if "id" in hero]


def _format_value(val) -> str:
    """将数值格式化为字符串：整数去小数点，浮点数保留原样。"""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def resolve_name_loc(name_loc: str, talent_name: str,
                     talent_special_values: list, abilities: list) -> str:
    """替换 name_loc 中的占位符。

    参数：
        name_loc: 原始的本地化名称（含占位符）
        talent_name: 当前天赋的 name（用于匹配 bonus）
        talent_special_values: 当前天赋自身的 special_values
        abilities: 该英雄所有技能列表（含 special_values 及 bonuses）
    """
    single_placeholder = len(PLACEHOLDER_RE.findall(name_loc)) == 1

    def _replace(match):
        key = match.group(1)
        val = None

        if key.startswith("bonus_"):
            sv_name = key[len("bonus_"):]
            # 1. 在所有技能的 special_values 中查找 name=sv_name 且
            #    bonuses 中 name 等于天赋 name 的条目
            for ab in abilities:
                for sv in ab.get("special_values", []):
                    if sv.get("name") != sv_name:
                        continue
                    for bonus in sv.get("bonuses", []):
                        if bonus.get("name") == talent_name:
                            val = bonus.get("value")
                            break
                    if val is not None:
                        break
                if val is not None:
                    break
            # 2. 天赋自身的 special_values 中查找 name=完整 key（含 bonus_ 前缀）
            if val is None:
                for sv in talent_special_values:
                    if sv.get("name") == key:
                        values = sv.get("values_float", [])
                        if values:
                            val = values[0]
                        break
            # 3. 仅单个占位符时，回退查找任意含此天赋 bonus 的 special_value
            if val is None and single_placeholder:
                for ab in abilities:
                    for sv in ab.get("special_values", []):
                        for bonus in sv.get("bonuses", []):
                            if bonus.get("name") == talent_name:
                                val = bonus.get("value")
                                break
                        if val is not None:
                            break
                    if val is not None:
                        break
        else:
            # 占位符形如 {s:value}：取天赋自身 special_values 中同名条目的 values_float[0]
            for sv in talent_special_values:
                if sv.get("name") == key:
                    values = sv.get("values_float", [])
                    if values:
                        val = values[0]
                    break

        if val is None:
            return match.group(0)  # 未找到对应值，保留原占位符
        return _format_value(val)

    return PLACEHOLDER_RE.sub(_replace, name_loc)


def extract_talents(hero: dict) -> dict[str, str]:
    """从英雄数据中提取天赋映射 {name: 替换后的 name_loc}。"""
    abilities = hero.get("abilities", [])
    result: dict[str, str] = {}
    for talent in hero.get("talents", []):
        name = talent.get("name")
        name_loc = talent.get("name_loc", "")
        resolved = resolve_name_loc(
            name_loc, name, talent.get("special_values", []), abilities
        )
        result[name] = resolved
    return result


def main() -> None:
    # 1. 获取英雄 ID 列表
    print("从 OpenDota 获取英雄 ID 列表 ...")
    hero_ids = get_hero_ids()
    print(f"  共 {len(hero_ids)} 个英雄")

    # 2. 逐个抓取天赋数据，合并为扁平的 {name: name_loc} 映射
    all_talents: dict[str, str] = {}
    for i, hero_id in enumerate(hero_ids, 1):
        url = DOTA2_HERODATA.format(hero_id=hero_id)
        try:
            data = _fetch_json(url)
            heroes = data["result"]["data"]["heroes"]
            if not heroes:
                print(f"  [{i}/{len(hero_ids)}] hero_id={hero_id} 无数据")
                continue
            hero = heroes[0]
            talents = extract_talents(hero)
            all_talents.update(talents)
            print(f"  [{i}/{len(hero_ids)}] hero_id={hero_id} "
                  f"({hero.get('name_loc', '?')}) - {len(talents)} 个天赋")
        except Exception as e:
            print(f"  [{i}/{len(hero_ids)}] hero_id={hero_id} 获取失败: {e}")

        if i < len(hero_ids):
            time.sleep(REQUEST_DELAY)

    # 3. 保存到 JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_talents, f, ensure_ascii=False, indent=2)
    print(f"\n数据已保存到: {OUTPUT_FILE}（共 {len(all_talents)} 个天赋）")


if __name__ == "__main__":
    main()
