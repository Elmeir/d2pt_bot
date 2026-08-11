import os
import json
from datetime import datetime, timedelta
import urllib.request
import urllib.parse

BASE_URL = "https://dota2protracker.com/api/heroes/stats"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

def fetch_and_save(pos):
    params = {
        "mmr": "7000",
        "position": f"pos {pos}",
        "order_by": "matches",
        "min_matches": "20",
        "period": "8",
        "legacy": "false"
    }
    query_string = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{BASE_URL}?{query_string}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read())

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

    output_file = os.path.join(OUTPUT_DIR, f"d2pt_pos{pos}.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Saved {output_file}")

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

def main():
    for pos in range(1, 6):
        try:
            fetch_and_save(pos)
        except Exception as e:
            print(f"Error fetching pos {pos}: {e}")

if __name__ == "__main__":
    main()