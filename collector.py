#!/usr/bin/env python3
"""楽天 毎朝100商品コレクター

楽天市場のリアルタイムランキング + ROOMシード商品から、
「売れ筋(順位) × レビュー数 × 報酬率」のスコア上位100商品を選び、
ジャンル別・季節トレンド別に並べ替えて Google スプレッドシートに書き出す。

使い方:
    python collector.py              # 本番(スプレッドシートに書き込み)
    python collector.py --dry-run    # 書き込まず、結果をコンソールに表示

必要な環境変数:
    RAKUTEN_APP_ID, RAKUTEN_ACCESS_KEY, RAKUTEN_ORIGIN
    RAKUTEN_AFFILIATE_ID            (任意。あるとアフィリエイトURLが付く)
    SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON   (--dry-run のときは不要)
"""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------
# 設定(ここを書き換えて調整する)
# ------------------------------------------------------------------
# 2026年5月以降の新エンドポイント。ランキングAPIのURLは検索APIと同じ並びで
# 組み立てているので、403/404になる場合は公式ドキュメントで確認すること。
RANKING_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Ranking/20220601"
SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"

TOTAL = 100            # 1日に出す商品数
PAGES_PER_GENRE = 2    # ジャンルごとに取るページ数(1ページ=30位まで)
PER_GENRE_CAP = 15     # 1ジャンルあたりの上限(偏り防止)
DEDUP_DAYS = 7         # 過去何日分と重複する商品を除外するか

# スコアの重み(合計1.0)
W_RANK, W_REVIEW, W_RATE = 0.4, 0.3, 0.3
RATE_CAP = 10.0        # 報酬率(%)はこの値を満点として正規化
ROOM_RANK_SCORE = 0.7  # ROOMシード商品に与える「順位スコア」の固定値

# ジャンルID(表示順もこの順)。IDは楽天ランキングのURLで確認できるので、
# 実行前に必ず見直すこと。
GENRES = {
    "日用品雑貨": 215783,
    "キッチン用品・食器": 211742,
    "インテリア・寝具・収納": 100804,
    "家電": 562637,
    "ダイエット・健康": 100938,
    "美容・コスメ": 100939,
    "食品": 100227,
    "キッズ・ベビー": 100533,
    "ペット用品": 101213,
    "スポーツ・アウトドア": 101070,
}
ROOM_GENRE_DEFAULT = "ROOMピックアップ"

# 季節・イベントのタグ辞書(上から順に判定、最初にヒットしたものを採用)。
# 並び順がそのまま「季節別」シートの表示順になる。自由に追加してよい。
SEASON_TAGS = {
    "年末・大掃除": ["大掃除", "掃除", "洗剤", "カビ", "換気扇", "おせち", "お歳暮", "年賀", "正月", "福袋"],
    "クリスマス": ["クリスマス", "ツリー", "サンタ", "イルミネーション"],
    "冬・防寒": ["暖房", "ヒーター", "加湿", "毛布", "こたつ", "湯たんぽ", "防寒", "手袋",
                "マフラー", "ブランケット", "着る毛布", "あったか", "温かい", "保温", "結露", "静電気"],
    "春・新生活": ["新生活", "花粉", "入学", "入園", "桜", "お花見", "引っ越し"],
    "梅雨": ["除湿", "レイン", "傘", "部屋干し", "カビ取り", "防カビ"],
    "夏・暑さ対策": ["冷感", "扇風機", "ハンディファン", "日焼け", "UV", "虫よけ", "蚊", "冷却", "接触冷感", "保冷"],
    "秋・行楽": ["ハロウィン", "運動会", "紅葉", "キャンプ", "秋"],
}
DEFAULT_SEASON = "通年"

SHEET_TODAY_GENRE = "today_ジャンル別"
SHEET_TODAY_SEASON = "today_季節別"
SHEET_LOG = "log"
SHEET_SEEDS = "room_seeds"   # A列: 商品URL / B列(任意): ジャンル名

HEADER = ["日付", "ジャンル", "季節タグ", "ジャンル内順位", "商品名", "価格", "レビュー数",
          "レビュー平均", "報酬率(%)", "スコア", "取得元", "商品URL", "アフィリエイトURL", "商品コード"]

JST = ZoneInfo("Asia/Tokyo")


# ------------------------------------------------------------------
# 楽天API
# ------------------------------------------------------------------
def api_get(url, params, retries=4):
    headers = {"Origin": os.environ["RAKUTEN_ORIGIN"]}
    for attempt in range(retries):
        r = requests.get(url, params=params, headers=headers, timeout=20)
        if r.status_code == 200:
            time.sleep(1.1)  # レート制限対策
            return r.json()
        if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))
            continue
        raise RuntimeError(f"API error {r.status_code}: {r.text[:300]}")


def base_params():
    p = {
        "format": "json",
        "formatVersion": 2,
        "applicationId": os.environ["RAKUTEN_APP_ID"],
        "accessKey": os.environ["RAKUTEN_ACCESS_KEY"],
    }
    if os.environ.get("RAKUTEN_AFFILIATE_ID"):
        p["affiliateId"] = os.environ["RAKUTEN_AFFILIATE_ID"]
    return p


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize(raw, genre, source, genre_rank=None):
    raw = raw.get("Item", raw)  # formatVersion 1 / 2 どちらでも読めるように
    return {
        "code": raw.get("itemCode", ""),
        "name": raw.get("itemName", ""),
        "price": int(to_float(raw.get("itemPrice"))),
        "reviews": int(to_float(raw.get("reviewCount"))),
        "review_avg": to_float(raw.get("reviewAverage")),
        "rate": to_float(raw.get("affiliateRate")),
        "url": raw.get("itemUrl", ""),
        "aff_url": raw.get("affiliateUrl", ""),
        "caption": raw.get("itemCaption", ""),
        "genre": genre,
        "source": source,
        "rank": genre_rank,
    }


def fetch_ranking():
    items = []
    for genre, gid in GENRES.items():
        for page in range(1, PAGES_PER_GENRE + 1):
            params = {**base_params(), "genreId": gid, "page": page, "period": "realtime"}
            try:
                data = api_get(RANKING_URL, params)
            except RuntimeError as e:
                print(f"[warn] ランキング取得失敗 {genre} p{page}: {e}", file=sys.stderr)
                break
            for i, raw in enumerate(data.get("Items", [])):
                rank = int(to_float((raw.get("Item", raw)).get("rank"), (page - 1) * 30 + i + 1))
                it = normalize(raw, genre, "市場ランキング", rank)
                it["rank_score"] = 1 - (rank - 1) / (PAGES_PER_GENRE * 30)
                items.append(it)
    return items


def item_code_from_url(url):
    m = re.search(r"item\.rakuten\.co\.jp/([^/]+)/([^/?#]+)", url)
    return f"{m.group(1)}:{m.group(2)}" if m else None


def fetch_seeds(sh):
    """room_seeds シートの商品URLから、検索APIで商品情報を取得する。"""
    if sh is None:
        return []
    try:
        ws = sh.worksheet(SHEET_SEEDS)
    except Exception:
        return []
    items = []
    for row in ws.get_all_values()[1:]:
        if not row or not row[0].strip():
            continue
        code = item_code_from_url(row[0].strip())
        if not code:
            print(f"[skip] 商品コードを読み取れないURL: {row[0]}", file=sys.stderr)
            continue
        genre = row[1].strip() if len(row) > 1 and row[1].strip() else ROOM_GENRE_DEFAULT
        try:
            data = api_get(SEARCH_URL, {**base_params(), "itemCode": code, "hits": 1})
        except RuntimeError as e:
            print(f"[warn] シード取得失敗 {code}: {e}", file=sys.stderr)
            continue
        for raw in data.get("Items", [])[:1]:
            it = normalize(raw, genre, "ROOMシード")
            it["rank_score"] = ROOM_RANK_SCORE
            items.append(it)
    return items


# ------------------------------------------------------------------
# スコアリング・選定・並び替え
# ------------------------------------------------------------------
def season_tag(item):
    text = item["name"] + " " + item["caption"][:200]
    for tag, words in SEASON_TAGS.items():
        if any(w in text for w in words):
            return tag
    return DEFAULT_SEASON


def score_items(items):
    max_rev = max((i["reviews"] for i in items), default=1) or 1
    for it in items:
        review_score = math.log1p(it["reviews"]) / math.log1p(max_rev)
        rate_score = min(it["rate"], RATE_CAP) / RATE_CAP
        it["score"] = round(W_RANK * it["rank_score"] + W_REVIEW * review_score + W_RATE * rate_score, 4)
        it["season"] = season_tag(it)
    return items


def merge_duplicates(items):
    """同じ商品がランキングとROOMの両方にある場合は1件にまとめる(ランキング側を優先)。"""
    by_code = {}
    for it in items:
        if not it["code"]:
            continue
        if it["code"] in by_code:
            by_code[it["code"]]["source"] = "市場ランキング+ROOM"
            continue
        by_code[it["code"]] = it
    return list(by_code.values())


def select(items, seen_codes):
    pool = [i for i in items if i["code"] not in seen_codes]
    pool.sort(key=lambda i: i["score"], reverse=True)
    chosen, per_genre = [], {}
    for it in pool:
        if len(chosen) >= TOTAL:
            break
        if per_genre.get(it["genre"], 0) >= PER_GENRE_CAP:
            continue
        chosen.append(it)
        per_genre[it["genre"]] = per_genre.get(it["genre"], 0) + 1
    if len(chosen) < TOTAL:  # 上限で埋まらなかった分は上限を外して補充
        chosen_codes = {i["code"] for i in chosen}
        for it in pool:
            if len(chosen) >= TOTAL:
                break
            if it["code"] not in chosen_codes:
                chosen.append(it)
    return chosen


def sort_by_genre(items):
    order = {g: n for n, g in enumerate(list(GENRES) + [ROOM_GENRE_DEFAULT])}
    s_order = {t: n for n, t in enumerate(list(SEASON_TAGS) + [DEFAULT_SEASON])}
    return sorted(items, key=lambda i: (order.get(i["genre"], 99), s_order[i["season"]], -i["score"]))


def sort_by_season(items):
    order = {g: n for n, g in enumerate(list(GENRES) + [ROOM_GENRE_DEFAULT])}
    s_order = {t: n for n, t in enumerate(list(SEASON_TAGS) + [DEFAULT_SEASON])}
    return sorted(items, key=lambda i: (s_order[i["season"]], order.get(i["genre"], 99), -i["score"]))


def to_rows(items, date_str):
    rows = []
    for it in items:
        rows.append([date_str, it["genre"], it["season"], it["rank"] or "", it["name"], it["price"],
                     it["reviews"], it["review_avg"], it["rate"], it["score"], it["source"],
                     it["url"], it["aff_url"], it["code"]])
    return rows


# ------------------------------------------------------------------
# Googleスプレッドシート
# ------------------------------------------------------------------
def open_sheet():
    import gspread
    gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    return gc.open_by_key(os.environ["SPREADSHEET_ID"])


def get_ws(sh, title):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=1000, cols=len(HEADER))


def load_seen_codes(sh, today):
    ws = get_ws(sh, SHEET_LOG)
    cutoff = (today - timedelta(days=DEDUP_DAYS)).strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    seen = set()
    for row in ws.get_all_values()[1:]:
        if len(row) > 13 and cutoff <= row[0] < today_s:
            seen.add(row[13])
    return seen


def write_sheets(sh, today_s, by_genre, by_season):
    for title, items in ((SHEET_TODAY_GENRE, by_genre), (SHEET_TODAY_SEASON, by_season)):
        ws = get_ws(sh, title)
        ws.clear()
        ws.update(values=[HEADER] + to_rows(items, today_s), range_name="A1")

    ws = get_ws(sh, SHEET_LOG)
    values = ws.get_all_values()
    if not values:
        ws.update(values=[HEADER], range_name="A1")
        values = [HEADER]
    # 同じ日に再実行した場合は、その日の行を消してから追記する
    idx = [n for n, r in enumerate(values, start=1) if n > 1 and r and r[0] == today_s]
    if idx:
        ws.delete_rows(idx[0], idx[-1])
    ws.append_rows(to_rows(by_genre, today_s), value_input_option="RAW")


# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="スプレッドシートに書き込まず表示だけ行う")
    args = ap.parse_args()

    today = datetime.now(JST)
    today_s = today.strftime("%Y-%m-%d")

    sh = None if args.dry_run else open_sheet()
    seen = load_seen_codes(sh, today) if sh else set()

    items = fetch_ranking() + fetch_seeds(sh)
    items = merge_duplicates(items)
    items = score_items(items)
    chosen = select(items, seen)

    by_genre, by_season = sort_by_genre(chosen), sort_by_season(chosen)
    print(f"{today_s}: 候補{len(items)}件 / 過去{DEDUP_DAYS}日の重複除外{len(seen)}件 / 選定{len(chosen)}件")

    if args.dry_run:
        for it in by_genre:
            print(f"{it['genre']}\t{it['season']}\t{it['score']}\t{it['reviews']}件\t{it['rate']}%\t{it['name'][:40]}")
        return
    write_sheets(sh, today_s, by_genre, by_season)
    print("スプレッドシートに書き込みました")


if __name__ == "__main__":
    main()
