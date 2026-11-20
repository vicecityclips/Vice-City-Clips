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

import requests
import numpy as np
from scipy.io import wavfile
from faster_whisper import WhisperModel
import edge_tts
from gtts import gTTS
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==============================================================================
# CHANNELFORGE USER CONFIGURATION (Maps 1:1 to Web Dashboard Toggles)
# ==============================================================================
CHANNEL_CONFIG = {
    "channel_handle": "@Vice-City_ClipsVault",
    "upload_to_youtube": True,          # True = schedule/upload | False = artifacts only
    "youtube_privacy": "public",        # "public" (scheduled slots) or "private" (test)
    "max_uploads_per_day": 4,           # Daily Shorts cap (00:00, 06:00, 12:00, 18:00 UTC)
    "default_framing": "splitcam",      # "splitcam" (top cam + bottom game), "crop", or "blur"
    "enable_jump_cuts": True,           # Auto-trim dead-air pauses > 0.70s
    "enable_ai_voiceover": True,        # 2.2s spoken hook with streamer audio ducked
    "min_quality_score": 7,             # Reject low-action / menu-browsing clips (< 7/10)
    "caption_highlight_hex": "&H0000FFFF&",  # Bright Yellow active-word pop (ASS BGR format)
}

RUN_MODE = os.environ.get("RUN_MODE", "auto").strip().lower()
MAX_STREAMS_TO_RECORD = 6
CHUNK_SECONDS = 480

def make_url(host, path="", query=""):
    return urllib.parse.urlunparse(("https", host, path, "", query, ""))

TWITCH_GQL_URL = make_url("gql.twitch.tv", "/gql")
TWITCH_ORIGIN_URL = make_url("www.twitch.tv")
TWITCH_REFERER_URL = make_url("www.twitch.tv", "/")
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

SINGLE_CLIP_DUR = 38
MULTI_CLIP_DUR = 15
HOOK_DURATION = 2.2
TRANSITION_DUR = 0.35

HISTORY_FILE = "history.json"
CANDIDATE_DIR = "candidate_pool"
ARTIFACTS_DIR = "artifacts"
MAX_CLIPS_PER_STREAM = 2
MIN_GAP_SECONDS = 7200

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

XFADE_TRANSITIONS = [
    "smoothleft", "wipeleft", "slidedown", "slideup",
    "circleopen", "radial", "pixelize", "fadeblack", "distance"
]

PROFANITY_WORDS = {
    "FUCK", "FUCKING", "FUCKED", "FUCKS", "MOTHERFUCKER", "MOTHERFUCKING",
    "SHIT", "SHITTING", "BULLSHIT", "BITCH", "BITCHES", "BITCHING",
    "ASSHOLE", "DICK", "DICKS", "PUSSY", "CUNT", "BASTARD", "WHORE", "SLUT",
    "NIGGA", "NIGGER", "FAGGOT", "RETARD", "RETARDED", "COCK", "TWAT"
}

def censor_word_text(word_upper):
    clean = "".join(c for c in word_upper if c.isalpha())
    if clean in PROFANITY_WORDS and len(clean) >= 2:
        masked = clean[:1] + "*" * (len(clean) - 1)
        return word_upper.replace(clean, masked)
    return word_upper

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "streams" not in data:
                    return {"streams": data, "scheduled_slots": []}
                return data
        except Exception:
            return {"streams": {}, "scheduled_slots": []}
    return {"streams": {}, "scheduled_slots": []}

def save_history(history_data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2)

def can_clip_stream(stream_key, history_data):
    streams_map = history_data.get("streams", {})
    entry = streams_map.get(stream_key)
    if not entry:
        return True
    if entry.get("count", 0) >= MAX_CLIPS_PER_STREAM:
        return False
    elapsed = int(time.time() - entry.get("last_clipped_at", 0))
    return elapsed >= MIN_GAP_SECONDS

def get_available_publish_slots(history_data, max_needed=3):
    now_utc = datetime.now(timezone.utc)
    booked = set(history_data.get("scheduled_slots", []))

    active_booked = [s for s in booked if s > now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")]
    history_data["scheduled_slots"] = active_booked
    booked = set(active_booked)

    cutoff_24h = (now_utc + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    booked_next_24h = [s for s in booked if s <= cutoff_24h]
    max_daily = CHANNEL_CONFIG["max_uploads_per_day"]
    remaining_daily_quota = max(0, max_daily - len(booked_next_24h))

    if remaining_daily_quota == 0:
        print(f"Daily cap reached ({max_daily}/{max_daily} slots booked in next 24h).", flush=True)
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

def record_scheduled_upload(stream_specs, slot_iso):
    history_data = load_history()
    streams_map = history_data.setdefault("streams", {})
    for cand, _, peak_sec, _ in stream_specs:
        key = cand.get("stream_id") or cand["live_url"]
        entry = streams_map.get(key, {
            "title": cand["stream_title"],
            "channel": cand["channel_name"],
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
    save_history(history_data)

def check_creator_live_via_streamlink(login):
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

def fetch_twitch_directory_streams(limit=25):
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
                    # Strict filter: ignore generic GTA V RP unless explicitly GTA 6 / Vice City
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

def fetch_kick_gta_streams(limit=12):
    try:
        from curl_cffi import requests as cffi_requests
        fetcher = lambda u: cffi_requests.get(u, impersonate="chrome124", timeout=15)
    except Exception:
        fetcher = lambda u: requests.get(
            u,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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

def record_single_live_stream(idx_and_item):
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

def harvest_youtube_gta_videos(history_data, max_videos=4):
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

def extract_audio_candidates(recorded_items):
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


def plan_shorts_from_candidates(candidates, max_shorts=3):
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
Evaluate these {len(candidates)} recordings and build up to {max_shorts} SEPARATE viral Shorts.
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
                model="gemini-2.5-flash",
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

                score_dig = "".join(ch for ch in parts[0] if ch.isdigit())
                score = int(score_dig) if score_dig else 5
                if score < min_score:
                    print(f"[Quality Gate] Rejected low-action clip (Score {score}/10 < {min_score}/10).", flush=True)
                    continue

                m_raw, f_raw, picks_raw = parts[1], parts[2].lower(), parts[3]
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

def apply_auto_jump_cuts(raw_mp4, out_mp4):
    """Trims awkward dead-air pauses > 0.70s and adds subtle zoom punch-ins on cuts."""
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

    merged = [raw_windows[0]]
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

def transcribe_segment_ass(clip_path, time_offset=0.0):
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
                    styled_tokens.append(f"{{\\c{hi_color}\\fscx114\\fscy114\\t(0,80,\\fscx100\\fscy100)}}{tok_txt}{{\\c&H00FFFFFF&}}")
                else:
                    styled_tokens.append(tok_txt)
            ass_events.append((w_start, max(w_start + 0.12, w_end), " ".join(styled_tokens)))

    return ass_events, " ".join(full_text), bad_intervals

def write_ass_file(all_events, out_path="captions.ass"):
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
Style: Default,DejaVu Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,3,2,40,40,360,1

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

def generate_viral_metadata(transcript, mode, creators_list):
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
            model="gemini-2.5-flash",
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
        print(f"Generated Title: {title} | Commentary: {commentary[:65]}...", flush=True)
        return title, banner, commentary
    except Exception as e:
        print(f"Gemini metadata error: {e}", flush=True)
        return fallback_title, "INSTANT REGRET", fallback_commentary

async def create_voiceover_audio(text, filename="hook_vo.mp3"):
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

async def create_voiceover_audio(text, filename="hook_vo.mp3"):
    for voice in ("en-US-GuyNeural", "en-US-ChristopherNeural"):
        try:
            comm = edge_tts.Communicate(text, voice, rate="+14%")
            await comm.save(filename)
            if os.path.exists(filename) and os.path.getsize(filename) > 0:
                return filename
        except Exception:
            await asyncio.sleep(1)
    tts = gTTS(text=text, lang="en", slow=False)
    tts.save(filename)
    return filename

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

    # Mix transformative AI narrator commentary across the timeline with auto-ducking
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

    if framing_mode == "splitcam":
        # Top 35% zoomed-in streamer facecam (1080x680) + Bottom 65% full-bleed gameplay (1080x1240)
        video_base_filter = (
            "[0:v]fps=30,split=2[raw_cam][raw_game];"
            "[raw_cam]crop=iw*0.26:ih*0.32:iw*0.72:ih*0.64,scale=1080:680:force_original_aspect_ratio=increase,crop=1080:680,setsar=1[cam_top];"
            "[raw_game]scale=-2:1240,crop=1080:1240:(iw-1080)/2:0,setsar=1[game_bot];"
            f"[cam_top][game_bot]vstack=inputs=2,"
            f"drawbox=x=0:y=676:w=1080:h=8:color=#FF00FF@0.95:t=fill,"
            f"eq=contrast=1.15:saturation=1.28:brightness='if({flash_cond},0.16,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)+{shake_x}':y='ih/2-(ih/zoom/2)+{shake_y}':d=1:s=1080x1920:fps=30[base_v];"
        )
        next_idx = 1
    elif framing_mode == "crop":
        video_base_filter = (
            f"[0:v]fps=30,scale=-2:1920,crop=1080:1920:(iw-1080)/2:0,setsar=1,"
            f"eq=contrast=1.15:saturation=1.28:brightness='if({flash_cond},0.16,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)+{shake_x}':y='ih/2-(ih/zoom/2)+{shake_y}':d=1:s=1080x1920:fps=30[base_v];"
        )
        next_idx = 1
    else:
        bg_filter = "[0:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:6,eq=brightness=-0.04:saturation=1.35[bg];"
        fg_filter = (
            f"[0:v]fps=30,scale=1080:720,setsar=1,"
            f"eq=contrast=1.15:saturation=1.28:brightness='if({flash_cond},0.16,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)+{shake_x}':y='ih/2-(ih/zoom/2)+{shake_y}':d=1:s=1080x720:fps=30[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base_v];"
        )
        video_base_filter = bg_filter + fg_filter
        next_idx = 1

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
        f"[base_v]drawbox=x=160:y=636:w=760:h=84:color=#111111@0.92:t=fill:enable='between(t,0,3.8)',"
        f"drawtext=text='{safe_banner}':fontcolor=#FFFF00:fontsize=40:x=(w-text_w)/2:y=658:enable='between(t,0,3.8)',"
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
            f"[{next_idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay_ms}|{delay_ms},volume=0.85[sfx_a];"
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

    return viral_title, banner_hook, primary_climax

def create_short_thumbnail(video_path, peak_rel_sec, banner_text, out_thumb="thumb.jpg"):
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

def create_short_thumbnail(video_path, peak_rel_sec, banner_text, out_thumb="thumb.jpg"):
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

def upload_to_youtube(video_path, thumb_path, title, clip_specs, publish_at_iso=None):
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

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:95],
            "description": (
                f"🔥 Transformative GTA 6 Highlights & Commentary Breakdown!\n\n"
                f"Original Stream Credits:\n" + "\n".join(credit_lines) + "\n\n"
                f"Edited with original voiceover commentary, pacing cuts, and captions by {handle}. Subscribe for daily GTA 6 breakdowns! 🌴🚗💨\n\n"
                "#GTA6 #GrandTheftAuto6 #GrandTheftAutoVI #ViceCity #Leonida #GTA6Shorts #ViceCityClips #GamingShorts #Shorts"
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
    sched_msg = f"SCHEDULED for {publish_at_iso}" if publish_at_iso and privacy == "public" else f"uploaded as {privacy.upper()}"
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

def gather_all_sources(history_data):
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

    if len(recorded_items) < 4:
        yt_items = harvest_youtube_gta_videos(history_data, max_videos=4 - len(recorded_items))
        recorded_items.extend(yt_items)

    return recorded_items

if __name__ == "__main__":
    handle = CHANNEL_CONFIG["channel_handle"]
    print(f"{handle} | Mode: {RUN_MODE.upper()} | Framing: {CHANNEL_CONFIG['default_framing'].upper()}", flush=True)
    history_data = load_history()

    if CHANNEL_CONFIG["upload_to_youtube"] and CHANNEL_CONFIG["youtube_privacy"] == "public":
        open_slots = get_available_publish_slots(history_data, max_needed=3)
        if not open_slots:
            print("All daily slots for the next 24 hours are already scheduled! Exiting cleanly.", flush=True)
            exit(0)
    else:
        open_slots = [None, None, None]

    recorded_items = gather_all_sources(history_data)
    if not recorded_items:
        print("[Scout] No Twitch, Kick, or YouTube recordings succeeded this run.", flush=True)
        exit(0)

    candidates = extract_audio_candidates(recorded_items)
    if not candidates:
        print("[Scout] No usable audio segments extracted from recordings.", flush=True)
        exit(0)

    planned_shorts = plan_shorts_from_candidates(candidates, max_shorts=len(open_slots))
    if not planned_shorts:
        print("[Producer] No clips passed the quality gate this hour.", flush=True)
        exit(0)

    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    os.makedirs(CANDIDATE_DIR, exist_ok=True)

    for idx, (mode, framing_mode, clip_specs) in enumerate(planned_shorts):
        slot_iso = open_slots[idx] if idx < len(open_slots) else None
        out_mp4 = os.path.join(ARTIFACTS_DIR, f"short_{idx+1}.mp4")
        out_jpg = os.path.join(ARTIFACTS_DIR, f"thumb_{idx+1}.jpg")

        print(f"\n=== Rendering Short #{idx+1}/{len(planned_shorts)} ({mode} | {framing_mode.upper()}) ===", flush=True)
        viral_title, banner_hook, primary_climax = build_and_render_short(mode, framing_mode, clip_specs, out_video_name=out_mp4)
        create_short_thumbnail(out_mp4, primary_climax, banner_hook, out_thumb=out_jpg)

        if RUN_MODE == "scout":
            shutil.copy2(out_mp4, os.path.join(CANDIDATE_DIR, f"cand_{idx+1}.mp4"))
            print(f"[Scout] Saved candidate clip to {CANDIDATE_DIR}/cand_{idx+1}.mp4", flush=True)
        elif CHANNEL_CONFIG["upload_to_youtube"]:
            upload_to_youtube(out_mp4, out_jpg, viral_title, clip_specs, publish_at_iso=slot_iso)
            record_scheduled_upload(clip_specs, slot_iso)
        else:
            print(f"[Test Mode] Saved {out_mp4} and {out_jpg}.", flush=True)
