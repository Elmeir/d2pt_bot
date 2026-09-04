"""
抓取英雄天赋的中文名称（名称与数值均来自 d2vpkr 游戏文件镜像）。

数据源（GitHub dotabuff/d2vpkr，jsDelivr CDN 优先、raw 备用）：
    - 名称：resource/localization/abilities_schinese.txt
      词条 "DOTA_Tooltip_ability_special_bonus_*"（前缀大小写存在变体，忽略大小写匹配）
      → 天赋中文名（含 {s:...} 占位符）
    - 数值：
      1) scripts/npc/heroes/{npc}.txt：技能 AbilityValues 扁平子块中
         "special_bonus_*" "+0.5" 形式的增量（新版天赋挂在技能特殊值上）
      2) scripts/npc/npc_abilities.txt：老式天赋定义块 AbilityValues."value"
         中的数值（通用属性类天赋，如 special_bonus_hp_325 → 325）
    - 英雄名单：scripts/npc/npc_heroes.txt 顶层键（排除 base / target_dummy）

占位符替换规则（增量取绝对值，名称文本中已带正负号）：
    - {s:bonus_X} → 块名（不区分大小写）等于 X 的子块中该天赋的增量
    - {s:value}   → 该天赋唯一的增量；不唯一时取定义块中的 value
    解析失败的占位符保留原样，该天赋跳过（由存量数据兜底）。

输出：data/talents_cn.json，扁平的 {name: name_loc} 映射（不区分英雄），
      name 与 fetch_d2pt_build_api.build_talents 的简化规则保持一致。

采用存量更新：在已有 talents_cn.json 基础上合并本次构建结果，
个别 KV 下载失败不会丢失其已有的天赋翻译。

更新检测：分别查询依赖文件/目录的最新 commit（GitHub API path 过滤），
与上次构建记录（data/talents_cn.sha，{路径: commit} 映射）全部一致
且数据完好时，判定游戏文件未变化，跳过重建；传 --force 可强制
重建；查询失败时按需重建兜底。
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# d2vpkr 游戏文件镜像：jsDelivr CDN 优先（国内可达），raw.githubusercontent 备用
D2VPKR = "https://cdn.jsdelivr.net/gh/dotabuff/d2vpkr@master"
D2VPKR_BACKUP = "https://raw.githubusercontent.com/dotabuff/d2vpkr/master"
LOCALE_PATH = "/dota/resource/localization/abilities_schinese.txt"
ABILITIES_PATH = "/dota/scripts/npc/npc_abilities.txt"
HEROES_PATH = "/dota/scripts/npc/npc_heroes.txt"
HEROES_KV_DIR = "/dota/scripts/npc/heroes"
HERO_KV_PATH = HEROES_KV_DIR + "/{npc}.txt"
KV_DL_WORKERS = 8

# npc_heroes.txt 中的非英雄顶层键
NON_HERO_NPCS = {"npc_dota_hero_base", "npc_dota_hero_target_dummy"}

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data", "talents_cn.json")
# 更新检测：按路径查询各依赖文件/目录的最新 commit（GitHub API path 过滤）
D2VPKR_COMMITS_API = "https://api.github.com/repos/dotabuff/d2vpkr/commits"
D2VPKR_DEP_PATHS = (LOCALE_PATH, ABILITIES_PATH, HEROES_PATH, HEROES_KV_DIR)
STATE_FILE = os.path.join(OUTPUT_DIR, "data", "talents_cn.sha")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 本地化词条（"DOTA_Tooltip_ability_..." 前缀大小写有变体，忽略大小写匹配）
LOCALE_PAIR_RE = re.compile(
    r'"DOTA_Tooltip_ability_(special_bonus_[^"]+)"\s*"([^"]*)"', re.IGNORECASE)
# name_loc 中的占位符，如 {s:value}、{s:bonus_AbilityChannelTime}
PLACEHOLDER_RE = re.compile(r"\{s:([^}]+)\}")
# KV 中天赋增量对
KV_PAIR_RE = re.compile(r'"(special_bonus_[^"]+)"\s*"([^"]*)"')


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


def _fetch_text(url: str, timeout: int = 30) -> str:
    """发送 GET 请求并返回 UTF-8 文本。"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_text_mirrored(path: str) -> str:
    """按镜像优先级下载 d2vpkr 文件，全部失败时抛出异常。"""
    last = None
    for base in (D2VPKR, D2VPKR_BACKUP):
        try:
            return _fetch_text(base + path)
        except Exception as e:
            last = e
    raise RuntimeError(f"下载 {path} 失败: {last!r}")


def get_hero_npcs() -> list[str]:
    """从 d2vpkr npc_heroes.txt 顶层键解析英雄 npc 名（排除非英雄）。"""
    text = _fetch_text_mirrored(HEROES_PATH)
    npcs = re.findall(r'^\t"(npc_dota_hero_[^"]+)"', text, re.MULTILINE)
    return [npc for npc in npcs if npc not in NON_HERO_NPCS]


def _format_value(val) -> str:
    """将数值格式化为字符串：整数去小数点，浮点数保留原样。"""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _parse_delta(raw: str) -> float | None:
    """解析 KV 增量为数值（取绝对值，名称文本中已带正负号/单位）。

    支持 "+125" "-0.5" "50"、赋值语义 "=45"、百分号后缀 "-18%"
    （名称文本中已含 %）、乘数前缀 "x2.5"（名称文本中为 "2.5倍"）、
    以及 "+-0.1" 这类双符号写法；无法解析时返回 None。
    """
    s = raw.strip()
    if s.startswith("="):
        s = s[1:]
    s = s.lstrip("+")
    if s.endswith("%"):
        s = s[:-1]
    if s.lower().startswith("x"):
        s = s[1:]
    try:
        return abs(float(s))
    except ValueError:
        return None


def parse_locale(text: str) -> dict[str, str]:
    """从本地化文件提取 {完整天赋名: 名称文本}，跳过空文本。"""
    result: dict[str, str] = {}
    for m in LOCALE_PAIR_RE.finditer(text):
        if m.group(2):
            result.setdefault(m.group(1), m.group(2))
    return result


def download_hero_kvs(npcs: list[str]) -> dict[str, str]:
    """并行下载全部英雄 KV 文件，返回 {npc: 文本}，失败者打印警告并跳过。"""
    def _one(npc: str) -> tuple[str, str | None]:
        try:
            return npc, _fetch_text_mirrored(HERO_KV_PATH.format(npc=npc))
        except Exception as e:
            print(f"  警告: {npc} KV 下载失败: {e}")
            return npc, None

    result: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=KV_DL_WORKERS) as ex:
        for npc, text in ex.map(_one, npcs):
            if text is not None:
                result[npc] = text
    return result


def _enclosing_block_name(text: str, pos: int) -> str:
    """返回 text 中 pos 之前最近的未配对 '{' 对应的块名。

    增量对可能位于扁平子块中，也可能与嵌套的 "value" 子块并列，
    因此逐字符向前回溯括号配对，取最近一层未配对 '{' 的块名。
    """
    depth = 0
    for i in range(pos - 1, -1, -1):
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                # 块名与 '{' 之间允许空白和行尾注释
                m = re.search(r'"([^"\n]+)"\s*(?://[^\n]*\s*)?$', text[:i])
                return m.group(1) if m else ""
            depth -= 1
    return ""


def collect_pair_deltas(kv_texts: dict[str, str],
                        ab_text: str) -> dict[str, list[tuple[str, str]]]:
    """汇总 KV 中的天赋增量对：{完整天赋名: [(特殊值块名, 原始增量), ...]}。"""
    deltas: dict[str, list[tuple[str, str]]] = {}
    for text in (*kv_texts.values(), ab_text):
        for m in KV_PAIR_RE.finditer(text):
            deltas.setdefault(m.group(1), []).append(
                (_enclosing_block_name(text, m.start()), m.group(2)))
    return deltas


def _kv_block_at(text: str, brace_idx: int) -> str:
    """从 text 的 brace_idx（指向 '{'）开始括号配对，返回块文本。"""
    depth = 0
    for i in range(brace_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_idx:i + 1]
    return ""


def _definition_value(ab_text: str, talent_full: str) -> float | None:
    """从 npc_abilities.txt 天赋定义块提取 {s:value} 的数值。

    通用属性类天赋的定义结构：
        "special_bonus_hp_325"
        {
            "AbilityValues"
            {
                "value"
                {
                    "value" "325"
                }
            }
        }
    """
    for m in re.finditer(rf'"{talent_full}"\s*\{{', ab_text):
        pair = re.search(r'"value"\s*"([^"]+)"', _kv_block_at(ab_text, m.end() - 1))
        if pair:
            return _parse_delta(pair.group(1))
    return None


def resolve_loc(loc: str, deltas: list[tuple[str, str]],
                def_val: float | None) -> str | None:
    """替换 loc 中的占位符；仍有占位符无法解析时返回 None（该天赋跳过）。"""
    def _sub(match):
        key = match.group(1)
        if key == "value":
            vals = {v for v in (_parse_delta(raw) for _, raw in deltas)
                    if v is not None}
            if len(vals) == 1:
                return _format_value(vals.pop())
            if def_val is not None:
                return _format_value(def_val)
            return match.group(0)
        if key.startswith("bonus_"):
            sv_name = key[len("bonus_"):].lower()
            # 收集同名块的全部数值；出现不同值时视为歧义，保留占位符
            vals = {v for v in (_parse_delta(raw)
                                for block_name, raw in deltas
                                if block_name.lower() == sv_name)
                    if v is not None}
            if len(vals) == 1:
                return _format_value(vals.pop())
            return match.group(0)
        return match.group(0)

    resolved = PLACEHOLDER_RE.sub(_sub, loc)
    return None if PLACEHOLDER_RE.search(resolved) else resolved


def build_talents(locale: dict[str, str],
                  deltas: dict[str, list[tuple[str, str]]],
                  ab_text: str) -> tuple[dict[str, str], list[str]]:
    """构建 {简化名: 中文名}，返回 (结果, 因占位符无法解析而跳过的简化名)。"""
    built: dict[str, str] = {}
    skipped: list[str] = []
    for full_name, loc in locale.items():
        pair = deltas.get(full_name, [])
        def_val = (_definition_value(ab_text, full_name)
                   if "{s:value}" in loc else None)
        if not pair and def_val is None:
            continue  # 无任何数值来源的天赋（多为已移除的旧天赋）
        resolved = resolve_loc(loc, pair, def_val)
        if resolved is None:
            skipped.append(simplify_talent_name(full_name))
            continue
        built[simplify_talent_name(full_name)] = resolved
    return built, skipped


def _load_existing() -> dict[str, str]:
    """加载存量数据；文件不存在或损坏时返回空字典。"""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get_dep_shas() -> dict[str, str] | None:
    """按路径查询各依赖文件/目录的最新 commit；任一查询失败返回 None。"""
    shas: dict[str, str] = {}
    for path in D2VPKR_DEP_PATHS:
        url = f"{D2VPKR_COMMITS_API}?per_page=1&path={urllib.parse.quote(path)}"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read())
        except Exception as e:
            print(f"  警告: 查询 {path} 最新 commit 失败: {e!r}")
            return None
        if not data:
            print(f"  警告: {path} 无 commit 记录")
            return None
        shas[path] = data[0]["sha"]
    return shas


def _read_state() -> dict[str, str] | None:
    """读取上次构建记录的 {路径: commit} 映射。"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict) and state:
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _write_state(shas: dict[str, str]) -> None:
    """保存本次构建记录的 {路径: commit} 映射。"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(shas, f, indent=2)


def main() -> None:
    # 0. 更新检测：依赖文件/目录的最新 commit 全部未变化且数据完好时跳过重建
    force = "--force" in sys.argv
    existing = _load_existing()
    shas = get_dep_shas()
    if not force and shas is not None and shas == _read_state() and existing:
        print("d2vpkr 依赖文件无更新，跳过重建")
        return
    if existing:
        print(f"已加载存量数据 {len(existing)} 个天赋")

    # 1. 英雄名单（npc 名用于 KV 文件名）
    print("从 npc_heroes.txt 解析英雄名单 ...")
    npcs = get_hero_npcs()
    print(f"  共 {len(npcs)} 个英雄")

    # 2. 本地化名称
    print("下载本地化文件 abilities_schinese.txt ...")
    locale = parse_locale(_fetch_text_mirrored(LOCALE_PATH))
    print(f"  共 {len(locale)} 个 special_bonus 词条")

    # 3. 并行下载英雄 KV，汇总增量
    print(f"并行下载 {len(npcs)} 个英雄 KV 文件（{KV_DL_WORKERS} 线程）...")
    kv_texts = download_hero_kvs(npcs)
    print(f"  成功 {len(kv_texts)}/{len(npcs)}")
    ab_text = _fetch_text_mirrored(ABILITIES_PATH)
    deltas = collect_pair_deltas(kv_texts, ab_text)
    print(f"  汇总 {len(deltas)} 个天赋的增量对")

    # 4. 构建名称映射
    built, skipped = build_talents(locale, deltas, ab_text)
    print(f"  构建完成 {len(built)} 个天赋"
          + (f"，跳过 {len(skipped)} 个: {skipped[:20]}" if skipped else ""))

    # 5. 存量合并 + 保存
    existing.update(built)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"\n数据已保存到: {OUTPUT_FILE}（共 {len(existing)} 个天赋）")

    # 6. 记录本次构建对应的各依赖路径 commit，供下次更新检测
    if shas:
        _write_state(shas)


if __name__ == "__main__":
    main()
