"""
抓取英雄天赋的中文名称。

数据源：
    - 英雄列表：OpenDota /api/heroes（无 Cloudflare）
    - 天赋详情：https://www.dota2.com/datafeed/herodata?language=schinese&hero_id={hero_id}
    - 兜底数值：d2vpkr 游戏文件镜像。datafeed 对个别天赋缺失 bonus 关联
      （如基恩载具 -0.5、捕捞 +125），此时从英雄 KV 文件
      dota/scripts/npc/heroes/npc_dota_hero_{npc}.txt 的扁平子块提取增量

name_loc 占位符替换规则：
    - {s:value} → 取天赋自身 special_values 中 name="value" 的 values_float[0]
    - {s:bonus_X} → 遍历所有技能的 special_values，找到 name="X" 且 bonuses
      中存在 name 等于当前天赋 name 的条目，取该 bonus 的 value

输出：talents_cn.json，扁平的 {name: name_loc} 映射（不区分英雄）。

采用存量更新：在已有 talents_cn.json 基础上合并抓取结果，
避免个别英雄抓取失败时丢失其已有的天赋翻译。
"""

import json
import os
import re
import time
import urllib.request

OPENDOTA_HEROES = "https://api.opendota.com/api/heroes"
DOTA2_HERODATA = "https://www.dota2.com/datafeed/herodata?language=schinese&hero_id={hero_id}"
# d2vpkr 兜底数据源（游戏 KV 文件镜像）：jsDelivr CDN 优先，raw 备用
# {npc} 为完整 NPC 名（OpenDota name 字段，如 npc_dota_hero_tinker）
D2VPKR_HERO_KV = ("https://cdn.jsdelivr.net/gh/dotabuff/d2vpkr@master"
                  "/dota/scripts/npc/heroes/{npc}.txt")
D2VPKR_HERO_KV_BACKUP = ("https://raw.githubusercontent.com/dotabuff/d2vpkr/master"
                         "/dota/scripts/npc/heroes/{npc}.txt")
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data", "talents_cn.json")

# 请求间隔（秒），避免对 dota2.com 请求过于频繁
REQUEST_DELAY = 0.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 匹配 name_loc 中的占位符，如 {s:value}、{s:bonus_mana_void_damage_per_mana}
PLACEHOLDER_RE = re.compile(r"\{s:([^}]+)\}")

# 匹配 KV 文件中的扁平子块（不含嵌套大括号），块名为特殊值名：
#   "AbilityChannelTime" { "value" "3.0" "special_bonus_unique_tinker_5" "-0.5" }
KV_FLAT_BLOCK_RE = re.compile(r'"([^"\n]+)"\s*\{([^{}]*)\}')


def simplify_talent_name(name: str) -> str:
    """简化天赋的内部标识符，去掉 common 前缀。

    与 fetch_d2pt_build_api.build_talents 的简化规则保持一致，
    保证 talents_cn.json 的 key 能匹配 d2pt_core_build.json 中简化的 n。
    例：special_bonus_unique_antimage_5 -> antimage_5
        special_bonus_hp_200             -> hp_200
    """
    for prefix in ("special_bonus_unique_", "special_bonus_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _fetch_json(url: str, timeout: int = 30) -> dict | list:
    """发送 GET 请求并返回 JSON 数据。"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def get_heroes() -> list[dict]:
    """从 OpenDota API 获取英雄列表（id 与 npc 名，npc 用于 d2vpkr 文件名）。"""
    data = _fetch_json(OPENDOTA_HEROES)
    return [{"id": hero["id"], "npc": hero.get("name", "")}
            for hero in data if "id" in hero]


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


def _load_hero_kv(npc: str) -> str | None:
    """下载英雄 KV 定义文件（d2vpkr 镜像），失败返回 None。"""
    for url in (D2VPKR_HERO_KV.format(npc=npc), D2VPKR_HERO_KV_BACKUP.format(npc=npc)):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception:
            continue
    return None


def _resolve_via_kv(kv_text: str, talent_full: str, name_loc: str) -> str:
    """用 KV 文件中该天赋的 bonus 增量替换 name_loc 里未解析的占位符。

    KV 中 bonus 以特殊值子块形式挂在技能 AbilityValues 下：
        "AbilityChannelTime"
        {
            "value"                         "3.0"
            "special_bonus_unique_tinker_5" "-0.5"
        }
    name_loc 中通常已带正负号（如 "-{s:bonus_X}秒"），因此取增量绝对值替换。
    """
    def _sub(match):
        key = match.group(1)
        if not key.startswith("bonus_"):
            return match.group(0)
        sv_name = key[len("bonus_"):].lower()

        # 收集 KV 中所有引用该天赋增量的扁平子块
        candidates: list[tuple[str, str]] = []
        for block in KV_FLAT_BLOCK_RE.finditer(kv_text):
            pair = re.search(rf'"{talent_full}"\s*"([^"]+)"', block.group(2))
            if pair:
                candidates.append((block.group(1), pair.group(1)))

        # 优先取块名与占位符特殊值名一致的增量
        chosen = None
        for block_name, value in candidates:
            if block_name.lower() == sv_name:
                chosen = value
                break
        # 无同名块时，若所有增量一致则直接采用
        if chosen is None and len({v for _, v in candidates}) == 1:
            chosen = candidates[0][1]
        if chosen is None:
            return match.group(0)

        try:
            val = abs(float(chosen.strip()))
        except ValueError:
            return match.group(0)
        return _format_value(val)

    return PLACEHOLDER_RE.sub(_sub, name_loc)


def apply_kv_fallback(talents: dict[str, str], kv_text: str) -> None:
    """用 d2vpkr KV 文件兜底解析 talents 中剩余的占位符（原地修改）。"""
    for tkey in [k for k, v in talents.items() if "{s:" in v]:
        # 由简化名反推 KV 中的完整天赋名（unique_ 前缀优先）
        full = next(
            (c for c in (f"special_bonus_unique_{tkey}", f"special_bonus_{tkey}")
             if f'"{c}"' in kv_text),
            None,
        )
        if full:
            talents[tkey] = _resolve_via_kv(kv_text, full, talents[tkey])


def extract_talents(hero: dict) -> dict[str, str]:
    """从英雄数据中提取天赋映射 {简化 name: 替换后的 name_loc}。"""
    abilities = hero.get("abilities", [])
    result: dict[str, str] = {}
    for talent in hero.get("talents", []):
        name = talent.get("name")
        name_loc = talent.get("name_loc", "")
        resolved = resolve_name_loc(
            name_loc, name, talent.get("special_values", []), abilities
        )
        result[simplify_talent_name(name)] = resolved
    return result


def main() -> None:
    # 0. 加载已有数据作为存量，抓取结果在此基础上更新
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                all_talents: dict[str, str] = json.load(f)
            print(f"已加载存量数据 {len(all_talents)} 个天赋")
        except (json.JSONDecodeError, OSError):
            all_talents = {}
    else:
        all_talents = {}

    # 1. 获取英雄列表（npc 名用于 d2vpkr 兜底文件名）
    print("从 OpenDota 获取英雄列表 ...")
    heroes = get_heroes()
    print(f"  共 {len(heroes)} 个英雄")

    # 2. 逐个抓取天赋数据，合并为扁平的 {name: name_loc} 映射
    for i, entry in enumerate(heroes, 1):
        hero_id, hero_npc = entry["id"], entry["npc"]
        url = DOTA2_HERODATA.format(hero_id=hero_id)
        try:
            data = _fetch_json(url)
            heroes_data = data["result"]["data"]["heroes"]
            if not heroes_data:
                print(f"  [{i}/{len(heroes)}] hero_id={hero_id} 无数据")
                continue
            hero = heroes_data[0]
            talents = extract_talents(hero)
            # datafeed 缺失 bonus 关联时，从 d2vpkr KV 兜底解析剩余占位符
            if any("{s:" in v for v in talents.values()):
                kv_text = _load_hero_kv(hero_npc)
                if kv_text:
                    apply_kv_fallback(talents, kv_text)
            all_talents.update(talents)
            print(f"  [{i}/{len(heroes)}] hero_id={hero_id} "
                  f"({hero.get('name_loc', '?')}) - {len(talents)} 个天赋")
        except Exception as e:
            print(f"  [{i}/{len(heroes)}] hero_id={hero_id} 获取失败: {e}")

        if i < len(heroes):
            time.sleep(REQUEST_DELAY)

    # 3. 保存到 JSON
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_talents, f, ensure_ascii=False, indent=2)
    print(f"\n数据已保存到: {OUTPUT_FILE}（共 {len(all_talents)} 个天赋）")


if __name__ == "__main__":
    main()
