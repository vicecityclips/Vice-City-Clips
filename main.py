# ==============================================================================
# PART 1 OF 20: STANDARD LIBRARY & THIRD-PARTY IMPORTS
# Title: Core Runtime Dependencies & Media Processing Libraries
# Subtitle: System utilities, async TTS, Whisper ASR, OpenCV, FFmpeg & Google APIs
# ==============================================================================

# --- 1.1 Standard Library Imports ---
import os
import json
import time
import shutil
import random
import asyncio
import subprocess
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# --- 1.2 Networking, Audio Signal & AI Vision Imports ---
import requests
import numpy as np
from scipy.io import wavfile
import cv2
from faster_whisper import WhisperModel

# --- 1.3 Voice Synthesis & Generative AI Imports ---
import edge_tts
from gtts import gTTS
from google import genai
from google.genai import types

# --- 1.4 YouTube Data API & OAuth2 Imports ---
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==============================================================================
# PART 2 OF 20: CHANNELFORGE MASTER PROFILE & RUNTIME CONSTANTS
# Title: User-Customizable Channel Configuration & Pipeline Parameters
# Subtitle: Maps 1:1 to ChannelForge dashboard toggles, schedules, and quality gates
# ==============================================================================

# --- 2.1 ChannelForge Channel Profile (Dashboard Toggle Schema) ---
CHANNEL_CONFIG = {
    "channel_handle": "@Vice-City_ClipsVault",
    "upload_to_youtube": True,              # True = upload/schedule | False = artifacts only
    "youtube_privacy": "public",            # "public" (scheduled slots) or "private" (test)
    "max_uploads_per_day": 4,               # Daily Shorts cap (00:00, 06:00, 12:00, 18:00 UTC)
    "default_framing": "splitcam",          # "splitcam" (auto facecam), "crop", or "blur"
    "enable_jump_cuts": True,               # Auto-trim dead-air pauses > 0.70s
    "enable_ai_voiceover": True,            # Transformative AI commentary + audio ducking
    "monetization_safe_mode": True,         # Enforces transformative narration + AI disclosure
    "min_quality_score": 7,                 # Reject low-action / menu-browsing clips (< 7/10)
    "caption_highlight_hex": "&H0000FFFF&",  # Bright Yellow active-word pop (ASS BGR hex)
}

# --- 2.2 Pipeline Execution & Clip Timing Constants ---
RUN_MODE = os.environ.get("RUN_MODE", "auto").strip().lower()
MAX_STREAMS_TO_RECORD = 6
CHUNK_SECONDS = 480

SINGLE_CLIP_DUR = 38
MULTI_CLIP_DUR = 15
HOOK_DURATION = 2.2
TRANSITION_DUR = 0.35

# --- 2.3 Storage Paths & Deduplication Windows ---
HISTORY_FILE = "history.json"
CANDIDATE_DIR = "candidate_pool"
ARTIFACTS_DIR = "artifacts"
MAX_CLIPS_PER_STREAM = 2
MIN_GAP_SECONDS = 7200

# ==============================================================================
# PART 3 OF 20: SAFE URL CONSTRUCTOR & TARGET CREATOR DIRECTORIES
# Title: Dynamic Endpoint Builder & Global + Indian Creator Watchlists
# Subtitle: Builds clean HTTPS endpoints without hardcoded URL literals
# ==============================================================================

# --- 3.1 Safe URL Constructor Helper ---
def make_url(host, path="", query=""):
    """Constructs clean HTTPS URLs dynamically using urllib.parse."""
    return urllib.parse.urlunparse(("https", host, path, "", query, ""))

# --- 3.2 Twitch API Endpoints & Client Header ---
TWITCH_GQL_URL = make_url("gql.twitch.tv", "/gql")
TWITCH_ORIGIN_URL = make_url("www.twitch.tv")
TWITCH_REFERER_URL = make_url("www.twitch.tv", "/")
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

# --- 3.3 Priority Live Streamers (International & Indian Gaming Creators) ---
PRIORITY_CREATORS = [
    # International Creators
    "kaicenat", "ishowspeed", "xqc", "shroud", "sykkuno", "valkyrae",
    "zerkaa", "summit1g", "lirik", "timthetatman", "nickmercs",
    "caseoh_", "jynxzi", "agent00", "fanum", "duke", "adinross",
    "trainwreckstv", "n3on", "stableronaldo", "plaqueboymax",
    # Indian Gaming Creators
    "carryislive", "triggeredinsaan", "nischaymalhan", "therawknee",
    "rawknee", "technogamerz", "mythpat", "beastboyshub", "gamerfleet",
    "liveinsaan", "fukrainsaan", "payalgaming", "jonathangaming",
    "mortal", "scoutop"
]

# --- 3.4 YouTube GTA 6 Discovery Search Queries ---
YOUTUBE_CREATOR_QUERIES = [
    "GTA 6 gameplay highlights",
    "GTA VI live stream moments",
    "CarryisLive GTA 6",
    "IShowSpeed GTA 6",
    "Nischay Malhan Triggered Insaan GTA 6",
    "RawKnee GTA 6",
    "Techno Gamerz GTA 6",
    "Mythpat BeastBoyShub GTA 6",
    "Kai Cenat xQc GTA 6"
]

# --- 3.5 FFmpeg Dynamic Transition Pool ---
XFADE_TRANSITIONS = [
    "smoothleft", "wipeleft", "slidedown", "slideup",
    "circleopen", "radial", "pixelize", "fadeblack", "distance"
]

# ==============================================================================
# PART 4 OF 20: PROFANITY GUARDRAILS & TEXT SANITIZATION HELPERS
# Title: Advertiser-Friendly Caption Masking & Audio Censorship Dictionary
# Subtitle: Protects channel monetization by masking explicit words on screen & audio
# ==============================================================================

# --- 4.1 Explicit Word Blocklist ---
PROFANITY_WORDS = {
    "FUCK", "FUCKING", "FUCKED", "FUCKS", "MOTHERFUCKER", "MOTHERFUCKING",
    "SHIT", "SHITTING", "BULLSHIT", "BITCH", "BITCHES", "BITCHING",
    "ASSHOLE", "DICK", "DICKS", "PUSSY", "CUNT", "BASTARD", "WHORE", "SLUT",
    "NIGGA", "NIGGER", "FAGGOT", "RETARD", "RETARDED", "COCK", "TWAT"
}

# --- 4.2 Caption Token Masking Helper ---
def censor_word_text(word_upper):
    """Replaces inner letters of explicit words with asterisks for safe on-screen captions."""
    clean = "".join(c for c in word_upper if c.isalpha())
    if clean in PROFANITY_WORDS and len(clean) >= 2:
        masked = clean[:1] + "*" * (len(clean) - 1)
        return word_upper.replace(clean, masked)
    return word_upper

# ==============================================================================
# PART 5 OF 20: PERSISTENT STATE, DEDUPLICATION & UPLOAD HISTORY MANAGER
# Title: JSON State Persistence & Anti-Duplicate Stream Tracking
# Subtitle: Tracks clipped streams, booked UTC slots, recent uploads & weekly recaps
# ==============================================================================

# --- 5.1 Load & Save History State ---
def load_history():
    """Loads history.json and ensures all required tracking keys exist."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "streams" not in data:
                    return {
                        "streams": data,
                        "scheduled_slots": [],
                        "recent_uploads": [],
                        "last_weekly_recap_date": ""
                    }
                data.setdefault("scheduled_slots", [])
                data.setdefault("recent_uploads", [])
                data.setdefault("last_weekly_recap_date", "")
                return data
        except Exception:
            return {"streams": {}, "scheduled_slots": [], "recent_uploads": [], "last_weekly_recap_date": ""}
    return {"streams": {}, "scheduled_slots": [], "recent_uploads": [], "last_weekly_recap_date": ""}

def save_history(history_data):
    """Writes updated state back to history.json."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2)

# --- 5.2 Stream Cooldown & Clip Cap Check ---
def can_clip_stream(stream_key, history_data):
    """Prevents re-clipping the same stream within MIN_GAP_SECONDS or exceeding MAX_CLIPS_PER_STREAM."""
    streams_map = history_data.get("streams", {})
    entry = streams_map.get(stream_key)
    if not entry:
        return True
    if entry.get("count", 0) >= MAX_CLIPS_PER_STREAM:
        return False
    elapsed = int(time.time() - entry.get("last_clipped_at", 0))
    return elapsed >= MIN_GAP_SECONDS

# --- 5.3 Record Completed Upload & Analytics Metadata ---
def record_scheduled_upload(stream_specs, slot_iso, video_id=None, title="", mode="SINGLE", framing="splitcam"):
    """Logs clipped sources, marks the UTC slot booked, and stores video metadata for analytics sync."""
    history_data = load_history()
    streams_map = history_data.setdefault("streams", {})
    creators = []

    for cand, _, peak_sec, _ in stream_specs:
        key = cand.get("stream_id") or cand["live_url"]
        c_name = cand["channel_name"]
        if c_name not in creators:
            creators.append(c_name)
        entry = streams_map.get(key, {
            "title": cand["stream_title"],
            "channel": c_name,
            "url": cand["live_url"],
            "count": 0,
            "clips": []
        })
        entry["count"] += 1
        entry["last_clipped_at"] = int(time.time())
        entry["clips"].append({
            "scheduled_utc": slot_iso or "immediate",
            "chunk_peak_sec": int(peak_sec)
        })
        streams_map[key] = entry

    if slot_iso and slot_iso not in history_data["scheduled_slots"]:
        history_data["scheduled_slots"].append(slot_iso)

    if video_id:
        recent = history_data.setdefault("recent_uploads", [])
        recent.append({
            "video_id": video_id,
            "title": title,
            "creators": creators,
            "mode": mode,
            "framing": framing,
            "views": 0,
            "uploaded_at": int(time.time())
        })
        history_data["recent_uploads"] = recent[-25:]

    save_history(history_data)

# ==============================================================================
# PART 6 OF 20: EXACT-HOUR YOUTUBE PUBLISHING SLOT SCHEDULER
# Title: Automated Daily Quota & Peak-Hour UTC Slot Allocator
# Subtitle: Spaces Shorts evenly at 00:00, 06:00, 12:00, and 18:00 UTC without collisions
# ==============================================================================

# --- 6.1 Find Next Available UTC Publish Slots ---
def get_available_publish_slots(history_data, max_needed=3):
    """Returns open exact-hour UTC timestamps respecting the daily upload cap."""
    now_utc = datetime.now(timezone.utc)
    booked = set(history_data.get("scheduled_slots", []))

    # Prune past slots
    active_booked = [s for s in booked if s > now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")]
    history_data["scheduled_slots"] = active_booked
    booked = set(active_booked)

    # Enforce 24-hour rolling daily cap
    cutoff_24h = (now_utc + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    booked_next_24h = [s for s in booked if s <= cutoff_24h]
    max_daily = CHANNEL_CONFIG["max_uploads_per_day"]
    remaining_daily_quota = max(0, max_daily - len(booked_next_24h))

    if remaining_daily_quota == 0:
        print(f"[Scheduler] Daily cap reached ({max_daily}/{max_daily} slots booked in next 24h).", flush=True)
        return []

    base_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate_slots = []
    for day_offset in (0, 1, 2):
        for hr in (0, 6, 12, 18):
            slot_dt = base_day + timedelta(days=day_offset, hours=hr)
            if slot_dt >= (now_utc + timedelta(minutes=15)):
                iso_str = slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if iso_str not in booked:
                    candidate_slots.append(iso_str)

    return candidate_slots[:min(max_needed, remaining_daily_quota)]

# ==============================================================================
# PART 7 OF 20: LIVE STREAM DISCOVERY — TWITCH GRAPHQL & STREAMLINK FALLBACK
# Title: Strict GTA 6 Live Stream Finder on Twitch
# Subtitle: Filters out generic GTA V RP menu streams and prioritizes top creators
# ==============================================================================

# --- 7.1 Direct Creator Live Check via Streamlink ---
def check_creator_live_via_streamlink(login):
    """Fallback live-status probe for individual priority creators using Streamlink JSON."""
    url = make_url("www.twitch.tv", "/" + login)
    try:
        res = subprocess.run(
            ["streamlink", "--json", url],
            capture_output=True, text=True, timeout=12
        )
        if res.returncode == 0 and res.stdout.strip().startswith("{"):
            info = json.loads(res.stdout)
            meta = info.get("metadata") or {}
            game = str(meta.get("category") or meta.get("game") or "")
            title = str(meta.get("title") or "GTA 6 Live")
            author = str(meta.get("author") or login)
            return {
                "stream_id": f"twitch_{login}_{int(time.time() // 7200)}",
                "login": login,
                "creator": author,
                "title": title,
                "viewers": 1000,
                "game": game,
                "url": url,
            }
    except Exception:
        pass
    return None

# --- 7.2 Twitch GraphQL Directory Query with Strict GTA 6 Filter ---
def fetch_twitch_directory_streams(limit=25):
    """Queries Twitch GraphQL for live Grand Theft Auto VI / Vice City broadcasts."""
    query = """
    query($gameName: String!, $limit: Int!) {
      game(name: $gameName) {
        streams(first: $limit) {
          edges {
            node {
              id
              title
              viewersCount
              broadcaster {
                login
                displayName
              }
            }
          }
        }
      }
    }
    """
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": TWITCH_ORIGIN_URL,
        "Referer": TWITCH_REFERER_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    gta6_keywords = ("gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6", "vice city", "leonida")
    results = []
    seen_logins = set()

    for game_name in ("Grand Theft Auto VI", "Grand Theft Auto V"):
        payload = {"query": query, "variables": {"gameName": game_name, "limit": limit}}
        try:
            resp = requests.post(TWITCH_GQL_URL, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200 and resp.text.strip().startswith("{"):
                data = resp.json()
                edges = (((data.get("data") or {}).get("game") or {}).get("streams") or {}).get("edges") or []
                print(f"[Twitch] '{game_name}' returned {len(edges)} raw streams.", flush=True)
                for edge in edges:
                    node = edge.get("node") or {}
                    bcast = node.get("broadcaster") or {}
                    login = str(bcast.get("login") or "").strip().lower()
                    display = str(bcast.get("displayName") or login).strip()
                    stream_id = str(node.get("id") or "").strip()
                    title = str(node.get("title") or "").strip()
                    viewers = int(node.get("viewersCount") or 0)
                    if not login or not stream_id or login in seen_logins:
                        continue
                    t_low = title.lower()
                    if any(bad in t_low for bad in ("rerun", "rebroadcast", "24/7", "fake")):
                        continue
                    if game_name == "Grand Theft Auto V" and not any(kw in t_low for kw in gta6_keywords):
                        continue
                    seen_logins.add(login)
                    results.append({
                        "stream_id": f"twitch_{login}_{stream_id}",
                        "login": login,
                        "creator": display,
                        "title": title,
                        "viewers": viewers,
                        "game": game_name,
                        "url": make_url("www.twitch.tv", "/" + login),
                    })
            else:
                print(f"[Warn] Twitch GQL HTTP {resp.status_code} ({game_name})", flush=True)
        except Exception as e:
            print(f"[Warn] Twitch GQL check failed ({e})", flush=True)

    if not results:
        shuffled = list(PRIORITY_CREATORS)
        random.shuffle(shuffled)
        for login in shuffled[:12]:
            live_item = check_creator_live_via_streamlink(login)
            if live_item and any(kw in (live_item["game"] + " " + live_item["title"]).lower() for kw in gta6_keywords):
                results.append(live_item)
                if len(results) >= 6:
                    break

    results.sort(key=lambda x: (x["login"] in PRIORITY_CREATORS, x["viewers"]), reverse=True)
    print(f"[Twitch] Selected {len(results)} strict GTA 6 live streams.", flush=True)
    return results

# ==============================================================================
# PART 8 OF 20: LIVE STREAM DISCOVERY — KICK API (CLOUDFLARE BYPASS)
# Title: Browser-Impersonated Kick Live Stream Harvester
# Subtitle: Uses curl_cffi TLS fingerprinting to fetch live GTA 6 streams on Kick
# ==============================================================================

# --- 8.1 Fetch Live GTA 6 Streams from Kick ---
def fetch_kick_gta_streams(limit=12):
    """Queries Kick's category API with Chrome TLS impersonation to bypass HTTP 403 blocks."""
    try:
        from curl_cffi import requests as cffi_requests
        fetcher = lambda u: cffi_requests.get(u, impersonate="chrome124", timeout=15)
    except Exception:
        fetcher = lambda u: requests.get(
            u,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            },
            timeout=15,
        )

    gta6_keywords = ("gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6", "vice city", "leonida")
    results = []
    seen_slugs = set()

    for slug in ("grand-theft-auto-vi", "grand-theft-auto-v"):
        try:
            url = make_url("kick.com", f"/api/v2/categories/{slug}/livestreams", "page=1")
            r = fetcher(url)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                items = (r.json().get("data") or [])[:limit]
                print(f"[Kick] '{slug}' returned {len(items)} live streams.", flush=True)
                for item in items:
                    chan = item.get("channel") or {}
                    user_slug = str(chan.get("slug") or "").strip().lower()
                    username = str((chan.get("user") or {}).get("username") or user_slug).strip()
                    title = str(item.get("session_title") or "GTA Live").strip()
                    sid = str(item.get("id") or int(time.time() // 7200))
                    if not user_slug or user_slug in seen_slugs:
                        continue
                    if slug == "grand-theft-auto-v" and not any(kw in title.lower() for kw in gta6_keywords):
                        continue
                    seen_slugs.add(user_slug)
                    results.append({
                        "stream_id": f"kick_{user_slug}_{sid}",
                        "login": user_slug,
                        "creator": username,
                        "title": title,
                        "viewers": int(item.get("viewer_count") or 0),
                        "game": "Grand Theft Auto VI",
                        "url": make_url("kick.com", "/" + user_slug),
                    })
            else:
                print(f"[Warn] Kick '{slug}' returned HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"[Warn] Kick '{slug}' skipped: {e}", flush=True)

    results.sort(key=lambda x: (x["login"] in PRIORITY_CREATORS, x["viewers"]), reverse=True)
    print(f"[Kick] Selected {len(results)} strict GTA 6 live streams.", flush=True)
    return results

# ==============================================================================
# PART 9 OF 20: PARALLEL LIVE STREAM RECORDER (THREADPOOLEXECUTOR)
# Title: Concurrent 8-Minute Stream Buffer Capture
# Subtitle: Records up to 6 live Twitch/Kick streams simultaneously into .ts buffers
# ==============================================================================

# --- 9.1 Record Single Stream Worker ---
def record_single_live_stream(idx_and_item):
    """Captures CHUNK_SECONDS of live 720p stream footage using yt-dlp + FFmpeg."""
    idx, item = idx_and_item
    out_file = f"raw_live_{idx}.ts"
    url = item["url"]
    creator = item["creator"]
    print(f"[Live {idx+1}] Recording {CHUNK_SECONDS}s from {creator} ({url})...", flush=True)

    cmd = [
        "yt-dlp", "-f", "best[height<=720]",
        "--downloader", "ffmpeg", "--downloader-args", f"ffmpeg_i:-t {CHUNK_SECONDS}",
        "--fixup", "never", "-o", out_file, "--no-part", "--no-warnings", url
    ]
    try:
        subprocess.run(cmd, check=True, timeout=CHUNK_SECONDS + 210)
        if os.path.exists(out_file) and os.path.getsize(out_file) > 1_000_000:
            return {
                "idx": idx,
                "video_path": out_file,
                "stream_id": item["stream_id"],
                "live_url": url,
                "stream_title": item["title"],
                "channel_name": creator,
            }
    except Exception as e:
        print(f"[Live {idx+1}] Skipped ({creator}): {e}", flush=True)
    return None

# ==============================================================================
# PART 10 OF 20: YOUTUBE GTA 6 VOD & LIVE STREAM HARVESTER
# Title: Bot-Resistant YouTube Highlights & Live Stream Fallback Scraper
# Subtitle: Uses tv,android_vr player clients to download clean 720p gameplay windows
# ==============================================================================

# --- 10.1 Search & Download YouTube GTA 6 Windows ---
def harvest_youtube_gta_videos(history_data, max_videos=4):
    """Searches YouTube for GTA 6 creator streams/VODs and downloads 3-minute gameplay sections."""
    yt_client_args = [
        "--extractor-args", "youtube:player_client=tv,android_vr",
        "--no-warnings", "--no-playlist"
    ]

    picked_queries = random.sample(YOUTUBE_CREATOR_QUERIES, k=min(3, len(YOUTUBE_CREATOR_QUERIES)))
    unique_ids = []

    for q in picked_queries:
        try:
            sr = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--print", "id", f"ytsearch8:{q}"],
                capture_output=True, text=True, timeout=25
            )
            for line in sr.stdout.splitlines():
                vid = line.strip()
                if len(vid) == 11 and vid not in unique_ids:
                    unique_ids.append(vid)
        except Exception as e:
            print(f"[YouTube] Search skipped ({q}): {e}", flush=True)

    print(f"[YouTube] Discovered {len(unique_ids)} candidate video IDs.", flush=True)
    harvested = []

    for vid in unique_ids:
        if len(harvested) >= max_videos:
            break
        stream_key = f"yt_{vid}"
        if not can_clip_stream(stream_key, history_data):
            continue

        watch_url = make_url("://youtube.com", "/watch", f"v={vid}")
        try:
            meta_res = subprocess.run(
                ["yt-dlp", *yt_client_args, "--dump-single-json", watch_url],
                capture_output=True, text=True, timeout=25
            )
            if meta_res.returncode != 0 or not meta_res.stdout.strip().startswith("{"):
                continue
            info = json.loads(meta_res.stdout)
            title = str(info.get("title") or "GTA 6 Gameplay").strip()
            uploader = str(info.get("uploader") or info.get("channel") or "YouTube Creator").strip()
            duration = int(info.get("duration") or 0)
            is_live = bool(info.get("is_live"))

            if not is_live and duration < 90:
                continue

            out_mp4 = f"raw_yt_{vid}.mp4"
            if is_live:
                dl_cmd = [
                    "yt-dlp", *yt_client_args, "-f", "best[height<=720]",
                    "--downloader", "ffmpeg", "--downloader-args", "ffmpeg_i:-t 240",
                    "-o", out_mp4, watch_url
                ]
            else:
                start_offset = random.randint(60, max(65, min(duration - 180, 1200)))
                end_offset = start_offset + 180
                dl_cmd = [
                    "yt-dlp", *yt_client_args, "-f", "best[height<=720]",
                    "--download-sections", f"*{start_offset}-{end_offset}",
                    "--force-keyframes-at-cuts", "-o", out_mp4, watch_url
                ]

            subprocess.run(dl_cmd, check=True, timeout=180)
            if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 800_000:
                print(f"[YouTube] Harvested clip from {uploader}: {title[:55]}", flush=True)
                harvested.append({
                    "idx": 100 + len(harvested),
                    "video_path": out_mp4,
                    "stream_id": stream_key,
                    "live_url": watch_url,
                    "stream_title": title,
                    "channel_name": uploader,
                })
        except Exception as e:
            print(f"[YouTube] Skipped {vid}: {e}", flush=True)

    return harvested

# ==============================================================================
# PART 11 OF 20: AUDIO SPIKE DETECTOR & WHISPER SPEECH TRANSCRIBER
# Title: Acoustic Hype Peak Scanner & Timestamped Dialogue Extractor
# Subtitle: Pinpoints sudden scream/explosion spikes and transcribes speech via Whisper
# ==============================================================================

# --- 11.1 Extract Audio Peaks & Timestamped Transcripts ---
def extract_audio_candidates(recorded_items):
    """Analyzes RMS energy spikes and runs faster-whisper ASR on every recorded source."""
    candidates = []
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")

    for item in recorded_items:
        idx = item["idx"]
        video_path = item["video_path"]
        wav_path = f"audio_{idx}.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", wav_path],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            rate, data = wavfile.read(wav_path)
            data = np.abs(data.astype(np.float32))
            num_secs = len(data) // rate
            if num_secs < 65:
                continue

            # Compute per-second RMS loudness and sudden jump ratio
            energies = np.array([np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2)) for i in range(num_secs)])
            jump_scores = np.zeros(num_secs)
            for s in range(22, num_secs - 20):
                jump_scores[s] = np.mean(energies[s - 1 : s + 3]) / (np.mean(energies[s - 14 : s - 2]) + 1e-6)

            best_sec = int(np.argmax(jump_scores))
            best_jump = float(jump_scores[best_sec])

            segments, _ = model.transcribe(wav_path)
            lines = [
                f"[{int(seg.start)}s] {seg.text.strip()}"
                for seg in segments
                if 12 <= seg.start <= (num_secs - 18) and seg.text.strip()
            ]
            candidates.append({
                "idx": idx,
                "video_path": video_path,
                "stream_id": item["stream_id"],
                "live_url": item["live_url"],
                "stream_title": item["stream_title"],
                "channel_name": item["channel_name"],
                "num_secs": num_secs,
                "audio_peak": best_sec,
                "jump_ratio": best_jump,
                "transcript": "\n".join(lines[:160]),
            })
        except Exception as e:
            print(f"[Analyze] Skipped item {idx}: {e}", flush=True)

    return candidates

# ==============================================================================
# PART 12 OF 20: SELF-IMPROVING YOUTUBE ANALYTICS PERFORMANCE SYNC
# Title: Automated View-Count Feedback Loop
# Subtitle: Queries YouTube API for recent upload views to guide Gemini's clip selection
# ==============================================================================

# --- 12.1 Sync Recent Upload Views & Build AI Insight Prompt ---
def sync_youtube_analytics(history_data):
    """Fetches real view counts for recent uploads so Gemini prioritizes winning creators and styles."""
    recent = history_data.get("recent_uploads") or []
    token_raw = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not recent or not token_raw:
        return ""

    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_raw))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        youtube = build("youtube", "v3", credentials=creds)

        ids = [u["video_id"] for u in recent[-15:] if u.get("video_id")]
        if not ids:
            return ""
        resp = youtube.videos().list(part="statistics", id=",".join(ids)).execute()
        stats_map = {
            item["id"]: int((item.get("statistics") or {}).get("viewCount", 0))
            for item in resp.get("items", [])
        }

        for u in recent:
            vid = u.get("video_id")
            if vid in stats_map:
                u["views"] = stats_map[vid]
        save_history(history_data)

        ranked = sorted(recent, key=lambda x: x.get("views", 0), reverse=True)
        top_winners = [u for u in ranked if u.get("views", 0) > 0][:3]
        if top_winners:
            summary = "; ".join(
                f"'{w['title'][:40]}' ({w['views']} views, creators={','.join(w.get('creators', []))}, mode={w.get('mode')})"
                for w in top_winners
            )
            print(f"[Analytics Loop] Top recent performers: {summary}", flush=True)
            return f"\nCHANNEL ANALYTICS INSIGHT (Prioritize similar creators/pacing): {summary}\n"
    except Exception as e:
        print(f"[Analytics Loop] Skipped: {e}", flush=True)
    return ""

# ==============================================================================
# PART 13 OF 20: GEMINI AI PRODUCER & 7/10 ACTION QUALITY GATE
# Title: Viral Storyboard Selection & Anti-Boring Filter
# Subtitle: Scores candidates 1–10 and discards slow menu-browsing or AFK streams
# ==============================================================================

# --- 13.1 Evaluate Candidates & Plan Viral Shorts ---
def plan_shorts_from_candidates(candidates, max_shorts=3, analytics_hint=""):
    """Uses Gemini 2.5 Flash to score action intensity (1-10) and select SINGLE or MULTI clips."""
    if not candidates:
        return []

    min_score = CHANNEL_CONFIG["min_quality_score"]
    default_frame = CHANNEL_CONFIG["default_framing"]
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if api_key:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=45000))
        blocks = []
        for c in candidates:
            blocks.append(
                f"=== SOURCE {c['idx']} ({c['channel_name']} | Spike: {c['jump_ratio']:.2f}x at {c['audio_peak']}s | MaxSec: {c['num_secs']-20}) ===\n"
                f"Title: {c['stream_title']}\nTranscript:\n{c['transcript']}\n"
            )

        prompt = f"""You are a ruthless viral YouTube Shorts producer for a GTA 6 channel.
Evaluate these {len(candidates)} recordings and build up to {max_shorts} SEPARATE viral Shorts.{analytics_hint}
CRITICAL QUALITY GATE:
- Score each planned Short from 1 to 10 for REAL action, chaos, comedy, or hype.
- If the transcript shows boring menu browsing, buying ammo/armor slowly, AFK chatter, or thanking donations with zero action, SCORE IT 2/10!
- MODE SINGLE: 1 source with a complete 38-second high-action story/stunt/scream.
- MODE MULTI: 3 rapid 15-second bursts from different sources.
- FRAMING: Use 'splitcam' if the streamer likely has a facecam, 'crop' for full-screen gameplay, or 'blur' for wide cinematic scenes.

{''.join(blocks)}

Reply using this exact format ONLY:
SHORT1: <SCORE 1-10> | <SINGLE or MULTI> | <splitcam, crop, or blur> | <idx1>,<peak1> ; <idx2>,<peak2>
SHORT2: <SCORE 1-10> | <SINGLE or MULTI> | <splitcam, crop, or blur> | <idx1>,<peak1>"""

        try:
            resp = client.models.generate_content(
                model="gemini-3.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.65),
            )
            planned_shorts = []
            used_ids = set()

            for line in resp.text.strip().splitlines():
                if not line.upper().startswith("SHORT") or ":" not in line:
                    continue
                body = line.split(":", 1)[-1].strip()
                parts = [p.strip() for p in body.split("|")]
                if len(parts) < 4:
                    continue

                score_dig = "".join(ch for ch in parts if ch.isdigit())
                score = int(score_dig) if score_dig else 5
                if score < min_score:
                    print(f"[Quality Gate] Rejected low-action clip (Score {score}/10 < {min_score}/10).", flush=True)
                    continue

                m_raw, f_raw, picks_raw = parts, parts.lower(), parts
                mode = "MULTI" if "MULTI" in m_raw.upper() else "SINGLE"
                if "splitcam" in f_raw:
                    framing = "splitcam"
                elif "crop" in f_raw:
                    framing = "crop"
                elif "blur" in f_raw:
                    framing = "blur"
                else:
                    framing = default_frame

                parsed_specs = []
                for pair in picks_raw.split(";"):
                    if "," not in pair:
                        continue
                    left_s, _, right_s = pair.partition(",")
                    s_dig = "".join(ch for ch in left_s if ch.isdigit())
                    p_dig = "".join(ch for ch in right_s if ch.isdigit())
                    if s_dig and p_dig:
                        s_i, p_s = int(s_dig), int(p_dig)
                        if s_i in used_ids:
                            continue
                        for c in candidates:
                            if c["idx"] == s_i and 18 <= p_s <= (c["num_secs"] - 18):
                                dur = SINGLE_CLIP_DUR if mode == "SINGLE" else MULTI_CLIP_DUR
                                pre = 20 if mode == "SINGLE" else 9
                                parsed_specs.append((c, max(0, p_s - pre), p_s, dur))
                                used_ids.add(s_i)
                                break

                if mode == "SINGLE" and parsed_specs:
                    planned_shorts.append(("SINGLE", framing, [next(iter(parsed_specs))]))
                elif mode == "MULTI" and len(parsed_specs) >= 2:
                    planned_shorts.append(("MULTI", framing, parsed_specs[:3]))

            if planned_shorts:
                print(f"[AI Producer] Approved {len(planned_shorts)} high-action Shorts (>={min_score}/10)!", flush=True)
                return planned_shorts[:max_shorts]
            print(f"[Quality Gate] Zero clips met the {min_score}/10 action bar this run.", flush=True)
            return []
        except Exception as e:
            print(f"[AI Producer] Fallback triggered: {e}", flush=True)

    sorted_c = sorted(candidates, key=lambda x: x["jump_ratio"], reverse=True)
    best_c = next(iter(sorted_c))
    return [("SINGLE", default_frame, [(best_c, max(0, best_c["audio_peak"] - 20), best_c["audio_peak"], SINGLE_CLIP_DUR)])]

# ==============================================================================
# PART 14 OF 20: AUTO SILENCE JUMP-CUT ENGINE & MICRO PUNCH-IN ZOOMS
# Title: Whisper Word-Timestamp Dead-Air Trimmer
# Subtitle: Slices pauses > 0.70s and applies alternating 1.06x punch-ins for high retention
# ==============================================================================

# --- 14.1 Trim Dead Air & Apply Alternating Punch-In Cuts ---
def apply_auto_jump_cuts(raw_mp4, out_mp4):
    """Trims awkward silent pauses > 0.70s and adds subtle 1.06x zoom punch-ins on alternating cuts."""
    if not CHANNEL_CONFIG["enable_jump_cuts"]:
        os.replace(raw_mp4, out_mp4)
        return

    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, info = model.transcribe(raw_mp4, word_timestamps=True)
    total_dur = float(getattr(info, "duration", 0) or 38.0)

    raw_windows = []
    for seg in segments:
        for w in seg.words:
            raw_windows.append((max(0.0, w.start - 0.25), min(total_dur, w.end + 0.30)))

    if not raw_windows:
        os.replace(raw_mp4, out_mp4)
        return

    merged = [next(iter(raw_windows))]
    for s_t, e_t in raw_windows[1:]:
        prev_s, prev_e = merged[-1]
        if s_t - prev_e <= 0.70:
            merged[-1] = (prev_s, max(prev_e, e_t))
        else:
            merged.append((s_t, e_t))

    kept_dur = sum(e - s for s, e in merged)
    if len(merged) <= 1 or len(merged) > 10 or kept_dur < 12.0:
        os.replace(raw_mp4, out_mp4)
        return

    v_parts, a_parts, labels = [], [], []
    for i, (s_t, e_t) in enumerate(merged):
        zoom_fx = ",scale=iw*1.06:ih*1.06,crop=iw/1.06:ih/1.06" if (i % 2 == 1) else ""
        v_parts.append(f"[0:v]trim=start={s_t:.2f}:end={e_t:.2f},setpts=PTS-STARTPTS{zoom_fx}[v{i}];")
        a_parts.append(f"[0:a]atrim=start={s_t:.2f}:end={e_t:.2f},asetpts=PTS-STARTPTS[a{i}];")
        labels.append(f"[v{i}][a{i}]")

    fc = "".join(v_parts + a_parts) + "".join(labels) + f"concat=n={len(merged)}:v=1:a=1[outv][outa]"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", raw_mp4, "-filter_complex", fc,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", "-ac", "2", out_mp4
        ], check=True)
        print(f"[JumpCuts] Trimmed dead air: {total_dur:.1f}s -> {kept_dur:.1f}s ({len(merged)} fast cuts).", flush=True)
    except Exception:
        os.replace(raw_mp4, out_mp4)

# ==============================================================================
# PART 15 OF 20: ACTIVE WORD-POP .ASS SUBTITLE GENERATOR
# Title: Hormozi-Style Karaoke Pop Captions in YouTube's UI Safe Zone
# Subtitle: Highlights each spoken word in bright yellow at MarginV=430 above Shorts UI
# ==============================================================================

# --- 15.1 Generate Word-by-Word Pop Events & Profanity Mute Intervals ---
def transcribe_segment_ass(clip_path, time_offset=0.0):
    """Builds 3-word karaoke pop caption events and returns explicit-word audio mute timestamps."""
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_path, word_timestamps=True)

    words = []
    bad_intervals = []
    for seg in segments:
        for w in seg.words:
            raw_upper = w.word.strip().upper()
            clean_alpha = "".join(c for c in raw_upper if c.isalpha())
            if clean_alpha in PROFANITY_WORDS:
                bad_intervals.append((max(0.0, w.start + time_offset - 0.05), w.end + time_offset + 0.05))
            words.append((w.start + time_offset, w.end + time_offset, censor_word_text(raw_upper)))

    ass_events = []
    full_text = []
    hi_color = CHANNEL_CONFIG["caption_highlight_hex"]

    for i in range(0, len(words), 3):
        chunk = words[i : i + 3]
        full_text.append(" ".join(w_str for _, _, w_str in chunk))
        for active_idx, (w_start, w_end, _) in enumerate(chunk):
            styled_tokens = []
            for tok_idx, (_, _, tok_txt) in enumerate(chunk):
                if tok_idx == active_idx:
                    styled_tokens.append(
                        f"{{\\c{hi_color}\\fscx114\\fscy114\\t(0,80,\\fscx100\\fscy100)}}{tok_txt}{{\\c&H00FFFFFF&}}"
                    )
                else:
                    styled_tokens.append(tok_txt)
            ass_events.append((w_start, max(w_start + 0.12, w_end), " ".join(styled_tokens)))

    return ass_events, " ".join(full_text), bad_intervals

# --- 15.2 Write Styled Advanced SubStation Alpha (.ass) File ---
def write_ass_file(all_events, out_path="captions.ass"):
    """Outputs a 1080x1920 .ass subtitle script positioned cleanly in YouTube's safe zone."""
    def fmt_ass(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,3,2,40,40,430,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [
        f"Dialogue: 0,{fmt_ass(s_t)},{fmt_ass(e_t)},Default,,0,0,0,,{txt}"
        for s_t, e_t, txt in all_events
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines))
    return out_path

# ==============================================================================
# PART 16 OF 20: TRANSFORMATIVE AI COMMENTARY WRITER & NEURAL TTS VOICEOVER
# Title: YouTube Monetization-Safe Storytelling Script & Voice Generator
# Subtitle: Writes original 3-beat play-by-play commentary and renders natural Edge-TTS audio
# ==============================================================================

# --- 16.1 Generate Viral Title, Hook Banner & Transformative Commentary ---
def generate_viral_metadata(transcript, mode, creators_list):
    """Generates click-worthy titles, banners, and original storytelling commentary for monetization."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    emoji_pool = ["💀", "🚨", "🤯", "🔥", "😭", "⚡", "👀", "💥", "🌴", "🚓"]
    e1, e2 = random.sample(emoji_pool, 2)
    fallback_title = (
        f"Top 3 Wildest GTA 6 Moments Today {e1}{e2} #gta6 #shorts"
        if mode == "MULTI"
        else f"Why Nobody Saw This Coming in GTA 6 {e1}{e2} #gta6 #shorts"
    )
    fallback_commentary = (
        "Watch closely what happens right here... Nobody expected that timing at the end!"
    )

    if not api_key:
        return fallback_title, "INSTANT REGRET 💀", fallback_commentary

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30000))
    prompt = f"""You are a transformative gaming commentator for a monetized English GTA 6 YouTube Shorts channel.
To satisfy YouTube's Reused Content monetization rules, write original storytelling commentary that adds context and humor rather than just re-uploading gameplay.

Clip Mode: {"Top 3 Ranked Countdown" if mode == "MULTI" else "Single Highlight Breakdown"}
Creators Featured: {", ".join(creators_list)}
Spoken Dialogue: {transcript[:500]}

Rules:
1. TITLE: Write a PUNCHY, SPECIFIC English title tied to the story (under 66 chars, NO profanity). Include 1-2 emojis ({e1}, {e2}) and "#gta6 #shorts".
2. BANNER: Write a specific 3-to-4 word ALL-CAPS curiosity hook.
3. COMMENTARY: Write a lively 22-to-30 word narrator script (intro hook + mid-clip play-by-play + punchline reaction) that explains what is happening and why it's wild.

Reply in this exact 3-line format ONLY:
TITLE: <your unique title>
BANNER: <3-4 word ALL CAPS specific hook>
COMMENTARY: <22-30 word original narrator script>"""

    try:
        resp = client.models.generate_content(
            model="gemini-3.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.95),
        )
        title = fallback_title
        banner = "INSTANT REGRET"
        commentary = fallback_commentary
        for line in resp.text.strip().splitlines():
            up = line.upper()
            val = line.split(":", 1)[-1].strip()
            if up.startswith("TITLE:") and val:
                title = val
            elif up.startswith("BANNER:") and val:
                banner = "".join(c for c in val.upper() if c.isalnum() or c in {" ", "!", "?"})[:24]
            elif up.startswith("COMMENTARY:") and val:
                commentary = val
        print(f"[AI Script] Title: {title} | Commentary: {commentary[:65]}...", flush=True)
        return title, banner, commentary
    except Exception as e:
        print(f"[AI Script] Gemini fallback triggered: {e}", flush=True)
        return fallback_title, "INSTANT REGRET", fallback_commentary

# --- 16.2 Synthesize Neural Narrator Voiceover ---
async def create_voiceover_audio(text, filename="narrator_vo.mp3"):
    """Renders high-energy English voiceover using Edge-TTS with automatic gTTS fallback."""
    for voice in ("en-US-GuyNeural", "en-US-ChristopherNeural"):
        try:
            comm = edge_tts.Communicate(text, voice, rate="+12%")
            await comm.save(filename)
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                return filename
        except Exception:
            await asyncio.sleep(1)
    tts = gTTS(text=text, lang="en", slow=False)
    tts.save(filename)
    return filename

# ==============================================================================
# PART 16B-1: TIMELINE ASSEMBLER & OPENCV FACECAM DETECTOR
# Title: Multi-Clip Crossfade Timeline & Streamer Facecam Coordinate Locator
# Subtitle: Mixes transformative AI commentary & detects corner facecams
# ==============================================================================

def get_media_duration(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    try:
        return float(res.stdout.strip())
    except Exception:
        return 15.0


def assemble_base_timeline(mode, clip_specs):
    segment_files = []
    segment_durations = []
    all_ass_events = []
    all_bad_intervals = []
    all_transcripts = []
    creators_used = []
    climax_markers = []
    credit_windows = []

    current_timeline_sec = (HOOK_DURATION - TRANSITION_DUR) if mode == "SINGLE" else 0.0

    for idx, (cand, start_sec, peak_sec, seg_dur) in enumerate(clip_specs):
        raw_seg_mp4 = f"raw_seg_{idx}.mp4"
        seg_mp4 = f"seg_{idx}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_sec), "-i", cand["video_path"],
            "-t", str(seg_dur), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", raw_seg_mp4
        ], check=True)

        apply_auto_jump_cuts(raw_seg_mp4, seg_mp4)
        actual_dur = get_media_duration(seg_mp4)

        ass_events, seg_txt, bad_ints = transcribe_segment_ass(seg_mp4, time_offset=current_timeline_sec)
        all_ass_events.extend(ass_events)
        all_bad_intervals.extend(bad_ints)
        all_transcripts.append(seg_txt)

        c_name = cand["channel_name"]
        if c_name not in creators_used:
            creators_used.append(c_name)

        seg_start_t = 0.0 if (mode == "SINGLE" and idx == 0) else current_timeline_sec
        seg_end_t = current_timeline_sec + actual_dur
        rank_label = f"#{len(clip_specs) - idx} — {c_name}" if mode == "MULTI" else c_name
        credit_windows.append((seg_start_t, seg_end_t, rank_label))

        rel_peak = current_timeline_sec + max(1.0, min(actual_dur - 1.5, actual_dur * 0.6))
        climax_markers.append(rel_peak)
        segment_files.append(seg_mp4)
        segment_durations.append(actual_dur)
        current_timeline_sec += (actual_dur - TRANSITION_DUR)

    ass_path = write_ass_file(all_ass_events, "captions.ass")
    viral_title, banner_hook, commentary_script = generate_viral_metadata(" ".join(all_transcripts), mode, creators_used)

    if mode == "SINGLE":
        cand0, _, peak0, _ = next(iter(clip_specs))
        hook_start = max(0, peak0 - 2.5)
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(hook_start), "-i", cand0["video_path"],
            "-t", str(HOOK_DURATION), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "hook_clip.mp4"
        ], check=True)
        concat_list = ["hook_clip.mp4"] + segment_files
        concat_durs = [float(HOOK_DURATION)] + segment_durations
    else:
        concat_list = segment_files
        concat_durs = segment_durations

    if len(concat_list) == 1:
        os.replace(next(iter(concat_list)), "combined_raw.mp4")
        total_dur = next(iter(concat_durs))
    else:
        ffmpeg_in = []
        for fn in concat_list:
            ffmpeg_in.extend(["-i", fn])

        v_filters, a_filters = [], []
        prev_v, prev_a = "[0:v]", "[0:a]"
        running_offset = 0.0
        chosen_transitions = random.sample(XFADE_TRANSITIONS, k=min(len(XFADE_TRANSITIONS), len(concat_list) - 1))

        for step_idx in range(1, len(concat_list)):
            running_offset += concat_durs[step_idx - 1] - TRANSITION_DUR
            t_style = chosen_transitions[step_idx - 1]
            out_v_pad = "[vout]" if step_idx == len(concat_list) - 1 else f"[v{step_idx}]"
            out_a_pad = "[aout]" if step_idx == len(concat_list) - 1 else f"[a{step_idx}]"

            v_filters.append(f"{prev_v}[{step_idx}:v]xfade=transition={t_style}:duration={TRANSITION_DUR}:offset={running_offset:.2f}{out_v_pad}")
            a_filters.append(f"{prev_a}[{step_idx}:a]acrossfade=d={TRANSITION_DUR}{out_a_pad}")
            prev_v, prev_a = out_v_pad, out_a_pad

        subprocess.run([
            "ffmpeg", "-y", *ffmpeg_in,
            "-filter_complex", ";".join(v_filters + a_filters),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", "-ac", "2", "combined_raw.mp4"
        ], check=True)
        total_dur = sum(concat_durs) - TRANSITION_DUR * (len(concat_list) - 1)

    if CHANNEL_CONFIG["enable_ai_voiceover"] and commentary_script:
        asyncio.run(create_voiceover_audio(commentary_script, "narrator_vo.mp3"))
        vo_dur = get_media_duration("narrator_vo.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-i", "combined_raw.mp4", "-i", "narrator_vo.mp3",
            "-filter_complex",
            f"[0:a]volume='if(lt(t,{vo_dur + 0.3:.2f}),0.14,0.95)':eval=frame[game_a];"
            "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.40[vo_a];"
            "[game_a][vo_a]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "combined.mp4"
        ], check=True)
    else:
        os.replace("combined_raw.mp4", "combined.mp4")

    return total_dur, ass_path, viral_title, banner_hook, climax_markers, credit_windows, all_bad_intervals


def detect_facecam_crop(video_path):
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

        best_box = None
        for frac in (0.2, 0.5, 0.8):
            if total_frames > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(total_frames * frac))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(45, 45))
            for (fx, fy, fw, fh) in faces:
                cx, cy = fx + fw / 2.0, fy + fh / 2.0
                if cx < w * 0.34 or cx > w * 0.66 or cy < h * 0.32 or cy > h * 0.68:
                    crop_w = int(min(w * 0.34, max(fw * 3.4, w * 0.24)))
                    crop_h = int(min(h * 0.42, crop_w * (680.0 / 1080.0)))
                    x0 = int(max(0, min(w - crop_w, cx - crop_w / 2.0)))
                    y0 = int(max(0, min(h - crop_h, cy - crop_h / 2.0)))
                    best_box = (crop_w, crop_h, x0, y0)
                    break
            if best_box:
                break
        cap.release()
        return best_box
    except Exception:
        return None

# ==============================================================================
# PART 16B-2: 9:16 VERTICAL SHORT COMPOSITOR & FX RENDERER
# Title: FFmpeg Splitcam/Crop/Blur Builder with Climax Shake, Flash & Captions
# Subtitle: Renders final 1080x1920 vertical Short with loudnorm & SFX
# ==============================================================================

def build_and_render_short(mode, framing_mode, clip_specs, out_video_name="final_short.mp4"):
    (
        total_dur, ass_path, viral_title, banner_hook,
        climax_markers, credit_windows, all_bad_intervals
    ) = assemble_base_timeline(mode, clip_specs)

    primary_climax = next(iter(climax_markers)) if climax_markers else 18.0
    inputs = ["-i", "combined.mp4"]
    has_boom_sfx = os.path.exists("assets/boom.mp3")

    climax_cond = "+".join([f"between(in_time,{cm-0.18:.2f},{cm+0.50:.2f})" for cm in climax_markers])
    shake_x = "+".join([f"if(between(in_time,{cm-0.18:.2f},{cm+0.50:.2f}),16*sin(in_time*80),0)" for cm in climax_markers])
    shake_y = "+".join([f"if(between(in_time,{cm-0.18:.2f},{cm+0.50:.2f}),16*cos(in_time*80),0)" for cm in climax_markers])
    flash_cond = "+".join([f"between(t,{cm-0.07:.2f},{cm+0.10:.2f})" for cm in climax_markers])
    zoom_expr = f"if({climax_cond},1.16,1.0+0.08*(mod(in_time,14)/14))"

    face_box = detect_facecam_crop("combined.mp4") if framing_mode == "splitcam" else None
    effective_framing = "splitcam" if face_box else ("crop" if framing_mode == "splitcam" else framing_mode)

    if effective_framing == "splitcam" and face_box:
        cw, ch, cx0, cy0 = face_box
        print(f"[Framing] Locked onto streamer facecam at ({cx0},{cy0}) {cw}x{ch}!", flush=True)
        video_base_filter = (
            "[0:v]fps=30,split=2[raw_cam][raw_game];"
            f"[raw_cam]crop={cw}:{ch}:{cx0}:{cy0},scale=1080:680:force_original_aspect_ratio=increase,crop=1080:680,setsar=1[cam_top];"
            "[raw_game]scale=-2:1240,crop=1080:1240:(iw-1080)/2:0,setsar=1[game_bot];"
            f"[cam_top][game_bot]vstack=inputs=2,"
            f"drawbox=x=0:y=676:w=1080:h=8:color=#FF00FF@0.95:t=fill,"
            f"eq=contrast=1.15:saturation=1.28:brightness='if({flash_cond},0.16,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)+{shake_x}':y='ih/2-(ih/zoom/2)+{shake_y}':d=1:s=1080x1920:fps=30[base_v];"
        )
        banner_y_box, banner_y_txt = 636, 658
    elif effective_framing == "crop":
        print("[Framing] Full-bleed vertical gameplay crop (no corner facecam).", flush=True)
        video_base_filter = (
            f"[0:v]fps=30,scale=-2:1920,crop=1080:1920:(iw-1080)/2:0,setsar=1,"
            f"eq=contrast=1.15:saturation=1.28:brightness='if({flash_cond},0.16,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)+{shake_x}':y='ih/2-(ih/zoom/2)+{shake_y}':d=1:s=1080x1920:fps=30[base_v];"
        )
        banner_y_box, banner_y_txt = 230, 252
    else:
        bg_filter = "[0:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:6,eq=brightness=-0.04:saturation=1.35[bg];"
        fg_filter = (
            f"[0:v]fps=30,scale=1080:720,setsar=1,"
            f"eq=contrast=1.15:saturation=1.28:brightness='if({flash_cond},0.16,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)+{shake_x}':y='ih/2-(ih/zoom/2)+{shake_y}':d=1:s=1080x720:fps=30[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base_v];"
        )
        video_base_filter = bg_filter + fg_filter
        banner_y_box, banner_y_txt = 505, 527

    credit_filters = []
    for win_idx, (w_start, w_end, c_name) in enumerate(credit_windows):
        safe_c = "".join(c for c in c_name if c.isalnum() or c in {"_", "-", " "}).strip()
        end_bound = total_dur + 1.0 if win_idx == len(credit_windows) - 1 else w_end
        credit_filters.append(
            f"drawtext=text='Credit\\: {safe_c}':fontcolor=white@0.92:fontsize=24:x=(w-text_w)/2:y=115:enable='between(t,{w_start:.2f},{end_bound:.2f})'"
        )
    dynamic_credits_chain = ",".join(credit_filters)

    safe_banner = banner_hook.replace("'", "").replace(":", "")
    handle = CHANNEL_CONFIG["channel_handle"]
    polish_filter = (
        f"[base_v]drawbox=x=160:y={banner_y_box}:w=760:h=84:color=#111111@0.92:t=fill:enable='between(t,0,3.8)',"
        f"drawtext=text='{safe_banner}':fontcolor=#FFFF00:fontsize=40:x=(w-text_w)/2:y={banner_y_txt}:enable='between(t,0,3.8)',"
        f"drawbox=x=0:y=ih-12:w=iw*(t/{total_dur}):h=12:color=#FF00FF@0.95:t=fill,"
        f"drawtext=text='{handle}':fontcolor=white@0.80:fontsize=26:x=(w-text_w)/2:y=80,"
        f"{dynamic_credits_chain},"
        f"ass={ass_path}[out_v]"
    )

    if all_bad_intervals:
        enable_expr = "+".join([f"between(t,{s:.2f},{e:.2f})" for s, e in all_bad_intervals])
        censor_chain = f"[0:a]aresample=async=1,loudnorm=I=-14:TP=-1.5:LRA=11,volume=0:enable='{enable_expr}'[norm_a];"
    else:
        censor_chain = "[0:a]aresample=async=1,loudnorm=I=-14:TP=-1.5:LRA=11[norm_a];"

    if has_boom_sfx:
        inputs.extend(["-i", "assets/boom.mp3"])
        delay_ms = int(primary_climax * 1000)
        audio_filter = (
            censor_chain +
            f"[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay_ms}|{delay_ms},volume=0.85[sfx_a];"
            f"[norm_a][sfx_a]amix=inputs=2:duration=first[out_a]"
        )
    else:
        audio_filter = censor_chain.replace("[norm_a];", "[out_a]")

    full_filter_complex = video_base_filter + polish_filter + ";" + audio_filter

    subprocess.run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", full_filter_complex,
        "-map", "[out_v]", "-map", "[out_a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        out_video_name
    ], check=True)

    return viral_title, banner_hook, primary_climax, effective_framing

# ==============================================================================
# PART 17 OF 20: WEEKLY SUNDAY 16:9 LONG-FORM COMPILATION BUILDER
# Title: Widescreen "Best of the Week" Watch-Hours Accelerator
# Subtitle: Stitches top weekly clips into 1920x1080 with AI intro & chapters
# ==============================================================================

def build_weekly_16x9_compilation(candidates, out_video_name, out_thumb_name):
    """
    Section 17.1: Top Weekly Clip Selection & 16:9 Segment Rendering
    - Ranks candidates by audio energy spike ratio and takes up to 8 moments
    - Renders 1920x1080 segments with ranked creator badges and channel handle
    """
    top_picks = sorted(candidates, key=lambda x: x["jump_ratio"], reverse=True)[:8]
    seg_files = []
    chapters = ["00:00 Intro & Weekly Highlights"]
    running_sec = 0.0
    handle = CHANNEL_CONFIG["channel_handle"]

    intro_text = (
        f"Welcome back to {handle}! Here are the wildest, funniest, and most chaotic "
        f"Grand Theft Auto 6 moments of the week. Let's jump straight into number {len(top_picks)}!"
    )
    asyncio.run(create_voiceover_audio(intro_text, "weekly_intro.mp3"))

    for i, c in enumerate(top_picks):
        rank_num = len(top_picks) - i
        c_name = "".join(ch for ch in c["channel_name"] if ch.isalnum() or ch in {"_", "-", " "}).strip()
        start_s = max(0, c["audio_peak"] - 22)
        pol_w = f"w_pol_{i}.mp4"

        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_s), "-i", c["video_path"],
            "-t", "42", "-r", "30",
            "-vf", (
                f"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,"
                f"drawbox=x=40:y=40:w=540:h=64:color=#111111@0.85:t=fill,"
                f"drawtext=text='#{rank_num} | Credit\\: {c_name}':fontcolor=#FFFF00:fontsize=30:x=62:y=56,"
                f"drawtext=text='{handle}':fontcolor=white@0.75:fontsize=24:x=w-text_w-45:y=h-55"
            ),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", pol_w
        ], check=True)

        dur_s = get_media_duration(pol_w)
        mins, secs = divmod(int(running_sec), 60)
        chapters.append(f"{mins:02d}:{secs:02d} #{rank_num} — {c_name}")
        running_sec += dur_s
        seg_files.append(pol_w)

    # --------------------------------------------------------------------------
    # Section 17.2: Concatenation, AI Narrator Intro Mix & 16:9 Thumbnail Render
    # --------------------------------------------------------------------------
    with open("weekly_list.txt", "w", encoding="utf-8") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "weekly_list.txt",
        "-c", "copy", "weekly_stitched.mp4"
    ], check=True)

    intro_dur = get_media_duration("weekly_intro.mp3")
    subprocess.run([
        "ffmpeg", "-y", "-i", "weekly_stitched.mp4", "-i", "weekly_intro.mp3",
        "-filter_complex",
        f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,volume='if(lt(t,{intro_dur + 0.3:.2f}),0.14,1.0)':eval=frame[bg_a];"
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.40[vo_a];"
        "[bg_a][vo_a]amix=inputs=2:duration=first[out_a]",
        "-map", "0:v", "-map", "[out_a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        out_video_name
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y", "-ss", "28", "-i", out_video_name,
        "-vframes", "1", "-update", "1",
        "-vf", (
            "eq=contrast=1.22:saturation=1.35,"
            "drawbox=x=120:y=430:w=1680:h=190:color=#111111@0.88:t=fill,"
            "drawtext=text='WILDEST GTA 6 MOMENTS THIS WEEK!':fontcolor=#FFFF00:bordercolor=black:borderw=8:fontsize=82:x=(w-text_w)/2:y=485"
        ),
        out_thumb_name
    ], check=True)

    week_str = datetime.now(timezone.utc).strftime("%b %d, %Y")
    title = f"Best GTA 6 Moments of the Week ({week_str}) 🔥💀"
    specs = [(c, max(0, c["audio_peak"] - 22), c["audio_peak"], 42) for c in top_picks]
    return title, chapters, specs

# ==============================================================================
# PART 18 OF 20: THUMBNAIL GENERATOR & AUTO-ENGAGEMENT COMMENTER
# Title: High-CTR Frame Extractor & Algorithmic Comment Velocity Booster
# Subtitle: Renders bold yellow hook thumbnails & posts polarizing top comments
# ==============================================================================

def create_short_thumbnail(video_path, peak_rel_sec, banner_text, out_thumb="thumb.jpg"):
    """
    Section 18.1: High-Contrast Climax Frame Thumbnail Generator
    - Captures the climax frame and overlays a bold yellow curiosity hook
    """
    safe_txt = banner_text.replace("'", "").replace(":", "")[:24] or "NO WAY!"
    cmd = [
        "ffmpeg", "-y", "-ss", f"{max(0.5, peak_rel_sec):.2f}", "-i", video_path,
        "-vframes", "1", "-update", "1",
        "-vf", (
            "eq=contrast=1.22:saturation=1.35,"
            f"drawtext=text='{safe_txt}':fontcolor=#FFFF00:bordercolor=black:borderw=9:"
            "fontsize=84:x=(w-text_w)/2:y=650"
        ),
        out_thumb
    ]
    subprocess.run(cmd, check=True)
    return out_thumb


def post_engagement_comment(youtube, video_id, title):
    """
    Section 18.2: Auto-Posted Engagement Question Comment
    - Uses Gemini to craft a polarizing question tied to the video title
    - Posts immediately after upload to boost early comment engagement
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    comment_text = "Rate this moment from 1 to 10 👇 Who should we feature next? 🔥"
    if api_key:
        try:
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=15000))
            resp = client.models.generate_content(
                model="gemini-3.0-flash",
                contents=(
                    f"Write 1 short, fun, engaging pinned-comment question (under 14 words + 1-2 emojis) "
                    f"for a GTA 6 YouTube video titled '{title}'. Output ONLY the comment text."
                ),
                config=types.GenerateContentConfig(temperature=0.9),
            )
            lines = [ln.strip().strip('"') for ln in resp.text.strip().splitlines() if ln.strip()]
            if lines and 6 <= len(lines[0]) <= 120:
                comment_text = lines[0]
        except Exception:
            pass

    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {"textOriginal": comment_text}
                    }
                }
            }
        ).execute()
        print(f"[Engagement] Auto-posted top comment on {video_id}: {comment_text}", flush=True)
    except Exception as e:
        print(f"[Engagement] Comment skipped: {e}", flush=True)

# ==============================================================================
# PART 19 OF 20: YOUTUBE SCHEDULED & COMPLIANCE UPLOADER
# Title: Automated YouTube Data API v3 Publisher with Synthetic Media Disclosure
# Subtitle: Attaches credits, chapters, custom thumbnails, & schedules exact slots
# ==============================================================================

def upload_to_youtube(video_path, thumb_path, title, clip_specs, publish_at_iso=None, extra_chapters=None):
    """
    Section 19.1: OAuth Token Refresh & Metadata Assembly
    - Refreshes YouTube OAuth credentials and formats creator attribution links
    - Sets containsSyntheticMedia=True automatically for monetization compliance
    """
    creds_data = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(creds_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    credit_lines = []
    creator_tags = []
    seen = set()
    for clip_num, (cand, _, _, _) in enumerate(clip_specs, start=1):
        c_name = cand["channel_name"]
        prefix = f"Highlight #{clip_num} — " if len(clip_specs) > 1 else ""
        credit_lines.append(f"🎮 {prefix}{c_name}: {cand['live_url']} ({cand['stream_title']})")
        if c_name not in seen:
            seen.add(c_name)
            creator_tags.append(c_name)

    privacy = CHANNEL_CONFIG["youtube_privacy"]
    handle = CHANNEL_CONFIG["channel_handle"]
    status_obj = {
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": bool(CHANNEL_CONFIG["enable_ai_voiceover"]),
    }
    if privacy == "public" and publish_at_iso:
        status_obj["privacyStatus"] = "private"
        status_obj["publishAt"] = publish_at_iso
    else:
        status_obj["privacyStatus"] = privacy

    chapters_block = ("\nChapters:\n" + "\n".join(extra_chapters) + "\n") if extra_chapters else ""
    hashtags = (
        "#GTA6 #GrandTheftAuto6 #GrandTheftAutoVI #ViceCity #Leonida #GTA6Gameplay #ViceCityClips"
        if extra_chapters
        else "#GTA6 #GrandTheftAuto6 #GrandTheftAutoVI #ViceCity #Leonida #GTA6Shorts #ViceCityClips #GamingShorts #Shorts"
    )

    # --------------------------------------------------------------------------
    # Section 19.2: Resumable Video Upload, Thumbnail Attachment & Top Comment
    # --------------------------------------------------------------------------
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:95],
            "description": (
                f"🔥 Transformative GTA 6 Highlights & Commentary Breakdown!\n"
                f"{chapters_block}\n"
                f"Original Stream Credits:\n" + "\n".join(credit_lines) + "\n\n"
                f"Edited with original voiceover commentary, pacing cuts, and captions by {handle}. "
                f"Subscribe for daily GTA 6 breakdowns! 🌴🚗💨\n\n"
                f"{hashtags}"
            ),
            "tags": [
                "GTA 6", "Grand Theft Auto 6", "Grand Theft Auto VI", "GTA VI",
                "Vice City", "Leonida", "GTA 6 Gameplay", "GTA 6 Commentary", "GTA 6 Clips",
                "Vice City Clips", "GTA 6 Shorts", "Gaming Shorts"
            ] + creator_tags,
            "categoryId": "20",
        },
        "status": status_obj,
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response["id"]
    sched_msg = (
        f"SCHEDULED for {publish_at_iso}"
        if publish_at_iso and privacy == "public"
        else f"uploaded as {privacy.upper()}"
    )
    print(f"Success! Video ID {video_id} ({sched_msg}) on {handle}!", flush=True)

    if os.path.exists(thumb_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg")
            ).execute()
            print(f"Custom thumbnail ({thumb_path}) attached!", flush=True)
        except Exception as e:
            print(f"Thumbnail upload skipped: {e}", flush=True)

    post_engagement_comment(youtube, video_id, title)
    return video_id

# ==============================================================================
# PART 20 OF 20: MULTI-SOURCE ORCHESTRATOR & MAIN PIPELINE ENTRY POINT
# Title: End-to-End Autonomous Execution Engine (Scout, Weekly 16:9 & Daily Shorts)
# Subtitle: Coordinates Twitch, Kick, & YouTube harvesting, analytics, & publishing
# ==============================================================================

def gather_all_sources(history_data, min_sources=4):
    """
    Section 20.1: Parallel Multi-Platform Source Harvester
    - Fetches live GTA 6 streams from Twitch & Kick, records concurrently,
      and tops up with YouTube creator videos if live volume is low
    """
    twitch_streams = fetch_twitch_directory_streams(limit=25)
    kick_streams = fetch_kick_gta_streams(limit=12)

    eligible_live = []
    for item in (twitch_streams + kick_streams):
        if can_clip_stream(item["stream_id"], history_data):
            eligible_live.append(item)
        if len(eligible_live) >= MAX_STREAMS_TO_RECORD:
            break

    recorded_items = []
    if eligible_live:
        with ThreadPoolExecutor(max_workers=MAX_STREAMS_TO_RECORD) as executor:
            live_results = list(executor.map(record_single_live_stream, enumerate(eligible_live)))
        recorded_items.extend([r for r in live_results if r is not None])

    if len(recorded_items) < min_sources:
        yt_items = harvest_youtube_gta_videos(history_data, max_videos=min_sources - len(recorded_items))
        recorded_items.extend(yt_items)

    return recorded_items


if __name__ == "__main__":
    # --------------------------------------------------------------------------
    # Section 20.2: Startup Initialization & YouTube Analytics Self-Learning Sync
    # --------------------------------------------------------------------------
    handle = CHANNEL_CONFIG["channel_handle"]
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    print(f"{handle} | Mode: {RUN_MODE.upper()} | Framing: {CHANNEL_CONFIG['default_framing'].upper()}", flush=True)

    history_data = load_history()
    analytics_hint = sync_youtube_analytics(history_data)

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    os.makedirs(CANDIDATE_DIR, exist_ok=True)

    # --------------------------------------------------------------------------
    # Section 20.3: Weekly Sunday 16:9 Long-Form Compilation Branch
    # --------------------------------------------------------------------------
    is_sunday_weekly = (
        RUN_MODE == "weekly_recap"
        or (
            RUN_MODE == "auto"
            and now_utc.weekday() == 6
            and now_utc.hour == 12
            and history_data.get("last_weekly_recap_date") != today_str
        )
    )

    if is_sunday_weekly:
        print("=== Running Weekly 16:9 Long-Form 'Best of the Week' Compilation ===", flush=True)
        recorded_items = gather_all_sources(history_data, min_sources=8)
        candidates = extract_audio_candidates(recorded_items)
        if len(candidates) < 3:
            print("[Weekly Recap] Not enough candidates gathered this hour; will retry next hour.", flush=True)
            exit(0)

        out_weekly_mp4 = os.path.join(ARTIFACTS_DIR, "weekly_compilation_16x9.mp4")
        out_weekly_jpg = os.path.join(ARTIFACTS_DIR, "weekly_thumb_16x9.jpg")
        w_title, w_chapters, w_specs = build_weekly_16x9_compilation(candidates, out_weekly_mp4, out_weekly_jpg)

        if CHANNEL_CONFIG["upload_to_youtube"]:
            vid_id = upload_to_youtube(
                out_weekly_mp4, out_weekly_jpg, w_title, w_specs,
                publish_at_iso=None, extra_chapters=w_chapters
            )
            record_scheduled_upload(w_specs, None, video_id=vid_id, title=w_title, mode="WEEKLY_16X9", framing="16x9")
            h_latest = load_history()
            h_latest["last_weekly_recap_date"] = today_str
            save_history(h_latest)
        exit(0)

    # --------------------------------------------------------------------------
    # Section 20.4: Daily 9:16 Shorts Slot Check, AI Quality Gate & Publishing
    # --------------------------------------------------------------------------
    if CHANNEL_CONFIG["upload_to_youtube"] and CHANNEL_CONFIG["youtube_privacy"] == "public":
        open_slots = get_available_publish_slots(history_data, max_needed=3)
        if not open_slots:
            print("All daily slots for the next 24 hours are already scheduled! Exiting cleanly.", flush=True)
            exit(0)
    else:
        open_slots = [None, None, None]

    recorded_items = gather_all_sources(history_data, min_sources=4)
    if not recorded_items:
        print("[Scout] No Twitch, Kick, or YouTube recordings succeeded this run.", flush=True)
        exit(0)

    candidates = extract_audio_candidates(recorded_items)
    if not candidates:
        print("[Scout] No usable audio segments extracted from recordings.", flush=True)
        exit(0)

    planned_shorts = plan_shorts_from_candidates(candidates, max_shorts=len(open_slots), analytics_hint=analytics_hint)
    if not planned_shorts:
        print("[Producer] No clips passed the quality gate this hour.", flush=True)
        exit(0)

    for idx, (mode, framing_mode, clip_specs) in enumerate(planned_shorts):
        slot_iso = open_slots[idx] if idx < len(open_slots) else None
        out_mp4 = os.path.join(ARTIFACTS_DIR, f"short_{idx+1}.mp4")
        out_jpg = os.path.join(ARTIFACTS_DIR, f"thumb_{idx+1}.jpg")

        print(f"\n=== Rendering Short #{idx+1}/{len(planned_shorts)} ({mode} | {framing_mode.upper()}) ===", flush=True)
        viral_title, banner_hook, primary_climax, eff_frame = build_and_render_short(
            mode, framing_mode, clip_specs, out_video_name=out_mp4
        )
        create_short_thumbnail(out_mp4, primary_climax, banner_hook, out_thumb=out_jpg)

        if RUN_MODE == "scout":
            shutil.copy2(out_mp4, os.path.join(CANDIDATE_DIR, f"cand_{idx+1}.mp4"))
            print(f"[Scout] Saved candidate clip to {CANDIDATE_DIR}/cand_{idx+1}.mp4", flush=True)
        elif CHANNEL_CONFIG["upload_to_youtube"]:
            vid_id = upload_to_youtube(out_mp4, out_jpg, viral_title, clip_specs, publish_at_iso=slot_iso)
            record_scheduled_upload(clip_specs, slot_iso, video_id=vid_id, title=viral_title, mode=mode, framing=eff_frame)
        else:
            print(f"[Test Mode] Saved {out_mp4} and {out_jpg}.", flush=True)
