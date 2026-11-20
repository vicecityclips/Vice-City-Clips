# ==============================================================================
# PART 1 OF 12: IMPORTS, CONTROL TOGGLES, CHANNEL SETTINGS & DISCOVERY LISTS
# ==============================================================================
import os
import io
import re
import json
import time
import shutil
import random
import zipfile
import subprocess
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import requests
from scipy.io import wavfile
from faster_whisper import WhisperModel
from google import genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# --- EASY CONTROL TOGGLES ---
UPLOAD_TO_YOUTUBE = True        # True = upload/schedule to YouTube | False = local artifact test
YOUTUBE_PRIVACY = "public"      # "public" (uses exact scheduled slots) or "private"
MAX_UPLOADS_PER_DAY = 4         # Strict cap: maximum 4 Shorts scheduled/uploaded per day
MAX_STREAMS_TO_RECORD = 6       # Simultaneous Twitch streams per hourly scout
CHUNK_SECONDS = 360             # 6 mins/hr in scout mode (5 hourly scouts = 30 mins/stream per slot)

# --- CHANNEL & CLIP SETTINGS ---
CHANNEL_HANDLE = "@Vice-City_ClipsVault"
SINGLE_CLIP_DUR = 45
MULTI_CLIP_DUR = 19
TRANSITION_DUR = 0.4
MIN_AI_WATCH_SCORE = 6.8        # Rejects weak/boring clips below 6.8/10
HISTORY_FILE = "history.json"
SCHEDULE_HOURS_UTC = list((0, 6, 12, 18))  # 5:30 AM, 11:30 AM, 5:30 PM, 11:30 PM IST

# --- PRIORITY TWITCH CREATORS ---
PRIORITY_CREATORS = [
    "xqc", "kaicenat", "timthetatman", "nickmercs", "tfue",
    "summit1g", "shroud", "zerkaa", "joshzerker", "lirik",
    "sykkuno", "valkyrae", "hasanabi", "jynxzi", "caseoh_"
]

# --- YOUTUBE SEARCH QUERIES (GLOBAL + INDIAN GTA CREATORS) ---
YOUTUBE_SEARCH_QUERIES = [
    "GTA 6 live stream gameplay",
    "GTA 6 funny moments rage",
    "GTA 6 India live gameplay",
    "GTA 6 Techno Gamerz Mortal Scout",
    "GTA 6 Vice City crazy moments",
    "GTA 5 RP India funny moments live",
]

# ==============================================================================
# PART 2 OF 12: HISTORY TRACKER & SINGLE-SLOT EXACT-HOUR SCHEDULER
# ==============================================================================
def load_history():
    """Loads previously clipped stream/video IDs and booked UTC schedule slots."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("uploaded_ids", [])
                data.setdefault("scheduled_slots", [])
                return data
        except Exception as e:
            print(f"[Warn] Could not parse {HISTORY_FILE}, starting fresh: {e}", flush=True)
    return {"uploaded_ids": [], "scheduled_slots": []}


def save_history(hist):
    """Persists deduplication IDs and upcoming booked slots to history.json."""
    hist["uploaded_ids"] = list(dict.fromkeys(hist.get("uploaded_ids", [])))[-350:]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    hist["scheduled_slots"] = sorted(
        list(set(s for s in hist.get("scheduled_slots", []) if str(s) >= cutoff))
    )[-100:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)


def get_next_available_slot(hist):
    """Finds strictly the single next open 6-hour UTC slot (00, 06, 12, 18 UTC)."""
    now_utc = datetime.now(timezone.utc)
    booked_slots = set(hist.get("scheduled_slots", []))

    for day_offset in range(0, 4):
        base_day = (now_utc + timedelta(days=day_offset)).replace(
            minute=0, second=0, microsecond=0
        )
        day_prefix = base_day.strftime("%Y-%m-%d")
        day_count = sum(1 for s in booked_slots if str(s).startswith(day_prefix))
        if day_count >= MAX_UPLOADS_PER_DAY:
            continue

        for hr in SCHEDULE_HOURS_UTC:
            candidate_dt = base_day.replace(hour=hr)
            if candidate_dt <= now_utc + timedelta(minutes=10):
                continue
            slot_iso = candidate_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if slot_iso not in booked_slots:
                return slot_iso
    return None

# ==============================================================================
# PART 3 OF 12: TWITCH STREAM DISCOVERY & PARALLEL STREAMLINK RECORDER
# ==============================================================================
TWITCH_GQL_URL = "https://twitch.tv"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


def check_creator_live_via_streamlink(login):
    """Fallback live check using streamlink --json when Twitch GQL blocks datacenter IPs."""
    url = f"https://twitch.tv{login}"
    try:
        res = subprocess.run(
            ["streamlink", "--json", url],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if res.returncode == 0 and res.stdout.strip().startswith("{"):
            info = json.loads(res.stdout)
            if info.get("streams"):
                meta = info.get("metadata") or {}
                game = str(meta.get("category") or "Grand Theft Auto VI")
                title = str(meta.get("title") or f"{login} Live Stream")
                sid = str(meta.get("id") or int(time.time() // 7200))
                return {
                    "stream_id": f"twitch_{login}_{sid}",
                    "login": login,
                    "creator": str(meta.get("author") or login),
                    "title": title,
                    "viewers": 5000,
                    "game": game,
                    "url": url,
                }
    except Exception:
        pass
    return None


def fetch_twitch_directory_streams(game_name, limit=25):
    """Queries Twitch directory for live streams with browser headers and streamlink fallback."""
    query = """
    query($gameName: String!, $limit: Int!) {
      game(name: $gameName) {
        streams(first: $limit, options: {sort: VIEWER_COUNT}) {
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
        "Origin": "https://twitch.tv",
        "Referer": "https://twitch.tv",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    payload = {
        "query": query,
        "variables": {"gameName": game_name, "limit": limit},
    }
    results = []
    try:
        resp = requests.post(TWITCH_GQL_URL, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            data = resp.json()
            edges = (((data.get("data") or {}).get("game") or {}).get("streams") or {}).get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                bcast = node.get("broadcaster") or {}
                login = str(bcast.get("login") or "").strip().lower()
                display = str(bcast.get("displayName") or login).strip()
                stream_id = str(node.get("id") or "").strip()
                title = str(node.get("title") or "").strip()
                viewers = int(node.get("viewersCount") or 0)
                if not login or not stream_id:
                    continue
                results.append({
                    "stream_id": f"twitch_{login}_{stream_id}",
                    "login": login,
                    "creator": display,
                    "title": title,
                    "viewers": viewers,
                    "game": game_name,
                    "url": f"https://twitch.tv{login}",
                })
    except Exception as e:
        print(f"[Warn] Twitch GQL fallback triggered ({e})", flush=True)

    if not results:
        shuffled_creators = list(PRIORITY_CREATORS)
        random.shuffle(shuffled_creators)
        for login in shuffled_creators[:12]:
            live_item = check_creator_live_via_streamlink(login.lower())
            if live_item and ("grand theft auto" in live_item["game"].lower() or "gta" in live_item["game"].lower()):
                results.append(live_item)
                if len(results) >= 6:
                    break
    return results


def discover_top_live_streams(hist, max_count=MAX_STREAMS_TO_RECORD):
    """Selects top unclipped Twitch GTA 6 / GTA 5 live streams prioritizing PRIORITY_CREATORS."""
    seen_ids = set(hist.get("uploaded_ids", []))
    raw_streams = fetch_twitch_directory_streams("Grand Theft Auto VI", limit=30)
    if len(raw_streams) < max_count:
        fallback_streams = fetch_twitch_directory_streams("Grand Theft Auto V", limit=25)
        existing_logins = {s["login"] for s in raw_streams}
        for s in fallback_streams:
            if s["login"] not in existing_logins:
                raw_streams.append(s)

    priority_set = {p.lower() for p in PRIORITY_CREATORS}
    filtered = []
    for s in raw_streams:
        if s["stream_id"] in seen_ids:
            continue
        is_prio = 1 if s["login"] in priority_set else 0
        filtered.append((is_prio, s["viewers"], s))

    filtered.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen = [stream_obj for _, _, stream_obj in filtered[:max_count]]
    print(f"[Twitch] Selected {len(chosen)} live streams: {[c['creator'] for c in chosen]}", flush=True)
    return chosen


def record_streams_parallel(streams, duration_sec=CHUNK_SECONDS):
    """Records multiple live Twitch streams simultaneously into raw_X.ts files."""
    if not streams:
        return []
    procs = []
    recorded = []
    for idx, st in enumerate(streams):
        ts_path = f"raw_{idx}.ts"
        if os.path.exists(ts_path):
            os.remove(ts_path)
        cmd = (
            f"streamlink --twitch-disable-ads --twitch-low-latency -O "
            f"'{st['url']}' 720p60,720p,best | "
            f"ffmpeg -y -loglevel error -err_detect ignore_err -i pipe:0 "
            f"-t {int(duration_sec)} -c copy '{ts_path}'"
        )
        p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
        procs.append((p, st, ts_path))
        time.sleep(0.5)

    for p, st, ts_path in procs:
        try:
            p.wait(timeout=duration_sec + 45)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except Exception:
                pass
        if os.path.exists(ts_path) and os.path.getsize(ts_path) > 500_000:
            st_copy = dict(st)
            st_copy["ts_path"] = ts_path
            recorded.append(st_copy)
            print(f"[Twitch-Rec] {st['creator']} -> {ts_path} ({os.path.getsize(ts_path) // (1024*1024)} MB)", flush=True)
        else:
            print(f"[Skip] Failed or empty Twitch recording for {st['creator']}", flush=True)
    return recorded

# ==============================================================================
# PART 4 OF 12: YOUTUBE LIVE, FINISHED VOD & HEATMAP CLIP HARVESTER
# ==============================================================================
def harvest_youtube_candidates(hist, max_items=4):
    """Finds YouTube Live streams, finished VODs & uploads (Global + India) using Most Replayed heatmaps."""
    seen_ids = set(hist.get("uploaded_ids", []))
    queries = random.sample(YOUTUBE_SEARCH_QUERIES, min(3, len(YOUTUBE_SEARCH_QUERIES)))
    harvested = []

    for q in queries:
        if len(harvested) >= max_items:
            break
        try:
            res = subprocess.run(
                [
                    "yt-dlp", "--dump-json", "--flat-playlist",
                    "--no-warnings", f"ytsearch8:{q}"
                ],
                capture_output=True,
                text=True,
                timeout=25,
            )
            entries = [
                json.loads(line) for line in res.stdout.splitlines()
                if line.strip().startswith("{")
            ]
            random.shuffle(entries)
        except Exception as e:
            print(f"[Warn] YouTube search error for '{q}': {e}", flush=True)
            continue

        for entry in entries:
            if len(harvested) >= max_items:
                break
            vid_id = str(entry.get("id") or "").strip()
            yt_key = f"yt_{vid_id}"
            if not vid_id or yt_key in seen_ids:
                continue

            url = f"https://youtube.com{vid_id}"
            try:
                meta_res = subprocess.run(
                    ["yt-dlp", "--dump-json", "--no-warnings", url],
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                if meta_res.returncode != 0 or not meta_res.stdout.strip().startswith("{"):
                    continue
                info = json.loads(meta_res.stdout)
            except Exception:
                continue

            is_live = bool(info.get("is_live"))
            dur = float(info.get("duration") or 0)
            uploader = str(info.get("uploader") or info.get("channel") or "YouTuber").strip()
            title = str(info.get("title") or "GTA 6 Gameplay").strip()
            ts_path = f"raw_yt_{len(harvested)}.ts"

            if not is_live and (dur < 65 or dur > 18000):
                continue

            # Use YouTube's 'Most Replayed' viewer heatmap to pinpoint the exact viral climax
            start_sec = 30.0
            heatmap = info.get("heatmap") or []
            if heatmap and not is_live:
                best_seg = max(heatmap, key=lambda h: float(h.get("value", 0.0)))
                peak_t = float(best_seg.get("start_time", 30.0))
                start_sec = max(0.0, min(peak_t - 22.0, max(0.0, dur - 80.0)))
            elif not is_live and dur > 120:
                start_sec = random.uniform(30.0, max(35.0, dur - 85.0))

            grab_dur = 180 if is_live else 75
            try:
                stream_url = next(
                    iter(
                        subprocess.check_output(
                            [
                                "yt-dlp", "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
                                "--no-warnings", "-g", url
                            ],
                            text=True,
                            timeout=20,
                        ).splitlines()
                    ),
                    "",
                ).strip()
                if not stream_url:
                    continue

                ff_cmd = ["ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err"]
                if not is_live:
                    ff_cmd.extend(["-ss", f"{start_sec:.1f}"])
                ff_cmd.extend(["-i", stream_url, "-t", str(grab_dur), "-c", "copy", ts_path])
                subprocess.run(ff_cmd, timeout=grab_dur + 35)
            except Exception:
                continue

            if os.path.exists(ts_path) and os.path.getsize(ts_path) > 500_000:
                seen_ids.add(yt_key)
                harvested.append({
                    "stream_id": yt_key,
                    "login": uploader.lower(),
                    "creator": uploader,
                    "title": title,
                    "viewers": int(info.get("view_count") or 1000),
                    "game": "Grand Theft Auto VI",
                    "url": url,
                    "ts_path": ts_path,
                    "has_heatmap": bool(heatmap),
                })
                tag = "LIVE" if is_live else ("HEATMAP-VOD" if heatmap else "VOD")
                print(f"[YouTube-{tag}] Grabbed {uploader}: {title[:48]}", flush=True)

    return harvested

# ==============================================================================
# PART 5 OF 12: MULTI-WINDOW ACOUSTIC SCANNER & ENGLISH WHISPER TRANSLATOR
# ==============================================================================
def get_whisperm_model():
    """Loads multilingual Whisper base model so non-English speech translates cleanly to English."""
    return WhisperModel("base", device="cpu", compute_type="int8")


def find_top_peak_windows(energies, median_e, num_secs, max_peaks=3):
    """Finds up to 3 non-overlapping active high-energy windows while rejecting muted dead-air."""
    scored_secs = []
    for s in range(25, num_secs - 25):
        win = energies[s - 18 : s + 22]
        active_ratio = float(np.mean(win > (median_e * 0.55)))
        if active_ratio < 0.65:
            continue
        spike = float(np.mean(energies[s - 2 : s + 4])) / (float(np.mean(energies[s - 18 : s - 4])) + 1e-6)
        score = spike * active_ratio
        if score >= 1.20:
            scored_secs.append((score, s))

    scored_secs.sort(key=lambda item: item, reverse=True)
    chosen = []
    for score, s in scored_secs:
        if all(abs(s - prev_s) >= 46 for _, prev_s in chosen):
            chosen.append((score, s))
            if len(chosen) >= max_peaks:
                break
    return chosen


def extract_acoustic_candidates(recorded_streams):
    """Scans all Twitch & YouTube recordings, filters dead-air/stunts, and builds English transcripts."""
    if not recorded_streams:
        return []

    model = get_whisperm_model()
    candidates = []

    for st in recorded_streams:
        ts_path = st["ts_path"]
        channel_name = st["creator"]
        wav_path = ts_path.replace(".ts", ".wav")

        ext_res = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err",
            "-i", ts_path, "-vn", "-ac", "1", "-ar", "16000", wav_path
        ])
        if ext_res.returncode != 0 or not os.path.exists(wav_path) or os.path.getsize(wav_path) < 32000:
            continue

        try:
            rate, data = wavfile.read(wav_path)
            data = data.astype(np.float32) / 32768.0
        except Exception:
            continue

        num_secs = len(data) // rate
        try:
            probe_out = subprocess.check_output(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", ts_path
                ],
                text=True,
            ).strip()
            if probe_out and probe_out != "N/A":
                num_secs = min(num_secs, max(0, int(float(probe_out)) - 3))
        except Exception:
            pass

        if num_secs < 55:
            continue

        energies = np.array([np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2)) for i in range(num_secs)])
        median_e = float(np.median(energies)) + 1e-6
        peak_windows = find_top_peak_windows(energies, median_e, num_secs, max_peaks=3)

        for win_idx, (jump_score, best_sec) in enumerate(peak_windows):
            win_start = max(0, best_sec - 24)
            win_end = min(num_secs, best_sec + 21)
            sub_wav = ts_path.replace(".ts", f"_w{win_idx}.wav")
            wavfile.write(sub_wav, rate, (data[win_start * rate : win_end * rate] * 32767).astype(np.int16))

            lines = []
            try:
                segments, _ = model.transcribe(sub_wav, beam_size=1, task="translate")
                for seg in segments:
                    txt = re.sub(r"[^\x20-\x7E]", "", seg.text.strip())
                    if txt:
                        lines.append(f"[{win_start + seg.start:.1f}s] {txt}")
            except Exception:
                pass

            spoken_words = sum(max(0, len(ln.split()) - 1) for ln in lines)
            if spoken_words < 10 and jump_score < 2.2:
                continue

            heatmap_bonus = 1.25 if st.get("has_heatmap") else 1.0
            base_score = jump_score * (1.0 + min(spoken_words, 65) / 75.0) * heatmap_bonus
            candidates.append({
                "stream_id": st["stream_id"],
                "creator": channel_name,
                "title": st.get("title", ""),
                "game": st.get("game", "Grand Theft Auto VI"),
                "ts_path": ts_path,
                "audio_peak": best_sec,
                "jump_ratio": round(jump_score, 3),
                "spoken_words": spoken_words,
                "combined_score": round(base_score, 3),
                "transcript_snippet": " | ".join(lines[:14]),
            })

    print(f"[Acoustic] Extracted {len(candidates)} clean English-translated candidates.", flush=True)
    return candidates

# ==============================================================================
# PART 6 OF 12: GEMINI 2.5 FLASH VIRAL WATCHABILITY GATEKEEPER & LAYOUT CHOOSER
# ==============================================================================
def analyze_batch_for_multiple_shorts(recorded_streams):
    """Scores candidate clips with Gemini 2.5 Flash and picks SPLIT vs BLUR/CROP layout."""
    candidates = extract_acoustic_candidates(recorded_streams)
    if not candidates:
        return []

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        summary_lines = []
        for idx_c, c_item in enumerate(candidates[:14]):
            summary_lines.append(
                f"ID={idx_c} | Creator={c_item['creator']} | Peak={c_item['audio_peak']}s | "
                f"Spike={c_item['jump_ratio']}x | Words={c_item['spoken_words']} | "
                f"EnglishTranscript={c_item['transcript_snippet'][:280]}"
            )
        judge_prompt = (
            "You are an elite viral gaming Shorts producer. Rate each candidate clip 1-10 on REAL viewer watchability.\n"
            "REJECT (< 6.8): plain driving, walking, menu browsing, muted calls, or random yelling with no context.\n"
            "REWARD (>= 7.5): hilarious panic, unexpected crash/glitch, instant karma, wild NPC interaction, or clutch escape.\n"
            "Also choose LAYOUT:\n"
            "- 'SPLIT' if the streamer shows a noteworthy personal reaction, laugh, shock, or rage where seeing their facecam adds value.\n"
            "- 'CROP' if it is a pure cinematic stunt/crash with minimal facecam reaction.\n"
            "- 'BLUR' if standard widescreen HUD context is needed.\n"
            "Output strictly one line per ID: ID=<num>|SCORE=<1-10>|LAYOUT=<SPLIT|BLUR|CROP>\n\n"
            + "\n".join(summary_lines)
        )
        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(model="gemini-2.5-flash", contents=judge_prompt)
            for ln in (resp.text or "").splitlines():
                if "ID=" in ln and "SCORE=" in ln:
                    parts = dict(seg.split("=", 1) for seg in ln.strip().split("|") if "=" in seg)
                    cid = int(parts.get("ID", -1))
                    if 0 <= cid < len(candidates):
                        ai_score = float(parts.get("SCORE", 5.0))
                        layout_choice = parts.get("LAYOUT", "BLUR").strip().upper()
                        candidates[cid]["ai_watch_score"] = ai_score
                        candidates[cid]["preferred_layout"] = (
                            layout_choice if layout_choice in ("SPLIT", "BLUR", "CROP") else "BLUR"
                        )
                        candidates[cid]["combined_score"] = round(
                            candidates[cid]["combined_score"] * (ai_score / 5.0), 3
                        )
        except Exception as e:
            print(f"[Warn] AI watchability judge fallback used: {e}", flush=True)

    worthy = [c for c in candidates if c.get("ai_watch_score", 7.0) >= MIN_AI_WATCH_SCORE]
    if not worthy:
        # Keep the single best fallback candidate so hourly scout still contributes to the 5-clip pool
        top_fallback = max(candidates, key=lambda x: x["combined_score"])
        worthy = [top_fallback]

    sorted_c = sorted(worthy, key=lambda x: x["combined_score"], reverse=True)
    best_c = next(iter(sorted_c))
    chosen_layout = best_c.get(
        "preferred_layout",
        "SPLIT" if best_c.get("jump_ratio", 0) >= 2.3 else "BLUR",
    )
    print(
        f"[Selector] Top Hour Pick: {best_c['creator']} | AI Score={best_c.get('ai_watch_score', 'N/A')}/10 | "
        f"Layout={chosen_layout} | Total={best_c['combined_score']}",
        flush=True,
    )
    return [(
        "SINGLE",
        chosen_layout,
        [(best_c, max(0, best_c["audio_peak"] - 22), best_c["audio_peak"], SINGLE_CLIP_DUR)],
    )]

# ==============================================================================
# PART 7 OF 12: VIRAL ENGLISH TITLE & ON-SCREEN HOOK BANNER GENERATOR
# ==============================================================================
VIRAL_EMOJIS = ["😱", "🔥", "💀", "🤯", "🚨", "⚡", "😂", "👀", "💥", "🎮"]

HOOK_BANNERS = [
    "WAIT FOR THE END!",
    "WATCH UNTIL THE LAST SECOND!",
    "HOW DID THIS HAPPEN?!",
    "1 IN A MILLION MOMENT!",
    "NO WAY THAT JUST HAPPENED!",
    "PURE VICE CITY CHAOS!",
]


def clean_english_title(raw_text, fallback):
    """Enforces strictly English ASCII + emojis and appends #gta6 #shorts if missing."""
    cleaned = re.sub(
        r"[^\x00-\x7F\U0001F300-\U0001FAFF\u2600-\u27BF]+", "", str(raw_text or "")
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) < 12:
        return fallback
    if "#shorts" not in cleaned.lower():
        cleaned = f"{cleaned[:76].strip()} #gta6 #shorts"
    return cleaned[:95]


def generate_viral_metadata(mode, creators_list, transcript_text, game_name="Grand Theft Auto VI"):
    """Generates a unique English title and an ALL-CAPS opening screen hook banner."""
    e1, e2 = random.sample(VIRAL_EMOJIS, 2)
    lead_creator = next(iter(creators_list), "This Streamer")

    single_templates = [
        f"{lead_creator} Just Broke GTA 6 Physics {e1}{e2} #gta6 #shorts",
        f"Vice City Cops Had NO Mercy on {lead_creator} {e1}{e2} #gta6 #shorts",
        f"1 in 1,000,000 GTA 6 Moment by {lead_creator} {e1}{e2} #gta6 #shorts",
        f"How Did {lead_creator} Survive This in Leonida?! {e1}{e2} #gta6 #shorts",
        f"{lead_creator}'s Reaction Says Everything in GTA 6 {e1}{e2} #gta6 #shorts",
        f"Never Trust NPCs in Vice City ({lead_creator}) {e1}{e2} #gta6 #shorts",
    ]
    multi_templates = [
        f"3 Unhinged GTA 6 Live Moments Today {e1}{e2} #gta6 #shorts",
        f"Vice City Chaos Caught Live on Stream {e1}{e2} #gta6 #shorts",
        f"GTA 6 Streamers Losing Their Minds {e1}{e2} #gta6 #shorts",
        f"Peak Leonida Madness in 45 Seconds {e1}{e2} #gta6 #shorts",
    ]
    fallback_title = random.choice(multi_templates if mode == "MULTI" else single_templates)
    banner_hook = random.choice(HOOK_BANNERS)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return fallback_title, banner_hook

    angles = [
        "unbelievable timing or physics glitch",
        "hilarious panic or instant karma",
        "insane stunt or near-miss escape",
        "unexpected NPC or police chaos",
    ]
    chosen_angle = random.choice(angles)
    creators_str = ", ".join(creators_list[:3]) if creators_list else "top streamers"

    prompt = (
        f"You write high-CTR YouTube Shorts titles in ENGLISH ONLY for '{CHANNEL_HANDLE}'.\n"
        f"Mode: {mode} | Game: {game_name} | Streamer(s): {creators_str}\n"
        f"Angle: {chosen_angle}\n"
        f"English Transcript: {str(transcript_text)[:520]}\n"
        f"Rules:\n"
        f"- Output strictly 2 lines:\n"
        f"  TITLE: <catchy English title under 74 chars + 2 emojis {e1}{e2} + #gta6 #shorts>\n"
        f"  HOOK: <ALL CAPS 3-5 word on-screen banner hook>\n"
        f"- Include '{lead_creator}' in TITLE if SINGLE mode.\n"
        f"- Strictly ENGLISH ONLY. NO profanity, NO swear words, NO generic 'Nobody Expected This' cliches.\n"
    )

    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        out = (resp.text or "").strip()
        title_line, hook_line = "", ""
        for ln in out.splitlines():
            if ln.upper().startswith("TITLE:"):
                title_line = ln.split(":", 1)[1].strip()
            elif ln.upper().startswith("HOOK:"):
                hook_line = ln.split(":", 1)[1].strip().upper()

        final_title = clean_english_title(title_line, fallback_title)
        final_hook = re.sub(r"[^A-Z0-9 !?'-]", "", hook_line)[:32].strip() or banner_hook
        return final_title, final_hook
    except Exception as e:
        print(f"[Warn] Gemini metadata fallback used: {e}", flush=True)
        return fallback_title, banner_hook

# ==============================================================================
# PART 8 OF 12: ANIMATED .ASS ENGLISH POP-IN SUBTITLES & PROFANITY FILTER
# ==============================================================================
PROFANITY_PATTERN = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|asshole\w*|motherfuck\w*|nigga\w*|nigger\w*|cunt\w*|dick\w*|pussy\w*)\b",
    re.IGNORECASE,
)


def censor_caption_text(text):
    """Strips non-ASCII characters so captions stay strictly English and masks profanity."""
    ascii_only = re.sub(r"[^\x20-\x7E]", "", str(text or "")).strip()
    return PROFANITY_PATTERN.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), ascii_only)


def format_ass_time(sec):
    """Formats seconds into ASS subtitle timestamp H:MM:SS.cs."""
    sec = max(0.0, float(sec))
    hrs = int(sec // 3600)
    mins = int((sec % 3600) // 60)
    secs = sec % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def generate_animated_ass_captions(video_path, ass_path="captions.ass"):
    """Generates word-by-word animated pop-in English subtitles (translates non-English audio)."""
    wav_path = "final_cap_audio.wav"
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: PopCap,Arial Black,68,&H00FFFFFF,&H0000D7FF,&H00000000,&H96000000,"
        "-1,0,0,0,100,100,1.5,0,1,5,3,2,70,70,390,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    ext = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000", wav_path
    ])
    events = []
    if ext.returncode == 0 and os.path.exists(wav_path) and os.path.getsize(wav_path) > 16000:
        try:
            model = get_whisperm_model()
            segments, _ = model.transcribe(wav_path, beam_size=1, task="translate", word_timestamps=True)
            words = []
            for seg in segments:
                if getattr(seg, "words", None):
                    for w in seg.words:
                        clean_w = censor_caption_text(w.word)
                        if clean_w:
                            words.append((float(w.start), float(w.end), clean_w.upper()))
                else:
                    clean_s = censor_caption_text(seg.text)
                    if clean_s:
                        words.append((float(seg.start), float(seg.end), clean_s.upper()))

            chunk_size = 3
            for i in range(0, len(words), chunk_size):
                chunk = words[i : i + chunk_size]
                if not chunk:
                    continue
                for active_idx, (w_start, w_end, _) in enumerate(chunk):
                    next_start = (
                        chunk[active_idx + 1][0]
                        if active_idx + 1 < len(chunk)
                        else max(w_end + 0.12, w_start + 0.25)
                    )
                    if next_start <= w_start:
                        next_start = w_start + 0.22
                    styled_parts = []
                    for j, (_, _, w_txt) in enumerate(chunk):
                        safe_w = w_txt.replace("{", "").replace("}", "").replace("\\", "")
                        if j == active_idx:
                            styled_parts.append(
                                r"{\c&H0000D7FF&\fscx114\fscy114}" + safe_w + r"{\c&H00FFFFFF&\fscx100\fscy100}"
                            )
                        else:
                            styled_parts.append(safe_w)
                    pop_anim = r"{\an2\fscx88\fscy88\t(0,75,\fscx100\fscy100)}"
                    line_txt = pop_anim + " ".join(styled_parts)
                    events.append(
                        f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(next_start)},PopCap,,0,0,0,,{line_txt}\n"
                    )
        except Exception as e:
            print(f"[Warn] ASS caption generation skipped: {e}", flush=True)

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "".join(events))
    return ass_path

# ==============================================================================
# PART 9A OF 12: TEXT SANITIZER, OPENCV FACECAM DETECTOR & SEGMENT BUILDER
# ==============================================================================
XFADE_TRANSITIONS = ["fade", "wipeleft", "wiperight", "slideleft", "slideright", "smoothleft", "circleopen"]


def sanitize_ffmpeg_text(text):
    """Sanitizes strings for safe FFmpeg drawtext filter usage."""
    clean = re.sub(r"[^\x20-\x7E]", "", str(text or ""))
    clean = clean.replace("\\", "").replace("'", "").replace('"', "").replace(":", "\\:").replace("%", "\\%")
    return clean.strip()


def detect_facecam_crop(video_path):
    """Detects streamer facecam corner (TL, TR, BL, BR) and returns FFmpeg crop filter, or None."""
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    corners = {
        "TL": (0, 0, 420, 260),
        "TR": (860, 0, 420, 260),
        "BL": (0, 460, 420, 260),
        "BR": (860, 460, 420, 260),
    }
    hits = {k: 0 for k in corners}

    for frac in (0.15, 0.35, 0.55, 0.75):
        if total_frames > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total_frames * frac))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frame = cv2.resize(frame, (1280, 720))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for name, (cx, cy, cw, ch) in corners.items():
            roi = gray[cy : cy + ch, cx : cx + cw]
            faces = cascade.detectMultiScale(roi, scaleFactor=1.15, minNeighbors=4, minSize=(36, 36))
            if len(faces) > 0:
                hits[name] += 1
    cap.release()

    best_corner, best_count = max(hits.items(), key=lambda kv: kv)
    if best_count >= 2:
        cx, cy, cw, ch = corners[best_corner]
        return f"crop={cw}:{ch}:{cx}:{cy}"
    return None


def prepare_timeline_and_hook(clip_specs):
    """Extracts normalized segments, merges transitions, and attaches a 2.3s climax hook teaser."""
    seg_files = []
    credit_windows = []
    current_timeline_sec = 0.0

    for idx, (cand_info, start_s, peak_s, dur_s) in enumerate(clip_specs):
        seg_path = f"seg_{idx}.mp4"
        src_video = cand_info.get("video_path") or cand_info["ts_path"]
        actual_start = 0.0 if cand_info.get("video_path") else float(start_s)
        low_speech = int(cand_info.get("spoken_words", 15)) < 10
        vol_filter = ",volume=0.45" if low_speech else ""

        res = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err",
            "-ss", f"{actual_start:.2f}", "-i", src_video, "-t", f"{float(dur_s):.2f}",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30",
            "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11{vol_filter},aresample=48000",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2", seg_path
        ])
        if res.returncode != 0 or not os.path.exists(seg_path) or os.path.getsize(seg_path) < 50000:
            continue

        seg_files.append(seg_path)
        c_name = cand_info.get("creator", "Streamer")
        seg_start_t = 0.0 if len(seg_files) == 1 else current_timeline_sec
        seg_end_t = current_timeline_sec + float(dur_s) - (TRANSITION_DUR if idx < len(clip_specs) - 1 else 0.0)
        credit_windows.append((seg_start_t, seg_end_t, c_name))
        current_timeline_sec += float(dur_s) - (TRANSITION_DUR if idx < len(clip_specs) - 1 else 0.0)

    if not seg_files:
        raise RuntimeError("No valid segments could be extracted for rendering.")

    merged_raw = "merged_timeline.mp4"
    first_seg = next(iter(seg_files))
    if len(seg_files) == 1:
        shutil.copy(first_seg, merged_raw)
    else:
        inputs = []
        for sf in seg_files:
            inputs.extend(["-i", sf])
        v_chain, a_chain = "", ""
        prev_v, prev_a = "[0:v]", "[0:a]"
        offset_acc = 0.0
        for i in range(1, len(seg_files)):
            _, _, _, dur_prev = clip_specs[i - 1]
            offset_acc += float(dur_prev) - TRANSITION_DUR
            trans = random.choice(XFADE_TRANSITIONS)
            out_v, out_a = f"[v{i}]", f"[a{i}]"
            v_chain += f"{prev_v}[{i}:v]xfade=transition={trans}:duration={TRANSITION_DUR}:offset={offset_acc:.2f}{out_v};"
            a_chain += f"{prev_a}[{i}:a]acrossfade=d={TRANSITION_DUR}:c1=tri:c2=tri{out_a};"
            prev_v, prev_a = out_v, out_a

        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
                "-filter_complex", f"{v_chain}{a_chain}".rstrip(";"),
                "-map", prev_v, "-map", prev_a,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", merged_raw
            ],
            check=True,
        )

    first_cand, _, first_peak, first_dur = next(iter(clip_specs))
    hook_src = first_cand.get("video_path") or first_cand["ts_path"]
    hook_start = max(0.0, (float(first_dur) - 6.0) if first_cand.get("video_path") else (float(first_peak) - 3.0))
    hook_res = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err",
        "-ss", f"{hook_start:.2f}", "-i", hook_src, "-t", "2.3",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2", "hook_clip.mp4"
    ])
    if hook_res.returncode != 0 or not os.path.exists("hook_clip.mp4") or os.path.getsize("hook_clip.mp4") < 50000:
        shutil.copy(first_seg, "hook_clip.mp4")

    combined_pre_fx = "combined_pre_fx.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", "hook_clip.mp4", "-i", merged_raw,
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", combined_pre_fx
    ], check=True)

    return combined_pre_fx, credit_windows, current_timeline_sec

# ==============================================================================
# PART 9B OF 12: MULTI-LAYOUT VERTICAL COMPOSITOR & THUMBNAIL EXPORTER
# ==============================================================================
def build_and_render_short(
    mode,
    framing_mode,
    clip_specs,
    hook_text="WATCH UNTIL THE END!",
    out_video_name="final_short.mp4",
    out_thumb_name="final_thumb.jpg",
):
    """Composites final 1080x1920 vertical Short (SPLIT, CROP, or BLUR) and extracts thumbnail."""
    combined_pre_fx, credit_windows, current_timeline_sec = prepare_timeline_and_hook(clip_specs)

    facecam_crop = detect_facecam_crop(combined_pre_fx) if framing_mode == "SPLIT" else None
    ass_file = generate_animated_ass_captions(combined_pre_fx, "captions.ass")
    safe_handle = sanitize_ffmpeg_text(CHANNEL_HANDLE)
    safe_hook = sanitize_ffmpeg_text(hook_text)

    hook_offset = 2.3
    credit_y = 708 if facecam_crop else 220
    credit_filters = []
    for idx_w, (w_s, w_e, c_nm) in enumerate(credit_windows):
        start_b = 0.0 if idx_w == 0 else (w_s + hook_offset)
        end_b = (w_e + hook_offset) if idx_w < len(credit_windows) - 1 else 999.0
        safe_c = sanitize_ffmpeg_text(c_nm)
        credit_filters.append(
            f"drawtext=text='Credit\\: {safe_c}':fontcolor=white@0.95:fontsize=26:"
            f"x=(w-text_w)/2:y={credit_y}:box=1:boxcolor=black@0.45:boxborderw=6:"
            f"enable='gte(t,{start_b:.2f})*lt(t,{end_b:.2f})'"
        )
    credits_chain = ",".join(credit_filters)
    total_dur = max(15.0, current_timeline_sec + hook_offset)

    hook_banner_filter = (
        f"drawtext=text='{safe_hook}':fontcolor=white:fontsize=38:"
        f"x=(w-text_w)/2:y=310:box=1:boxcolor=0xE11D48@0.95:boxborderw=18:"
        f"enable='lt(t,3.8)'"
    )

    if facecam_crop:
        # LAYOUT 1 (SPLIT): Streamer Facecam on Top (1080x760) + Gameplay on Bottom (1080x1160)
        vf_complex = (
            f"[0:v]{facecam_crop},scale=1080:760:force_original_aspect_ratio=increase,crop=1080:760,"
            "eq=contrast=1.08:saturation=1.15[cam];"
            "[0:v]scale='trunc(1080*(1+0.001*t)/2)*2':'trunc(1160*(1+0.001*t)/2)*2',"
            "crop=1080:1160,eq=contrast=1.08:saturation=1.20[game];"
            "[cam][game]vstack=inputs=2[stacked];"
            "[stacked]drawbox=x=0:y=756:w=1080:h=8:color=0x00F5FF@0.95:t=fill,"
            f"drawbox=x=0:y=1906:w='min(1080,1080*(t/{total_dur:.1f}))':h=14:color=0xFF2A85@0.95:t=fill,"
            f"drawtext=text='{safe_handle}':fontcolor=white@0.95:fontsize=28:x=32:y=42:box=1:boxcolor=black@0.45:boxborderw=8,"
            f"{hook_banner_filter},{credits_chain},ass={ass_file}[vout]"
        )
    elif framing_mode == "CROP":
        # LAYOUT 2 (CROP): Full-Screen Vertical 9:16 Center Crop
        vf_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "eq=contrast=1.08:saturation=1.18,"
            f"drawbox=x=0:y=1906:w='min(1080,1080*(t/{total_dur:.1f}))':h=14:color=0x00F5FF@0.95:t=fill,"
            f"drawtext=text='{safe_handle}':fontcolor=white@0.95:fontsize=28:x=(w-text_w)/2:y=155,"
            f"{hook_banner_filter},{credits_chain},ass={ass_file}[vout]"
        )
    else:
        # LAYOUT 3 (BLUR): Full-Screen Blurred Background + Centered 16:9 Gameplay + Neon Bars
        vf_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "boxblur=26:4,eq=brightness=-0.12:saturation=1.25[bg];"
            "[0:v]eq=contrast=1.08:saturation=1.18:brightness=0.01,"
            "scale='trunc(1080*(1+0.001*t)/2)*2':'trunc(608*(1+0.001*t)/2)*2',"
            "crop=1080:608[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[comp1];"
            "[comp1]drawbox=x=0:y=652:w=1080:h=6:color=0x00F5FF@0.95:t=fill,"
            "drawbox=x=0:y=1262:w=1080:h=6:color=0xFF2A85@0.95:t=fill,"
            f"drawbox=x=0:y=1906:w='min(1080,1080*(t/{total_dur:.1f}))':h=14:color=0x00F5FF@0.95:t=fill,"
            f"drawtext=text='{safe_handle}':fontcolor=white@0.96:fontsize=30:x=(w-text_w)/2:y=175,"
            f"{hook_banner_filter},{credits_chain},ass={ass_file}[vout]"
        )

    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", combined_pre_fx,
        "-filter_complex", vf_complex,
        "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        out_video_name
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", "4.5", "-i", out_video_name,
        "-vframes", "1", "-q:v", "2", out_thumb_name
    ])
    print(f"[Render] Built {out_video_name} ({framing_mode}, facecam={bool(facecam_crop)})", flush=True)
    return out_video_name, out_thumb_name

# ==============================================================================
# PART 10 OF 12: HOURLY CANDIDATE POOL SAVER & 6-HOUR WINDOW WINNER SELECTOR
# ==============================================================================
def save_hourly_candidate(mode, framing_mode, clip_specs, out_dir="candidate_out"):
    """Saves the top hourly raw clip + metadata into candidate_out/ for the 6-hour pool."""
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    saved_clips = []
    total_score = 0.0
    for idx, (cand_info, start_s, peak_s, dur_s) in enumerate(clip_specs):
        raw_name = f"clip_{idx}.mp4"
        raw_path = os.path.join(out_dir, raw_name)
        res = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err",
            "-ss", f"{float(start_s):.2f}", "-i", cand_info["ts_path"],
            "-t", f"{float(dur_s):.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", raw_path
        ])
        if res.returncode != 0 or not os.path.exists(raw_path) or os.path.getsize(raw_path) < 50000:
            continue

        score_val = float(cand_info.get("combined_score") or cand_info.get("jump_ratio") or 1.0)
        total_score += score_val
        saved_clips.append({
            "file": raw_name,
            "stream_id": cand_info.get("stream_id", ""),
            "creator": cand_info.get("creator", "Streamer"),
            "game": cand_info.get("game", "Grand Theft Auto VI"),
            "duration": float(dur_s),
            "spoken_words": int(cand_info.get("spoken_words", 15)),
            "score": score_val,
            "transcript_snippet": cand_info.get("transcript_snippet", ""),
        })

    if not saved_clips:
        print("[Scout] No valid raw clips saved this hour.", flush=True)
        return False

    avg_score = round(total_score / len(saved_clips), 3)
    meta_payload = {
        "mode": mode if len(saved_clips) > 1 else "SINGLE",
        "framing_mode": framing_mode,
        "score": avg_score,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "clips": saved_clips,
    }
    with open(os.path.join(out_dir, "candidate_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta_payload, f, indent=2)
    print(f"[Scout] Saved {meta_payload['mode']} candidate (score={avg_score}) to {out_dir}/", flush=True)
    return True


def fetch_and_pick_best_candidate(work_dir="pool_in"):
    """Fetches all hourly candidate artifacts in the 6-hour window, picks the #1 winner & deletes old artifacts."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        print("[Pool] Missing GITHUB_TOKEN or GITHUB_REPOSITORY; skipping artifact fetch.", flush=True)
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = requests.get(
            f"https://github.com{repo}/actions/artifacts?per_page=30",
            headers=headers,
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[Pool] Artifact list returned HTTP {resp.status_code}", flush=True)
            return None
    except Exception as e:
        print(f"[Pool] Artifact list request failed: {e}", flush=True)
        return None

    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    pool = []
    for art in resp.json().get("artifacts", []):
        name = str(art.get("name", ""))
        if art.get("expired") or not name.startswith("vault-candidate-"):
            continue
        art_id = art.get("id")
        try:
            dl = requests.get(
                f"https://github.com{repo}/actions/artifacts/{art_id}/zip",
                headers=headers,
                timeout=60,
            )
            if dl.status_code == 200:
                sub = os.path.join(work_dir, str(art_id))
                with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
                    zf.extractall(sub)
                meta_p = os.path.join(sub, "candidate_meta.json")
                if os.path.exists(meta_p):
                    with open(meta_p, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    valid_clips = []
                    for c_entry in meta.get("clips", []):
                        vp = os.path.join(sub, c_entry.get("file", ""))
                        if os.path.exists(vp) and os.path.getsize(vp) > 50000:
                            c_copy = dict(c_entry)
                            c_copy["video_path"] = vp
                            valid_clips.append(c_copy)
                    if valid_clips:
                        meta["clips"] = valid_clips
                        meta["artifact_id"] = art_id
                        pool.append(meta)
        except Exception as e:
            print(f"[Pool] Error downloading artifact {art_id}: {e}", flush=True)
        finally:
            try:
                requests.delete(
                    f"https://github.com{repo}/actions/artifacts/{art_id}",
                    headers=headers,
                    timeout=15,
                )
            except Exception:
                pass

    if not pool:
        print("[Pool] No candidate artifacts found in pool.", flush=True)
        return None

    sorted_pool = sorted(pool, key=lambda x: float(x.get("score", 0.0)), reverse=True)
    winner = next(iter(sorted_pool))
    print(
        f"[Pool] Picked #1 winner from {len(pool)} hourly candidates "
        f"(layout={winner.get('framing_mode')}, score={winner.get('score')})",
        flush=True,
    )
    return winner

# ==============================================================================
# PART 11 OF 12: YOUTUBE OAUTH UPLOADER, THUMBNAIL SETTER & AUTO-COMMENTER
# ==============================================================================
def get_youtube_client():
    """Authenticates and returns a fresh YouTube Data API v3 client from YOUTUBE_TOKEN_JSON."""
    token_raw = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not token_raw:
        raise RuntimeError("YOUTUBE_TOKEN_JSON environment variable is missing.")
    creds_info = json.loads(token_raw)
    creds = Credentials.from_authorized_user_info(creds_info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_path, thumb_path, title, creators_list, publish_at_iso=None):
    """Uploads Short, schedules exact UTC publishAt slot, sets thumbnail & posts engagement comment."""
    youtube = get_youtube_client()
    unique_creators = list(dict.fromkeys([c for c in creators_list if c]))
    credits_str = ", ".join(unique_creators) if unique_creators else "GTA Streamers"

    description = (
        f"{title}\n\n"
        f"Featured Streamer(s): {credits_str}\n"
        f"Subscribe to {CHANNEL_HANDLE} for daily GTA 6 & Vice City live highlights!\n\n"
        f"#gta6 #grandtheftauto6 #vicecity #gta6india #indianstreamers #gta6clips #gaming #shorts"
    )

    status_body = {
        "selfDeclaredMadeForKids": False,
    }
    if YOUTUBE_PRIVACY == "public" and publish_at_iso:
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = publish_at_iso
    else:
        status_body["privacyStatus"] = YOUTUBE_PRIVACY

    body = {
        "snippet": {
            "title": title[:98],
            "description": description[:4900],
            "tags": [
                "GTA 6", "Grand Theft Auto VI", "GTA 6 Clips", "Vice City",
                "GTA 6 India", "Indian Gaming", "GTA 6 Funny Moments",
                "Shorts", "Gaming Shorts"
            ],
            "categoryId": "20",  # Gaming
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": status_body,
    }

    media = MediaFileUpload(video_path, chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[Upload] Progress: {int(status.progress() * 100)}%", flush=True)

    video_id = response.get("id", "")
    print(f"[YouTube] Uploaded video ID={video_id} | Scheduled={publish_at_iso or YOUTUBE_PRIVACY}", flush=True)

    if video_id and thumb_path and os.path.exists(thumb_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg")
            ).execute()
            print(f"[YouTube] Custom thumbnail set for {video_id}", flush=True)
        except Exception as e:
            print(f"[Warn] Thumbnail set skipped (requires phone-verified channel): {e}", flush=True)

    if video_id:
        try:
            youtube.commentThreads().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "snippet": {
                                "textOriginal": (
                                    f"Rate this {credits_str} GTA moment 1-10 🔥 "
                                    f"Which streamer should we feature next? 👇"
                                )
                            }
                        },
                    }
                },
            ).execute()
            print(f"[YouTube] Posted engagement starter comment on {video_id}", flush=True)
        except Exception as e:
            print(f"[Warn] Auto-comment skipped: {e}", flush=True)

    return video_id

# ==============================================================================
# PART 12 OF 12: MAIN ORCHESTRATOR (HOURLY SCOUT VS SINGLE-SLOT PUBLISHER)
# ==============================================================================
def run_scout_mode(hist):
    """Hourly scout: records Twitch + YouTube Live/Heatmap VODs and saves top raw clip."""
    twitch_streams = discover_top_live_streams(hist, max_count=MAX_STREAMS_TO_RECORD)
    twitch_recorded = record_streams_parallel(twitch_streams, duration_sec=CHUNK_SECONDS) if twitch_streams else []
    yt_recorded = harvest_youtube_candidates(hist, max_items=4)

    all_recorded = twitch_recorded + yt_recorded
    if not all_recorded:
        print("[Scout] No Twitch or YouTube recordings succeeded this hour.", flush=True)
        return

    short_plans = analyze_batch_for_multiple_shorts(all_recorded)
    if not short_plans:
        print("[Scout] No worthy viral moments passed filters this hour.", flush=True)
        return

    top_plan = next(iter(short_plans))
    mode, framing_mode, clip_specs = top_plan
    save_hourly_candidate(mode, framing_mode, clip_specs, out_dir="candidate_out")


def run_publish_mode(hist):
    """Pre-slot publisher: picks #1 winner from 6h candidate pool and schedules strictly 1 slot."""
    slot_iso = None
    if UPLOAD_TO_YOUTUBE and YOUTUBE_PRIVACY == "public":
        slot_iso = get_next_available_slot(hist)
        if not slot_iso:
            print("[Publish] All upcoming schedule slots are already booked. Skipping.", flush=True)
            return

    winner = fetch_and_pick_best_candidate(work_dir="pool_in")
    mode = "SINGLE"
    framing_mode = "BLUR"
    clip_specs = []
    creators_list = []
    transcript_pieces = []
    game_name = "Grand Theft Auto VI"
    used_stream_ids = []

    if winner:
        mode = winner.get("mode", "SINGLE")
        framing_mode = winner.get("framing_mode", "BLUR")
        for c_entry in winner.get("clips", []):
            dur_s = float(c_entry.get("duration", SINGLE_CLIP_DUR))
            clip_specs.append((c_entry, 0.0, max(3.0, dur_s - 4.0), dur_s))
            creators_list.append(c_entry.get("creator", "Streamer"))
            transcript_pieces.append(c_entry.get("transcript_snippet", ""))
            game_name = c_entry.get("game", game_name)
            if c_entry.get("stream_id"):
                used_stream_ids.append(c_entry.get("stream_id"))
    else:
        print("[Publish] Candidate pool empty; running live Twitch + YouTube fallback scan...", flush=True)
        twitch_streams = discover_top_live_streams(hist, max_count=MAX_STREAMS_TO_RECORD)
        twitch_recorded = record_streams_parallel(twitch_streams, duration_sec=CHUNK_SECONDS) if twitch_streams else []
        yt_recorded = harvest_youtube_candidates(hist, max_items=4)
        all_recorded = twitch_recorded + yt_recorded

        short_plans = analyze_batch_for_multiple_shorts(all_recorded)
        if not short_plans:
            print("[Publish] No valid clips found in fallback scan.", flush=True)
            return
        top_plan = next(iter(short_plans))
        mode, framing_mode, clip_specs = top_plan
        for cand_info, _, _, _ in clip_specs:
            creators_list.append(cand_info.get("creator", "Streamer"))
            transcript_pieces.append(cand_info.get("transcript_snippet", ""))
            game_name = cand_info.get("game", game_name)
            if cand_info.get("stream_id"):
                used_stream_ids.append(cand_info.get("stream_id"))

    if not clip_specs:
        print("[Publish] No clip specs available to render.", flush=True)
        return

    transcript_text = " | ".join([t for t in transcript_pieces if t])
    viral_title, hook_banner = generate_viral_metadata(
        mode, creators_list, transcript_text, game_name=game_name
    )
    out_mp4, out_jpg = build_and_render_short(
        mode,
        framing_mode,
        clip_specs,
        hook_text=hook_banner,
        out_video_name="final_short.mp4",
        out_thumb_name="final_thumb.jpg",
    )

    if UPLOAD_TO_YOUTUBE:
        video_id = upload_to_youtube(
            out_mp4,
            out_jpg,
            viral_title,
            creators_list,
            publish_at_iso=slot_iso,
        )
        if video_id:
            hist["uploaded_ids"].extend(used_stream_ids)
            if slot_iso:
                hist["scheduled_slots"].append(slot_iso)
            save_history(hist)
    else:
        print(f"[DryRun] Rendered {out_mp4} ({framing_mode}) | Title: {viral_title}", flush=True)


def main():
    run_mode = os.environ.get("RUN_MODE", "publish").strip().lower()
    print(f"=== {CHANNEL_HANDLE} | Mode: {run_mode.upper()} ===", flush=True)
    hist = load_history()

    if run_mode == "scout":
        run_scout_mode(hist)
    else:
        run_publish_mode(hist)


if __name__ == "__main__":
    main()
