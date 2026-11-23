"""抓取 Dotabuff 英雄 Meta 数据（默认 Turbo 模式 / 7 天 / 冠绝一世）。

页面由 Cloudflare 防护，实测 firefox 指纹可通过，chrome 指纹会被 403 拦截；
且放行不稳定，因此带重试。表格为服务端渲染，直接解析 HTML。
"""
import os
import re
import json
import html
import time
import argparse
import urllib.request
from datetime import datetime, timedelta, timezone

from curl_cffi import requests

BASE_URL = "https://www.dotabuff.com/heroes"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# 站点按 UA 指纹拦截，firefox133 当前可稳定拿到 200
IMPERSONATE = "firefox133"
HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.dotabuff.com/heroes",
}

# 列顺序：Hero / Tier / Win rate / Change / Pick rate / Change / Ban rate
EXPECTED_COLUMNS = 7
MIN_ROWS = 100


def build_url(mode, date, rank_tier):
    return (
        f"{BASE_URL}?show=heroes&view=meta&mode={mode}&date={date}&rankTier={rank_tier}"
    )


def fetch_html(url, attempts=6, interval=5):
    """请求页面，遇 Cloudflare 拦截时重试。"""
    session = requests.Session(impersonate=IMPERSONATE)
    last = None
    for i in range(attempts):
        try:
            resp = session.get(url, headers=HEADERS, timeout=60)
            last = f"HTTP {resp.status_code}，{len(resp.text)} 字节"
            if resp.status_code == 200 and "<table" in resp.text:
                return resp.text
        except Exception as e:
            last = str(e)
        if i < attempts - 1:
            time.sleep(interval)
    print(f"直连失败（已重试 {attempts} 次）：{last}，改用 r.jina.ai 兜底")
    return fetch_via_jina(url)


def fetch_via_jina(url):
    """兜底：通过 r.jina.ai 读取页面 HTML。

    CI 出口 IP 容易被 Cloudflare 整段拦截，jina 服务端抓取可绕过；
    返回值仍是原始 HTML，表格结构与直连一致。
    """
    proxy_url = f"https://r.jina.ai/{url}"
    req = urllib.request.Request(
        proxy_url, headers={"User-Agent": "Mozilla/5.0", "X-Return-Format": "html"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            page = resp.read().decode("utf-8", "replace")
    except Exception as e:
        raise SystemExit(f"r.jina.ai 兜底也失败：{e}")
    if "<table" not in page:
        raise SystemExit(f"r.jina.ai 返回内容不含数据表格（{len(page)} 字节），疑似被限流")
    return page


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _number(text):
    """'60.49 %' -> 60.49；'-2.77 %' -> -2.77；无有效值时返回 None。"""
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def parse_heroes(page):
    table = re.search(r"<table.*?</table>", page, re.S)
    if not table:
        raise SystemExit("页面中未找到数据表格，Dotabuff 可能已改版")
    rows = re.findall(r"<tr.*?</tr>", table.group(0), re.S)[1:]  # 跳过表头
    if len(rows) < MIN_ROWS:
        raise SystemExit(f"仅解析到 {len(rows)} 行，低于预期 {MIN_ROWS}，可能已改版")

    heroes = []
    for row in rows:
        tds = re.findall(r"<td.*?</td>", row, re.S)
        if len(tds) != EXPECTED_COLUMNS:
            raise SystemExit(f"列数变为 {len(tds)}（预期 {EXPECTED_COLUMNS}），可能已改版")
        link = re.search(r'<a href="/heroes/([^"]+)">([^<]+)</a>', tds[0])
        if not link:
            raise SystemExit("英雄列中未找到链接，可能已改版")
        cells = [_text(td) for td in tds]
        heroes.append(
            {
                "hero": html.unescape(link.group(2)),
                "slug": link.group(1),
                "tier": cells[1],
                "win_rate": _number(cells[2]),
                "win_rate_change": _number(cells[3]),
                "pick_rate": _number(cells[4]),
                "pick_rate_change": _number(cells[5]),
                "ban_rate": _number(cells[6]),
            }
        )
    return heroes


def main():
    parser = argparse.ArgumentParser(description="抓取 Dotabuff 英雄 Meta 数据")
    parser.add_argument("--mode", default="turbo", help="游戏模式（turbo / all_pick）")
    parser.add_argument("--date", default="7d", help="时间范围（7d / 30d / patch 等）")
    parser.add_argument("--rank", default="immortal", help="段位（immortal / all 等）")
    parser.add_argument("--out", default=None, help="输出文件路径")
    args = parser.parse_args()

    url = build_url(args.mode, args.date, args.rank)
    heroes = parse_heroes(fetch_html(url))

    output_file = args.out or os.path.join(OUTPUT_DIR, "data", f"dotabuff_{args.mode}.json")
    result = {
        "source": url,
        "mode": args.mode,
        "date": args.date,
        "rank_tier": args.rank,
        # 站点时间为 UTC，加 8 小时转为北京时间
        "fetched_at": (
            datetime.now(timezone.utc) + timedelta(hours=8)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "heroes": heroes,
    }

    # 数据无变化时不写文件，避免仅因 fetched_at 变更产生无意义的提交
    existing = None
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except ValueError:
            pass
    if existing and all(
        existing.get(key) == result[key] for key in ("source", "mode", "date", "rank_tier", "heroes")
    ):
        print(f"数据无变化，跳过写入 {output_file}")
        return

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Fetched {len(heroes)} heroes")
    print(f"Saved {output_file}")


if __name__ == "__main__":
    main()
