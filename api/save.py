# ───────────────────────────────────────────────────────────
# /api/save.py  — 크루 맛집 저장 (v6 - Apify + Gemini + Naver)
#
# 파이프라인:
#   단축어(URL) → Apify(인스타 본문) → Gemini(식당명 추출)
#   → Naver Local Search(정확한 주소) → Notion(저장)
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
NOTION_DB_ID        = os.environ.get("NOTION_DB_ID", "").strip()
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "").strip()
APIFY_API_TOKEN     = os.environ.get("APIFY_API_TOKEN", "").strip()
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()


# Apify Instagram Scraper — 공식 Apify 유지 관리 버전
APIFY_ACTOR_ID = "apify~instagram-scraper"



# ══════════════════════════════════════════════════════════
#  Step 1: Apify로 인스타 본문 가져오기
# ══════════════════════════════════════════════════════════
def fetch_instagram_caption(instagram_url: str) -> tuple[bool, str]:
    """
    Apify Instagram Scraper를 동기 방식으로 호출하여 캡션을 반환합니다.
    Returns (success, caption_or_error_message)
    """
    # Apify Synchronous run API (최대 120초 대기)
    run_url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    params  = {"token": APIFY_API_TOKEN, "timeout": 60, "memory": 1024}
    payload = {
        "directUrls":   [instagram_url],
        "resultsType":  "posts",
        "resultsLimit": 1,
        "addParentData": False,
    }

    try:
        res = requests.post(run_url, json=payload, params=params, timeout=90)
        if res.status_code not in (200, 201):
            return False, f"Apify 오류 (HTTP {res.status_code}): {res.text[:200]}"


        items = res.json()
        if not items:
            return False, "게시물을 찾을 수 없습니다. URL을 다시 확인해주세요."

        item    = items[0]
        caption = item.get("caption") or item.get("alt") or item.get("description") or ""

        if not caption:
            # 캡션이 없어도 최소한 URL과 작성자는 있음
            username = item.get("ownerUsername", "")
            return False, f"이 게시물에는 캡션(본문)이 없습니다. (작성자: @{username})"

        return True, caption

    except requests.exceptions.Timeout:
        return False, "Apify 요청 시간 초과 (60초). 잠시 후 다시 시도해주세요."
    except Exception as e:
        return False, f"Apify 호출 오류: {str(e)}"


# ══════════════════════════════════════════════════════════
#  Step 2: Gemini로 식당명 추출
# ══════════════════════════════════════════════════════════
def extract_restaurant_info(caption: str) -> tuple[bool, str, str]:
    """
    Returns (success, restaurant_name, search_query)
    """
    genai.configure(api_key=GEMINI_API_KEY)
    
    prompt = f"""다음은 인스타그램 맛집/카페 게시물 본문입니다.

[본문]
{caption[:2000]}

이 본문에서 소개하는 식당 또는 카페의 정보를 추출하여, 아래 JSON 형식으로만 응답하세요 (다른 말 절대 금지):
{{
  "restaurant_name": "식당 또는 카페 이름 (없으면 null)",
  "location_hint": "언급된 동네나 지역명 (없으면 빈 문자열)",
  "search_query": "네이버 지도 검색에 적합한 쿼리 (이름+지역, 예: 홍대 몽카페)"
}}"""

    models_to_try = ["gemini-3.1-flash-lite", "gemini-1.5-flash-8b", "gemini-1.5-flash"]
    
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            resp = model.generate_content(prompt)
            clean = resp.text.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean)
            
            name = data.get("restaurant_name")
            query = data.get("search_query", name or "")
            
            if not name or name == "null":
                print(f"[Gemini Debug] 본문: {caption[:300]}")
                print(f"[Gemini Debug] 결과: {clean}")
                return False, "", f"식당 이름을 찾을 수 없습니다. (본문: {caption[:100]}... / 응답: {clean})"

            return True, str(name), str(query)
        except Exception as e:
            if "404" in str(e):
                continue # try next model
            return False, "", str(e)
            
    return False, "", "모든 Gemini 모델 테스트 실패 (404 오류)"



# ══════════════════════════════════════════════════════════
#  Step 3: 네이버 지역 검색 API로 정확한 주소 획득
# ══════════════════════════════════════════════════════════
def search_naver_address(query: str) -> tuple[Optional[str], Optional[str], Optional[float], Optional[float]]:
    """
    Returns (road_address, naver_map_link, lat, lon)
    """
    url     = "https://openapi.naver.com/v1/search/local.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": 1}

    try:
        res  = requests.get(url, headers=headers, params=params, timeout=8)
        data = res.json()
        items = data.get("items", [])
        if not items:
            return None, None, None, None
        item    = items[0]
        address = re.sub(r"<.*?>", "", item.get("roadAddress") or item.get("address") or "")
        link    = item.get("link", "")
        
        # Convert Coordinates (Handle both WGS84*1e7 and KATECH)
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
                    katech_crs = "+proj=tmerc +lat_0=38 +lon_0=128 +k=0.9999 +x_0=400000 +y_0=600000 +ellps=bessel +units=m +no_defs +towgs84=-115.80,474.99,674.11,1.16,-2.31,-1.63,6.43"
                    wgs84_crs = "EPSG:4326"
                    transformer = pyproj.Transformer.from_crs(katech_crs, wgs84_crs, always_xy=True)
                    cand_lon, cand_lat = transformer.transform(x, y)
                    if str(cand_lon).lower() not in ['inf', '-inf', 'nan'] and str(cand_lat).lower() not in ['inf', '-inf', 'nan']:
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
) -> dict:
    if not NOTION_API_KEY or not NOTION_DB_ID:
        return {"error": "Notion 환경 변수 미설정"}

    # Use the 2025 API version which supports the Place property
    notion = NotionClient(auth=NOTION_API_KEY, notion_version="2025-09-03")

    properties: dict = {
        "식당 이름":  {"title": [{"text": {"content": restaurant_name}}]},
        "인스타 링크": {"url": instagram_url},
        "등록자":    {"select": {"name": nickname}},
    }
    
    # Extract Region from address
    if address:
        parts = address.strip().split()
        if parts:
            first_part = parts[0]
            region = first_part
            # Simplify names (e.g., 서울특별시 -> 서울, 대구광역시 -> 대구)
            if first_part.startswith("서울"): region = "서울"
            elif first_part.startswith("부산"): region = "부산"
            elif first_part.startswith("대구"): region = "대구"
            elif first_part.startswith("인천"): region = "인천"
            elif first_part.startswith("광주"): region = "광주"
            elif first_part.startswith("대전"): region = "대전"
            elif first_part.startswith("울산"): region = "울산"
            elif first_part.startswith("세종"): region = "세종"
            elif first_part.startswith("경기"): region = "경기"
            elif first_part.startswith("강원"): region = "강원"
            elif first_part.startswith("충북") or first_part.startswith("충청북도"): region = "충북"
            elif first_part.startswith("충남") or first_part.startswith("충청남도"): region = "충남"
            elif first_part.startswith("전북") or first_part.startswith("전라북도") or first_part.startswith("전북특별자치도"): region = "전북"
            elif first_part.startswith("전남") or first_part.startswith("전라남도"): region = "전남"
            elif first_part.startswith("경북") or first_part.startswith("경상북도"): region = "경북"
            elif first_part.startswith("경남") or first_part.startswith("경상남도"): region = "경남"
            elif first_part.startswith("제주"): region = "제주"
            
            properties["지역"] = {"select": {"name": region}}

    if address:
        # Fallback text just in case, but we also populate the place if we have lat/lon
        properties["주소"] = {"rich_text": [{"text": {"content": address}}]}
        
        # If the user changed the "주소" column to a Place type, they might have renamed it to "위치" 
        # or kept it as "주소". Let's assume they kept the name as "주소" but changed type to Place.
        # Wait, if it's Place, we MUST send the `place` object. We can check if lat/lon exist.
        if lat and lon:
            properties["주소"] = {
                "place": {
                    "lat": lat,
                    "lon": lon,
                    "name": address,
                    "address": address
                }
            }
    if naver_link:
        try:
            properties["가게 인스타"] = {"url": naver_link}
        except Exception:
            pass

    try:
        page = notion.pages.create(
            parent={"database_id": NOTION_DB_ID},
            properties=properties,
        )
        return {"notion_page_id": page["id"]}
    except Exception as e:
        error_msg = str(e)
        # Fallback if "네이버 지도" or "지역" column is missing in Notion
        retry = False
        if "is not a property that exists" in error_msg:
            if "가게 인스타" in properties:
                del properties["가게 인스타"]
                retry = True
            if "지역" in properties:
                del properties["지역"]
                retry = True
                
            if retry:
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
#  Vercel HTTP Handler
# ══════════════════════════════════════════════════════════
class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[InstaSave] {format % args}")

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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        import pathlib
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ("/", "/index.html"):
            # index.html을 직접 서빙 (api/save.py 기준 상위 디렉토리)
            try:
                html_path = pathlib.Path(__file__).parent.parent / "index.html"
                html_bytes = html_path.read_text(encoding="utf-8").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",   "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(html_bytes)
            except Exception as e:
                self._json(500, {"error": f"index.html 로딩 실패: {e}"})
        else:
            self._json(200, {
                "status":   "ok",
                "service":  "🍕 인스타 맛집 자동 저장기",
                "version":  "6.0.0",
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

        print(f"[InstaSave v6] 저장 시작 | {instagram_url} | by {nickname}")

        # ── Step 1: Apify로 인스타 본문 스크래핑
        ok, caption = fetch_instagram_caption(instagram_url)
        if not ok:
            self._json(502, {"success": False, "message": f"❌ 스크래핑 실패: {caption}"})
            return
        print(f"[InstaSave v6] 캡션 획득 완료 ({len(caption)}자)")

        # ── Step 2: Gemini로 식당명 추출
        ok, restaurant_name, search_query = extract_restaurant_info(caption)
        if not ok:
            self._json(400, {"success": False, "message": f"❌ {search_query}"})
            return

        print(f"[InstaSave v6] 식당명: {restaurant_name} / 검색어: {search_query}")

        # ── Step 3: 네이버 지도 정밀 주소 검색
        address, naver_link, lat, lon = search_naver_address(search_query)
        print(f"[InstaSave v6] 주소 매칭: {address} / lat:{lat} lon:{lon}")

        # ── Step 4: Notion 저장
        result = save_to_notion(
            restaurant_name=restaurant_name,
            address=address,
            instagram_url=instagram_url,
            nickname=nickname,
            naver_link=naver_link,
            lat=lat,
            lon=lon,
        )

        elapsed = round(time.time() - t_start, 2)

        if "error" in result:
            self._json(502, {"success": False, "message": f"❌ Notion 저장 실패: {result['error']}"})
            return

        addr_display = address or "주소 미확인"
        print(f"[InstaSave v6] ✅ 완료 | {restaurant_name} | {addr_display} | {elapsed}s")

        self._json(200, {
            "success":         True,
            "message":         f"✅ {restaurant_name} 저장 완료!",
            "restaurant_name": restaurant_name,
            "address":         addr_display,
            "naver_link":      naver_link or "",
            "registered_by":   nickname,
            "elapsed_s":       elapsed,
        })
