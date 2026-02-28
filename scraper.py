"""助成金スクレイパー - CANPAN / NPOWEB / 助成財団センターから助成金情報を収集"""
import requests
from bs4 import BeautifulSoup
import re
import json
import time
import hashlib
from datetime import datetime
from pathlib import Path
import logging

try:
    import feedparser
except ImportError:
    feedparser = None

from config import (
    HIGH_PRIORITY_KEYWORDS, LOW_PRIORITY_KEYWORDS, EXCLUDE_KEYWORDS,
    AMOUNT_THRESHOLD, SCORE_THRESHOLD, EXCLUDED_REGION_PATTERNS,
    CANPAN_BASE_URL, NPOWEB_RSS_URL, JFC_URL,
    REQUEST_DELAY, REQUEST_TIMEOUT, MAX_PAGES, HEADERS, DATA_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# ユーティリティ
# =============================================================================

def _get(url: str) -> requests.Response | None:
    """HTTP GET with error handling."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.encoding = resp.apparent_encoding or "utf-8"
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.warning(f"GET失敗 {url}: {e}")
        return None


def parse_amount(text: str) -> int | None:
    """日本語テキストから助成金額（円）を抽出する。最大額を返す。"""
    amounts: list[int] = []

    # X億円
    for m in re.finditer(r"(\d[\d,.]*)\s*億\s*円", text):
        try:
            amounts.append(int(float(m.group(1).replace(",", "")) * 100_000_000))
        except ValueError:
            pass

    # X万円
    for m in re.finditer(r"(\d[\d,.]*)\s*万\s*円", text):
        try:
            amounts.append(int(float(m.group(1).replace(",", "")) * 10_000))
        except ValueError:
            pass

    # X,XXX,XXX円（カンマ区切り直接表記）
    for m in re.finditer(r"(\d{1,3}(?:,\d{3})+)\s*円", text):
        try:
            amounts.append(int(m.group(1).replace(",", "")))
        except ValueError:
            pass

    return max(amounts) if amounts else None


def extract_amount_text(text: str) -> str:
    """金額に関する記述を抽出。"""
    patterns = [
        r"助成[金額総]*[：:]\s*(.+?)(?:\n|$)",
        r"助成[金額総]*\s+(.+?万円.+?)(?:\n|$)",
        r"(\d[\d,.]*万円[^\n]{0,50})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()[:120]
    return ""


def check_region(text: str) -> tuple[bool, str]:
    """地域制限をチェック。(適格かどうか, 地域テキスト) を返す。"""
    # 明確な除外パターンにマッチしたら不適格
    for pattern in EXCLUDED_REGION_PATTERNS:
        if pattern in text:
            return False, pattern

    # 対象地域の明示を探す
    region_patterns = [
        r"対象地域[：:]\s*(.+?)(?:\n|$)",
        r"活動地域[：:]\s*(.+?)(?:\n|$)",
        r"対象エリア[：:]\s*(.+?)(?:\n|$)",
        r"助成対象[：:]\s*(.+?)(?:に所在|に拠点)",
    ]
    for pat in region_patterns:
        m = re.search(pat, text)
        if m:
            return True, m.group(1).strip()[:60]

    return True, "指定なし"


def score_grant(grant: dict) -> tuple[int, list[str]]:
    """キーワードマッチングで適合スコアを算出。"""
    text = " ".join([
        grant.get("name", ""),
        grant.get("summary", ""),
        grant.get("categories", ""),
        grant.get("full_text", ""),
    ])

    score = 0
    matched: list[str] = []

    for kw in HIGH_PRIORITY_KEYWORDS:
        if kw in text:
            score += 2
            matched.append(kw)

    for kw in LOW_PRIORITY_KEYWORDS:
        if kw in text:
            score += 1
            matched.append(kw)

    return score, matched


def make_id(source: str, url: str) -> str:
    """URLベースの一意IDを生成。"""
    return f"{source}_{hashlib.md5(url.encode()).hexdigest()[:10]}"


# =============================================================================
# CANPAN Fields スクレイパー
# =============================================================================

def scrape_canpan() -> list[dict]:
    """CANPAN Fields の助成金リストをスクレイピング。"""
    grants: list[dict] = []
    seen_urls: set[str] = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = f"{CANPAN_BASE_URL}?page={page_num}&sort=1"
        logger.info(f"CANPAN ページ {page_num} 取得中: {url}")

        resp = _get(url)
        if resp is None:
            break

        soup = BeautifulSoup(resp.text, "lxml")

        # 助成金詳細へのリンクを抽出
        grant_links = soup.find_all("a", href=re.compile(r"/grant/detail/\d+"))
        if not grant_links:
            logger.info(f"CANPAN ページ {page_num}: リンクなし。終了。")
            break

        page_count = 0
        for link in grant_links:
            href = link.get("href", "")
            m = re.search(r"/grant/detail/(\d+)", href)
            if not m:
                continue

            detail_url = f"https://fields.canpan.info/grant/detail/{m.group(1)}"
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            title = link.get_text(strip=True)
            if not title:
                continue

            # 親要素からステータスを確認
            parent = link.find_parent(["div", "li", "tr", "article", "section"])
            parent_text = parent.get_text() if parent else ""

            # 募集終了は除外
            if "募集終了" in parent_text and "募集中" not in parent_text:
                continue

            # 団体名を抽出
            org = ""
            if parent:
                org_link = parent.find("a", href=re.compile(r"/organization/detail/"))
                if org_link:
                    org = org_link.get_text(strip=True)

            grants.append({
                "id": make_id("canpan", detail_url),
                "name": title,
                "url": detail_url,
                "source": "CANPAN",
                "organization": org,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
            })
            page_count += 1

        logger.info(f"CANPAN ページ {page_num}: {page_count}件取得")
        time.sleep(REQUEST_DELAY)

    # 詳細ページを取得
    logger.info(f"CANPAN 詳細ページ取得: {len(grants)}件")
    for grant in grants:
        detail = _scrape_canpan_detail(grant["url"])
        grant.update(detail)
        time.sleep(REQUEST_DELAY)

    return grants


def _scrape_canpan_detail(url: str) -> dict:
    """CANPAN 助成金詳細ページをパース。"""
    result = {
        "summary": "",
        "categories": "",
        "deadline": "",
        "status": "要確認",
        "amount_text": "",
        "amount_value": None,
        "full_text": "",
    }

    resp = _get(url)
    if resp is None:
        return result

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(separator="\n")
    result["full_text"] = text[:3000]

    # カテゴリ / 対象分野
    for heading in soup.find_all(["dt", "th", "h3", "h4", "strong"]):
        h_text = heading.get_text()
        if "対象分野" in h_text:
            nxt = heading.find_next(["dd", "td", "p", "div", "span"])
            if nxt:
                result["categories"] = nxt.get_text(strip=True)[:120]
            break

    # 募集期間 → 締切日
    for heading in soup.find_all(["dt", "th", "h3", "h4", "strong"]):
        h_text = heading.get_text()
        if "募集時期" in h_text or "募集期間" in h_text:
            nxt = heading.find_next(["dd", "td", "p", "div", "span"])
            if nxt:
                period = nxt.get_text(strip=True)
                # 終了日を取得（「〜」の後の日付）
                parts = re.split(r"[～〜~\-―]", period)
                target = parts[-1] if len(parts) > 1 else period
                dm = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", target)
                if dm:
                    result["deadline"] = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            break

    # ステータス
    if "募集中" in text:
        result["status"] = "募集中"
    elif "募集予定" in text:
        result["status"] = "募集予定"
    elif "募集終了" in text:
        result["status"] = "募集終了"

    # 金額
    result["amount_value"] = parse_amount(text)
    result["amount_text"] = extract_amount_text(text)

    # 概要（「内容／対象」セクション以降の本文を抽出）
    SKIP_PREFIXES = [
        "最終更新日時", "助成制度名", "実施団体", "関連URL", "お問い合わせ先",
        "募集ステータス", "募集時期", "募集期間", "対象分野", "対象事業",
    ]
    content_started = False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "内容" in line and ("対象" in line or "概要" in line or "説明" in line):
            content_started = True
            continue
        if content_started and len(line) > 30:
            result["summary"] = line[:300]
            break
    # フォールバック: 内容セクションが見つからない場合
    if not result["summary"]:
        for line in text.split("\n"):
            line = line.strip()
            if not line or len(line) < 30:
                continue
            if any(line.startswith(p) for p in SKIP_PREFIXES):
                continue
            result["summary"] = line[:300]
            break

    return result


# =============================================================================
# NPOWEB RSS スクレイパー
# =============================================================================

def scrape_npoweb_rss() -> list[dict]:
    """NPOWEB の RSS フィードから助成金情報を取得。"""
    if feedparser is None:
        logger.warning("feedparser がインストールされていません。NPOWEB をスキップ。")
        return []

    grants: list[dict] = []

    try:
        logger.info(f"NPOWEB RSS 取得中: {NPOWEB_RSS_URL}")
        feed = feedparser.parse(NPOWEB_RSS_URL)

        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            summary_raw = entry.get("summary", entry.get("description", ""))

            # メールマガジンのまとめ記事を除外（「No.XXX」「【新着XX件】」パターン）
            if re.match(r"^No\.\d+", title) or "新着" in title and "件" in title:
                continue

            # 助成金関連のエントリのみ
            combined = title + summary_raw
            if not any(kw in combined for kw in ["助成", "補助", "奨励", "支援金", "基金"]):
                continue

            summary_text = BeautifulSoup(summary_raw, "lxml").get_text()[:500]

            grants.append({
                "id": make_id("npoweb", link),
                "name": title,
                "url": link,
                "source": "NPOWEB",
                "organization": "",
                "summary": summary_text[:300],
                "categories": "",
                "deadline": "",
                "status": "要確認",
                "amount_text": extract_amount_text(summary_text),
                "amount_value": parse_amount(summary_text),
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "full_text": summary_text,
            })

    except Exception as e:
        logger.error(f"NPOWEB RSS エラー: {e}")

    return grants


# =============================================================================
# 助成財団センター（助成情報navi）スクレイパー
# =============================================================================

def scrape_jfc() -> list[dict]:
    """助成財団センターのトップページから最新助成情報を取得。"""
    grants: list[dict] = []

    resp = _get(JFC_URL)
    if resp is None:
        return grants

    try:
        soup = BeautifulSoup(resp.text, "lxml")

        for link in soup.find_all("a", href=True):
            title = link.get_text(strip=True)
            href = link.get("href", "")

            if not title or len(title) < 5:
                continue

            # 助成金プログラムらしいリンクのみ
            if not any(kw in title for kw in ["助成", "補助", "奨励", "支援", "基金", "募集"]):
                continue

            full_url = href if href.startswith("http") else f"https://jyosei-navi.jfc.or.jp{href}"

            grants.append({
                "id": make_id("jfc", full_url),
                "name": title,
                "url": full_url,
                "source": "助成財団センター",
                "organization": "",
                "summary": "",
                "categories": "",
                "deadline": "",
                "status": "要確認",
                "amount_text": "",
                "amount_value": None,
                "found_date": datetime.now().strftime("%Y-%m-%d"),
                "full_text": title,
            })

    except Exception as e:
        logger.error(f"助成財団センター エラー: {e}")

    return grants


# =============================================================================
# メイン処理
# =============================================================================

def load_existing() -> dict:
    """既存データを読み込む。"""
    data_file = DATA_DIR / "grants.json"
    if data_file.exists():
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"既存データ読み込みエラー: {e}")
    return {"last_updated": None, "grants": []}


def save_grants(data: dict):
    """データを JSON に保存。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_file = DATA_DIR / "grants.json"
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"保存完了: {len(data['grants'])}件 → {data_file}")


def run():
    """全スクレイパーを実行し、フィルタリング・スコアリングして保存。"""
    logger.info("=" * 60)
    logger.info("助成金スクレイパー開始")
    logger.info("=" * 60)

    existing = load_existing()
    existing_ids = {g["id"] for g in existing.get("grants", [])}

    # 各ソースからスクレイピング
    all_grants: list[dict] = []

    logger.info("--- CANPAN Fields ---")
    all_grants.extend(scrape_canpan())

    logger.info("--- NPOWEB RSS ---")
    all_grants.extend(scrape_npoweb_rss())

    logger.info("--- 助成財団センター ---")
    all_grants.extend(scrape_jfc())

    logger.info(f"取得合計: {len(all_grants)}件")

    # URL で重複除去
    seen_urls: set[str] = set()
    unique: list[dict] = []
    for g in all_grants:
        if g["url"] not in seen_urls:
            seen_urls.add(g["url"])
            unique.append(g)
    logger.info(f"重複除去後: {len(unique)}件")

    # スコアリング・フィルタリング
    filtered: list[dict] = []
    for grant in unique:
        score, matched = score_grant(grant)
        grant["relevance_score"] = score
        grant["matched_keywords"] = matched

        region_ok, region_text = check_region(grant.get("full_text", ""))
        grant["region"] = region_text

        # フィルタ適用
        if score < SCORE_THRESHOLD:
            continue
        if not region_ok:
            continue
        # 除外キーワードチェック（助成金名・カテゴリに含まれる場合は除外）
        exclude_target = grant.get("name", "") + grant.get("categories", "")
        if any(kw in exclude_target for kw in EXCLUDE_KEYWORDS):
            continue
        if grant.get("amount_value") and grant["amount_value"] < AMOUNT_THRESHOLD:
            continue
        # 明らかに期限切れの助成金を除外
        if grant.get("deadline"):
            try:
                dl = datetime.strptime(grant["deadline"], "%Y-%m-%d").date()
                if dl < datetime.now().date():
                    continue
            except ValueError:
                pass

        # 新着フラグ
        grant["is_new"] = grant["id"] not in existing_ids

        # 保存前に full_text を除去（容量節約）
        grant.pop("full_text", None)

        filtered.append(grant)

    logger.info(f"フィルタ後: {len(filtered)}件")

    # スコア降順でソート
    filtered.sort(key=lambda g: g.get("relevance_score", 0), reverse=True)

    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "grants": filtered,
    }
    save_grants(data)

    logger.info("助成金スクレイパー完了")
    return data


if __name__ == "__main__":
    run()
