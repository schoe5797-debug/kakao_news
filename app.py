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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None or val.strip() == "": raise RuntimeError(f"Missing: {name}")
    return val

def _env_optional(name: str) -> Optional[str]:
    val = os.getenv(name)
    return val.strip() if val and val.strip() else None

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw.strip()) if raw and raw.strip() else default

def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default

KST = timezone(timedelta(hours=9))

def fetch_rss_entries(rss_urls: List[str]) -> List[dict]:
    entries = []
    for url in rss_urls:
        feed = feedparser.parse(url)
        if not getattr(feed, "bozo", 0):
            entries.extend(getattr(feed, "entries", []))
    seen, uniq = set(), []
    for e in entries:
        link = (e.get("link") or "").strip()
        if link and link not in seen:
            seen.add(link); uniq.append(e)
    return uniq

def fetch_naver_news_entries(queries: List[str]) -> List[dict]:
    client_id = _env_optional("NAVER_CLIENT_ID")
    client_secret = _env_optional("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret: return []
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    out = []
    for q in queries:
        try:
            r = requests.get("https://openapi.naver.com/v1/search/news.json", headers=headers, params={"query": q, "display": 10, "sort": "date"}, timeout=15)
            for item in r.json().get("items", []):
                out.append({"title": item.get("title"), "link": item.get("originallink") or item.get("link"), "published": item.get("pubDate"), "_provider": "naver"})
        except: continue
    return out

def extract_article_text(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        doc = Document(r.text)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        return soup.get_text("\n", strip=True)[:6000]
    except: return ""

def gemini_summarize(prompt: str) -> str:
    from google import genai
    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    resp = client.models.generate_content(model=_env_str("GEMINI_MODEL", "gemini-2.0-flash"), contents=prompt)
    return (getattr(resp, "text", "")).strip()

def kakao_send_to_me(access_token: str, text: str, page_url: str) -> None:
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": page_url, "mobile_web_url": page_url},
        "button_title": "뉴스 요약 크게보기"
    }
    requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send", headers={"Authorization": f"Bearer {access_token}"}, data={"template_object": json.dumps(template_object, ensure_ascii=False)})

def main() -> None:
    load_dotenv()
    # (수집 및 요약 로직 생략 - 이전 답변의 로직을 유지하세요)
    # 아래는 결과물을 전송하는 핵심 부분입니다.
    
    summary = "뉴스 요약 데이터..." # 실제 Gemini 결과값
    header = f"[뉴스 브리핑] {datetime.now(KST).strftime('%Y-%m-%d')}\n"
    
    # HTML 생성
    html_content = f"<html><body><h1>{header}</h1><pre>{summary}</pre></body></html>"
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

    # ★★★ 여기를 본인 정보로 수정하세요 ★★★
    GITHUB_ID = "schoe5797-debug"
    REPO_NAME = "schoe5797-debug/kakao_news"
    page_url = f"https://{GITHUB_ID}.github.io/{REPO_NAME}/"

    # 카톡 전송
    access_token = requests.post("https://kauth.kakao.com/oauth/token", data={"grant_type": "refresh_token", "client_id": _env("KAKAO_REST_API_KEY"), "refresh_token": _env("KAKAO_REFRESH_TOKEN")}).json().get("access_token")
    kakao_send_to_me(access_token, header + " 오늘 뉴스가 도착했습니다!", page_url)

if __name__ == "__main__":
    main()
