import html as ihtml
import json
import os
import re
import textwrap
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlparse

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
    text: str


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)


def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None or val.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _env_optional(name: str) -> Optional[str]:
    val = os.getenv(name)
    if val is None:
        return None
    val = val.strip()
    return val if val else None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return default
    return int(raw)


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


KST = timezone(timedelta(hours=9))


def fetch_rss_entries(rss_urls: List[str], timeout_s: int = 15) -> List[dict]:
    entries: List[dict] = []
    for url in rss_urls:
        feed = feedparser.parse(url)
        if getattr(feed, "bozo", 0):
            continue
        for e in getattr(feed, "entries", []):
            e["_feed_url"] = url
            entries.append(e)
    # Deduplicate by link
    seen = set()
    uniq: List[dict] = []
    for e in entries:
        link = (e.get("link") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        uniq.append(e)
    return uniq


def fetch_naver_news_entries(queries: List[str], display_per_query: int = 10) -> List[dict]:
    """
    Naver Search News OpenAPI. Requires env:
      - NAVER_CLIENT_ID
      - NAVER_CLIENT_SECRET
    If missing, returns [] (so the bot can still run with Google only).
    """
    client_id = _env_optional("NAVER_CLIENT_ID")
    client_secret = _env_optional("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return []

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }

    out: List[dict] = []
    for q in queries:
        try:
            r = requests.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers=headers,
                params={"query": q, "display": display_per_query, "sort": "date"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue

        for item in data.get("items", []):
            title_raw = item.get("title") or ""
            desc_raw = item.get("description") or ""

            title = BeautifulSoup(ihtml.unescape(title_raw), "html.parser").get_text(" ", strip=True)
            desc = BeautifulSoup(ihtml.unescape(desc_raw), "html.parser").get_text(" ", strip=True)
            link = (item.get("originallink") or item.get("link") or "").strip()
            if not title or not link:
                continue

            out.append(
                {
                    "title": title,
                    "link": link,
                    "summary": desc,
                    "description": desc,
                    "published": (item.get("pubDate") or "").strip(),
                    # Mark as Naver for later grouping/dedupe preference
                    "source": {"title": "Naver"},
                    "_provider": "naver",
                }
            )
    return out


def extract_article_text(url: str, timeout_s: int = 15, max_chars: int = 6000) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout_s)
        r.raise_for_status()
        html = r.text
        doc = Document(html)
        main_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(main_html, "html.parser")
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 200:
            # Fallback: strip full page quickly
            soup2 = BeautifulSoup(html, "html.parser")
            text = soup2.get_text("\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def build_articles(entries: List[dict], max_articles: int = 12) -> List[Article]:
    articles: List[Article] = []
    for e in entries[: max_articles * 3]:
        title = (e.get("title") or "").strip()
        url = (e.get("link") or "").strip()
        if not title or not url:
            continue

        source = ""
        if isinstance(e.get("source"), dict):
            source = (e["source"].get("title") or "").strip()
        source = source or (e.get("author") or "").strip() or "Unknown"
        published = (e.get("published") or e.get("updated") or "").strip()

        snippet = ""
        if e.get("summary"):
            snippet = BeautifulSoup(e["summary"], "html.parser").get_text(" ", strip=True)
        if not snippet and e.get("description"):
            snippet = BeautifulSoup(e["description"], "html.parser").get_text(" ", strip=True)

        text = extract_article_text(url)
        if not text:
            text = snippet
        if not text:
            continue

        articles.append(
            Article(
                idx=len(articles) + 1,
                title=title,
                url=url,
                source=source,
                published=published,
                text=text,
            )
        )
        if len(articles) >= max_articles:
            break
    return articles


def _normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\s+", " ", t).strip()
    # Remove most punctuation to improve cross-source matching
    t = re.sub(r"[^\w\s가-힣]", "", t)
    return t


def _root_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        host = host.split("@")[-1]
        host = host.split(":")[0]
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


def dedupe_entries(entries: List[dict], prefer_provider: str) -> List[dict]:
    """
    Dedupe by URL and normalized title. When clashes happen, prefer entries whose
    _provider matches prefer_provider (e.g., prefer naver for domestic bucket).
    """
    by_url: OrderedDict[str, dict] = OrderedDict()
    for e in entries:
        url = (e.get("link") or "").strip()
        if not url:
            continue
        if url not in by_url:
            by_url[url] = e
    entries2 = list(by_url.values())

    chosen: OrderedDict[str, dict] = OrderedDict()
    for e in entries2:
        key = _normalize_title((e.get("title") or "").strip())
        if not key:
            continue
        if key not in chosen:
            chosen[key] = e
            continue
        cur = chosen[key]
        cur_p = (cur.get("_provider") or "").lower()
        new_p = (e.get("_provider") or "").lower()
        if new_p == prefer_provider and cur_p != prefer_provider:
            chosen[key] = e
    return list(chosen.values())


def select_mixed_entries(
    *,
    intl_entries: List[dict],
    domestic_entries: List[dict],
    max_total: int,
    intl_target: int,
) -> List[dict]:
    intl_target = max(0, min(intl_target, max_total))
    dom_target = max_total - intl_target

    picked: List[dict] = []
    picked.extend(intl_entries[:intl_target])
    picked.extend(domestic_entries[:dom_target])

    # If one side doesn't have enough, fill from the other.
    if len(picked) < max_total:
        remaining = max_total - len(picked)
        extra = intl_entries[intl_target:] + domestic_entries[dom_target:]
        picked.extend(extra[:remaining])
    return picked[:max_total]


MEGA_KEYWORDS = [
    "war",
    "invasion",
    "missile",
    "nuclear",
    "pandemic",
    "outbreak",
    "covid",
    "ai",
    "chip",
    "semiconductor",
    "sanction",
    "tariff",
    "crisis",
    "earthquake",
    "tsunami",
    "attack",
    "coup",
    "inflation",
    "rate hike",
    "fed",
    "opec",
    "oil",
    "gas",
    "blackout",
    "ransomware",
]


def score_mega_issue(a: Article) -> int:
    hay = f"{a.title}\n{a.text}".lower()
    return sum(1 for k in MEGA_KEYWORDS if k in hay)


def prioritize_articles(articles: List[Article]) -> List[Article]:
    scored = [(score_mega_issue(a), a.idx, a) for a in articles]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [a for _, _, a in scored]


def reindex_articles(articles: List[Article]) -> List[Article]:
    return [
        Article(
            idx=i + 1,
            title=a.title,
            url=a.url,
            source=a.source,
            published=a.published,
            text=a.text,
        )
        for i, a in enumerate(articles)
    ]


TOPIC_ORDER = [
    ("메가이슈/지정학", ["war", "invasion", "missile", "nuclear", "attack", "coup", "sanction", "tariff"]),
    ("거시/금리/인플레", ["fed", "rate", "rate hike", "inflation", "cpi", "jobs", "gdp", "recession"]),
    ("반도체/칩", ["semiconductor", "chip", "hbm", "dram", "nand", "tsmc", "nvidia", "삼성", "삼성전자", "반도체"]),
    ("에너지/유가", ["opec", "oil", "gas", "lng", "brent", "wti", "유가", "원유", "가스", "에너지"]),
    ("AI/빅테크", ["ai", "openai", "microsoft", "google", "alphabet", "anthropic", "gpu", "모델", "인공지능"]),
    ("한국 정책/시장", ["korea", "kospi", "kosdaq", "금통위", "한국은행", "기재부", "금리", "원달러", "환율"]),
]


def article_topics(a: Article) -> List[str]:
    hay = f"{a.title}\n{a.text}".lower()
    topics: List[str] = []
    for label, kws in TOPIC_ORDER:
        if any(k.lower() in hay for k in kws):
            topics.append(label)
    return topics


def build_extra_links(
    *,
    selected: List[Article],
    remaining: List[Article],
    max_links: int,
) -> List[tuple[str, str]]:
    present = set()
    for a in selected:
        present.update(article_topics(a))

    extras: List[tuple[str, str]] = []
    used_urls = {a.url for a in selected}
    for label, _kws in TOPIC_ORDER:
        if len(extras) >= max_links:
            break
        if label in present:
            continue
        pick = None
        for a in remaining:
            if a.url in used_urls:
                continue
            if label in article_topics(a):
                pick = a
                break
        if pick:
            extras.append((label, pick.url))
            used_urls.add(pick.url)
    return extras


def make_prompt(articles: List[Article]) -> str:
    sources_block = []
    for a in articles:
        # Keep input compact to reduce quota pressure
        max_chars = _env_int("MAX_SOURCE_CHARS", 1400)
        a_text = a.text[:max_chars]
        sources_block.append(
            textwrap.dedent(
                f"""
                [SOURCE {a.idx}]
                TITLE: {a.title}
                PUBLISHED: {a.published}
                OUTLET: {a.source}
                URL: {a.url}
                TEXT:
                {a_text}
                """
            ).strip()
        )

    sources = "\n\n".join(sources_block)
    return textwrap.dedent(
        f"""
        당신은 '매일 아침 경제/메가이슈 브리핑'을 쓰는 전담 기자다.

        가장 중요한 규칙:
        - 아래에 제공된 SOURCE 텍스트에 없는 사실/수치/원인/결과를 절대 추가하지 마라.
        - 추측, 일반상식, 과거 지식으로 보완하지 마라.
        - 불확실하면 "정보 부족"이라고 명시해라.
        - 각 문장에는 반드시 근거 SOURCE 번호를 (예: [1][3])처럼 붙여라.
        - 링크(URL)는 SOURCE에 제공된 URL만 사용할 것. (새 URL 생성 금지)

        편집 요구사항:
        - 메가 이슈(전쟁/팬데믹/AI/지정학/에너지 쇼크 등)가 있으면 최상단.
        - 경제 전반 및 반도체/에너지/대형 기술주(삼성전자, 엔비디아, 두산에너빌리티, 팔란티어, 구글, MS 등)에 영향을 줄 소식을 우선.
        - 기사를 나열하지 말고 연관된 내용을 묶어 1~2개의 매끄러운 브리핑 문장으로 정리.
        - 한국어로 작성.

        출력 형식(그대로 지켜라):
        [메가 이슈]
        - (1~2문장)

        [시장/종목 영향 포인트]
        - (1~3문장)

        [오늘 한 줄]
        - (딱 1문장)

        [근거 링크]
        - [1] <URL>
        - [2] <URL>
        ...

        아래 SOURCE들만 사용해 브리핑을 작성하라.

        {sources}
        """
    ).strip()


def gemini_summarize(prompt: str) -> str:
    api_key = _env("GEMINI_API_KEY")
    model = _env_str("GEMINI_MODEL", "gemini-2.0-flash")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            top_p=0.9,
            max_output_tokens=700,
        ),
    )
    return (getattr(resp, "text", None) or "").strip()


URL_RE = re.compile(r"https?://[^\s)>]+", re.IGNORECASE)


def filter_output_urls(text: str, allowed_urls: Iterable[str]) -> str:
    allowed = {u.strip() for u in allowed_urls if u.strip()}

    def _replace(match: re.Match) -> str:
        url = match.group(0)
        return url if url in allowed else ""

    filtered = URL_RE.sub(_replace, text)
    # Clean doubled spaces / empty bullets caused by removals
    filtered = re.sub(r"[ \t]{2,}", " ", filtered)
    filtered = re.sub(r"\n-\s*\[\d+\]\s*$", "", filtered, flags=re.MULTILINE)
    filtered = re.sub(r"\n{3,}", "\n\n", filtered).strip()
    return filtered


def kakao_refresh_access_token(rest_api_key: str, refresh_token: str) -> str:
    r = requests.post(
        "https://kauth.kakao.com/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        data={
            "grant_type": "refresh_token",
            "client_id": rest_api_key,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Failed to refresh Kakao access token: {data}")
    return token


def kakao_send_to_me(access_token: str, text: str) -> None:
    # Kakao memo default template: "text"
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": "https://news.google.com", "mobile_web_url": "https://news.google.com"},
        "button_title": "뉴스 보기",
    }

    r = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template_object, ensure_ascii=False)},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("result_code") not in (0, "0", None):
        raise RuntimeError(f"Kakao send failed: {data}")


def chunk_text(text: str, max_len: int = 900) -> List[str]:
    # Keep it simple: split by paragraphs first, then hard cut.
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        candidate = (buf + ("\n\n" if buf else "") + p).strip()
        if len(candidate) <= max_len:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(p) <= max_len:
            buf = p
        else:
            for i in range(0, len(p), max_len):
                chunks.append(p[i : i + max_len])
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def main() -> None:
    load_dotenv()

    max_total = _env_int("MAX_ARTICLES", 10)
    # Default mix: overseas(google) 6 + domestic(naver) 4 (total 10)
    intl_target = _env_int("INTL_TARGET", 6)
    candidate_total = max(max_total, _env_int("CANDIDATE_ARTICLES", max_total * 3))

    # Overseas: Google News (English / global) feeds
    google_intl_rss = [
        "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=semiconductor%20NVIDIA&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=energy%20OPEC%20oil%20gas&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=Palantir%20AI&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=Microsoft%20Google%20AI%20chip&hl=en-US&gl=US&ceid=US:en",
    ]
    intl_entries = fetch_rss_entries(google_intl_rss)
    for e in intl_entries:
        e["_provider"] = "google"

    # Domestic: Naver News OpenAPI (Korean)
    naver_queries = [
        "국내 경제",
        "반도체 삼성전자",
        "엔비디아 반도체",
        "두산에너빌리티",
        "에너지 유가 OPEC",
        "팔란티어",
        "구글 AI",
        "마이크로소프트 AI",
    ]
    domestic_entries = fetch_naver_news_entries(naver_queries, display_per_query=10)

    # Dedupe within each bucket first
    intl_entries = dedupe_entries(intl_entries, prefer_provider="google")
    domestic_entries = dedupe_entries(domestic_entries, prefer_provider="naver")

    # Cross-bucket dedupe: if same normalized title appears, keep domestic (naver) version.
    seen_titles = {_normalize_title((e.get("title") or "").strip()) for e in domestic_entries}
    intl_entries = [e for e in intl_entries if _normalize_title((e.get("title") or "").strip()) not in seen_titles]

    mixed_entries = select_mixed_entries(
        intl_entries=intl_entries,
        domestic_entries=domestic_entries,
        max_total=candidate_total,
        intl_target=intl_target,
    )

    articles_all = build_articles(mixed_entries, max_articles=candidate_total)
    articles_all = prioritize_articles(articles_all)
    selected = articles_all[:max_total]
    remaining = articles_all[max_total:]
    selected = reindex_articles(selected)
    articles = selected
    if not articles:
        raise RuntimeError("No articles collected from Google RSS / Naver API.")

    prompt = make_prompt(articles)

    summary = gemini_summarize(prompt)
    if not summary:
        raise RuntimeError("Gemini returned empty summary.")

    allowed_urls = [a.url for a in articles]
    summary = filter_output_urls(summary, allowed_urls)

    extra_links = build_extra_links(
        selected=articles,
        remaining=remaining,
        max_links=_env_int("EXTRA_LINKS_MAX", 5),
    )
    if extra_links:
        extra_block = ["\n[바쁜 날 참고 링크(요약에 못 담은 분야)]"]
        for label, url in extra_links:
            extra_block.append(f"- {label}: {url}")
        summary = summary.rstrip() + "\n" + "\n".join(extra_block) + "\n"

    # Add a timestamp header for clarity
    kst_now = datetime.now(timezone.utc).astimezone(KST)
    header = f"[뉴스 브리핑] {kst_now.strftime('%Y-%m-%d')} (KST 08:00 자동)\n"
    final_text = header + summary

    rest_api_key = _env("KAKAO_REST_API_KEY")
    refresh_token = _env("KAKAO_REFRESH_TOKEN")
    access_token = kakao_refresh_access_token(rest_api_key, refresh_token)

    for part in chunk_text(final_text):
        kakao_send_to_me(access_token, part)


if __name__ == "__main__":
    main()
