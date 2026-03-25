import html as ihtml
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse, quote

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from readability import Document


@dataclass(frozen=True)
class Article:
    idx: int
    title: str
    url: str
    source: str
    published: str
    published_dt: datetime          # ★ 파싱된 datetime (정렬·필터용)
    text: str
    category: str  # 'semiconductor' | 'energy' | 'macro' | 'global_event'


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
KST = timezone(timedelta(hours=9))

# ── 구글 RSS (해외) ──────────────────────────────────────────
GOOGLE_RSS_QUERIES = [
    ("semiconductor market", "semiconductor"),
    ("NVIDIA stock earnings", "semiconductor"),
    ("Samsung Electronics chip", "semiconductor"),
    ("TSMC foundry", "semiconductor"),
    ("AI chip demand", "semiconductor"),
    ("nuclear energy stock", "energy"),
    ("CEG Constellation Energy", "energy"),
    ("Doosan Enerbility", "energy"),
    ("LNG oil price", "energy"),
    ("renewable energy policy", "energy"),
    ("Federal Reserve interest rate", "macro"),
    ("US China trade tariff", "macro"),
    ("global inflation economy", "macro"),
    ("Korea economy export", "macro"),
    ("Middle East war oil", "global_event"),
    ("geopolitical risk market", "global_event"),
    ("pandemic outbreak disease", "global_event"),
]

# ── 네이버 뉴스 검색 쿼리 (국내) ────────────────────────────
NAVER_QUERIES = [
    ("삼성전자 반도체", "semiconductor"),
    ("SK하이닉스 HBM", "semiconductor"),
    ("엔비디아 주가", "semiconductor"),
    ("반도체 수출 경기", "semiconductor"),
    ("와이씨 주가", "semiconductor"),
    ("두산에너빌리티 원전", "energy"),
    ("한국 원자력 에너지", "energy"),
    ("CEG 에너지 주가", "energy"),
    ("LNG 천연가스 가격", "energy"),
    ("한국 경제 수출 환율", "macro"),
    ("코스피 외국인 수급", "macro"),
    ("미국 금리 한국 영향", "macro"),
    ("중동 전쟁 한국 경제", "global_event"),
    ("지정학 리스크 주식", "global_event"),
]

# ── 네이버 주요 신문사 RSS ───────────────────────────────────
NAVER_NEWSPAPER_RSS = [
    ("https://rss.hankyung.com/economy.xml", "macro"),
    ("https://rss.hankyung.com/it.xml", "semiconductor"),
    ("https://www.mk.co.kr/rss/30000001/", "macro"),
    ("https://www.mk.co.kr/rss/30200030/", "semiconductor"),
    ("https://feeds.feedburner.com/mt/economy", "macro"),
    ("https://www.sedaily.com/RSS/economic.xml", "macro"),
]

FALLBACK_TOPICS_PROMPT = """오늘은 {today}입니다.
수집된 뉴스가 없습니다. 아래 보유 종목과 관련된 오늘 날짜 기준 주요 이슈 및 투자 인사이트를 직접 분석해주세요.

보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)

다음 카테고리별로 최신 동향과 투자 인사이트를 작성해주세요:
- 반도체: AI 반도체 수요, 메모리 가격 동향, 주요 기업 실적
- 에너지: 원전 정책, LNG 가격, 재생에너지 동향
- 거시경제: 금리 정책, 환율, 무역 이슈
- 글로벌 이벤트: 지정학적 리스크, 주요 이벤트

마지막에 오늘의 투자 인사이트를 종목별로 정리해주세요.
"""

# ★ 최대 허용 시간 (초): 기본 24시간
MAX_AGE_SECONDS = int(os.getenv("MAX_AGE_HOURS", "24")) * 3600


# ── 헬퍼 함수 ────────────────────────────────────────────────
def _env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"환경 변수 '{key}'가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return val


def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


# ── 날짜 파싱 ────────────────────────────────────────────────
_DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
]


def parse_published(published: str) -> Optional[datetime]:
    """RSS published 문자열 → timezone-aware datetime (UTC)."""
    if not published:
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(published, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def age_seconds(dt: datetime, now: datetime) -> float:
    """dt 기준 현재까지 경과 초. dt가 미래면 0 반환."""
    diff = now - dt
    return max(diff.total_seconds(), 0.0)


def is_recent(published_dt: Optional[datetime], now: datetime, max_age: int = MAX_AGE_SECONDS) -> bool:
    """
    ★ 핵심 변경점 ★
    - published_dt가 None(파싱 실패)이면 무조건 False → 오래된 기사 차단
    - 경과 시간이 max_age 초 이내인 기사만 True
    """
    if published_dt is None:
        return False  # 날짜 불명 → 버린다
    return age_seconds(published_dt, now) <= max_age


# ── 기사 본문 추출 ───────────────────────────────────────────
def fetch_article_text(url: str, max_chars: int = 1500) -> str:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        doc = Document(resp.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        return text[:max_chars]
    except Exception as e:
        print(f"  [본문 추출 실패] {url}: {e}")
        return ""


# ── 구글 뉴스 RSS 수집 ───────────────────────────────────────
def collect_google_rss(max_per_query: int = 3, now: Optional[datetime] = None) -> List[Article]:
    articles: List[Article] = []
    idx = 0
    if now is None:
        now = datetime.now(timezone.utc)

    # ★ Google RSS: when=1d 파라미터로 최근 24시간 기사만 요청
    for query, category in GOOGLE_RSS_QUERIES:
        encoded_query = quote(query)
        rss_url = (
            f"https://news.google.com/rss/search"
            f"?q={encoded_query}+when:1d&hl=en&gl=US&ceid=US:en"
        )
        try:
            feed = feedparser.parse(rss_url)
            collected = 0
            for entry in feed.entries:
                if collected >= max_per_query:
                    break
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                published = entry.get("published", "")
                source = urlparse(url).netloc.replace("www.", "")

                if not title or not url:
                    continue

                published_dt = parse_published(published)

                # ★ 날짜 파싱 실패 or 24시간 초과 → 버린다
                if not is_recent(published_dt, now):
                    age_str = f"{age_seconds(published_dt, now)/3600:.1f}h ago" if published_dt else "날짜불명"
                    print(f"  [Google RSS 스킵] {age_str} | {title[:40]}")
                    continue

                text = fetch_article_text(url)
                articles.append(Article(
                    idx=idx,
                    title=title,
                    url=url,
                    source=source,
                    published=published,
                    published_dt=published_dt,
                    text=text,
                    category=category,
                ))
                idx += 1
                collected += 1
                age_h = age_seconds(published_dt, now) / 3600
                print(f"  [Google RSS ✓] {age_h:.1f}h ago | {category} | {title[:45]}")
        except Exception as e:
            print(f"  [Google RSS 실패] {query}: {e}")

    return articles


# ── 네이버 뉴스 검색 수집 ────────────────────────────────────
def collect_naver_search(max_per_query: int = 3, now: Optional[datetime] = None) -> List[Article]:
    articles: List[Article] = []
    idx = 10000
    if now is None:
        now = datetime.now(timezone.utc)

    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("  [네이버 API] 키 미설정, 건너뜀")
        return articles

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    for query, category in NAVER_QUERIES:
        try:
            # ★ 넉넉히 가져오고 파이썬에서 엄격히 필터
            resp = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={"query": query, "display": max_per_query * 5, "sort": "date"},
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            collected = 0
            for item in items:
                if collected >= max_per_query:
                    break
                title = BeautifulSoup(item.get("title", ""), "html.parser").get_text()
                url = item.get("originallink") or item.get("link", "")
                published = item.get("pubDate", "")
                source = urlparse(url).netloc.replace("www.", "")
                description = BeautifulSoup(item.get("description", ""), "html.parser").get_text()

                if not title or not url:
                    continue

                published_dt = parse_published(published)

                if not is_recent(published_dt, now):
                    age_str = f"{age_seconds(published_dt, now)/3600:.1f}h ago" if published_dt else "날짜불명"
                    print(f"  [Naver 스킵] {age_str} | {title[:40]}")
                    continue

                text = fetch_article_text(url) or description
                articles.append(Article(
                    idx=idx,
                    title=title,
                    url=url,
                    source=source,
                    published=published,
                    published_dt=published_dt,
                    text=text,
                    category=category,
                ))
                idx += 1
                collected += 1
                age_h = age_seconds(published_dt, now) / 3600
                print(f"  [Naver ✓] {age_h:.1f}h ago | {category} | {title[:45]}")
        except Exception as e:
            print(f"  [Naver Search 실패] {query}: {e}")

    return articles


# ── 네이버 신문사 RSS 수집 ───────────────────────────────────
def collect_naver_newspaper_rss(max_per_feed: int = 5, now: Optional[datetime] = None) -> List[Article]:
    articles: List[Article] = []
    idx = 20000
    if now is None:
        now = datetime.now(timezone.utc)

    for rss_url, category in NAVER_NEWSPAPER_RSS:
        try:
            feed = feedparser.parse(rss_url)
            source = urlparse(rss_url).netloc.replace("www.", "").replace("rss.", "")
            collected = 0
            for entry in feed.entries:
                if collected >= max_per_feed:
                    break
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                published = entry.get("published", "")

                if not title or not url:
                    continue

                published_dt = parse_published(published)

                if not is_recent(published_dt, now):
                    age_str = f"{age_seconds(published_dt, now)/3600:.1f}h ago" if published_dt else "날짜불명"
                    print(f"  [Newspaper 스킵] {age_str} | {title[:40]}")
                    continue

                text = fetch_article_text(url)
                articles.append(Article(
                    idx=idx,
                    title=title,
                    url=url,
                    source=source,
                    published=published,
                    published_dt=published_dt,
                    text=text,
                    category=category,
                ))
                idx += 1
                collected += 1
                age_h = age_seconds(published_dt, now) / 3600
                print(f"  [Newspaper ✓] {age_h:.1f}h ago | {category} | {title[:45]}")
        except Exception as e:
            print(f"  [Newspaper RSS 실패] {rss_url}: {e}")

    return articles


# ── 중복 제거 + 최신순 정렬 ─────────────────────────────────
def deduplicate_and_sort(articles: List[Article]) -> List[Article]:
    """URL 기준 중복 제거 후 최신 기사가 먼저 오도록 정렬."""
    seen: set = set()
    unique: List[Article] = []
    for a in articles:
        if a.url not in seen:
            seen.add(a.url)
            unique.append(a)
    # ★ 최신순 정렬: published_dt 내림차순
    unique.sort(key=lambda a: a.published_dt, reverse=True)
    return unique


# ── 전체 수집 진입점 ─────────────────────────────────────────
def collect_articles(max_articles: int = 60) -> List[Article]:
    now = datetime.now(timezone.utc)  # ★ 단일 기준 시각

    print(f"[기준 시각] {now.astimezone(KST).strftime('%Y-%m-%d %H:%M')} KST")
    print(f"[필터 기준] {MAX_AGE_SECONDS // 3600}시간 이내 기사만 수집")

    all_articles: List[Article] = []

    print("[1/3] 구글 뉴스 RSS 수집 중...")
    all_articles += collect_google_rss(max_per_query=3, now=now)

    print("[2/3] 네이버 뉴스 검색 수집 중...")
    all_articles += collect_naver_search(max_per_query=3, now=now)

    print("[3/3] 네이버 신문사 RSS 수집 중...")
    all_articles += collect_naver_newspaper_rss(max_per_feed=5, now=now)

    all_articles = deduplicate_and_sort(all_articles)  # ★ 최신순 정렬 포함
    print(f"[중복 제거·정렬 후] {len(all_articles)}개")

    # ★ 수집 결과 요약 출력
    if all_articles:
        oldest = all_articles[-1]
        newest = all_articles[0]
        print(f"  최신: {newest.published_dt.astimezone(KST).strftime('%H:%M')} KST | {newest.title[:40]}")
        print(f"  최구: {oldest.published_dt.astimezone(KST).strftime('%H:%M')} KST | {oldest.title[:40]}")

    return all_articles[:max_articles]


def gemini_summarize(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    resp = client.models.generate_content(
        model=_env_str("GEMINI_MODEL", "gemini-2.0-flash"),
        contents=prompt,
    )
    return (getattr(resp, "text", "")).strip()


def build_summary(articles: List[Article], today: str) -> str:
    if not articles:
        prompt = FALLBACK_TOPICS_PROMPT.format(today=today)
        return gemini_summarize(prompt)

    CATEGORIES = ["semiconductor", "energy", "macro", "global_event"]
    filled = {cat for cat in CATEGORIES if any(a.category == cat for a in articles)}
    empty  = [cat for cat in CATEGORIES if cat not in filled]

    CATEGORY_KR = {
        "semiconductor": "반도체",
        "energy": "에너지",
        "macro": "거시경제",
        "global_event": "글로벌 이벤트",
    }

    def fmt(cat: str) -> str:
        cat_articles = [a for a in articles if a.category == cat]
        if cat_articles:
            return "\n".join(
                # ★ 발행 시각도 함께 표시해서 Gemini가 최신 기사임을 인지하게
                f"- [{a.source} | {a.published_dt.astimezone(KST).strftime('%H:%M KST')}] {a.title}\n  {a.text[:800]}"
                for a in cat_articles
            )
        return ""

    hot_issue_sections = ""
    if empty:
        empty_kr = ", ".join(CATEGORY_KR[c] for c in empty)
        hot_issue_prompt = f"""오늘은 {today}입니다.
아래 카테고리에 대해 오늘 날짜({today}) 기준으로 **최근 며칠 사이 뉴스에 가장 많이 등장한 기업 이슈 또는 시장 이슈**를 카테고리별로 요약해주세요.

조건:
- 보유 종목 우선 언급: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
- 보유 종목 이슈가 없다면 해당 분야에서 가장 화제가 된 기업/이슈를 대신 소개
- 각 카테고리 3~5줄 이내
- 출처가 불분명한 내용은 "추정" 또는 "알려진 바에 따르면" 등으로 표현

요약이 필요한 카테고리: {empty_kr}

각 카테고리 형식:
===== [카테고리명] 최근 핫이슈 (자체 분석) =====
(내용)
"""
        hot_issue_sections = gemini_summarize(hot_issue_prompt)

    article_blocks = "\n\n".join(
        f"===== {CATEGORY_KR[cat]} =====\n{fmt(cat)}"
        for cat in CATEGORIES
        if cat in filled
    )

    per_article_prompt = f"""오늘은 {today}입니다.
아래 기사들은 **모두 최근 24시간 이내** 발행된 기사입니다. 각각 개별적으로 분석해주세요.

분석 조건:
- 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
- 영어 기사는 한국어로 번역
- 기사 제목 옆에 발행 시각([HH:MM KST])을 표시해주세요

**서술 방식**
- 기사마다 제목과 시각을 먼저 쓰고, 아래 항목 중 해당되는 것만 자연스럽게 서술하세요.
  - 무슨 일이 일어났는지
  - 관련 이해관계자들의 입장 (있을 경우)
  - 왜 이 이슈가 중요한지 / 배경
  - 앞으로 어떻게 흘러갈지 전망
  - 보유 종목 영향도 (긍정/부정/중립 + 이유)
- 단순 수치 발표나 단신이라면 2~3줄로 간결하게 마무리해도 됩니다.

{article_blocks}
"""

    per_article_analysis = gemini_summarize(per_article_prompt)

    hot_section_note = ""
    if hot_issue_sections:
        empty_kr = ", ".join(CATEGORY_KR[c] for c in empty)
        hot_section_note = f"""
※ [{empty_kr}] 카테고리는 오늘 수집된 기사가 없어 최근 핫이슈로 대체합니다:

{hot_issue_sections}
"""

    category_summary_prompt = f"""오늘은 {today}입니다.
아래는 오늘 수집된 뉴스의 기사별 심층 분석 결과입니다 (모두 24시간 이내 기사).
{hot_section_note}
이를 바탕으로 **카테고리별 종합 요약**과 **투자 인사이트**를 작성해주세요.

작성 조건:
- 보유 종목: 삼성전자, 와이씨, 두산에너빌리티, 엔비디아, CEG(Constellation Energy)
- 비슷한 기사는 묶어서 중복 없이 정리
- 각 카테고리 3줄 이내 핵심 요약
- 기사 없는 카테고리는 위 핫이슈 내용을 바탕으로 요약
- 마지막에 오늘의 투자 인사이트 (전체 종합, 종목별 대응 방향 포함)

출력 형식:
===== 💾 반도체 요약 =====
(3줄 이내)

===== ⚡ 에너지 요약 =====
(3줄 이내)

===== 📊 거시경제 요약 =====
(3줄 이내)

===== 🌏 글로벌 이벤트 요약 =====
(3줄 이내)

===== 💡 오늘의 투자 인사이트 =====
(종목별 대응 방향 포함, 5줄 이내)

--- 기사별 분석 원문 ---
{per_article_analysis}
"""

    category_summary = gemini_summarize(category_summary_prompt)

    hot_appendix = (
        f"\n\n{'='*60}\n\n🔥 기사 없는 카테고리 - 최근 핫이슈 (자체 분석)\n\n{'='*60}\n\n{hot_issue_sections}"
        if hot_issue_sections else ""
    )

    return (
        f"{category_summary}"
        f"\n\n{'='*60}\n\n📋 기사별 상세 분석\n\n{'='*60}\n\n{per_article_analysis}"
        f"{hot_appendix}"
    )


CATEGORY_LABEL = {
    "semiconductor": "💾 반도체",
    "energy": "⚡ 에너지",
    "macro": "📊 거시경제",
    "global_event": "🌏 글로벌 이벤트",
}


def build_html(header: str, summary: str, articles: List[Article]) -> str:
    sections = ""
    for cat, label in CATEGORY_LABEL.items():
        cat_articles = [a for a in articles if a.category == cat]
        if not cat_articles:
            continue
        items = "\n".join(
            f'<li>'
            f'<span class="age">{a.published_dt.astimezone(KST).strftime("%H:%M")}</span> '
            f'<a href="{a.url}" target="_blank">{ihtml.escape(a.title)}</a> '
            f'<span class="source">({a.source})</span>'
            f'</li>'
            for a in cat_articles
        )
        sections += f"<h2>{label}</h2><ul>{items}</ul>\n"

    summary_html = ihtml.escape(summary)
    summary_html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", summary_html)
    summary_html = summary_html.replace("\n", "<br>")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ihtml.escape(header)}</title>
<style>
  body {{ font-family: 'Apple SD Gothic Neo', sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; line-height: 1.8; color: #222; }}
  h1 {{ font-size: 1.5em; border-bottom: 2px solid #333; padding-bottom: 8px; }}
  h2 {{ font-size: 1.15em; margin-top: 32px; color: #444; }}
  .summary-box {{ background: #f4f8ff; border-left: 4px solid #3a7bd5; padding: 20px 24px; border-radius: 6px; margin: 20px 0; }}
  ul {{ padding-left: 18px; }}
  li {{ margin: 6px 0; }}
  a {{ color: #1a5cc8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .source {{ color: #999; font-size: 0.82em; }}
  .age {{ color: #e07000; font-size: 0.85em; font-weight: bold; margin-right: 4px; }}
  .updated {{ color: #aaa; font-size: 0.85em; margin-top: 4px; }}
</style>
</head>
<body>
<h1>📰 {ihtml.escape(header)}</h1>
<p class="updated">업데이트: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST</p>
<h2>🤖 AI 투자 브리핑</h2>
<div class="summary-box">{summary_html}</div>
{sections}
</body>
</html>"""


def kakao_send_to_me(access_token: str, text: str, page_url: str) -> None:
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": page_url, "mobile_web_url": page_url},
        "buttons": [{
            "title": "뉴스 요약 크게보기",
            "link": {"web_url": page_url, "mobile_web_url": page_url},
        }],
    }
    resp = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template_object, ensure_ascii=False)},
    )
    print(f"[Kakao] status={resp.status_code}, body={resp.text}")
    if resp.status_code != 200:
        raise RuntimeError(f"카카오 전송 실패: {resp.status_code} {resp.text}")


def main() -> None:
    load_dotenv()

    today = datetime.now(KST).strftime("%Y-%m-%d")
    header = f"[투자 뉴스 브리핑] {today}"
    max_articles = _env_int("MAX_ARTICLES", 60)

    print(f"[수집 시작] 최대 {max_articles}개")
    articles = collect_articles(max_articles)
    print(f"[수집 완료] {len(articles)}개")

    summary = build_summary(articles, today)
    print("[요약 완료]")

    html_content = build_html(header, summary, articles)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("[HTML 생성 완료]")

    GITHUB_ID = "schoe5797-debug"
    REPO_NAME = "kakao_news"
    page_url = f"https://{GITHUB_ID}.github.io/{REPO_NAME}/"
    print(f"[page_url] {page_url}")

    token_resp = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": _env("KAKAO_REST_API_KEY"),
            "client_secret": _env("KAKAO_CLIENT_SECRET"),
            "refresh_token": _env("KAKAO_REFRESH_TOKEN"),
        },
    ).json()
    print(f"[Kakao Token] {token_resp}")

    access_token = token_resp.get("access_token")
    if not access_token:
        raise RuntimeError(f"액세스 토큰 갱신 실패: {token_resp}")

    short_summary = summary[:200].replace("\n", " ")
    kakao_text = f"{header}\n\n{short_summary}...\n\n자세한 내용은 아래 버튼을 눌러주세요."
    kakao_send_to_me(access_token, kakao_text, page_url)


if __name__ == "__main__":
    main()
