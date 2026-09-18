import os
import json
from datetime import datetime, timedelta
import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "https://dota2protracker.com/api/heroes/stats"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data", "d2pt_pos.json")
PARAMS_FILE = os.path.join(OUTPUT_DIR, "data", "d2pt_params.json")

# 站点会不定期调整参数取值，参数失效时 API 返回 403。
# 请求失败时按 PARAM_CANDIDATES 逐参数轮换搜索可用组合（自愈），
# 成功后写入 PARAMS_FILE，后续运行直接复用。
DEFAULT_PARAMS = {
    "mmr": "7000",
    "order_by": "matches",
    "min_matches": "20",
    "period": "8",
    "legacy": "false",
}

PARAM_CANDIDATES = {
    "min_matches": ["20", "1", "5", "10", "50"],
    "period": ["8", "1", "6", "7", "30", "patch"],
    "mmr": ["7000", "8000", "9000"],
    "order_by": ["matches", "win_rate"],
    "legacy": ["false", "true"],
}

# 站点把"只带 User-Agent"的请求当作 bot 拒绝（403），必须带浏览器头
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://dota2protracker.com/meta?position=pos%20{pos}",
    "Origin": "https://dota2protracker.com",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


def _request_json(params, pos):
    """带浏览器头请求 stats API 并解析 JSON。"""
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{BASE_URL}?{query}&position={urllib.parse.quote(f'pos {pos}')}"
    headers = {k: v.replace("{pos}", str(pos)) for k, v in BASE_HEADERS.items()}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read())


def _try_params(params, pos):
    """请求一次，成功且非空返回 (data, None)，否则返回 (None, 原因)。"""
    try:
        data = _request_json(params, pos)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)
    if isinstance(data, list) and data:
        return data, None
    return None, "响应为空"


def _heal_params(pos):
    """参数自愈：先单参数偏离默认值轮换，再试 min_matches×period 组合。"""
    base = dict(DEFAULT_PARAMS)
    print("参数自愈：逐参数尝试候选值 ...")
    for key, values in PARAM_CANDIDATES.items():
        for v in values:
            if v == base[key]:
                continue
            trial = {**base, key: v}
            data, err = _try_params(trial, pos)
            if err is None:
                print(f"  自愈成功：{key}={v}")
                return trial, data
    for mm in PARAM_CANDIDATES["min_matches"]:
        for pd in PARAM_CANDIDATES["period"]:
            if mm == base["min_matches"] and pd == base["period"]:
                continue
            trial = {**base, "min_matches": mm, "period": pd}
            data, err = _try_params(trial, pos)
            if err is None:
                print(f"  自愈成功：min_matches={mm} period={pd}")
                return trial, data
    return None, None


def _load_params():
    """读取上次自愈保存的参数；缺失或损坏时退回默认参数。"""
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict) and saved:
            return {k: str(v) for k, v in saved.items()}
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_PARAMS)


def _save_params(params):
    os.makedirs(os.path.dirname(PARAMS_FILE), exist_ok=True)
    with open(PARAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2)


def resolve_params(pos=1):
    """确定本次运行的参数：记忆参数 -> 默认参数 -> 自愈搜索。"""
    saved = _load_params()
    if saved != DEFAULT_PARAMS:
        data, err = _try_params(saved, pos)
        if err is None:
            print(f"使用记忆参数（{PARAMS_FILE}）: {saved}")
            return saved, data
        print(f"记忆参数不可用（{err}），回退默认参数")
    data, err = _try_params(DEFAULT_PARAMS, pos)
    if err is None:
        print(f"使用默认参数: {DEFAULT_PARAMS}")
        _save_params(DEFAULT_PARAMS)
        return dict(DEFAULT_PARAMS), data
    print(f"默认参数不可用（{err}），开始自愈搜索")
    healed, data = _heal_params(pos)
    if healed is None:
        raise SystemExit("所有候选参数均不可用，需人工检查 dota2protracker 的改动")
    _save_params(healed)
    print(f"已保存自愈参数到 {PARAMS_FILE}: {healed}")
    return healed, data


# 需要从顶层去除的字段
DROP_TOP_KEYS = {
    "hero_variant", "league_id", "mmr", "period", "contest_rate",
    "daily_stats", "facet_name", "icon", "color", "background", "description",
    "position",
}

def transform_hero(hero):
    """精简英雄数据：去除冗余字段，只保留所需字段。"""
    ds = hero.get("detailed_stats", {})
    best = ds.get("best_build_winrate", {})
    result = {k: v for k, v in hero.items() if k not in DROP_TOP_KEYS}
    result["detailed_stats"] = {
        "lane_avg_adv_pct": round(ds.get("lane_avg_adv_pct"), 4) if ds.get("lane_avg_adv_pct") is not None else None,
        "best_build_winrate": best.get("win_rate"),
    }
    return result

def transform_data(data, params):
    """响应回显校验 + 精简英雄数据。"""
    # 响应自带 mmr/period 回显，与请求不符说明参数可能已被服务端改写
    if data:
        first = data[0]
        for key in ("mmr", "period"):
            echo = first.get(key)
            if echo is not None and str(echo) != str(params.get(key)):
                print(f"警告：服务端回显 {key}={echo}，与请求值 {params.get(key)} 不符，参数可能已被改写")

    data = [transform_hero(hero) for hero in data]

    # 只保留第一条的 updated_at，并加上 8 小时（UTC -> 北京时间）
    if data:
        first_ts = data[0].get("updated_at")
        if first_ts:
            data[0]["updated_at"] = (
                datetime.strptime(first_ts.replace("T", " "), "%Y-%m-%d %H:%M:%S")
                + timedelta(hours=8)
            ).strftime("%Y-%m-%d %H:%M:%S")
        for hero in data[1:]:
            hero.pop("updated_at", None)

    return data

def merge_position(existing, new):
    """存量更新：以 hero_id 为键合并，保留存量数据，更新或追加新抓取的数据。"""
    merged = {hero["hero_id"]: hero for hero in existing}
    for hero in new:
        merged[hero["hero_id"]] = hero
    return list(merged.values())

def save_positions(positions):
    """将各位置数据合并写入 d2pt_pos.json，保存时对存量数据做更新。"""
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)

    data = {}
    for pos in range(1, 6):
        key = f"pos{pos}"
        data[key] = merge_position(existing.get(key, []), positions.get(key, []))

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Saved {OUTPUT_FILE}")

def main():
    params, first_data = resolve_params()
    positions = {}
    if first_data is not None:
        positions["pos1"] = transform_data(first_data, params)
        print(f"Fetched pos 1: {len(first_data)} heroes")
    for pos in range(2, 6):
        try:
            data, err = _try_params(params, pos)
            if err is not None:
                raise RuntimeError(err)
            positions[f"pos{pos}"] = transform_data(data, params)
            print(f"Fetched pos {pos}: {len(data)} heroes")
        except Exception as e:
            print(f"Error fetching pos {pos}: {e}")
    save_positions(positions)

if __name__ == "__main__":
    main()
