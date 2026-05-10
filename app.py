import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import time
import urllib.parse
import re
import math
import random

st.set_page_config(page_title="nomikai-hub", layout="wide")

st.title("🍻 nomikai-hub")

st.markdown(
    """
    ### 飲み会の『ちょうどいい駅』を自動で提案

    nomikai-hub は、メンバーの最寄駅から
    **実際の乗車時間** をもとに、
    みんなが集まりやすい中間駅を自動で見つけるサービスです。
    """

)

HOTPEPPER_API_KEY = "e341cd10b36b67d8"

# --- 画面UI ---
station_inputs = []

st.sidebar.header("🗺️ メンバーの最寄駅")
st.sidebar.caption("2人以上、最大10人まで追加できます")

# 初期人数
if "member_count" not in st.session_state:
    st.session_state.member_count = 2

# 最大10人
MAX_MEMBERS = 10

# メンバー追加ボタン
if st.sidebar.button("＋ メンバーを追加"):
    if st.session_state.member_count < MAX_MEMBERS:
        st.session_state.member_count += 1

# メンバー入力欄を動的生成
for i in range(st.session_state.member_count):
    station = st.sidebar.text_input(
        f"{i + 1}人目の最寄駅",
        value="",
        key=f"station_{i}"
    )

    if station.strip():
        station_inputs.append(station.strip())

st.sidebar.header("🍔 お店のこだわり条件")

genre = st.sidebar.text_input(
    "ジャンル",
    "居酒屋",
    help="例：居酒屋 / 焼肉 / イタリアン / カフェ"
)

search_radius_km = st.sidebar.slider(
    "候補駅の探索範囲（km）",
    min_value=1,
    max_value=15,
    value=5,
    help="重心地点からどの範囲まで駅を探すか設定できます"
)

session = requests.Session()

# スクレイピング待機秒数
SCRAPE_INTERVAL = 0.1

# ルート検索する候補駅数上限
CANDIDATE_LIMIT = 50

# --- ロジック関数 ---

# ① 駅名から緯度・経度を取得
def get_station_coords(station_name):
    url = "https://express.heartrails.com/api/json?method=getStations"
    try:
        response = requests.get(url, params={"name": station_name}, timeout=5)
        if response.status_code == 200:
            stations = response.json().get("response", {}).get("station", [])
            if stations:
                return float(stations[0]["y"]), float(stations[0]["x"])
    except:
        pass
    return None

@st.cache_data(ttl=60 * 60)
def get_all_nearby_stations(lat, lng, radius_km):
    """
    重心から指定半径内の実在駅を取得する
    """

    url = "https://express.heartrails.com/api/json?method=getStations"

    # 緯度経度1度あたりのおおよそのkm換算
    lat_diff = radius_km / 111
    lng_diff = radius_km / (111 * math.cos(math.radians(lat)))

    stations_found = []
    seen = set()

    # 格子状に探索して広範囲の駅を取得
    # 半径5kmでも大量の駅を拾えるように密度を上げる
    step_count = 10

    try:
        for lat_step in np.linspace(lat - lat_diff, lat + lat_diff, step_count):
            for lng_step in np.linspace(lng - lng_diff, lng + lng_diff, step_count):

                response = session.get(
                    url,
                    params={
                        "y": lat_step,
                        "x": lng_step
                    },
                    timeout=5
                )

                if response.status_code != 200:
                    continue

                stations = response.json().get("response", {}).get("station", [])

                for s in stations:
                    try:
                        name = s["name"]
                        station_lat = float(s["y"])
                        station_lng = float(s["x"])

                        # 重心からの距離を計算
                        distance = math.sqrt(
                            ((station_lat - lat) * 111) ** 2 +
                            ((station_lng - lng) * 111 * math.cos(math.radians(lat))) ** 2
                        )

                        # 半径内の駅はすべて候補として保持
                        if distance <= radius_km and name not in seen:
                            seen.add(name)
                            stations_found.append({
                                "name": name,
                                "distance": distance
                            })

                    except:
                        pass

        # 重心に近い順にソート
        stations_found = sorted(stations_found, key=lambda x: x["distance"])

        # 候補駅をすべて返す
        all_station_names = [s["name"] for s in stations_found]

        print(f"Detected Stations Count: {len(all_station_names)}")

        return all_station_names

    except Exception as e:
        print(f"Station Search Error: {e}")

    return []

# ③ Yahoo!路線情報から「乗車時間（分）」をスクレイピング
@st.cache_data(ttl=60 * 30)
def scrape_travel_time(from_station, to_station):
    """
    Yahoo!路線情報から2駅間の乗車時間を取得する
    """
    # 同じ駅なら移動時間0分
    if from_station == to_station:
        return 0

    # 表記ゆれを軽減
    normalize_map = {
        "市ケ谷": "市ヶ谷",
        "ケ": "ヶ"
    }

    for before, after in normalize_map.items():
        from_station = from_station.replace(before, after)
        to_station = to_station.replace(before, after)

    f_station = from_station if from_station.endswith("駅") else f"{from_station}駅"
    t_station = to_station if to_station.endswith("駅") else f"{to_station}駅"

    from_enc = urllib.parse.quote(f_station)
    to_enc = urllib.parse.quote(t_station)

    url = (
        "https://transit.yahoo.co.jp/search/result"
        f"?from={from_enc}&to={to_enc}&y=2026&m=05&d=10&hh=18&m2=0&type=1"
    )
    print(f"SCRAPE URL: {url}")

    # 最大3回リトライ
    for attempt in range(3):
        try:
            response = session.get(url, timeout=10)

            if response.status_code != 200:
                time.sleep(0.5)
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            extracted_times = []

            # Yahoo路線情報の「経路サマリー」のみを厳密取得
            route_summaries = soup.select(".routeSummary")

            for summary in route_summaries:
                try:
                    summary_text = summary.get_text(" ", strip=True)

                    print(f"ROUTE SUMMARY: {summary_text[:200]}")

                    # Yahoo路線情報の「（ XX分 ）」部分だけを厳密取得
                    # 例: 15:15 → 15:39 （ 24分 ）
                    match = re.search(r"（\s*(\d+)分\s*）", summary_text)

                    # fallback: 半角カッコ対応
                    if not match:
                        match = re.search(r"\(\s*(\d+)分\s*\)", summary_text)

                    if not match:
                        continue

                    minute = int(match.group(1))

                    # 異常値除外
                    if 0 <= minute <= 180:
                        extracted_times.append(minute)

                except Exception as e:
                    print(f"SUMMARY PARSE ERROR: {e}")


            if extracted_times:
                # 異常値除去
                extracted_times = sorted(list(set(extracted_times)))

                print(
                    f"EXTRACTED TIMES: {from_station} -> {to_station} = {extracted_times}"
                )

                return min(extracted_times)

        except Exception as e:
            print(f"Scraping Retry {attempt + 1} Failed: {from_station} -> {to_station} / {e}")

        # 少し待ってリトライ
        time.sleep(0.5)

    print(f"FAILED TO EXTRACT: {from_station} -> {to_station}")
    return None

 # ④ 指定駅周辺の東京都内店舗数と店舗リストを取得
def get_shop_count_and_list(station_name, genre_name):
    url = "http://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
    params = {
        "key": HOTPEPPER_API_KEY,
        "keyword": f"{station_name} {genre_name}",
        "format": "json",
        "count": 5,
        "large_area": "Z011"  # 東京都限定
    }
    try:
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            results = response.json().get("results", {})
            total_count = int(results.get("results_available", 0))
            shop_list = results.get("shop", [])
            return total_count, shop_list
    except:
        pass
    return 0, []

# --- メイン処理 ---
if st.sidebar.button("中間駅を検索する 🚀"):
    input_stations = [s.strip() for s in station_inputs if s.strip()]
    
    if len(input_stations) < 2:
        st.error("有効な駅名を2つ以上入力してください。")
    else:
        # 1. メンバーの最寄駅の緯度経度を取得し、中心点（重心）を計算する
        with st.spinner("📍 メンバーの位置情報を解析中..."):
            valid_coords = []
            for s in input_stations:
                coords = get_station_coords(s)
                if coords:
                    valid_coords.append(coords)
                    
        if len(valid_coords) < 2:
            st.error("入力された駅の位置情報を特定できませんでした。正しい駅名か確認してください。")
        else:
            avg_lat = sum(c[0] for c in valid_coords) / len(valid_coords)
            avg_lng = sum(c[1] for c in valid_coords) / len(valid_coords)
            
            # 2. 地図上の重心から、近くにある「実在する駅」を自動で発掘する
            with st.spinner("🚉 中間地点周辺の候補駅を探索中..."):
                detected_candidates = get_all_nearby_stations(
                    avg_lat,
                    avg_lng,
                    search_radius_km
                )
            
            if not detected_candidates:
                st.error("中間地点付近に実在する駅が見つかりませんでした。")
            else:
                # 候補駅が多すぎる場合は、重心に近い順で絞る
                if len(detected_candidates) > CANDIDATE_LIMIT:
                    st.warning(
                        f"候補駅が {len(detected_candidates)} 件見つかったため、\n"
                        f"重心に近い上位{CANDIDATE_LIMIT}駅に絞ってルート検索を行います。"
                    )
                    detected_candidates = detected_candidates[:CANDIDATE_LIMIT]

                st.info(
                    f"🚉 重心地点をもとに、半径 {search_radius_km}km 圏内の候補駅を自動抽出しました。"
                )

                with st.expander("候補駅一覧を見る"):
                    st.write(' / '.join(detected_candidates))

                st.info(
                    "🚃 Yahoo!路線情報からリアルタイムの乗車時間を取得しています。"
                )

                # 3. 自動検出された候補駅に対して、各メンバーの乗車時間をスクレイピング
                travel_times = {origin: {} for origin in input_stations}
                total_steps = len(input_stations) * len(detected_candidates)
                current_step = 0
                
                scrape_bar = st.progress(0, text="乗車時間データを収集中...")
                
                all_success = True
                failed_routes = []
                
                for origin in input_stations:
                    for destination in detected_candidates:
                        current_step += 1
                        scrape_bar.progress(current_step / total_steps, text=f"🚃 【{origin} ➔ {destination}】の乗車時間を取得中...")
                        
                        # スクレイピング実行
                        time_minutes = scrape_travel_time(origin, destination)
                        
                        if time_minutes is not None:
                            travel_times[origin][destination] = time_minutes
                            print(f"SUCCESS: {origin} -> {destination} = {time_minutes}分")
                        else:
                            all_success = False
                            failed_routes.append(f"「{origin} ➔ {destination}」")
                            travel_times[origin][destination] = None
                        
                        # アクセス過多を避けるためランダム待機
                        time.sleep(SCRAPE_INTERVAL + random.uniform(0.05, 0.2))
                        
                scrape_bar.empty()
                
                if not all_success:
                    st.warning("⚠️ 一部の経路で取得に失敗しました。取得成功分のみで続行します。")

                    with st.expander("取得失敗した経路を見る"):
                        for route in failed_routes:
                            st.write(f"- {route}")
                
                # 4. スコアリングと店舗情報の取得
                scores = []
                progress_text = "候補駅周辺の店舗数を調査中..."
                my_bar = st.progress(0, text=progress_text)
                
                for idx, candidate in enumerate(detected_candidates):
                    my_bar.progress((idx + 1) / len(detected_candidates), text=f"🚉 {candidate}駅 のお店を調査中...")
                    
                    shop_count, shops = get_shop_count_and_list(candidate, genre)
                    
                    # お店が少なすぎる駅（競技場など）は排除
                    if shop_count < 3:
                        continue
                        
                    times = [travel_times[origin][candidate] for origin in input_stations]

                    # None が含まれる駅は除外
                    if any(t is None for t in times):
                        continue

                    avg_time = np.mean(times)
                    std_dev = np.std(times)
                    max_diff = max(times) - min(times)

                    # スコア計算
                    # 「近さ」を最重要視しつつ、不公平すぎる駅は減点
                    # 最も遠い人の時間
                    worst_time = max(times)

                    # 「近さ」を最優先
                    # avg_time を強く評価しつつ、極端な不公平を防ぐ
                    fairness_score = (
                        avg_time 
                        + worst_time * 5
                    )

                    scores.append({
                        "station": candidate,
                        "times": times,
                        "avg_time": avg_time,
                        "worst_time": worst_time,
                        "std_dev": std_dev,
                        "max_diff": max_diff,
                        "score": fairness_score,
                        "shop_count": shop_count,
                        "shops": shops
                    })
                    
                my_bar.empty()
                
                if not scores:
                    st.warning("候補駅の中で、条件に合うお店がある駅が見つかりませんでした。別のジャンルでお試しください。")
                else:
                    # 公平性の高い順にソートし、最大5つの駅を最終選出
                    best_fair_stations = sorted(scores, key=lambda x: x["score"])[:5]
                    
                    st.success("🎉 乗車時間を考慮した、ベスト中間駅が決定しました！")
                    st.caption(
                        "※ 地理的な重心付近の駅を候補にし、その後に実際の乗車時間を比較して『時間的に最も公平な駅』を算出しています。"
                    )
                    
                    # タブで結果を表示
                    tabs = st.tabs([f"⚖️ {rank+1}位: {item['station']}駅 (店: {item['shop_count']}件)" for rank, item in enumerate(best_fair_stations)])
                    
                    for tab, item in zip(tabs, best_fair_stations):
                        hub_station = item["station"]
                        shops = item["shops"]
                        with tab:
                            st.write("#### 🚃 各メンバーの乗車時間（Yahoo!路線情報 リアルタイム値）")
                            
                            cols = st.columns(len(input_stations))
                            for col, orig, t in zip(cols, input_stations, item["times"]):
                                col.metric(label=f"{orig} から", value=f"{t} 分")
                            
                            metric1, metric2, metric3, metric4 = st.columns(4)

                            metric1.metric(
                                "平均移動時間",
                                f"{item['avg_time']:.1f}分"
                            )

                            metric2.metric(
                                "最長移動時間",
                                f"{item['worst_time']}分"
                            )

                            metric3.metric(
                                "最大時間差",
                                f"{item['max_diff']}分"
                            )

                            metric4.metric(
                                f"{genre} 店舗数",
                                f"{item['shop_count']}件"
                            )
                            st.write("---")
                            
                            st.write(f"### 🍻 {hub_station}駅 周辺のおすすめ店舗")
                            
                            # マップ表示
                            map_data = []
                            for shop in shops:
                                try:
                                    map_data.append({
                                        "lat": float(shop["lat"]),
                                        "lon": float(shop["lng"]),
                                        "name": shop["name"]
                                    })
                                except:
                                    pass
                            
                            if map_data:
                                df_map = pd.DataFrame(map_data)
                                st.map(df_map, latitude="lat", longitude="lon", size=30)
                            
                            st.write("---")
                            
                            # 店舗表示
                            for shop in shops:
                                col1, col2 = st.columns([1, 4])
                                with col1:
                                    logo = shop.get("logo_image", "")
                                    if logo:
                                        st.image(logo, width=80)
                                with col2:
                                    st.subheader(shop["name"])
                                    st.write(f"🚶 アクセス: {shop.get('access', '情報なし')}")
                                    st.write(f"💰 予算: {shop.get('budget', {}).get('name', '情報なし')}")
                                    st.write(f"🔗 [ホットペッパーで詳細を見る]({shop.get('urls', {}).get('pc', '#')})")
                                st.divider()