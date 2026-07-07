# ───────────────────────────────────────────────────────────
# /api/save.py  — ReelFork v7
#
# 파이프라인:
#   단축어(URL) → Apify(본문+썸네일) → Gemini(식당명+카테고리)
#   → Naver(주소) → Notion(저장)
#
# v7 신기능: 카테고리 자동 태깅, 썸네일 커버, 방문 체크박스, 통계 API
# ───────────────────────────────────────────────────────────

from http.server import BaseHTTPRequestHandler
import json
import os
import re
import time
import requests
import pyproj
from typing import Optional
from notion_client import Client as NotionClient
import google.generativeai as genai

NOTION_API_KEY      = os.environ.get("NOTION_API_KEY", "").strip()
NOTION_DB_ID        = os.environ.get("NOTION_DB_ID", "").strip().split("?")[0]
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "").strip()
APIFY_API_TOKEN     = os.environ.get("APIFY_API_TOKEN", "").strip()
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()

APIFY_ACTOR_ID = "apify~instagram-scraper"


# ══════════════════════════════════════════════════════════
#  Step 1: Apify — 인스타 본문 + 썸네일
# ══════════════════════════════════════════════════════════
def fetch_instagram_data(instagram_url: str) -> tuple[bool, str, Optional[str]]:
    """Returns (success, caption_or_error, thumbnail_url)"""
    run_url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    params  = {"token": APIFY_API_TOKEN, "timeout": 60, "memory": 1024}
    payload = {
        "directUrls":    [instagram_url],
        "resultsType":   "posts",
        "resultsLimit":  1,
        "addParentData": False,
    }

    try:
        res = requests.post(run_url, json=payload, params=params, timeout=90)
        if res.status_code not in (200, 201):
            return False, f"Apify 오류 (HTTP {res.status_code}): {res.text[:200]}", None

        items = res.json()
        if not items:
            return False, "게시물을 찾을 수 없습니다. URL을 다시 확인해주세요.", None

        item    = items[0]
        caption = item.get("caption") or item.get("alt") or item.get("description") or ""

        # 썸네일 URL (우선순위 순)
        thumbnail_url = (
            item.get("displayUrl") or
            item.get("thumbnailUrl") or
            item.get("previewUrl") or
            ((item.get("images") or [None])[0])
        )

        if not caption:
            username = item.get("ownerUsername", "")
            return False, f"캡션(본문)이 없는 게시물입니다. (작성자: @{username})", None

        return True, caption, thumbnail_url

    except requests.exceptions.Timeout:
        return False, "Apify 요청 시간 초과 (60초). 잠시 후 다시 시도해주세요.", None
    except Exception as e:
        return False, f"Apify 호출 오류: {str(e)}", None


# ══════════════════════════════════════════════════════════
#  Step 2: Gemini — 식당명 + 카테고리 추출
# ══════════════════════════════════════════════════════════
def extract_restaurant_info(caption: str) -> tuple[bool, str, str, str]:
    """Returns (success, restaurant_name, search_query, category)"""
    genai.configure(api_key=GEMINI_API_KEY)

    prompt = f"""다음은 인스타그램 맛집/카페 게시물 본문입니다.

[본문]
{caption[:2000]}

이 본문에서 소개하는 식당 또는 카페의 정보를 추출하여, 아래 JSON 형식으로만 응답하세요 (다른 말 절대 금지):
{{
  "restaurant_name": "식당 또는 카페 이름 (없으면 null)",
  "category": "한식|카페|일식|중식|양식|술집|분식|기타 중 정확히 하나",
  "location_hint": "언급된 동네나 지역명 (없으면 빈 문자열)",
  "search_query": "네이버 지도 검색 쿼리 (이름+지역, 예: 홍대 몽카페)"
}}"""

    models_to_try = ["gemini-3.1-flash-lite", "gemini-1.5-flash-8b", "gemini-1.5-flash"]

    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            resp  = model.generate_content(prompt)
            clean = resp.text.replace("```json", "").replace("```", "").strip()
            data  = json.loads(clean)

            name     = data.get("restaurant_name")
            query    = data.get("search_query", name or "")
            category = data.get("category", "기타") or "기타"

            if not name or name == "null":
                print(f"[Gemini] 본문: {caption[:300]}")
                print(f"[Gemini] 결과: {clean}")
                return False, "", f"식당 이름을 찾을 수 없습니다. (응답: {clean})", "기타"

            return True, str(name), str(query), str(category)
        except Exception as e:
            if "404" in str(e):
                continue
            return False, "", str(e), "기타"

    return False, "", "모든 Gemini 모델 실패 (404 오류)", "기타"


# ══════════════════════════════════════════════════════════
#  Step 3: Naver — 주소 조회
# ══════════════════════════════════════════════════════════
def search_naver_address(query: str) -> tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
    """Returns (road_address, naver_map_link, lat, lon)"""
    url     = "https://openapi.naver.com/v1/search/local.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": 1}

    try:
        res   = requests.get(url, headers=headers, params=params, timeout=8)
        data  = res.json()
        items = data.get("items", [])
        if not items:
            return None, None, None, None

        item    = items[0]
        address = re.sub(r"<.*?>", "", item.get("roadAddress") or item.get("address") or "")
        link    = item.get("link", "")

        lat, lon = None, None
        try:
            mapx_str = item.get("mapx")
            mapy_str = item.get("mapy")
            if mapx_str and mapy_str:
                x = float(mapx_str)
                y = float(mapy_str)
                if x > 10000000 and y > 10000000:
                    cand_lon = x / 10000000.0
                    cand_lat = y / 10000000.0
                    if 124 < cand_lon < 132 and 33 < cand_lat < 43:
                        lon, lat = cand_lon, cand_lat
                if lon is None or lat is None:
                    katech_crs  = "+proj=tmerc +lat_0=38 +lon_0=128 +k=0.9999 +x_0=400000 +y_0=600000 +ellps=bessel +units=m +no_defs +towgs84=-115.80,474.99,674.11,1.16,-2.31,-1.63,6.43"
                    wgs84_crs   = "EPSG:4326"
                    transformer = pyproj.Transformer.from_crs(katech_crs, wgs84_crs, always_xy=True)
                    cand_lon, cand_lat = transformer.transform(x, y)
                    if str(cand_lon).lower() not in ["inf", "-inf", "nan"]:
                        if 124 < cand_lon < 132 and 33 < cand_lat < 43:
                            lon, lat = cand_lon, cand_lat
        except Exception:
            pass

        return address or None, link or None, lat, lon
    except Exception:
        return None, None, None, None


# ══════════════════════════════════════════════════════════
#  Step 4: Notion 저장
# ══════════════════════════════════════════════════════════
def save_to_notion(
    restaurant_name: str,
    address: Optional[str],
    instagram_url: str,
    nickname: str,
    naver_link: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    category: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> dict:
    if not NOTION_API_KEY or not NOTION_DB_ID:
        return {"error": "Notion 환경 변수 미설정"}

    notion = NotionClient(auth=NOTION_API_KEY, notion_version="2025-09-03")

    properties: dict = {
        "식당 이름":  {"title": [{"text": {"content": restaurant_name}}]},
        "인스타 링크": {"url": instagram_url},
        "등록자":    {"select": {"name": nickname}},
        "방문":      {"checkbox": False},
    }

    # 카테고리
    if category:
        properties["카테고리"] = {"select": {"name": category}}

    # 지역 (주소 첫 단어에서 추출)
    if address:
        parts = address.strip().split()
        if parts:
            f = parts[0]
            region = f
            if   f.startswith("서울"): region = "서울"
            elif f.startswith("부산"): region = "부산"
            elif f.startswith("대구"): region = "대구"
            elif f.startswith("인천"): region = "인천"
            elif f.startswith("광주"): region = "광주"
            elif f.startswith("대전"): region = "대전"
            elif f.startswith("울산"): region = "울산"
            elif f.startswith("세종"): region = "세종"
            elif f.startswith("경기"): region = "경기"
            elif f.startswith("강원"): region = "강원"
            elif f.startswith("충북") or f.startswith("충청북도"): region = "충북"
            elif f.startswith("충남") or f.startswith("충청남도"): region = "충남"
            elif f.startswith("전북") or f.startswith("전라북도") or f.startswith("전북특별자치도"): region = "전북"
            elif f.startswith("전남") or f.startswith("전라남도"): region = "전남"
            elif f.startswith("경북") or f.startswith("경상북도"): region = "경북"
            elif f.startswith("경남") or f.startswith("경상남도"): region = "경남"
            elif f.startswith("제주"): region = "제주"
            properties["지역"] = {"select": {"name": region}}

    # 주소
    if address:
        if lat and lon:
            properties["주소"] = {
                "place": {"lat": lat, "lon": lon, "name": address, "address": address}
            }
        else:
            properties["주소"] = {"rich_text": [{"text": {"content": address}}]}

    # 네이버 지도 링크
    if naver_link:
        properties["가게 인스타"] = {"url": naver_link}

    # 썸네일 → 페이지 커버
    cover = {"type": "external", "external": {"url": thumbnail_url}} if thumbnail_url else None

    try:
        create_kwargs: dict = {
            "parent":     {"database_id": NOTION_DB_ID},
            "properties": properties,
        }
        if cover:
            create_kwargs["cover"] = cover

        page = notion.pages.create(**create_kwargs)
        return {"notion_page_id": page["id"]}
    except Exception as e:
        error_msg = str(e)
        if "is not a property that exists" in error_msg:
            for key in ["가게 인스타", "지역", "카테고리"]:
                properties.pop(key, None)
            try:
                page = notion.pages.create(
                    parent={"database_id": NOTION_DB_ID},
                    properties=properties,
                )
                return {"notion_page_id": page["id"]}
            except Exception as e2:
                return {"error": f"Notion API 오류: {str(e2)}"}
        return {"error": f"Notion API 오류: {error_msg}"}


# ══════════════════════════════════════════════════════════
#  Stats: Notion DB 집계
# ══════════════════════════════════════════════════════════
def get_stats() -> dict:
    if not NOTION_API_KEY or not NOTION_DB_ID:
        return {"error": "Notion 환경 변수 미설정"}

    notion    = NotionClient(auth=NOTION_API_KEY, notion_version="2025-09-03")
    all_pages = []
    cursor    = None

    try:
        while True:
            kwargs: dict = {"database_id": NOTION_DB_ID, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            result = notion.databases.query(**kwargs)
            all_pages.extend(result.get("results", []))
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
    except Exception as e:
        return {"error": str(e)}

    total          = len(all_pages)
    visited        = 0
    by_region:    dict = {}
    by_category:  dict = {}
    by_registrar: dict = {}

    for page in all_pages:
        props = page.get("properties", {})

        if props.get("방문", {}).get("checkbox", False):
            visited += 1

        if r := (props.get("지역", {}).get("select") or {}).get("name"):
            by_region[r] = by_region.get(r, 0) + 1

        if c := (props.get("카테고리", {}).get("select") or {}).get("name"):
            by_category[c] = by_category.get(c, 0) + 1

        if n := (props.get("등록자", {}).get("select") or {}).get("name"):
            by_registrar[n] = by_registrar.get(n, 0) + 1

    return {
        "total":        total,
        "visited":      visited,
        "unvisited":    total - visited,
        "by_region":    dict(sorted(by_region.items(),    key=lambda x: -x[1])),
        "by_category":  dict(sorted(by_category.items(),  key=lambda x: -x[1])),
        "by_registrar": dict(sorted(by_registrar.items(), key=lambda x: -x[1])),
    }


# ══════════════════════════════════════════════════════════
#  Vercel HTTP Handler
# ══════════════════════════════════════════════════════════
class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[ReelFork] {format % args}")

    def _json(self, status: int, body: dict):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",                  "application/json; charset=utf-8")
        self.send_header("Content-Length",                str(len(payload)))
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, filename: str):
        import pathlib
        try:
            path  = pathlib.Path(__file__).parent.parent / filename
            data  = path.read_text(encoding="utf-8").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json(500, {"error": f"{filename} 로딩 실패: {e}"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ("/", "/index.html"):
            self._html("index.html")
        elif path == "/stats":
            self._html("stats.html")
        elif path == "/api/stats":
            self._json(200, get_stats())
        else:
            self._json(200, {
                "status":   "ok",
                "service":  "🐷 ReelFork",
                "version":  "7.0.0",
                "pipeline": "Apify → Gemini → Naver → Notion",
            })

    def do_POST(self):
        t_start = time.time()

        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"success": False, "message": "요청 바디를 파싱하지 못했습니다."})
            return

        instagram_url = (body.get("url") or "").strip()
        nickname      = (body.get("nickname") or "익명").strip()[:20]

        if not instagram_url:
            self._json(400, {"success": False, "message": "url 파라미터가 필요합니다."})
            return

        print(f"[ReelFork v7] 저장 시작 | {instagram_url} | by {nickname}")

        # ── Step 1: Apify
        ok, caption, thumbnail_url = fetch_instagram_data(instagram_url)
        if not ok:
            self._json(502, {"success": False, "message": f"❌ 스크래핑 실패: {caption}"})
            return
        print(f"[ReelFork v7] 캡션 {len(caption)}자 / 썸네일: {bool(thumbnail_url)}")

        # ── Step 2: Gemini
        ok, restaurant_name, search_query, category = extract_restaurant_info(caption)
        if not ok:
            self._json(400, {"success": False, "message": f"❌ {search_query}"})
            return
        print(f"[ReelFork v7] {restaurant_name} / {category} / 검색어: {search_query}")

        # ── Step 3: Naver
        address, naver_link, lat, lon = search_naver_address(search_query)
        print(f"[ReelFork v7] 주소: {address}")

        # ── Step 4: Notion
        result = save_to_notion(
            restaurant_name=restaurant_name,
            address=address,
            instagram_url=instagram_url,
            nickname=nickname,
            naver_link=naver_link,
            lat=lat,
            lon=lon,
            category=category,
            thumbnail_url=thumbnail_url,
        )

        elapsed = round(time.time() - t_start, 2)

        if "error" in result:
            self._json(502, {"success": False, "message": f"❌ Notion 저장 실패: {result['error']}"})
            return

        addr_display = address or "주소 미확인"
        print(f"[ReelFork v7] ✅ {restaurant_name} ({category}) | {addr_display} | {elapsed}s")

        self._json(200, {
            "success":         True,
            "message":         f"✅ {restaurant_name} 저장 완료!",
            "restaurant_name": restaurant_name,
            "category":        category,
            "address":         addr_display,
            "naver_link":      naver_link or "",
            "registered_by":   nickname,
            "elapsed_s":       elapsed,
        })
