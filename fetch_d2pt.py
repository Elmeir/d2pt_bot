import json
import os
import time
import urllib.request
from urllib.parse import urlencode, quote

BASE_URL = "https://dota2protracker.com/api/heroes/stats"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
POSITIONS = range(1, 6)
SLEEP_SECONDS = 1

# 固定查询参数
BASE_PARAMS = {
    "mmr": "7000",
    "order_by": "matches",
    "min_matches": "20",
    "period": "8",
    "legacy": "false",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
}
TIMEOUT = 30


def build_url(pos: int) -> str:
    """拼接指定位置的查询 URL。"""
    params = {**BASE_PARAMS, "position": f"pos {pos}"}
    return f"{BASE_URL}?{urlencode(params, quote_via=quote)}"


def fetch_json(pos: int) -> dict:
    """请求指定位置的统计数据并返回解析后的 JSON。"""
    req = urllib.request.Request(build_url(pos), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def save_json(pos: int, data: dict) -> None:
    """将数据保存到对应位置的 JSON 文件。"""
    output_file = os.path.join(OUTPUT_DIR, f"d2pt_pos{pos}.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Saved {output_file}")


def fetch_and_save(pos: int) -> None:
    """抓取并保存单个位置的数据。"""
    save_json(pos, fetch_json(pos))


def main() -> None:
    for i, pos in enumerate(POSITIONS):
        if i > 0:
            time.sleep(SLEEP_SECONDS)
        try:
            fetch_and_save(pos)
        except Exception as e:
            print(f"Error fetching pos {pos}: {e}")


if __name__ == "__main__":
    main()
