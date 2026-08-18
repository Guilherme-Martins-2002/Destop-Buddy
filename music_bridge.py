import asyncio
import io
import os
import threading
import time
from datetime import datetime
import requests
from flask import Flask, jsonify, render_template_string, Response, make_response
from PIL import Image
from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
from winsdk.windows.storage.streams import DataReader, Buffer, InputStreamOptions

app = Flask(__name__)


# --- CONFIGURAÇÃO DE COORDENADAS (Lisboa) ---
LATITUDE = 39.79459032643735
LONGITUDE = -7.5853006775539935


# --- BUFFER EM MEMÓRIA ---
current_image_bytes = None
history_covers_cache = {}  # {track_id: image_bytes}
playback_history = []
last_logged_track = None
weather_cache = {"temp": "--", "desc": "A carregar...", "last_fetch": 0}

def parse_source_app(app_id: str) -> dict:
    if not app_id:
        return {"name": "Desconhecido", "icon": "🎵"}
    app_lower = app_id.lower()
    if "applemusic" in app_lower or "apple" in app_lower:
        return {"name": "Apple Music", "icon": "🍎"}
    elif "spotify" in app_lower:
        return {"name": "Spotify", "icon": "🟢"}
    elif "tidal" in app_lower:
        return {"name": "Tidal", "icon": "⬛"}
    elif "deezer" in app_lower:
        return {"name": "Deezer", "icon": "🎧"}
    elif any(b in app_lower for b in ["chrome", "msedge", "brave", "firefox"]):
        return {"name": "Web / YouTube", "icon": "🌐"}
    return {"name": app_id.split("!")[-1].replace(".exe", "").capitalize(), "icon": "🎵"}

def get_weather():
    global weather_cache
    now = time.time()
    if now - weather_cache["last_fetch"] < 900 and weather_cache["temp"] != "--":
        return weather_cache
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&current_weather=true"
        res = requests.get(url, timeout=3).json()
        temp = res["current_weather"]["temperature"]
        w_code = res["current_weather"]["weathercode"]

        if w_code == 0: desc = "Céu Limpo ☀️"
        elif w_code in [1, 2, 3]: desc = "Parc. Nublado ⛅"
        elif w_code in [45, 48]: desc = "Nevoeiro 🌫️"
        elif w_code in [51, 53, 55, 61, 63, 65, 80, 81, 82]: desc = "Chuva 🌧️"
        elif w_code in [95, 96, 99]: desc = "Trovoada ⛈️"
        else: desc = "Nublado ☁️"

        weather_cache = {"temp": f"{temp}°C", "desc": desc, "last_fetch": now}
    except Exception:
        weather_cache = {"temp": "N/D", "desc": "Sem Sinal", "last_fetch": now}
    return weather_cache

async def read_thumbnail_stream(thumbnail_ref):
    """Lê diretamente o stream de imagem sem corrupção de concorrência."""
    if not thumbnail_ref:
        return None
    try:
        stream = await thumbnail_ref.open_read_async()
        if not stream or stream.size == 0:
            return None
        
        reader = DataReader(stream)
        await reader.load_async(stream.size)
        data = bytearray(stream.size)
        reader.read_bytes(data)
        
        # Converte e valida como JPEG comprimido em RAM
        with Image.open(io.BytesIO(data)) as img:
            out_io = io.BytesIO()
            img.convert('RGB').save(out_io, format='JPEG', quality=85)
            return out_io.getvalue()
    except Exception as e:
        print(f"Erro a processar thumbnail: {e}")
        return None

async def get_current_media_state():
    global current_image_bytes
    try:
        manager = await MediaManager.request_async()
        session = manager.get_current_session()
        
        if session:
            info = await session.try_get_media_properties_async()
            timeline = session.get_timeline_properties()
            playback = session.get_playback_info()
            
            source_info = parse_source_app(session.source_app_user_model_id)
            title = info.title if info and info.title else ""
            artist = info.artist if info and info.artist else ""
            
            if not title:
                return {"status": "idle", "source": source_info}
                
            pos = timeline.position.total_seconds() if timeline and timeline.position else 0
            dur = (timeline.end_time - timeline.start_time).total_seconds() if timeline and timeline.end_time else 0
            is_playing = playback.playback_status == 4 if playback else False

            # Leitura de capa
            if info and info.thumbnail:
                img_data = await read_thumbnail_stream(info.thumbnail)
                if img_data:
                    current_image_bytes = img_data

            return {
                "status": "playing" if is_playing else "paused",
                "title": title,
                "artist": artist,
                "position": round(pos),
                "duration": round(dur),
                "source": source_info
            }
    except Exception:
        pass
    return {"status": "idle", "source": {"name": "Nenhum", "icon": "⏹️"}}

def background_tracker():
    global last_logged_track
    while True:
        try:
            state = asyncio.run(get_current_media_state())
            if state.get("title") and state["title"] != "":
                track_identifier = f"{state['title']} - {state['artist']}"
                
                if track_identifier != last_logged_track:
                    track_id = str(int(time.time()))
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    if current_image_bytes:
                        history_covers_cache[track_id] = current_image_bytes

                    entry = {
                        "id": track_id,
                        "title": state["title"],
                        "artist": state["artist"],
                        "played_at": timestamp,
                        "source": state.get("source", {"name": "Música", "icon": "🎵"})
                    }
                    playback_history.insert(0, entry)
                    
                    if len(playback_history) > 30:
                        removed = playback_history.pop()
                        history_covers_cache.pop(removed["id"], None)
                            
                    last_logged_track = track_identifier
        except Exception:
            pass
        time.sleep(2)

# ==============================================================================
# HTML TEMPLATE RESPONSIVO (MOBILE FRIENDLY)
# ==============================================================================
HTML_PAGE = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Live Music Dashboard</title>
    <style>
        :root {
            --bg-color: #0b0c10;
            --card-bg: rgba(255, 255, 255, 0.05);
            --card-border: rgba(255, 255, 255, 0.1);
            --accent: #fa2d48;
            --accent-gradient: linear-gradient(135deg, #fa2d48, #ff6b81);
            --text-main: #ffffff;
            --text-sub: #a0a0a0;
            --border-radius: 16px;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            display: flex;
            flex-direction: column;
            align-items: center;
            min-height: 100vh;
        }

        .top-bar {
            width: 100%;
            background: rgba(20, 20, 25, 0.9);
            border-bottom: 1px solid var(--card-border);
            backdrop-filter: blur(20px);
            padding: 12px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.85rem;
            position: sticky;
            top: 0;
            z-index: 100;
        }

        .top-item span { color: var(--text-main); font-weight: 600; }

        .container {
            width: 100%;
            max-width: 600px;
            display: flex;
            flex-direction: column;
            gap: 20px;
            padding: 20px 16px;
        }

        .now-playing-card {
            background: linear-gradient(145deg, rgba(250, 45, 72, 0.15), rgba(20, 20, 25, 0.85));
            border: 1px solid rgba(250, 45, 72, 0.3);
            border-radius: var(--border-radius);
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
        }

        .header-badges { display: flex; justify-content: space-between; align-items: center; }

        .live-badge {
            background: var(--accent);
            color: #fff;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.6px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .source-badge {
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--card-border);
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .media-layout { display: flex; align-items: center; gap: 18px; }

        /* VINIL GIRATÓRIO */
        .vinyl-container {
            position: relative;
            width: 110px;
            height: 110px;
            flex-shrink: 0;
            border-radius: 50%;
            background: radial-gradient(circle, #0d0d0d 0%, #1a1a1a 40%, #0d0d0d 70%, #2b2b2b 100%);
            box-shadow: 0 8px 20px rgba(0,0,0,0.6);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .vinyl-container::before {
            content: "";
            position: absolute;
            width: 96px;
            height: 96px;
            border-radius: 50%;
            border: 1px dashed rgba(255, 255, 255, 0.08);
            pointer-events: none;
        }

        .vinyl-art {
            width: 65px;
            height: 65px;
            border-radius: 50%;
            object-fit: cover;
            border: 2px solid #111;
        }

        .vinyl-center-hole {
            position: absolute;
            width: 12px;
            height: 12px;
            background: #0b0c10;
            border-radius: 50%;
            border: 2px solid #555;
            z-index: 2;
        }

        .spin { animation: rotateVinyl 4s linear infinite; }
        .spin-paused { animation-play-state: paused !important; }

        @keyframes rotateVinyl {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }

        .track-info-hero { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex-grow: 1; }
        .live-title { font-size: 1.2rem; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .live-artist { font-size: 0.9rem; color: var(--text-sub); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        .progress-wrapper { display: flex; flex-direction: column; gap: 6px; }
        .progress-container { width: 100%; height: 6px; background: rgba(255, 255, 255, 0.12); border-radius: 4px; overflow: hidden; }
        .progress-bar { height: 100%; width: 0%; background: var(--accent-gradient); border-radius: 4px; transition: width 0.3s linear; }
        .time-labels { display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-sub); }

        .section-title { font-size: 0.9rem; font-weight: 600; color: var(--text-sub); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 10px; }
        .track-list { display: flex; flex-direction: column; gap: 8px; }

        .track-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 8px 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }

        .track-card-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
        .history-vinyl {
            position: relative;
            width: 42px;
            height: 42px;
            border-radius: 50%;
            background: radial-gradient(circle, #111 0%, #222 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .history-thumb { width: 26px; height: 26px; border-radius: 50%; object-fit: cover; }
        .history-hole { position: absolute; width: 5px; height: 5px; background: #0b0c10; border-radius: 50%; border: 1px solid #666; }

        .track-card-info { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
        .track-card-title { font-size: 0.88rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .track-card-artist { font-size: 0.75rem; color: var(--text-sub); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .track-card-right { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; flex-shrink: 0; }
        .track-card-time { font-size: 0.75rem; color: var(--text-sub); }
        .history-source-tag { font-size: 0.7rem; color: var(--text-sub); }
    </style>
</head>
<body>

    <div class="top-bar">
        <div class="top-item">📅 <span id="clockDisplay">--:--:--</span></div>
        <div class="top-item">🌡️ <span>{{ weather.temp }} ({{ weather.desc }})</span></div>
    </div>

    <div class="container">
        <div class="now-playing-card">
            <div class="header-badges">
                <div class="live-badge">NOW PLAYING</div>
                <div id="liveSource" class="source-badge">🎵 A carregar...</div>
            </div>
            
            <div class="media-layout">
                <div id="vinylDisc" class="vinyl-container spin">
                    <img id="albumArt" class="vinyl-art" src="/artwork.jpg" alt="Capa" onerror="this.src='https://via.placeholder.com/65?text=Disc'">
                    <div class="vinyl-center-hole"></div>
                </div>

                <div class="track-info-hero">
                    <div id="liveTitle" class="live-title">A carregar...</div>
                    <div id="liveArtist" class="live-artist">-</div>
                </div>
            </div>
            
            <div class="progress-wrapper">
                <div class="progress-container">
                    <div id="progressBar" class="progress-bar"></div>
                </div>
                <div class="time-labels">
                    <span id="currTime">0:00</span>
                    <span id="totalTime">0:00</span>
                </div>
            </div>
        </div>

        <div>
            <div class="section-title">Histórico Recente</div>
            <div class="track-list">
                {% for track in history %}
                <div class="track-card">
                    <div class="track-card-left">
                        <div class="history-vinyl">
                            <img class="history-thumb" src="/history_cover/{{ track.id }}" alt="Capa" onerror="this.src='https://via.placeholder.com/26?text=V'">
                            <div class="history-hole"></div>
                        </div>
                        <div class="track-card-info">
                            <div class="track-card-title">{{ track.title }}</div>
                            <div class="track-card-artist">{{ track.artist }}</div>
                        </div>
                    </div>
                    <div class="track-card-right">
                        <div class="track-card-time">{{ track.played_at.split(' ')[1] }}</div>
                        <div class="history-source-tag">{{ track.source.icon }} {{ track.source.name }}</div>
                    </div>
                </div>
                {% else %}
                <div style="color: var(--text-sub); text-align: center; padding: 15px; font-size: 0.85rem;">Nenhuma música registada ainda.</div>
                {% endfor %}
            </div>
        </div>
    </div>

    <script>
        function updateClock() {
            const now = new Date();
            document.getElementById('clockDisplay').innerText = now.toLocaleTimeString('pt-PT');
        }
        setInterval(updateClock, 1000);
        updateClock();

        function formatTime(s) {
            if (!s || isNaN(s)) return "0:00";
            return `${Math.floor(s/60)}:${(s%60 < 10 ? '0' : '') + Math.floor(s%60)}`;
        }

        let lastTitle = "";

        async function updateLive() {
            try {
                const res = await fetch('/now-playing');
                const data = await res.json();
                const vinyl = document.getElementById('vinylDisc');

                if (data.source) {
                    document.getElementById('liveSource').innerText = `${data.source.icon} ${data.source.name}`;
                }

                if (data.status === 'playing' || data.status === 'paused') {
                    if (data.status === 'playing') vinyl.classList.remove('spin-paused');
                    else vinyl.classList.add('spin-paused');

                    document.getElementById('liveTitle').innerText = data.title;
                    document.getElementById('liveArtist').innerText = data.artist;

                    const pos = data.position || 0;
                    const dur = data.duration || 1;
                    document.getElementById('progressBar').style.width = Math.min((pos / dur) * 100, 100) + '%';
                    document.getElementById('currTime').innerText = formatTime(pos);
                    document.getElementById('totalTime').innerText = formatTime(dur);

                    if (lastTitle !== data.title) {
                        // Força a atualização da capa imediatamente
                        document.getElementById('albumArt').src = '/artwork.jpg?t=' + Date.now();
                        if (lastTitle !== "") {
                            setTimeout(() => window.location.reload(), 1200);
                        }
                        lastTitle = data.title;
                    }
                } else {
                    vinyl.classList.add('spin-paused');
                    document.getElementById('liveTitle').innerText = "Pausa / Inativo";
                    document.getElementById('liveArtist').innerText = "Nenhuma música detetada";
                    document.getElementById('progressBar').style.width = '0%';
                }
            } catch (err) {}
        }

        setInterval(updateLive, 500);
        updateLive();
    </script>
</body>
</html>
"""

# ==============================================================================
# ENDPOINTS REST
# ==============================================================================

@app.route('/now-playing', methods=['GET'])
def now_playing():
    data = asyncio.run(get_current_media_state())
    return jsonify(data)

@app.route('/artwork.jpg', methods=['GET'])
def get_artwork():
    if current_image_bytes:
        resp = make_response(current_image_bytes)
        resp.headers['Content-Type'] = 'image/jpeg'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return resp
    return "No artwork", 404

@app.route('/history_cover/<track_id>', methods=['GET'])
def get_history_cover(track_id):
    img_data = history_covers_cache.get(track_id)
    if img_data:
        resp = make_response(img_data)
        resp.headers['Content-Type'] = 'image/jpeg'
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    if current_image_bytes:
        return make_response(current_image_bytes)
    return "Not found", 404

@app.route('/history', methods=['GET'])
@app.route('/', methods=['GET'])
def view_history():
    weather = get_weather()
    return render_template_string(HTML_PAGE, history=playback_history, weather=weather)

if __name__ == '__main__':
    t = threading.Thread(target=background_tracker, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5001)