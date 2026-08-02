import os
import urllib.request
import urllib.parse

BASE_URL = "https://dota2protracker.com/api/heroes/stats"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

def fetch_and_save(pos):
    params = {
        "mmr": "7000",
        "position": f"pos {pos}",
        "order_by": "matches",
        "min_matches": "1",
        "period": "patch",
        "legacy": "false"
    }
    query_string = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"{BASE_URL}?{query_string}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()

    output_file = os.path.join(OUTPUT_DIR, f"d2pt_pos{pos}.json")
    with open(output_file, "wb") as f:
        f.write(data)

    print(f"Saved {output_file}")

def main():
    for pos in range(1, 6):
        try:
            fetch_and_save(pos)
        except Exception as e:
            print(f"Error fetching pos {pos}: {e}")

if __name__ == "__main__":
    main()
