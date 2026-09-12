"""
附近餐廳搜尋 API
----------------
輸入地址或地點名稱，回傳附近符合條件的餐廳清單。

流程：
1. 先用 Google Geocoding API 把輸入當地址轉成經緯度；失敗的話再用
   Google Places Find Place API 把輸入當地點名稱（如景點、店家名）查一次
2. 使用 Google Places Nearby Search API 搜尋附近餐廳
3. 依照使用者提供的條件（評分、價位、營業中、關鍵字...）進行篩選
"""

import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
CORS(app)  # 保留 CORS，方便日後仍想把前端獨立開在別的網域時使用

# 加上這行的原因：Flask 在 debug=True 時，預設會讓例外「往上傳」給 Werkzeug 顯示
# 互動式的 HTML 除錯頁，而不是交給下面 @app.errorhandler(Exception) 處理。
# 這正是你之前看到「伺服器回應的格式不正確」的成因：前端拿到的是 HTML 除錯頁，
# 不是 JSON。明確關掉傳播，才能保證 API 一定回傳 JSON。
app.config["PROPAGATE_EXCEPTIONS"] = False

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
NEARBY_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"


@app.route("/")
def serve_index():
    """由後端直接提供前端網頁，這樣整個服務只需要一個網址"""
    return send_from_directory(BASE_DIR, "index.html")


@app.errorhandler(HTTPException)
def handle_http_error(err):
    """404、405 這類內建錯誤也統一回傳 JSON"""
    return jsonify({"error": err.description}), err.code


@app.errorhandler(Exception)
def handle_any_error(err):
    """任何未預期的例外都回傳 JSON，避免前端拿到 Flask 預設的 HTML 錯誤頁而解析失敗"""
    app.logger.exception("Unhandled error")
    return jsonify({"error": f"伺服器發生錯誤: {err}"}), 500


def geocode_address(address: str):
    """把地址轉成經緯度，失敗回傳 None"""
    params = {"address": address, "key": GOOGLE_API_KEY, "language": "zh-TW"}
    resp = requests.get(GEOCODE_URL, params=params, timeout=10)
    data = resp.json()

    if data.get("status") != "OK" or not data.get("results"):
        return None, data.get("status", "GEOCODE_FAILED")

    location = data["results"][0]["geometry"]["location"]
    return (location["lat"], location["lng"]), None


def find_place(query: str):
    """把「地點」名稱（如景點、店家名）轉成經緯度，用來補地址轉換失敗時的 fallback"""
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "geometry",
        "key": GOOGLE_API_KEY,
        "language": "zh-TW",
    }
    resp = requests.get(FIND_PLACE_URL, params=params, timeout=10)
    data = resp.json()

    candidates = data.get("candidates") or []
    if data.get("status") != "OK" or not candidates:
        return None, data.get("status", "FIND_PLACE_FAILED")

    location = candidates[0]["geometry"]["location"]
    return (location["lat"], location["lng"]), None


def locate(query: str):
    """先當地址處理，失敗的話再當地點名稱查一次，讓使用者輸入地址或地點都能定位"""
    coords, error = geocode_address(query)
    if coords:
        return coords, None

    coords, place_error = find_place(query)
    if coords:
        return coords, None

    return None, error or place_error


def search_nearby_restaurants(lat, lng, radius, keyword, min_price, max_price, open_now):
    """呼叫 Google Places Nearby Search 搜尋附近餐廳，回傳 (餐廳清單, 錯誤訊息)"""
    params = {
        "location": f"{lat},{lng}",
        "radius": radius,
        "type": "restaurant",
        "key": GOOGLE_API_KEY,
        "language": "zh-TW",
    }
    if keyword:
        params["keyword"] = keyword
    if min_price is not None:
        params["minprice"] = min_price
    if max_price is not None:
        params["maxprice"] = max_price
    if open_now:
        params["opennow"] = "true"

    results = []
    next_page_token = None

    # Google 一頁最多回傳 20 筆，最多取 3 頁 (60 筆)
    for _ in range(3):
        if next_page_token:
            params["pagetoken"] = next_page_token
            import time
            time.sleep(2)  # Google 要求取下一頁前需短暫等待

        resp = requests.get(NEARBY_SEARCH_URL, params=params, timeout=10)
        data = resp.json()
        status = data.get("status")

        if status not in ("OK", "ZERO_RESULTS"):
            # 把 Google 的錯誤狀態帶出去，而不是靜靜吞掉變成空結果
            error_message = data.get("error_message", "")
            return results, f"{status}" + (f"：{error_message}" if error_message else "")

        if status == "ZERO_RESULTS":
            break

        results.extend(data.get("results", []))
        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break

    return results, None


def format_restaurant(place: dict) -> dict:
    """整理成前端好用的格式"""
    location = place.get("geometry", {}).get("location", {})
    photo_ref = None
    if place.get("photos"):
        photo_ref = place["photos"][0].get("photo_reference")

    return {
        "place_id": place.get("place_id"),
        "name": place.get("name"),
        "address": place.get("vicinity"),
        "rating": place.get("rating"),
        "user_ratings_total": place.get("user_ratings_total"),
        "price_level": place.get("price_level"),
        "open_now": place.get("opening_hours", {}).get("open_now"),
        "location": location,
        "photo_reference": photo_ref,
    }


@app.route("/api/restaurants", methods=["GET"])
def get_restaurants():
    if not GOOGLE_API_KEY:
        return jsonify({"error": "尚未設定 GOOGLE_API_KEY 環境變數"}), 500

    address = request.args.get("address")
    if not address:
        return jsonify({"error": "請提供 address 參數"}), 400

    # 篩選條件（皆為選填）
    radius = request.args.get("radius", default=1500, type=int)          # 搜尋半徑（公尺），預設 1500
    min_rating = request.args.get("min_rating", type=float)               # 最低評分 0~5
    price_level = request.args.get("price_level", type=int)               # 單一價位 0~4
    min_price = request.args.get("min_price", type=int)                   # 最低價位 0~4
    max_price = request.args.get("max_price", type=int)                   # 最高價位 0~4
    open_now = request.args.get("open_now", default="false").lower() == "true"  # 是否只顯示營業中
    keyword = request.args.get("keyword")                                 # 關鍵字，如「拉麵」「素食」
    limit = request.args.get("limit", default=20, type=int)               # 回傳筆數上限

    if price_level is not None:
        min_price = max_price = price_level

    coords, error = locate(address)
    if error:
        return jsonify({"error": f"找不到這個地址／地點: {error}"}), 400
    lat, lng = coords

    raw_results, search_error = search_nearby_restaurants(
        lat, lng, radius, keyword, min_price, max_price, open_now
    )
    if search_error:
        return jsonify({"error": f"Google Places 搜尋失敗: {search_error}"}), 502

    restaurants = [format_restaurant(p) for p in raw_results]

    # 依評分篩選（Nearby Search API 本身不支援 min_rating，需自行過濾）
    if min_rating is not None:
        restaurants = [r for r in restaurants if (r["rating"] or 0) >= min_rating]

    # 依評分高到低排序
    restaurants.sort(key=lambda r: (r["rating"] or 0), reverse=True)

    restaurants = restaurants[:limit]

    return jsonify({
        "address": address,
        "location": {"lat": lat, "lng": lng},
        "count": len(restaurants),
        "results": restaurants,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
