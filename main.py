# ==============================================================================
# PART 1 OF 40: CORE IMPORTS & RUNTIME DEPENDENCIES
# Subheading: Loads Standard Library, Media, AI, Whisper, TTS & YouTube SDKs
# What This Code Does:
#   - Imports Python built-ins for files, JSON state, randomness, and timezones.
#   - Loads OpenCV, NumPy, Pillow, Faster-Whisper, Edge-TTS, Google Gemini SDK,
#     and Google YouTube Data API v3 modules used across the 40-part pipeline.
# ==============================================================================
import os
import re
import json
import time
import math
import shutil
import random
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import edge_tts
from PIL import Image
from google import genai
from google.genai import types
from faster_whisper import WhisperModel
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==============================================================================
# PART 2 OF 40: MASTER CHANNEL & AUTONOMOUS BRAIN SETTINGS
# Subheading: Configures Channel Identity, Free-Tier Storage Limits & Base Goals
# What This Code Does:
#   - Sets channel name, handle, watermark, and YouTube upload toggle.
#   - Caps clip_vault/ to 10 clips (~45 MB) so GitHub storage stays free.
#   - Sets starting targets (3 Shorts/day, 2 Longs/week, 2-5 clips per Short)
#     which the AI Brain automatically experiments with and optimizes over time.
# ==============================================================================
CHANNEL_CONFIG = {
    "channel_name": "Vice City Clips Vault",
    "channel_handle": "@Vice-City_ClipsVault",
    "watermark_text": "@Vice-City_ClipsVault",
    "upload_to_youtube": True,
    "vault_dir": "clip_vault",
    "history_file": "history.json",
    "max_vault_items": 10,          # Keeps repo tiny (~45 MB max) for free tier
    "min_clips_per_short": 2,       # Avoids raw solo clips for YPP monetization
    "max_clips_per_short": 5,       # Up to 5 distinct creators per Short
    "max_new_clips_per_run": 3,     # Harvests up to 3 fresh clips per hourly run
    "min_motion_diff": 4.2,         # Filters out slow walking, lobbies & menus
    "default_shorts_per_day": 3,    # Initial Shorts/day target (AI experiments)
    "default_longs_per_week": 2,    # Initial Longs/week target (AI experiments)
    "tts_voice": "en-US-GuyNeural",
}

IST_TZ = timezone(timedelta(hours=5, minutes=30))

# ==============================================================================
# PART 3 OF 40: SAFE COMMAND RUNNER & MEDIA DURATION CHECKER
# Subheading: Runs FFmpeg, FFprobe & yt-dlp Safely With Timeout Protection
# What This Code Does:
#   - Executes external CLI tools cleanly without hanging the GitHub runner.
#   - Measures exact audio/video duration in seconds using ffprobe.
# ==============================================================================
def run_cmd_safe(cmd, label="Cmd", timeout_sec=120, check=True):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_sec,
    )
    if check and proc.returncode != 0:
        err_tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"[{label}] Exit {proc.returncode}: {err_tail}")
    return proc


def get_media_duration(file_path):
    if not file_path or not os.path.exists(file_path):
        return 0.0
    try:
        res = run_cmd_safe(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            label="FFprobeDur",
            timeout_sec=20,
            check=False,
        )
        val = float((res.stdout or "0").strip())
        return val if math.isfinite(val) and val > 0 else 0.0
    except Exception:
        return 0.0

# ==============================================================================
# PART 4 OF 40: AI BRAIN MEMORY & COMPACT JSON STATE MANAGER
# Subheading: Stores Revisit Pool, Exhausted Streams & Self-Tuning AI Decisions
# What This Code Does:
#   - Initializes the autonomous ai_brain state (cadence, peak hours, winning
#     hook styles, and experiment history).
#   - Saves history.json while trimming old logs so repository size stays tiny.
# ==============================================================================
def _default_ai_brain():
    return {
        "shorts_per_day": 3,
        "longs_per_week": 2,
        "long_weekdays":,     # Wednesday (2) & Sunday (6) initially
        "top_hours_ist": [],         # Empty initially -> 100% random exploration
        "winning_patterns": [],
        "winning_hook_styles": ["shock", "question", "countdown", "challenge"],
        "cadence_experiments": [],
        "last_tuned_week": "",
        "longs_published_this_week": [],
    }


def load_history():
    hfile = CHANNEL_CONFIG["history_file"]
    default_data = {
        "revisit_pool": {},          # Tracks checked timestamps & 24h deletion timers
        "exhausted_ids": [],         # Streams 100% scanned (never revisited again)
        "vault_queue": [],           # Active clips waiting inside clip_vault/
        "scheduled_slots": [],       # Booked UTC publish timestamps
        "uploads": [],               # Recent uploads for weekly AI performance review
        "ai_brain": _default_ai_brain(),
    }
    if not os.path.exists(hfile):
        return default_data
    try:
        with open(hfile, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("revisit_pool"), dict):
            data["revisit_pool"] = {}
        for k in ("exhausted_ids", "vault_queue", "scheduled_slots", "uploads"):
            if not isinstance(data.get(k), list):
                data[k] = []
        brain = _default_ai_brain()
        if isinstance(data.get("ai_brain"), dict):
            brain.update(data["ai_brain"])
        data["ai_brain"] = brain
        return data
    except Exception:
        return default_data


def save_history(history_data):
    history_data["exhausted_ids"] = history_data.get("exhausted_ids", [])[-300:]
    history_data["scheduled_slots"] = history_data.get("scheduled_slots", [])[-40:]
    history_data["uploads"] = history_data.get("uploads", [])[-90:]
    hfile = CHANNEL_CONFIG["history_file"]
    with open(hfile, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2, ensure_ascii=False)

# ==============================================================================
# PART 5 OF 40: ADAPTIVE AUDIENCE REACH & RANDOMIZED SCHEDULE ENGINE
# Subheading: Explores Random Upload Times First, Then Locks Onto Peak Reach
# What This Code Does:
#   - Picks random IST upload hours and minutes during initial exploration.
#   - Once weekly analytics learns top-performing hours, routes 80% of uploads
#     to proven peak hours and keeps 20% random exploration to discover new peaks.
#   - Enforces the AI Brain's active shorts_per_day cadence and multi-creator
#     minimum so raw solo clips are avoided.
# ==============================================================================
def get_next_ist_publish_slot(history_data):
    now_ist = datetime.now(IST_TZ)
    brain = history_data.get("ai_brain", {})
    shorts_per_day = int(brain.get("shorts_per_day", 3))
    top_hours = [int(h) for h in brain.get("top_hours_ist", []) if 0 <= int(h) <= 23]

    today_str = now_ist.strftime("%Y-%m-%d")
    booked = set(history_data.get("scheduled_slots", []))
    today_booked = 0
    for iso_s in booked:
        try:
            dt_ist = datetime.fromisoformat(iso_s.replace("Z", "+00:00")).astimezone(IST_TZ)
            if dt_ist.strftime("%Y-%m-%d") == today_str:
                today_booked += 1
        except Exception:
            pass

    target_day = now_ist if today_booked < shorts_per_day else (now_ist + timedelta(days=1))
    explore_pool = list(range(8, 24))

    if top_hours and random.random() < 0.80:
        chosen_hr = random.choice(top_hours)
    else:
        chosen_hr = random.choice(explore_pool)

    chosen_min = random.choice([5, 15, 25, 35, 45, 55])
    slot_dt = target_day.replace(hour=chosen_hr, minute=chosen_min, second=0, microsecond=0)
    if slot_dt <= now_ist + timedelta(minutes=15):
        slot_dt = (now_ist + timedelta(minutes=random.randint(25, 70))).replace(second=0, microsecond=0)

    return slot_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def should_trigger_publish_window(history_data, force_flag=False):
    slot_iso = get_next_ist_publish_slot(history_data)
    if force_flag:
        return True, slot_iso

    now_ist = datetime.now(IST_TZ)
    brain = history_data.get("ai_brain", {})
    shorts_per_day = int(brain.get("shorts_per_day", 3))
    today_str = now_ist.strftime("%Y-%m-%d")

    today_count = 0
    for u in history_data.get("uploads", []):
        if u.get("type") == "long_16x9":
            continue
        raw_t = str(u.get("uploaded_at", ""))
        try:
            u_ist = datetime.fromisoformat(raw_t.replace("Z", "+00:00")).astimezone(IST_TZ)
            if u_ist.strftime("%Y-%m-%d") == today_str:
                today_count += 1
        except Exception:
            pass

    if today_count >= shorts_per_day:
        return False, slot_iso

    spacing_hours = max(4, int(18 // max(1, shorts_per_day)))
    last_short_dt = None
    for u in reversed(history_data.get("uploads", [])):
        if u.get("type") != "long_16x9" and u.get("uploaded_at"):
            try:
                last_short_dt = datetime.fromisoformat(u["uploaded_at"].replace("Z", "+00:00"))
                break
            except Exception:
                pass

    if last_short_dt and (datetime.now(timezone.utc) - last_short_dt).total_seconds() < spacing_hours * 3600:
        return False, slot_iso

    ready_clips = [
        v for v in history_data.get("vault_queue", [])
        if v.get("vault_path") and os.path.exists(v["vault_path"])
    ]
    distinct_creators = {
        str(v.get("channel_name", "")).strip().lower()
        for v in ready_clips
        if str(v.get("channel_name", "")).strip()
    }
    if len(distinct_creators) >= int(CHANNEL_CONFIG["min_clips_per_short"]):
        return True, slot_iso

    # Emergency solo fallback ONLY if an ultra-rare 9.5+ clip has waited > 20 hours
    now_utc = datetime.now(timezone.utc)
    for v in ready_clips:
        if float(v.get("viral_score", 0.0)) >= 9.5 and v.get("harvested_at"):
            try:
                h_dt = datetime.fromisoformat(v["harvested_at"].replace("Z", "+00:00"))
                if (now_utc - h_dt).total_seconds() >= 20 * 3600:
                    return True, slot_iso
            except Exception:
                pass

    return False, slot_iso

# ==============================================================================
# PART 6 OF 40: ORIGINAL CREATOR DISCOVERY & 24H REVISIT POOL MANAGER
# Subheading: Finds Original Gameplay Streams & Revisits Them 24h After Deletion
# What This Code Does:
#   - Blocks secondary clippers, compilation channels, and short clip dumps.
#   - Never pulls a second clip from a video while its first clip is still in
#     clip_vault/, and enforces a strict 24-hour cooldown after deletion.
#   - Once a stream is 100% scanned, moves it to exhausted_ids and deletes its
#     revisit memory so history.json stays lightweight forever.
# ==============================================================================
BLOCKED_CLIPPER_TERMS = (
    "red arcade", "daily dose", "compilation", "compilations", "best moments",
    "funny moments", "top 10", "top 5", "highlights", "clip", "clips", "clipper",
    "vault", "montage", "fails", "twitch moments", "streamers react",
    "gaming central", "gamology", "ign", "gamespot",
)


def is_compilation_or_clipper(channel_name, video_title):
    ch_low = str(channel_name or "").strip().lower()
    ti_low = str(video_title or "").strip().lower()
    if any(term in ch_low for term in BLOCKED_CLIPPER_TERMS):
        return True
    if any(
        term in ti_low
        for term in ("compilation", "best moments", "funny moments", "top 10", "top 5", "clips", "montage", "ft.", "feat.")
    ):
        return True
    return False


def discover_gta6_candidates(history_data):
    now_utc = datetime.now(timezone.utc)
    exhausted = set(history_data.get("exhausted_ids", []))
    active_vault_ids = {
        str(x.get("video_id", "")).strip()
        for x in history_data.get("vault_queue", [])
        if isinstance(x, dict) and x.get("vault_path") and os.path.exists(x["vault_path"])
    }

    revisit_pool = history_data.get("revisit_pool", {})
    found = []
    seen_now = set()

    # 1. Check partially scanned streams whose previous clip was deleted >= 24h ago
    for vid_id, meta in list(revisit_pool.items()):
        if vid_id in exhausted:
            revisit_pool.pop(vid_id, None)
            continue
        if vid_id in active_vault_ids:
            continue
        del_ts = str(meta.get("last_deleted_at", "")).strip()
        if not del_ts:
            continue
        try:
            del_dt = datetime.fromisoformat(del_ts.replace("Z", "+00:00"))
            if (now_utc - del_dt).total_seconds() >= 24 * 3600:
                seen_now.add(vid_id)
                found.append({
                    "video_id": vid_id,
                    "url": "https://" + "://youtube.com" + "?v=" + vid_id,
                    "title": meta.get("title", "GTA 6 Gameplay"),
                    "channel_name": meta.get("channel_name", "GTA 6 Creator"),
                    "duration": float(meta.get("duration", 1200)),
                    "is_revisit": True,
                })
        except Exception:
            pass

    # 2. Discover brand-new original creator gameplay streams/VODs
    original_queries = [
        "GTA 6 full gameplay playthrough",
        "Grand Theft Auto 6 livestream VOD",
        "GTA 6 live stream gameplay",
        "Grand Theft Auto VI playthrough part",
        "GTA 6 story mode gameplay",
    ]
    random.shuffle(original_queries)

    for q in original_queries[:2]:
        cmd = ["yt-dlp", "--dump-json", "--flat-playlist", "--no-warnings", f"ytsearch12:{q}"]
        res = run_cmd_safe(cmd, label="YtDlpSearch", timeout_sec=45, check=False)
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                info = json.loads(line)
            except Exception:
                continue
            vid_id = str(info.get("id", "")).strip()
            dur = float(info.get("duration") or 0)
            title = str(info.get("title", "")).strip()
            uploader = str(info.get("uploader") or info.get("channel") or "").strip()

            if not vid_id or not uploader or vid_id in exhausted or vid_id in active_vault_ids or vid_id in seen_now:
                continue
            if vid_id in revisit_pool:
                continue
            if "#shorts" in title.lower() or is_compilation_or_clipper(uploader, title):
                continue
            if dur < 480 or dur > 9000:
                continue

            seen_now.add(vid_id)
            found.append({
                "video_id": vid_id,
                "url": "https://" + "://youtube.com" + "?v=" + vid_id,
                "title": title,
                "channel_name": uploader,
                "duration": dur,
                "is_revisit": False,
            })

    random.shuffle(found)
    return found[:8]

# ==============================================================================
# PART 7 OF 40: AUTHENTICATED & EJS-ENABLED CANDIDATE VIDEO DOWNLOADER
# Subheading: Downloads 720p MP4 Source Streams Using Deno Challenge Solver
# What This Code Does:
#   - Loads YOUTUBE_COOKIES_TXT from GitHub Secrets so datacenter IPs pass
#     YouTube bot verification cleanly.
#   - Uses Deno + remote ejs:github challenge solver scripts with cookie-safe
#     player clients (web,mweb and web_safari) plus a cookieless fallback.
# ==============================================================================
def _prepare_cookies_file():
    for candidate_path in ("yt_cookies.txt", "cookies.txt"):
        if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 40:
            return candidate_path

    for env_name in ("YOUTUBE_COOKIES_TXT", "YT_COOKIES_TXT", "YOUTUBE_COOKIES", "YT_COOKIES", "COOKIES_TXT"):
        raw_val = os.environ.get(env_name, "").strip()
        if len(raw_val) > 40:
            out_cookie = "yt_cookies.txt"
            with open(out_cookie, "w", encoding="utf-8") as cf:
                cf.write(raw_val + "\n")
            print(f"[Cookies] Loaded {len(raw_val)} bytes from {env_name}.", flush=True)
            return out_cookie
    return None


def download_candidate_video(candidate, out_path):
    if os.path.exists(out_path):
        os.remove(out_path)

    cookies_file = _prepare_cookies_file()
    vid_id = str(candidate["video_id"]).strip()

    attempts = [
        {"client": "youtube:player_client=web,mweb", "use_cookies": True, "fmt": "bv*[height<=720]+ba/b[height<=720]/b"},
        {"client": "youtube:player_client=web_safari", "use_cookies": True, "fmt": "b/bv*+ba"},
        {"client": "youtube:player_client=android_vr,mweb", "use_cookies": False, "fmt": "b[height<=720]/bv*[height<=720]+ba/b"},
    ]

    for idx, cfg in enumerate(attempts):
        if cfg["use_cookies"] and not cookies_file:
            continue

        cmd = [
            "yt-dlp",
            "--js-runtimes", "deno",
            "--remote-components", "ejs:github",
            "--extractor-args", cfg["client"],
            "-f", cfg["fmt"],
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--max-filesize", "220M",
            "-o", out_path,
        ]
        if cfg["use_cookies"] and cookies_file:
            cmd.extend(["--cookies", cookies_file])
        cmd.extend(["--", vid_id])

        res = run_cmd_safe(cmd, label=f"Download_{vid_id}_{idx}", timeout_sec=160, check=False)
        if res.returncode == 0 and os.path.exists(out_path) and get_media_duration(out_path) >= 20.0:
            print(f"[Downloaded] {vid_id} ({candidate['channel_name']}) via attempt {idx+1}", flush=True)
            return True

        err_lines = (res.stderr or res.stdout or "").strip().splitlines()[-2:]
        print(f"[Download Retry {idx+1}] {vid_id}: {err_lines}", flush=True)
        if os.path.exists(out_path):
            os.remove(out_path)

    return False

# ==============================================================================
# PART 8 OF 40: MULTI-SIGNAL CLIMAX PEAK DETECTOR (SURGE + TIMESTAMP MEMORY)
# Subheading: Finds Sudden Action/Vocal Surges While Skipping Checked Ranges
# What This Code Does:
#   - Compares each 2-second window against both the stream median and its local
#     neighborhood so constant background music is ignored and genuine reactions
#     or explosions stand out.
#   - Skips any timestamp window previously checked in revisit_pool so every
#     revisit finds a brand-new moment until the entire stream is exhausted.
# ==============================================================================
def detect_audio_video_peaks(video_path, max_peaks=2, blocked_ranges=None):
    total_dur = get_media_duration(video_path)
    if total_dur < 20.0:
        return []

    wav_tmp = "tmp_peak_scan.wav"
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)

    res = run_cmd_safe(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", wav_tmp],
        label="ExtractPeakPCM",
        timeout_sec=60,
        check=False,
    )
    if res.returncode != 0 or not os.path.exists(wav_tmp):
        return []

    try:
        raw = np.fromfile(wav_tmp, dtype=np.int16).astype(np.float32)
    finally:
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)

    sr = 8000
    win = sr * 2
    if len(raw) < win * 6:
        return []

    trimmed = raw[: (len(raw) // win) * win].reshape(-1, win)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
    median_rms = float(np.median(rms)) + 1e-5

    start_idx = max(5, int(10 // 2))
    end_idx = max(start_idx + 1, len(rms) - 5)

    scored_bins = []
    for idx in range(start_idx, end_idx):
        ts = idx * 2.0 + 1.0
        if any((float(b0) - 12.0) <= ts <= (float(b1) + 12.0) for b0, b1 in (blocked_ranges or [])):
            continue
        local_base = float(np.mean(rms[max(0, idx - 3) : min(len(rms), idx + 4)])) + 1e-5
        surge_ratio = (float(rms[idx]) / median_rms) * 0.6 + (float(rms[idx]) / local_base) * 0.4
        if surge_ratio >= 1.45:
            scored_bins.append((surge_ratio, ts))

    scored_bins.sort(key=lambda item: item[0], reverse=True)
    chosen_peaks = []
    for _, ts in scored_bins:
        if all(abs(ts - prev) >= 45.0 for prev in chosen_peaks):
            chosen_peaks.append(round(ts, 2))
        if len(chosen_peaks) >= max_peaks:
            break

    return chosen_peaks

# ==============================================================================
# PART 9 OF 40: HIGH-SPEED ACTION MOTION & PACING FILTER
# Subheading: Rejects Static Menus, Pause Screens, Lobbies & Slow Walking
# What This Code Does:
#   - Samples frames across the candidate clip and computes frame-to-frame
#     pixel movement (mean_diff and 85th-percentile burst).
#   - Blocks boring segments so only fast chases, shootouts, stunts, or chaotic
#     gameplay enter the scoring stage.
# ==============================================================================
def verify_action_motion_and_quality(clip_path):
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        return False

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_frames < int(fps * 8):
        cap.release()
        return False

    step = max(1, int(fps // 2))
    prev_gray = None
    diffs = []
    f_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if f_idx % step == 0:
            small = cv2.resize(frame, (160, 90))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                diffs.append(float(np.mean(np.abs(gray.astype(np.float32) - prev_gray.astype(np.float32)))))
            prev_gray = gray
        f_idx += 1

    cap.release()
    if len(diffs) < 6:
        return False

    mean_diff = float(np.mean(diffs))
    peak_diff = float(np.percentile(diffs, 85))
    print(f"[Motion Check] {os.path.basename(clip_path)}: mean_diff={mean_diff:.2f}, p85={peak_diff:.2f}", flush=True)
    return mean_diff >= float(CHANNEL_CONFIG["min_motion_diff"]) and peak_diff >= 6.2

# ==============================================================================
# PART 10 OF 40: GEMINI VISION + MOTION WATCHABILITY QUALITY GATE
# Subheading: Inspects Keyframes With Gemini Vision To Reject Unwatchable Clips
# What This Code Does:
#   - Combines motion intensity, speech excitement, and a Gemini 2.5 Flash
#     visual inspection of 2 keyframes from the clip.
#   - Rejects menus, map screens, loading screens, or dull walking (score < 6.8)
#     and assigns a true watchability score (6.8 - 9.7) to worthy clips.
# ==============================================================================
def compute_clip_viral_score(clip_path, transcript_text=""):
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        return 0.0

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(fps // 2))
    prev_gray = None
    diffs = []
    f_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if f_idx % step == 0:
            small = cv2.resize(frame, (160, 90))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                diffs.append(float(np.mean(np.abs(gray.astype(np.float32) - prev_gray.astype(np.float32)))))
            prev_gray = gray
        f_idx += 1
    cap.release()

    motion_val = float(np.mean(diffs)) if diffs else 0.0
    if motion_val < float(CHANNEL_CONFIG["min_motion_diff"]):
        return 0.0

    score = 6.9 + min(1.8, (motion_val - 4.2) * 0.35)
    hype_words = ("bro", "what", "no way", "cops", "shoot", "run", "crazy", "oh my", "help", "watch", "insane", "dead")
    if any(w in (transcript_text or "").lower() for w in hype_words):
        score += 0.45

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    dur = get_media_duration(clip_path)
    if api_key and dur >= 10.0:
        imgs = []
        tmp_files = []
        for idx, t_snap in enumerate((dur * 0.35, dur * 0.65)):
            fn = f"gem_watch_{idx}.jpg"
            run_cmd_safe(
                ["ffmpeg", "-y", "-ss", f"{t_snap:.2f}", "-i", clip_path, "-vframes", "1", "-q:v", "5", fn],
                label="WatchFrame",
                timeout_sec=15,
                check=False,
            )
            if os.path.exists(fn):
                tmp_files.append(fn)
                imgs.append(Image.open(fn))
        if imgs:
            try:
                client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=25000))
                prompt = (
                    f"Rate how exciting and watchable this GTA gameplay clip is for YouTube Shorts on a scale of 1 to 10. "
                    f"Reject menus, map screens, loading screens, or boring slow walking with score <= 5. "
                    f"Transcript: '{transcript_text[:140]}'. Return strict JSON: {{\"watch_score\": 8.2}}"
                )
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=imgs + [prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
                )
                w_score = float(json.loads(resp.text.strip()).get("watch_score", 7.5))
                if w_score < 6.8:
                    print(f"[Watchability Gate] Rejected low-excitement clip (vision={w_score})", flush=True)
                    return 0.0
                score = (score * 0.55) + (w_score * 0.45)
            except Exception:
                pass
            finally:
                for tf in tmp_files:
                    if os.path.exists(tf):
                        os.remove(tf)

    return round(min(9.7, score), 2)

# ==============================================================================
# PART 11 OF 40: SINGLE-CLIP-PER-STREAM VAULT HARVESTER
# Subheading: Extracts 1 Tight 28-Second Climax Slice Per Stream Into clip_vault/
# What This Code Does:
#   - Extracts a single 28-second high-action slice around the strongest unused
#     peak (11s build-up + 17s climax/payoff) so vault storage stays light.
#   - Enforces at most 1 active clip per stream in clip_vault/ at any time;
#     remaining peaks in that stream wait until this clip is deleted + 24h.
# ==============================================================================
def harvest_climax_slices_to_vault(source_path, candidate_meta, peak_timestamps):
    vault_dir = CHANNEL_CONFIG.get("vault_dir", "clip_vault")
    os.makedirs(vault_dir, exist_ok=True)

    total_dur = get_media_duration(source_path)
    if total_dur < 12.0 or not peak_timestamps:
        return []

    target_slice_dur = 28.0
    pre_roll = 11.0
    video_id = str(candidate_meta.get("video_id", "vid")).strip()
    channel_name = str(candidate_meta.get("channel_name", "GTA 6 Creator")).strip()
    source_title = str(candidate_meta.get("title", "GTA 6 Gameplay")).strip()

    raw_peak = next(iter(peak_timestamps))
    peak_sec = float(raw_peak)
    slice_dur = min(target_slice_dur, total_dur)
    slice_start = max(0.0, min(total_dur - slice_dur, peak_sec - pre_roll))
    slice_end = min(total_dur, slice_start + slice_dur)
    actual_span = round(slice_end - slice_start, 2)

    if actual_span < 11.0:
        return []

    local_peak = round(max(1.0, min(actual_span - 1.0, peak_sec - slice_start)), 2)
    safe_ts = int(round(slice_start))
    vault_filename = f"{video_id}_{safe_ts}s.mp4"
    vault_path = os.path.join(vault_dir, vault_filename)

    try:
        run_cmd_safe(
            [
                "ffmpeg", "-y",
                "-ss", f"{slice_start:.2f}",
                "-t", f"{actual_span:.2f}",
                "-i", source_path,
                "-r", "30",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                vault_path,
            ],
            label=f"HarvestSlice_{video_id}",
            timeout_sec=110,
        )
    except Exception as e:
        print(f"[Harvest Warning] Failed slice {vault_filename}: {e}", flush=True)
        if os.path.exists(vault_path):
            os.remove(vault_path)
        return []

    verified_dur = get_media_duration(vault_path)
    if verified_dur < 10.0:
        if os.path.exists(vault_path):
            os.remove(vault_path)
        return []

    print(
        f"[Vault Harvested] {vault_filename} ({verified_dur:.1f}s @ {safe_ts}s) from {channel_name}",
        flush=True,
    )
    return [{
        "vault_path": vault_path,
        "video_id": video_id,
        "channel_name": channel_name,
        "source_title": source_title,
        "source_total_dur": round(total_dur, 2),
        "source_start_sec": round(slice_start, 2),
        "duration_sec": round(verified_dur, 2),
        "local_peak_sec": local_peak,
        "viral_score": 7.8,
        "transcript_en": "",
        "word_events": [],
        "harvested_at": datetime.now(timezone.utc).isoformat(),
    }]

# ==============================================================================
# PART 12 OF 40: FASTER-WHISPER ENGLISH SPEECH TRANSLATOR & WORD TIMER
# Subheading: Translates Any Non-English Stream Dialogue Into English Text
# What This Code Does:
#   - Extracts audio from the harvested slice and runs Whisper (task="translate")
#     so Spanish, Portuguese, Hindi, or any non-English streamer dialogue is
#     converted into clean English text for Gemini context and hype scoring.
# ==============================================================================
_WHISPER_MODEL_CACHE = None


def _get_whisper_model():
    global _WHISPER_MODEL_CACHE
    if _WHISPER_MODEL_CACHE is None:
        _WHISPER_MODEL_CACHE = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _WHISPER_MODEL_CACHE


def transcribe_and_translate_clip(clip_path):
    wav_tmp = "tmp_whisper_clip.wav"
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)

    res = run_cmd_safe(
        ["ffmpeg", "-y", "-i", clip_path, "-vn", "-ac", "1", "-ar", "16000", wav_tmp],
        label="WhisperAudioExtract",
        timeout_sec=40,
        check=False,
    )
    if res.returncode != 0 or not os.path.exists(wav_tmp):
        return "", []

    try:
        model = _get_whisper_model()
        segments, _ = model.transcribe(
            wav_tmp,
            task="translate",
            beam_size=1,
            word_timestamps=True,
            vad_filter=True,
        )
        full_words = []
        word_events = []
        for seg in segments:
            if getattr(seg, "words", None):
                for w in seg.words:
                    tok = str(w.word or "").strip()
                    if tok:
                        full_words.append(tok)
                        word_events.append({
                            "word": tok,
                            "start": round(float(w.start), 2),
                            "end": round(float(w.end), 2),
                        })
            else:
                txt = str(seg.text or "").strip()
                if txt:
                    full_words.append(txt)
        return " ".join(full_words).strip(), word_events
    except Exception as e:
        print(f"[Whisper Warning] {os.path.basename(clip_path)}: {e}", flush=True)
        return "", []
    finally:
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)

# ==============================================================================
# PART 13 OF 40: VERIFIED STREAMER WEBCAM CORNER DETECTOR
# Subheading: Detects Confirmed Human Facecams (Never Mini-Maps or HUD Icons)
# What This Code Does:
#   - Scans the 4 corners of the gameplay frame across 5 timestamps using
#     OpenCV Haar cascades to check if a real streamer facecam is present.
#   - Only activates top/bottom split-cam layout when a face is verified in
#     at least 2 frames; otherwise uses the full-frame blurred cinema layout.
# ==============================================================================
def detect_verified_webcam_box(video_path, start_s, end_s):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if width < 640 or height < 360:
        cap.release()
        return None

    cascade_cls = getattr(cv2, "CascadeClassifier", None)
    data_mod = getattr(cv2, "data", None)
    if cascade_cls is None or data_mod is None:
        cap.release()
        return None
    cascade_path = os.path.join(data_mod.haarcascades, "haarcascade_frontalface_default.xml")
    face_cascade = cascade_cls(cascade_path)
    if face_cascade.empty():
        cap.release()
        return None

    box_w = int(width * 0.28)
    box_h = int(height * 0.30)
    corners = {
        "TL": (0, 0, box_w, box_h),
        "TR": (width - box_w, 0, box_w, box_h),
        "BL": (0, height - box_h, box_w, box_h),
        "BR": (width - box_w, height - box_h, box_w, box_h),
    }
    hits = {k: 0 for k in corners}

    span = max(1.0, end_s - start_s)
    sample_times = [start_s + span * r for r in (0.15, 0.35, 0.55, 0.75, 0.90)]

    for t_sec in sample_times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t_sec * fps))
        ret, frame = cap.read()
        if not ret:
            continue
        for cname, (cx, cy, cw, ch) in corners.items():
            roi = frame[cy : cy + ch, cx : cx + cw]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(
                gray,
                scaleFactor=1.15,
                minNeighbors=6,
                minSize=(int(cw * 0.16), int(ch * 0.16)),
            )
            if len(faces) > 0:
                hits[cname] += 1

    cap.release()
    best_corner = max(hits, key=lambda k: hits[k])
    if hits[best_corner] >= 2:
        print(f"[Webcam Verified] {best_corner} ({hits[best_corner]}/5 frames)", flush=True)
        return corners[best_corner]
    return None

# ==============================================================================
# PART 14 OF 40: FREE-TIER VAULT HOUSEKEEPING & 24H DELETION STAMPER
# Subheading: Caps Vault To 10 Clips (~45 MB) & Starts 24h Cooldown On Deletion
# What This Code Does:
#   - Keeps clip_vault/ strictly capped at 10 clips so GitHub storage stays free.
#   - Whenever a clip is published, expired (>36h), or evicted, records its
#     checked timestamp range in revisit_pool and sets last_deleted_at so the
#     harvester waits a full 24 hours before checking that stream again.
#   - Deletes any untracked orphan files inside clip_vault/.
# ==============================================================================
def _mark_clip_deleted_for_revisit(history_data, item):
    vid_id = str(item.get("video_id", "")).strip()
    if not vid_id or vid_id in history_data.get("exhausted_ids", []):
        return
    rpool = history_data.setdefault("revisit_pool", {})
    entry = rpool.setdefault(vid_id, {
        "title": item.get("source_title", "GTA 6 Gameplay"),
        "channel_name": item.get("channel_name", "GTA 6 Creator"),
        "duration": float(item.get("source_total_dur", 1200)),
        "checked_ranges": [],
    })
    st = float(item.get("source_start_sec", 0.0))
    en = st + float(item.get("duration_sec", 28.0))
    entry["checked_ranges"].append([round(st, 1), round(en, 1)])
    entry["last_deleted_at"] = datetime.now(timezone.utc).isoformat()


def prune_vault_and_temp_files(history_data, max_age_hours=36):
    max_vault_items = int(CHANNEL_CONFIG.get("max_vault_items", 10))
    now_utc = datetime.now(timezone.utc)
    valid_vault = []
    seen_video_ids = set()

    for item in history_data.get("vault_queue", []):
        vp = item.get("vault_path", "")
        vid_id = str(item.get("video_id", "")).strip()
        if not vp or not os.path.exists(vp):
            _mark_clip_deleted_for_revisit(history_data, item)
            continue

        # Enforce max 1 active clip per stream in vault
        if vid_id and vid_id in seen_video_ids:
            try:
                os.remove(vp)
            except Exception:
                pass
            continue

        ch_name = str(item.get("channel_name", "")).strip()
        if not ch_name or ch_name.lower() == "gta 6 creator" or is_compilation_or_clipper(ch_name, item.get("source_title", "")):
            try:
                os.remove(vp)
            except Exception:
                pass
            continue

        raw_ts = str(item.get("harvested_at", "")).strip()
        if raw_ts:
            try:
                h_dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if (now_utc - h_dt).total_seconds() > max_age_hours * 3600:
                    try:
                        os.remove(vp)
                    except Exception:
                        pass
                    _mark_clip_deleted_for_revisit(history_data, item)
                    continue
            except Exception:
                pass

        if vid_id:
            seen_video_ids.add(vid_id)
        valid_vault.append(item)

    valid_vault.sort(key=lambda x: float(x.get("viral_score", 0.0)), reverse=True)
    keep = valid_vault[:max_vault_items]
    discard = valid_vault[max_vault_items:]
    for old_item in discard:
        vp = old_item.get("vault_path", "")
        if vp and os.path.exists(vp):
            try:
                os.remove(vp)
            except Exception:
                pass
        _mark_clip_deleted_for_revisit(history_data, old_item)

    history_data["vault_queue"] = keep

    tracked_paths = {os.path.abspath(v["vault_path"]) for v in keep if v.get("vault_path")}
    vault_dir = CHANNEL_CONFIG.get("vault_dir", "clip_vault")
    if os.path.isdir(vault_dir):
        for fname in os.listdir(vault_dir):
            fpath = os.path.abspath(os.path.join(vault_dir, fname))
            if os.path.isfile(fpath) and fpath not in tracked_paths:
                try:
                    os.remove(fpath)
                except Exception:
                    pass

# ==============================================================================
# PART 15 OF 40: YPP MONETIZATION & TRANSFORMATIVE VALUE GUARD
# Subheading: Ensures Multi-Creator Pacing, Single Intro/Outro & Full Commentary
# What This Code Does:
#   - Validates that the rendered plan includes an opening intro hook, per-clip
#     commentary for every single clip rank, a closing outro + CTA, and on-screen
#     creator credits so every video meets YouTube Partner Program standards.
# ==============================================================================
def validate_monetization_eligibility(short_plan, script_data):
    segments = short_plan.get("segments", [])
    if not segments:
        return False
    clip_vos = script_data.get("clip_vos", {})
    if not str(script_data.get("intro_vo", "")).strip():
        return False
    if not str(script_data.get("outro_vo", "")).strip():
        return False
    for seg in segments:
        if not str(clip_vos.get(str(seg["rank"]), "")).strip():
            return False
    return True

# ==============================================================================
# PART 16 OF 40: CREATOR & STREAM METADATA SANITIZER
# Subheading: Cleans Uploader Names & Stream Titles For Safe On-Screen Text
# What This Code Does:
#   - Strips emojis, URLs, hashtags, and unsupported symbols from creator names
#     and stream titles so FFmpeg drawtext never fails or displays garbled text.
# ==============================================================================
def sanitize_creator_and_title(raw_channel, raw_title):
    ch = re.sub(r"[^\w\s\-\.\&]", "", str(raw_channel or "GTA 6 Creator")).strip()
    ch = re.sub(r"\s+", " ", ch)[:32] or "GTA 6 Creator"

    title = re.sub(r"http\S+|#\S+", "", str(raw_title or "GTA 6 Gameplay")).strip()
    title = re.sub(r"\s+", " ", title)[:90] or "GTA 6 Gameplay"
    return ch, title

# ==============================================================================
# PART 17 OF 40: MULTI-CLIP COUNTDOWN PLANNER (AVOIDS RAW SOLO CLIPS)
# Subheading: Combines 2 To 5 Distinct Creators Into Tight #N -> #1 Countdowns
# What This Code Does:
#   - Prioritizes combining 2 to 5 distinct-creator clips into a fast-paced
#     #N down to #1 countdown (11s-16s per clip) so every Short looks heavily
#     edited and transformative for YouTube Partner Program monetization.
#   - Defers publishing when fewer than 2 distinct creators are ready (unless
#     an elite 9.5+ gem has waited > 20 hours).
# ==============================================================================
def build_top5_plan_from_vault(history_data):
    vault = [
        item for item in history_data.get("vault_queue", [])
        if item.get("vault_path") and os.path.exists(item["vault_path"])
    ]
    if not vault:
        return None

    is_test_mode = not bool(CHANNEL_CONFIG.get("upload_to_youtube", False))
    if is_test_mode:
        untested = [v for v in vault if int(v.get("test_rendered_count", 0)) == 0]
        if not untested:
            print("[Test Standby] All clips in vault were already test-rendered. Waiting for new harvest.", flush=True)
            return None
        vault = untested

    random.shuffle(vault)
    vault.sort(
        key=lambda x: (
            int(x.get("test_rendered_count", 0)),
            -float(x.get("viral_score", 0.0)),
        )
    )

    distinct_best = []
    seen_creators = set()
    for item in vault:
        ch_key = str(item.get("channel_name", "")).strip().lower()
        if not ch_key or ch_key in seen_creators:
            continue
        seen_creators.add(ch_key)
        distinct_best.append(item)
        if len(distinct_best) >= int(CHANNEL_CONFIG["max_clips_per_short"]):
            break

    if not distinct_best:
        return None

    min_req = int(CHANNEL_CONFIG.get("min_clips_per_short", 2))
    top_item = next(iter(distinct_best))

    # Avoid raw solo clips unless a 9.5+ gem waited > 20 hours
    if len(distinct_best) < min_req:
        waited_long = False
        if float(top_item.get("viral_score", 0.0)) >= 9.5 and top_item.get("harvested_at"):
            try:
                h_dt = datetime.fromisoformat(top_item["harvested_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - h_dt).total_seconds() >= 20 * 3600:
                    waited_long = True
            except Exception:
                pass
        if not waited_long:
            print(
                f"[Planner Standby] Only {len(distinct_best)} creator ready; waiting for >= {min_req} creators to avoid solo clip.",
                flush=True,
            )
            return None
        chosen = [top_item]
    else:
        # Order lowest rank first -> #1 climax last
        chosen = list(reversed(distinct_best))

    count = len(chosen)
    target_total = 36.0 if count == 1 else 55.5
    per_clip_budget = target_total / count

    segments = []
    for idx, item in enumerate(chosen):
        vpath = item["vault_path"]
        avail_d = get_media_duration(vpath)
        if avail_d <= 3.0:
            continue

        use_dur = min(avail_d, per_clip_budget)
        peak_local = float(item.get("local_peak_sec", avail_d / 2.0))
        start_s = max(0.0, min(avail_d - use_dur, peak_local - use_dur * 0.42))
        clean_ch, clean_title = sanitize_creator_and_title(
            item.get("channel_name"), item.get("source_title")
        )

        segments.append({
            "rank": count - idx,
            "video_path": vpath,
            "start_sec": round(start_s, 2),
            "end_sec": round(start_s + use_dur, 2),
            "duration": round(use_dur, 2),
            "channel_name": clean_ch,
            "source_title": clean_title,
            "viral_score": float(item.get("viral_score", 7.8)),
            "transcript_en": item.get("transcript_en", ""),
            "word_events": item.get("word_events", []),
            "vault_item": item,
        })

    if not segments:
        return None

    final_count = len(segments)
    for idx, seg in enumerate(segments):
        seg["rank"] = final_count - idx

    print(
        f"[Vault Plan] Built {final_count}-clip Short (#{final_count} down to #1).",
        flush=True,
    )
    return {"segments": segments, "is_solo": final_count == 1}

# ==============================================================================
# PART 18 OF 40: EDGE-TTS NEURAL VOICEOVER SYNTHESIZER
# Subheading: Generates Crisp English Commentary Audio Tracks With Voice Fallback
# What This Code Does:
#   - Synthesizes natural English voiceovers for the single opening intro,
#     every individual clip commentary line, and the single closing outro + CTA.
#   - Automatically falls back across backup neural voices if one fails.
# ==============================================================================
async def _edge_tts_save_async(text, out_mp3, voice):
    communicate = edge_tts.Communicate(text, voice, rate="+9%")
    await communicate.save(out_mp3)


def synthesize_voiceover_mp3(text, out_mp3):
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean_text:
        return 0.0
    if os.path.exists(out_mp3):
        os.remove(out_mp3)

    voices = (
        CHANNEL_CONFIG.get("tts_voice", "en-US-GuyNeural"),
        "en-US-ChristopherNeural",
        "en-US-AndrewNeural",
    )
    for v in voices:
        try:
            asyncio.run(_edge_tts_save_async(clean_text, out_mp3, v))
            dur = get_media_duration(out_mp3)
            if dur > 0.35:
                return dur
        except Exception as e:
            print(f"[TTS Warning] {v}: {e}", flush=True)
            time.sleep(0.4)
    return 0.0

# ==============================================================================
# PART 19 OF 40: AI CREATOR SCRIPTWRITER (SINGLE INTRO + PER-CLIP + OUTRO CTA)
# Subheading: Writes 1 Opening Intro, Commentary For Every Clip & 1 Outro CTA
# What This Code Does:
#   - Inspects gameplay keyframes and uses the AI Brain's winning patterns &
#     hook styles to write a human-sounding creator script.
#   - Generates:
#       1) intro_vo: ONE punchy opening hook at the very beginning of the video.
#       2) clip_vos: Specific commentary for EVERY clip (#N down to #1).
#       3) outro_vo: ONE closing outro at the very end with a randomized, natural
#          Like & Subscribe call-to-action.
# ==============================================================================
RANDOM_LIKE_SUB_CTAS = (
    "Drop a like if number one caught you off guard, and subscribe for daily Vice City chaos!",
    "Which clip was your favorite? Hit that subscribe button so you never miss tomorrow's countdown!",
    "Smash like for that number one save, and subscribe to join our Vice City crew!",
    "Comment your favorite clip below, leave a like, and subscribe for more GTA 6 insanity!",
    "If you enjoyed this countdown, hit like and subscribe for fresh Vice City moments every day!",
)


def generate_scene_grounded_script(short_plan, history_data):
    segments = short_plan["segments"]
    count = len(segments)
    creators = [s["channel_name"] for s in segments]
    *_, top_seg = segments
    recent_titles = [u.get("title", "") for u in history_data.get("uploads", [])[-12:]]

    brain = history_data.get("ai_brain", {})
    winners = brain.get("winning_patterns", [])[:4]
    hook_styles = brain.get("winning_hook_styles", ["shock", "question", "countdown", "challenge"])
    chosen_style = random.choice(hook_styles) if hook_styles else "shock"
    chosen_cta = random.choice(RANDOM_LIKE_SUB_CTAS)

    frame_paths = []
    prompt_parts = []
    context_lines = []

    for idx, seg in enumerate(segments):
        mid_ts = (seg["start_sec"] + seg["end_sec"]) / 2.0
        for offset_i, sample_t in enumerate((max(0.2, mid_ts - 1.2), mid_ts + 0.8)):
            f_img = f"gem_ctx_{idx}_{offset_i}.jpg"
            try:
                run_cmd_safe(
                    [
                        "ffmpeg", "-y", "-ss", f"{sample_t:.2f}",
                        "-i", seg["video_path"], "-vframes", "1", "-q:v", "4",
                        f_img,
                    ],
                    label="GeminiFrame",
                    timeout_sec=20,
                )
                if os.path.exists(f_img):
                    frame_paths.append(f_img)
                    prompt_parts.append(Image.open(f_img))
            except Exception:
                pass

        context_lines.append(
            f"- Clip #{seg['rank']} | Creator: {seg['channel_name']} | "
            f"Stream Title: {seg['source_title']} | "
            f"Translated Dialogue: '{seg['transcript_en'] or 'intense gameplay reaction'}'"
        )

    rank_keys_desc = ", ".join(f'"{s["rank"]}"' for s in segments)
    banner_example = f"TOP {count} VICE CITY MOMENTS" if count > 1 else "CRAZIEST VICE CITY MOMENT"

    prompt_text = (
        f"You are the creative director for {CHANNEL_CONFIG['channel_name']} "
        f"({CHANNEL_CONFIG['channel_handle']}), a Grand Theft Auto VI Shorts channel.\n"
        f"Use a '{chosen_style}' hook style. Model energy after these channel winners: {winners}\n"
        f"Inspect the attached gameplay frames and clip details:\n"
        + "\n".join(context_lines)
        + f"\nAvoid repeating any of these recent titles: {recent_titles}\n\n"
        f"Return STRICT valid JSON with these exact 6 keys:\n"
        f'1. "title": Catchy English YouTube Shorts title describing the wildest moment (max 68 chars, 1 emoji, no #Shorts tag).\n'
        f'2. "banner": Short 3-to-5 word UPPERCASE top banner (e.g. "{banner_example}").\n'
        f'3. "intro_vo": ONE punchy 6-to-8 word opening hook spoken ONLY once at the very start of the video (do NOT repeat in clip_vos).\n'
        f'4. "clip_vos": JSON object mapping EVERY clip rank ({rank_keys_desc}) to a crisp 7-to-10 word English commentary line reacting to that exact clip (from #{count} down to #1, no intro/outro greetings).\n'
        f'5. "outro_vo": ONE punchy 10-to-14 word closing outro spoken ONLY once at the very end that naturally weaves in this CTA: "{chosen_cta}"\n'
        f'6. "tags": List of 8 relevant English tags including "GTA 6", "Grand Theft Auto 6", "Vice City", and creator names.\n'
    )
    prompt_parts.append(prompt_text)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=45000),
        )
        for model_id in ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-pro"):
            try:
                resp = client.models.generate_content(
                    model=model_id,
                    contents=prompt_parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.80,
                    ),
                )
                parsed = json.loads(resp.text.strip())
                if (
                    parsed.get("title")
                    and parsed.get("intro_vo")
                    and parsed.get("clip_vos")
                    and parsed.get("outro_vo")
                ):
                    parsed["creators"] = creators
                    parsed["hook_style"] = chosen_style
                    print(f"[Gemini Creator Brain] Script ({chosen_style}) via {model_id}: '{parsed['title']}'", flush=True)
                    for fp in frame_paths:
                        if os.path.exists(fp):
                            os.remove(fp)
                    return parsed
            except Exception as e:
                print(f"[Gemini Warning] {model_id}: {e}", flush=True)

    for fp in frame_paths:
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass

    top_creator = top_seg["channel_name"]
    fallback_vos = {}
    for s in segments:
        r = str(s["rank"])
        if s["rank"] == 1:
            fallback_vos[r] = f"Number 1 goes to {s['channel_name']} for pulling off the impossible!"
        else:
            fallback_vos[r] = f"At number {r}, watch {s['channel_name']} survive pure Vice City chaos!"

    return {
        "title": (f"Top {count} Craziest GTA 6 Moments Featuring {top_creator} 🔥" if count > 1 else f"{top_creator} Had The Wildest Moment In Vice City 😱")[:75],
        "banner": f"TOP {count} VICE CITY MOMENTS" if count > 1 else "CRAZIEST VICE CITY MOMENT",
        "intro_vo": f"Here are the top {count} wildest GTA 6 moments today!" if count > 1 else "Watch how fast Vice City spirals out of control!",
        "clip_vos": fallback_vos,
        "outro_vo": chosen_cta,
        "tags": ["GTA 6", "Grand Theft Auto 6", "Vice City", "GTA VI", "Shorts"] + creators[:3],
        "creators": creators,
        "hook_style": chosen_style,
    }

# ==============================================================================
# PART 20 OF 40: ASS WORD-SYNCED KINETIC SUBTITLE BUILDER
# Subheading: Generates High-Contrast Yellow Karaoke Captions For All Voiceovers
# What This Code Does:
#   - Splits the spoken intro, per-clip commentary, and closing outro into
#     4-word kinetic subtitle chunks timed precisely to the voiceover audio.
# ==============================================================================
def _fmt_ass_time(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass_subtitles_file(vo_tracks, ass_path="captions.ass"):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,60,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for tr in vo_tracks:
        txt = re.sub(r"\s+", " ", str(tr.get("text", ""))).strip().upper()
        st = float(tr.get("start", 0.0))
        en = float(tr.get("end", st + 1.0))
        if not txt or en <= st + 0.1:
            continue

        words = txt.split(" ")
        chunks = [" ".join(words[i : i + 4]) for i in range(0, len(words), 4)]
        span = (en - st) / max(1, len(chunks))
        for idx, ch in enumerate(chunks):
            c_st = st + idx * span
            c_en = st + (idx + 1) * span
            safe_ch = ch.replace("{", "").replace("}", "")
            events.append(
                f"Dialogue: 0,{_fmt_ass_time(c_st)},{_fmt_ass_time(c_en)},Default,,0,0,0,,{safe_ch}"
            )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")
    return ass_path

# ==============================================================================
# PART 21 OF 40: VERTICAL LAYOUT & RANDOMIZED TRANSITION RENDERER
# Subheading: Renders Each Clip With Accurate #N Rank Badge & Random Transitions
# What This Code Does:
#   - Renders every clip in the countdown (#N down to #1) to 1080x1920 @ 30fps
#     using either verified streamer split-cam or blurred cinema framing.
#   - Stamps the exact rank badge (#5, #4, #3, #2, #1) and creator credit on
#     every clip so viewers always know which rank is playing.
#   - Randomly selects smooth FFmpeg xfade transitions between clips so cuts
#     never look repetitive or robotic.
# ==============================================================================
def _escape_drawtext(txt):
    return (
        str(txt or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


RANDOM_XFADE_STYLES = (
    "fadewhite",
    "wipeleft",
    "slideright",
    "wipeup",
    "slidedown",
    "circleopen",
    "smoothleft",
    "radial",
)


def render_vertical_segments_and_concat(short_plan):
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    segments = short_plan["segments"]
    is_multi = len(segments) > 1
    xfade_dur = 0.35

    raw_durs = [float(s["duration"]) for s in segments]
    est_total = sum(raw_durs) - xfade_dur * max(0, len(segments) - 1)
    max_target_sec = 57.5
    scale_ratio = (max_target_sec / est_total) if est_total > max_target_sec else 1.0

    rendered_files = []
    seg_durations = []

    for idx, seg in enumerate(segments):
        seg_out = f"seg_render_{idx}.mp4"
        if os.path.exists(seg_out):
            os.remove(seg_out)

        start_s = float(seg["start_sec"])
        dur_s = round(max(6.5, float(seg["duration"]) * scale_ratio), 2)
        creator_label = _escape_drawtext(f"Credit: {seg['channel_name']}")
        wm_label = _escape_drawtext(CHANNEL_CONFIG["watermark_text"])
        rank_num = int(seg["rank"])

        cam_box = detect_verified_webcam_box(seg["video_path"], start_s, start_s + dur_s)

        if cam_box is not None:
            bx, by, bw, bh = cam_box
            layout_mode = "SPLITCAM_FACE"
            base_graph = (
                f"[0:v]split=2[v_game][v_cam];"
                f"[v_cam]crop={bw}:{bh}:{bx}:{by},scale=1080:614:force_original_aspect_ratio=increase,crop=1080:614[top_cam];"
                f"[v_game]scale=1080:1306:force_original_aspect_ratio=increase,crop=1080:1306[bot_game];"
                f"[top_cam][bot_game]vstack=inputs=2,"
                f"drawbox=x=0:y=611:w=1080:h=6:color=#00E5FF@0.90:t=fill[canvas]"
            )
        else:
            layout_mode = "CINEMA_BLUR"
            base_graph = (
                f"[0:v]split=2[v_bg][v_fg];"
                f"[v_bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:6,eq=brightness=-0.16[bg];"
                f"[v_fg]scale=1080:608:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                f"drawbox=x=0:y=650:w=1080:h=5:color=#00E5FF@0.90:t=fill,"
                f"drawbox=x=0:y=1265:w=1080:h=5:color=#FF2A85@0.90:t=fill[canvas]"
            )

        text_chain = (
            f"[canvas]drawtext=fontfile={font_bold}:text='{wm_label}':"
            f"fontsize=26:fontcolor=white@0.88:x=(w-text_w)/2:y=56:"
            f"shadowcolor=black@0.85:shadowx=2:shadowy=2,"
            f"drawtext=fontfile={font_bold}:text='{creator_label}':"
            f"fontsize=25:fontcolor=#FFD54F@0.95:x=(w-text_w)/2:y=94:"
            f"shadowcolor=black@0.90:shadowx=2:shadowy=2"
        )

        if is_multi:
            badge_color = "#FF1744" if rank_num == 1 else "#00E5FF"
            text_chain += (
                f",drawbox=x=42:y=226:w=118:h=58:color={badge_color}@0.92:t=fill,"
                f"drawtext=fontfile={font_bold}:text='#{rank_num}':"
                f"fontsize=36:fontcolor=white:x=68:y=238:"
                f"shadowcolor=black@0.85:shadowx=2:shadowy=2"
            )

        text_chain += "[vout]"
        full_fc = f"{base_graph};{text_chain}"

        run_cmd_safe(
            [
                "ffmpeg", "-y",
                "-ss", f"{start_s:.2f}", "-t", f"{dur_s:.2f}",
                "-i", seg["video_path"],
                "-filter_complex", full_fc,
                "-map", "[vout]", "-map", "0:a?",
                "-r", "30",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                seg_out,
            ],
            label=f"RenderSeg_{idx}",
            timeout_sec=140,
        )

        actual_d = get_media_duration(seg_out)
        rendered_files.append(seg_out)
        seg_durations.append(actual_d)
        print(f"[Segment #{rank_num}] Rendered {seg['channel_name']} ({layout_mode}, {actual_d:.1f}s)", flush=True)

    stitched_out = "stitched_video.mp4"
    if os.path.exists(stitched_out):
        os.remove(stitched_out)

    first_file, *rest_files = rendered_files
    first_dur, *rest_durs = seg_durations
    first_seg, *rest_segs = segments

    timeline_meta = [
        {
            **first_seg,
            "offset_start": 0.0,
            "offset_end": round(first_dur, 3),
            "rendered_dur": round(first_dur, 3),
        }
    ]

    if not rest_files:
        shutil.copy2(first_file, stitched_out)
        return stitched_out, timeline_meta

    cmd = ["ffmpeg", "-y"]
    for rf in rendered_files:
        cmd.extend(["-i", rf])

    fc_parts = []
    cum_dur = first_dur
    prev_v = "[0:v]"
    prev_a = "[0:a]"

    for idx_offset, (cur_dur, cur_seg) in enumerate(zip(rest_durs, rest_segs), start=1):
        offset_t = max(0.5, round(cum_dur - xfade_dur, 3))
        t_style = random.choice(RANDOM_XFADE_STYLES)
        is_last = idx_offset == len(rest_files)
        out_v = "[vfinal]" if is_last else f"[vx{idx_offset}]"
        out_a = "[afinal]" if is_last else f"[ax{idx_offset}]"

        fc_parts.append(
            f"{prev_v}[{idx_offset}:v]xfade=transition={t_style}:duration={xfade_dur}:offset={offset_t}{out_v}"
        )
        fc_parts.append(
            f"{prev_a}[{idx_offset}:a]acrossfade=d={xfade_dur}:c1=tri:c2=tri{out_a}"
        )

        new_end = round(offset_t + cur_dur, 3)
        timeline_meta.append({
            **cur_seg,
            "offset_start": offset_t,
            "offset_end": new_end,
            "rendered_dur": round(cur_dur, 3),
        })
        cum_dur = new_end
        prev_v = out_v
        prev_a = out_a

    run_cmd_safe(
        cmd + [
            "-filter_complex", ";".join(fc_parts),
            "-map", "[vfinal]", "-map", "[afinal]",
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            stitched_out,
        ],
        label="XfadeStitchSegments",
        timeout_sec=140,
    )
    print(f"[Stitched Master] Total runtime={cum_dur:.2f}s across {len(rendered_files)} clips (random transitions).", flush=True)
    return stitched_out, timeline_meta

# ==============================================================================
# PART 22 OF 40: TOP BANNER HEADER OVERLAY GENERATOR
# Subheading: Builds High-Contrast Gold & Black Top Title Card For Shorts
# What This Code Does:
#   - Draws a clean gold-bordered header bar at the top of the 1080x1920 frame
#     displaying the Short's headline banner without blocking gameplay.
# ==============================================================================
def build_top_banner_filter(banner_text):
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    safe_banner = _escape_drawtext(str(banner_text or "VICE CITY MOMENTS").upper()[:34])
    return (
        f"drawbox=x=90:y=142:w=900:h=68:color=black@0.78:t=fill,"
        f"drawbox=x=90:y=142:w=900:h=4:color=#FFD54F@0.95:t=fill,"
        f"drawbox=x=90:y=206:w=900:h=4:color=#FFD54F@0.95:t=fill,"
        f"drawtext=fontfile={font_bold}:text='{safe_banner}':"
        f"fontsize=34:fontcolor=#FFD54F:x=(w-text_w)/2:y=160:"
        f"shadowcolor=black@0.90:shadowx=2:shadowy=2"
    )

# ==============================================================================
# PART 23 OF 40: SINGLE-INTRO + PER-CLIP + SINGLE-OUTRO AUDIO TIMELINE MIXER
# Subheading: Plays 1 Intro At Start, Commentary On Every Clip & 1 Outro At End
# What This Code Does:
#   - Places ONE opening intro (intro_vo) at 0.25s of the video (never repeated
#     on later clips).
#   - Places specific commentary (clip_vos) on EVERY clip (#N down to #1),
#     shifting Clip 1's commentary right after the single opening intro finishes.
#   - Places ONE closing outro with randomized Like & Subscribe CTA (outro_vo)
#     at the very end of the video, ducking game audio smoothly during speech.
# ==============================================================================
def build_voiceover_tracks_and_mix(stitched_video, timeline_meta, script_data):
    total_dur = get_media_duration(stitched_video)
    clip_vos = script_data.get("clip_vos", {})
    vo_tracks = []

    # 1. Single Opening Intro at the very beginning ONLY
    intro_end_cursor = 0.30
    intro_txt = str(script_data.get("intro_vo", "")).strip()
    if intro_txt and total_dur >= 6.0:
        intro_mp3 = "vo_seg_intro.mp3"
        idur = synthesize_voiceover_mp3(intro_txt, intro_mp3)
        if idur > 0.35 and os.path.exists(intro_mp3):
            i_start = 0.25
            i_end = round(min(total_dur * 0.22, i_start + idur), 2)
            if i_end > i_start + 0.3:
                vo_tracks.append({
                    "path": intro_mp3,
                    "start": i_start,
                    "end": i_end,
                    "text": intro_txt,
                })
                intro_end_cursor = round(i_end + 0.35, 2)

    # 2. Per-Clip Commentary on EVERY clip (#N -> #1)
    for idx, seg in enumerate(timeline_meta):
        rank_key = str(seg["rank"])
        vo_line = str(clip_vos.get(rank_key, "")).strip()
        if not vo_line:
            continue
        mp3_path = f"vo_seg_{idx}.mp3"
        vdur = synthesize_voiceover_mp3(vo_line, mp3_path)
        if vdur > 0.35 and os.path.exists(mp3_path):
            seg_start = float(seg["offset_start"])
            seg_end = float(seg["offset_end"])
            # First clip waits until the single opening intro finishes
            v_start = round(max(seg_start + 0.35, intro_end_cursor if idx == 0 else seg_start + 0.35), 2)
            max_end = (total_dur - 4.3) if idx == len(timeline_meta) - 1 else (seg_end - 0.25)
            v_end = round(min(max_end, v_start + vdur), 2)
            if v_end > v_start + 0.3:
                vo_tracks.append({
                    "path": mp3_path,
                    "start": v_start,
                    "end": v_end,
                    "text": vo_line,
                })

    # 3. Single Closing Outro + Randomized Like/Subscribe CTA at the very end ONLY
    outro_txt = str(script_data.get("outro_vo", "")).strip()
    if outro_txt and total_dur >= 8.0:
        outro_mp3 = "vo_seg_outro.mp3"
        odur = synthesize_voiceover_mp3(outro_txt, outro_mp3)
        if odur > 0.35 and os.path.exists(outro_mp3):
            last_speech_end = max((tr["end"] for tr in vo_tracks), default=1.0)
            o_start = round(max(last_speech_end + 0.25, total_dur - min(odur + 0.35, 4.1)), 2)
            o_end = round(min(total_dur - 0.1, o_start + odur), 2)
            if o_end > o_start + 0.3:
                vo_tracks.append({
                    "path": outro_mp3,
                    "start": o_start,
                    "end": o_end,
                    "text": outro_txt,
                })

    mixed_wav = "mixed_master_audio.m4a"
    if os.path.exists(mixed_wav):
        os.remove(mixed_wav)

    if not vo_tracks:
        run_cmd_safe(
            ["ffmpeg", "-y", "-i", stitched_video, "-vn", "-c:a", "aac", "-b:a", "192k", mixed_wav],
            label="ExtractBaseAudio",
            timeout_sec=60,
        )
        return mixed_wav, []

    cmd = ["ffmpeg", "-y", "-i", stitched_video]
    for tr in vo_tracks:
        cmd.extend(["-i", tr["path"]])

    duck_conds = "+".join(f"between(t,{tr['start']},{tr['end']})" for tr in vo_tracks)
    fc_lines = [f"[0:a]volume='if(gt({duck_conds},0),0.40,0.92)':eval=frame[g_audio]"]

    mix_labels = ["[g_audio]"]
    for i, tr in enumerate(vo_tracks, start=1):
        delay_ms = int(round(tr["start"] * 1000))
        lbl = f"[vo{i}]"
        fc_lines.append(f"[{i}:a]adelay={delay_ms}|{delay_ms},volume=1.35{lbl}")
        mix_labels.append(lbl)

    fc_lines.append(
        f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    run_cmd_safe(
        cmd + [
            "-filter_complex", ";".join(fc_lines),
            "-map", "[aout]",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            mixed_wav,
        ],
        label="MixDuckAudio",
        timeout_sec=90,
    )
    return mixed_wav, vo_tracks

# ==============================================================================
# PART 24 OF 40: FINAL MASTER SHORT VIDEO & CAPTION COMPOSITOR
# Subheading: Burns Top Banner & Word-Synced Subtitles Into Final 1080x1920 MP4
# What This Code Does:
#   - Combines the stitched vertical video, mixed commentary + ducked game
#     audio, top title banner, and yellow karaoke captions into the final Short.
# ==============================================================================
def compose_final_short_with_captions_and_audio(stitched_video, timeline_meta, script_data, output_path):
    if os.path.exists(output_path):
        os.remove(output_path)

    mixed_audio, vo_tracks = build_voiceover_tracks_and_mix(
        stitched_video, timeline_meta, script_data
    )
    ass_file = build_ass_subtitles_file(vo_tracks, ass_path="captions.ass")
    banner_fc = build_top_banner_filter(script_data.get("banner", "VICE CITY MOMENTS"))

    vf_chain = f"{banner_fc},ass={ass_file}"

    run_cmd_safe(
        [
            "ffmpeg", "-y",
            "-i", stitched_video,
            "-i", mixed_audio,
            "-vf", vf_chain,
            "-map", "0:v",
            "-map", "1:a",
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_path,
        ],
        label="ComposeFinalShort",
        timeout_sec=140,
    )
    return output_path

# ==============================================================================
# PART 25 OF 40: HIGH-CTR 1280X720 LONG-FORM THUMBNAIL & COVER GENERATOR
# Subheading: Designs Click-Worthy Custom Thumbnails With Contrast, Badge & Text
# What This Code Does:
#   - For 16:9 Long-Form Videos (required for click-through rate): extracts the
#     sharpest action frame near the #1 climax, boosts color & contrast, adds a
#     cinematic border + dark bottom gradient, stamps a bold red #1 / TOP badge
#     and heavy black-stroked gold/white hook text, and saves 1280x720 JPG.
#   - Also exports a vertical preview frame for Shorts backup artifacts.
# ==============================================================================
from PIL import ImageDraw, ImageFont, ImageEnhance


def generate_high_ctr_long_thumbnail(video_path, hook_headline="IMPOSSIBLE ESCAPE?!", badge_text="TOP GTA 6", out_jpg="thumb_long_16x9.jpg"):
    dur = get_media_duration(video_path)
    if dur <= 2.0:
        return None

    # Sample 3 frames near the #1 climax (last 18% of countdown) and pick sharpest
    best_frame_path = None
    best_sharpness = -1.0
    for idx, ratio in enumerate((0.84, 0.90, 0.95)):
        cand_jpg = f"thumb_cand_{idx}.jpg"
        run_cmd_safe(
            ["ffmpeg", "-y", "-ss", f"{max(1.0, dur * ratio):.2f}", "-i", video_path, "-vframes", "1", "-q:v", "2", cand_jpg],
            label=f"ThumbCand_{idx}",
            timeout_sec=20,
            check=False,
        )
        if os.path.exists(cand_jpg):
            img_cv = cv2.imread(cand_jpg, cv2.IMREAD_GRAYSCALE)
            if img_cv is not None:
                sharp = float(cv2.Laplacian(img_cv, cv2.CV_64F).var())
                if sharp > best_sharpness:
                    best_sharpness = sharp
                    best_frame_path = cand_jpg

    if not best_frame_path or not os.path.exists(best_frame_path):
        return None

    try:
        im = Image.open(best_frame_path).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
        # Boost vibrancy and contrast so thumbnail pops on mobile & desktop feeds
        im = ImageEnhance.Color(im).enhance(1.32)
        im = ImageEnhance.Contrast(im).enhance(1.18)
        im = ImageEnhance.Sharpness(im).enhance(1.25)

        draw = ImageDraw.Draw(im, "RGBA")
        # Dark bottom gradient for readable hook text
        for y in range(430, 720):
            alpha = int(215 * ((y - 430) / 290.0))
            draw.rectangle([(0, y), (1280, y + 1)], fill=(0, 0, 0, alpha))

        # Bright gold-and-red outer border frame
        draw.rectangle([(0, 0), (1279, 719)], outline=(255, 213, 79, 255), width=10)

        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        try:
            font_badge = ImageFont.truetype(font_path, 42)
            font_main = ImageFont.truetype(font_path, 82)
        except Exception:
            font_badge = ImageFont.load_default()
            font_main = ImageFont.load_default()

        # Top-left red pill badge
        b_clean = str(badge_text or "TOP GTA 6").upper()[:18]
        draw.rectangle([(36, 34), (380, 102)], fill=(230, 25, 45, 245), outline=(255, 255, 255, 255), width=4)
        draw.text((56, 46), b_clean, font=font_badge, fill=(255, 255, 255, 255))

        # Bold bottom stroked hook headline
        h_clean = str(hook_headline or "WHAT JUST HAPPENED?!").upper()[:24]
        bbox = draw.textbbox((0, 0), h_clean, font=font_main)
        tw = bbox[2] - bbox[0]
        tx = max(40, (1280 - tw) // 2)
        ty = 585
        draw.text(
            (tx, ty),
            h_clean,
            font=font_main,
            fill=(255, 224, 65, 255),
            stroke_width=7,
            stroke_fill=(0, 0, 0, 255),
        )

        im.save(out_jpg, "JPEG", quality=94)
        print(f"[Long Thumbnail] Generated 1280x720 high-CTR cover: '{h_clean}'", flush=True)
        return out_jpg if os.path.exists(out_jpg) else None
    finally:
        for idx in range(3):
            fn = f"thumb_cand_{idx}.jpg"
            if os.path.exists(fn):
                try:
                    os.remove(fn)
                except Exception:
                    pass


def export_short_cover_thumbnail(video_path, out_jpg="short_cover.jpg"):
    dur = get_media_duration(video_path)
    if dur <= 1.0:
        return None
    snap_t = round(max(1.0, dur * 0.78), 2)
    run_cmd_safe(
        ["ffmpeg", "-y", "-ss", f"{snap_t:.2f}", "-i", video_path, "-vframes", "1", "-q:v", "2", out_jpg],
        label="ExportCoverThumb",
        timeout_sec=25,
        check=False,
    )
    return out_jpg if os.path.exists(out_jpg) else None

# ==============================================================================
# PART 26 OF 40: YOUTUBE DATA API V3 OAUTH CREDENTIAL LOADER
# Subheading: Builds & Auto-Refreshes Authenticated YouTube Client From Secrets
# What This Code Does:
#   - Reads YOUTUBE_TOKEN_JSON from GitHub repository secrets, refreshes the
#     OAuth access token automatically when expired, and returns a live YouTube
#     Data API v3 service object for uploads, thumbnails, and analytics.
# ==============================================================================
def get_youtube_service():
    raw_token = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not raw_token:
        raise RuntimeError("Missing YOUTUBE_TOKEN_JSON environment secret.")
    token_info = json.loads(raw_token)
    creds = Credentials.from_authorized_user_info(token_info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)

# ==============================================================================
# PART 27 OF 40: CREATOR ATTRIBUTION & SEO DESCRIPTION FORMATTER
# Subheading: Builds Monetization-Safe English Description With Creator Credits
# What This Code Does:
#   - Lists every featured original creator in order of appearance, summarizes
#     the countdown commentary, and includes high-intent GTA 6 / Vice City tags.
# ==============================================================================
def build_youtube_description(script_data, timeline_meta):
    seen = set()
    credit_lines = []
    for seg in timeline_meta:
        ch = str(seg.get("channel_name", "GTA 6 Creator")).strip()
        rank_num = seg.get("rank", "")
        if ch and ch.lower() not in seen:
            seen.add(ch.lower())
            prefix = f"#{rank_num} - " if len(timeline_meta) > 1 and rank_num else ""
            credit_lines.append(f"• {prefix}{ch}")

    credits_block = "\n".join(credit_lines) if credit_lines else "• Featured GTA 6 Creators"
    is_solo = len(timeline_meta) == 1
    mode_summary = (
        "Featuring an intense Vice City highlight with custom vertical framing, "
        "original commentary, and synced English subtitles."
        if is_solo
        else f"Counting down the Top {len(timeline_meta)} wildest GTA 6 Vice City moments with "
        "custom split-screen reactions, original commentary, and synced captions."
    )

    return (
        f"{script_data.get('title', 'GTA 6 Vice City Highlights')}\n\n"
        f"{mode_summary}\n\n"
        f"Featured Creators (Full Credit):\n{credits_block}\n\n"
        f"Subscribe to {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) "
        f"for daily GTA 6 countdowns & Vice City chaos!\n\n"
        f"#GTA6 #GrandTheftAuto6 #ViceCity #GTAVI #Shorts"
    )

# ==============================================================================
# PART 28 OF 40: YOUTUBE SHORTS UPLOADER & ADAPTIVE REACH SCHEDULER
# Subheading: Uploads Final Short & Schedules It At The Chosen IST Reach Slot
# What This Code Does:
#   - Publishes the rendered Short via YouTube Data API v3, attaches the
#     scheduled publishAt timestamp, and returns both the video URL and the
#     IST hour slot so the AI Brain can measure audience reach later.
# ==============================================================================
def upload_short_to_youtube(video_path, script_data, timeline_meta, publish_at_iso=None):
    youtube = get_youtube_service()

    raw_title = str(script_data.get("title", "Crazy GTA 6 Vice City Countdown")).strip()
    if "#shorts" not in raw_title.lower():
        final_title = f"{raw_title[:88]} #Shorts"
    else:
        final_title = raw_title[:98]

    description = build_youtube_description(script_data, timeline_meta)
    tags = [str(t).strip() for t in script_data.get("tags", []) if str(t).strip()][:12]

    status_body = {
        "privacyStatus": "private" if publish_at_iso else "public",
        "selfDeclaredMadeForKids": False,
    }
    if publish_at_iso:
        status_body["publishAt"] = publish_at_iso

    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": final_title,
                "description": description,
                "tags": tags,
                "categoryId": "20",
                "defaultLanguage": "en",
                "defaultAudioLanguage": "en",
            },
            "status": status_body,
        },
        media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True),
    )

    response = None
    while response is None:
        _, response = request.next_chunk()

    vid_id = response.get("id", "")
    return f"https://youtube.com{vid_id}"

# ==============================================================================
# PART 29 OF 40: SCRATCH RENDER & THUMBNAIL WORKSPACE CLEANER
# Subheading: Deletes Temporary Render Files So GitHub Runner & Repo Stay Clean
# What This Code Does:
#   - Removes intermediate video segments, TTS voiceover MP3s, Gemini frames,
#     and temporary thumbnail candidates after each run so nothing bloats git.
# ==============================================================================
def cleanup_scratch_render_files():
    prefixes = ("seg_render_", "vo_seg_", "gem_ctx_", "gem_watch_", "thumb_cand_", "raw_", "long_seg_", "long_vo_")
    exact_names = (
        "stitched_video.mp4",
        "mixed_master_audio.m4a",
        "captions.ass",
        "tmp_peak_scan.wav",
        "tmp_whisper_clip.wav",
        "long_stitched.mp4",
    )
    for fname in os.listdir("."):
        if fname in exact_names or any(fname.startswith(p) for p in prefixes):
            try:
                os.remove(fname)
            except Exception:
                pass

# ==============================================================================
# PART 30 OF 40: AUTONOMOUS AI BRAIN WEEKLY REVIEW & CADENCE EXPERIMENTER
# Subheading: Learns Peak Audience Hours, Best Shorts/Day & Best Longs/Week
# What This Code Does:
#   - Runs once every Sunday evening IST to evaluate real YouTube views, likes,
#     and comments across recent uploads.
#   - Learns which IST upload hours generate the highest reach and saves the
#     top 4 hours into ai_brain["top_hours_ist"] (80% exploitation / 20% explore).
#   - Experiments across weeks with Shorts/day (2, 3, or 4) and Longs/week
#     (1, 2, or 3) plus long-form weekdays, keeping whichever cadence wins
#     the highest average reach per video.
#   - Extracts winning titles and hook styles so Gemini continuously improves.
# ==============================================================================
def run_weekly_analytics_tuning(history_data):
    now_ist = datetime.now(IST_TZ)
    iso_year, iso_week, _ = now_ist.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"

    brain = history_data.setdefault("ai_brain", _default_ai_brain())
    if now_ist.weekday() != 6 or str(brain.get("last_tuned_week", "")) == week_key:
        return False

    uploads = history_data.get("uploads", [])
    vid_map = {}
    for u in uploads[-45:]:
        url = str(u.get("url", "")).strip()
        vid = ""
        if "/shorts/" in url:
            vid = url.split("/shorts/")[-1].split("?")[0].strip()
        elif "v=" in url:
            vid = url.split("v=")[-1].split("&")[0].strip()
        if len(vid) >= 8:
            vid_map[vid] = u

    if not vid_map:
        brain["last_tuned_week"] = week_key
        save_history(history_data)
        return False

    try:
        youtube = get_youtube_service()
        resp = youtube.videos().list(
            part="snippet,statistics",
            id=",".join(list(vid_map.keys())[:40]),
        ).execute()

        scored_videos = []
        hour_scores = {}
        style_scores = {}

        for item in resp.get("items", []):
            vid = item.get("id", "")
            snip = item.get("snippet", {})
            stats = item.get("statistics", {})
            views = int(stats.get("viewCount", 0) or 0)
            likes = int(stats.get("likeCount", 0) or 0)
            comments = int(stats.get("commentCount", 0) or 0)
            reach_score = views + (likes * 15) + (comments * 30)

            meta = vid_map.get(vid, {})
            pub_hr = meta.get("publish_hour_ist")
            if isinstance(pub_hr, int) and 0 <= pub_hr <= 23:
                hour_scores.setdefault(pub_hr, []).append(reach_score)

            h_style = str(meta.get("hook_style", "")).strip()
            if h_style:
                style_scores.setdefault(h_style, []).append(reach_score)

            scored_videos.append({
                "video_id": vid,
                "title": snip.get("title", ""),
                "type": meta.get("type", "short"),
                "views": views,
                "likes": likes,
                "comments": comments,
                "reach_score": reach_score,
            })

        # 1. Rank best audience reach hours (IST)
        if hour_scores:
            ranked_hours = sorted(
                hour_scores.items(),
                key=lambda kv: sum(kv[1]) / max(1, len(kv[1])),
                reverse=True,
            )
            brain["top_hours_ist"] = [hr for hr, _ in ranked_hours[:4]]

        # 2. Rank winning hook styles
        if style_scores:
            ranked_styles = sorted(
                style_scores.items(),
                key=lambda kv: sum(kv[1]) / max(1, len(kv[1])),
                reverse=True,
            )
            brain["winning_hook_styles"] = [st for st, _ in ranked_styles[:3]]

        # 3. Record this week's cadence performance & experiment with next week's cadence
        short_vids = [v for v in scored_videos if v["type"] != "long_16x9"]
        long_vids = [v for v in scored_videos if v["type"] == "long_16x9"]
        avg_short_reach = round(sum(v["reach_score"] for v in short_vids) / max(1, len(short_vids)), 1)
        avg_long_reach = round(sum(v["reach_score"] for v in long_vids) / max(1, len(long_vids)), 1)

        cur_shorts_day = int(brain.get("shorts_per_day", 3))
        cur_longs_wk = int(brain.get("longs_per_week", 2))
        exps = brain.setdefault("cadence_experiments", [])
        exps.append({
            "week": week_key,
            "shorts_per_day": cur_shorts_day,
            "longs_per_week": cur_longs_wk,
            "avg_short_reach": avg_short_reach,
            "avg_long_reach": avg_long_reach,
        })
        brain["cadence_experiments"] = exps[-12:]

        # Multi-armed bandit exploration: 70% pick best-performing cadence, 30% test adjacent cadence
        shorts_options = [2, 3, 4]
        long_options = [1, 2, 3]
        if len(exps) >= 2 and random.random() < 0.70:
            best_s = max(exps, key=lambda e: e.get("avg_short_reach", 0))
            best_l = max(exps, key=lambda e: e.get("avg_long_reach", 0))
            brain["shorts_per_day"] = int(best_s.get("shorts_per_day", 3))
            brain["longs_per_week"] = int(best_l.get("longs_per_week", 2))
        else:
            brain["shorts_per_day"] = random.choice(shorts_options)
            brain["longs_per_week"] = random.choice(long_options)

        # Pick optimal weekdays for long-form uploads based on longs_per_week
        lw = int(brain["longs_per_week"])
        if lw == 1:
            brain["long_weekdays"] = [6]          # Sunday
        elif lw == 2:
            brain["long_weekdays"] = random.choice([[2, 6], [3, 6], [4, 6]])  # Wed/Thu/Fri + Sun
        else:
            brain["long_weekdays"] = [1, 4, 6]    # Tue + Fri + Sun

        # 4. Save top 5 winning video titles for Gemini inspiration
        scored_videos.sort(key=lambda x: x["reach_score"], reverse=True)
        brain["winning_patterns"] = [
            f"{w['title']} ({w['views']} views, {w['likes']} likes)"
            for w in scored_videos[:5]
            if w.get("title")
        ]

        brain["last_tuned_week"] = week_key
        brain["longs_published_this_week"] = []
        save_history(history_data)
        print(
            f"[AI Brain Tuned {week_key}] top_hours={brain['top_hours_ist']}, "
            f"next_cadence={brain['shorts_per_day']} Shorts/day & {brain['longs_per_week']} Longs/week.",
            flush=True,
        )
        return True
    except Exception as e:
        print(f"[AI Brain Warning] {e}", flush=True)
        return False

# ==============================================================================
# PART 31 OF 40: WEEKLY 16:9 LONG-FORM SCHEDULE & CLIP COLLECTOR
# Subheading: Checks AI Brain Long-Form Cadence (2x/Week) & Gathers 8-9 Clips
# What This Code Does:
#   - Checks if today matches one of the AI Brain's target long-form weekdays
#     (e.g., Wednesday & Sunday) and hasn't already published today.
#   - Collects 8 to 9 distinct-creator GTA 6 clips (~55-60s each) using the
#     vault plus fresh discovery, respecting the 24h post-deletion revisit
#     cooldown and stamping consumed clips so no moment is ever wasted.
# ==============================================================================
def _fmt_chapter_ts(sec):
    sec = max(0, int(round(float(sec))))
    m = sec // 60
    s = sec % 60
    return f"{m:02d}:{s:02d}"


def should_run_long_form_today(history_data):
    if not CHANNEL_CONFIG.get("upload_to_youtube", False):
        return False

    now_ist = datetime.now(IST_TZ)
    if now_ist.hour < 17:
        return False

    iso_year, iso_week, _ = now_ist.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    today_str = now_ist.strftime("%Y-%m-%d")

    brain = history_data.setdefault("ai_brain", _default_ai_brain())
    # Reset weekly counter on new ISO week
    pub_list = [
        d for d in brain.get("longs_published_this_week", [])
        if str(d).startswith(week_key)
    ]
    brain["longs_published_this_week"] = pub_list

    target_per_week = int(brain.get("longs_per_week", 2))
    if len(pub_list) >= target_per_week:
        return False

    today_tag = f"{week_key}:{today_str}"
    if today_tag in pub_list:
        return False

    allowed_days = [int(d) for d in brain.get("long_weekdays", [2, 6])]
    return now_ist.weekday() in allowed_days


def _collect_long_form_clips(history_data):
    os.makedirs("long_pool", exist_ok=True)
    pool_items = []
    seen_creators = set()

    # 1. Harvest fresh distinct-creator 58s slices first (saves vault clips for Shorts)
    extra_cands = discover_gta6_candidates(history_data)
    rpool = history_data.setdefault("revisit_pool", {})
    exhausted = history_data.setdefault("exhausted_ids", [])

    for cand in extra_cands:
        if len(pool_items) >= 9:
            break
        ch = str(cand.get("channel_name", "")).strip()
        if not ch or ch.lower() in seen_creators:
            continue

        vid_id = cand["video_id"]
        prev_ranges = rpool.get(vid_id, {}).get("checked_ranges", [])
        raw_tmp = f"raw_long_{vid_id}.mp4"
        slice_tmp = os.path.join("long_pool", f"long_{vid_id}.mp4")

        try:
            if not download_candidate_video(cand, raw_tmp):
                continue
            tot_d = get_media_duration(raw_tmp)
            peaks = detect_audio_video_peaks(raw_tmp, max_peaks=2, blocked_ranges=prev_ranges)
            if not peaks:
                if vid_id not in exhausted:
                    exhausted.append(vid_id)
                rpool.pop(vid_id, None)
                continue

            peak_s = float(next(iter(peaks)))
            span = min(58.0, tot_d)
            st = max(0.0, min(tot_d - span, peak_s - 22.0))

            run_cmd_safe(
                [
                    "ffmpeg", "-y", "-ss", f"{st:.2f}", "-t", f"{span:.2f}",
                    "-i", raw_tmp, "-r", "30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                    slice_tmp,
                ],
                label=f"LongSlice_{vid_id}",
                timeout_sec=120,
            )

            entry = rpool.setdefault(vid_id, {
                "title": cand.get("title", "GTA 6 Gameplay"),
                "channel_name": ch,
                "duration": round(tot_d, 2),
                "checked_ranges": [],
            })
            entry["checked_ranges"].append([round(st, 1), round(st + span, 1)])
            entry["last_deleted_at"] = datetime.now(timezone.utc).isoformat()

            if get_media_duration(slice_tmp) >= 35.0 and verify_action_motion_and_quality(slice_tmp):
                score = compute_clip_viral_score(slice_tmp)
                if score >= 6.8:
                    seen_creators.add(ch.lower())
                    pool_items.append({
                        "path": slice_tmp,
                        "channel_name": ch,
                        "title": cand.get("title", "GTA 6 Gameplay"),
                        "viral_score": score,
                    })
        except Exception as e:
            print(f"[Long Harvest Warning] {vid_id}: {e}", flush=True)
        finally:
            if os.path.exists(raw_tmp):
                os.remove(raw_tmp)

    return pool_items

# ==============================================================================
# PART 32 OF 40: LONG-FORM AI COMMENTARY & THUMBNAIL HOOK GENERATOR
# Subheading: Writes 1 Intro, Commentary For Every Clip, 1 Outro CTA & Cover Hook
# What This Code Does:
#   - Generates a cohesive creator script for the 8-9 minute 16:9 video:
#       1) ONE opening intro hook at 00:00 (not repeated on every clip).
#       2) Individual commentary lines for EVERY clip (#N down to #1).
#       3) ONE closing outro at the very end with a randomized Like & Subscribe CTA.
#       4) A short 2-to-3 word high-CTR headline for the 1280x720 thumbnail.
# ==============================================================================
def _generate_long_form_script(selected_items, history_data):
    total_clips = len(selected_items)
    brain = history_data.get("ai_brain", {})
    winners = brain.get("winning_patterns", [])[:3]
    chosen_cta = random.choice(RANDOM_LIKE_SUB_CTAS)

    lines = []
    for idx, item in enumerate(selected_items):
        rank_num = total_clips - idx
        clean_ch, clean_ti = sanitize_creator_and_title(item["channel_name"], item["title"])
        lines.append(f"- #{rank_num}: Creator '{clean_ch}' | Stream: '{clean_ti}'")

    rank_keys = ", ".join(f'"{total_clips - i}"' for i in range(total_clips))
    prompt = (
        f"You are scripting an 8-to-10 minute 16:9 YouTube countdown for {CHANNEL_CONFIG['channel_name']} "
        f"({CHANNEL_CONFIG['channel_handle']}). Channel winners: {winners}\n"
        f"Clips (#{total_clips} down to #1):\n" + "\n".join(lines) + "\n\n"
        f"Return STRICT JSON with 5 keys:\n"
        f'1. "title": High-CTR English YouTube title (max 72 chars, no #Shorts).\n'
        f'2. "thumb_hook": 2-to-3 UPPERCASE words for the 1280x720 thumbnail (e.g. "IMPOSSIBLE ESCAPE?!").\n'
        f'3. "intro_vo": ONE punchy 10-to-14 word opening intro spoken ONLY at 00:00.\n'
        f'4. "clip_vos": JSON object mapping EVERY rank ({rank_keys}) to a 10-to-14 word commentary line describing that clip (no repeated intros/outros).\n'
        f'5. "outro_vo": ONE closing outro spoken ONLY at the very end weaving in: "{chosen_cta}"\n'
    )

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=35000))
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.8),
            )
            parsed = json.loads(resp.text.strip())
            if parsed.get("title") and parsed.get("intro_vo") and parsed.get("clip_vos") and parsed.get("outro_vo"):
                return parsed
        except Exception as e:
            print(f"[Long Script Warning] {e}", flush=True)

    fallback_vos = {}
    for idx, item in enumerate(selected_items):
        r = total_clips - idx
        ch, _ = sanitize_creator_and_title(item["channel_name"], item["title"])
        fallback_vos[str(r)] = (
            f"Taking our number 1 spot this week, {ch} pulls off pure Vice City madness!"
            if r == 1
            else f"Coming in at number {r}, watch how {ch} handles total chaos in Vice City!"
        )

    return {
        "title": f"Top {total_clips} Craziest GTA 6 Moments Of The Week! (Vice City Chaos)",
        "thumb_hook": "PURE CHAOS?!",
        "intro_vo": f"Welcome back to Vice City Clips Vault! Today we are counting down the top {total_clips} wildest GTA 6 moments of the week!",
        "clip_vos": fallback_vos,
        "outro_vo": chosen_cta,
    }

# ==============================================================================
# PART 33 OF 40: 1920X1080 SEGMENT RENDERER & RANDOMIZED TRANSITION STITCHER
# Subheading: Renders Ranked Landscape Clips With Random Transitions & Chapters
# What This Code Does:
#   - Renders each clip to 1920x1080 @ 30fps with an accurate #N rank badge,
#     lower-third creator credit bar, and channel watermark.
#   - Joins all clips using randomized FFmpeg xfade transitions and records
#     exact chapter timestamps for YouTube's progress-bar scrubber.
# ==============================================================================
def _render_and_stitch_long_video(selected_items, script_data):
    total_clips = len(selected_items)
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    wm_label = _escape_drawtext(CHANNEL_CONFIG["watermark_text"])

    rendered_segs = []
    seg_durs = []
    chapters = ["00:00 Intro & Weekly Vice City Countdown"]
    vo_events = []
    cum_t = 0.0
    xfade_d = 0.45

    # Single opening intro at 0.4s
    intro_mp3 = "long_vo_intro.mp3"
    intro_txt = str(script_data.get("intro_vo", "")).strip()
    intro_dur = synthesize_voiceover_mp3(intro_txt, intro_mp3)
    intro_end_guard = 0.5
    if intro_dur > 0.4 and os.path.exists(intro_mp3):
        vo_events.append({
            "path": intro_mp3,
            "start": 0.4,
            "end": round(0.4 + intro_dur, 2),
        })
        intro_end_guard = round(0.4 + intro_dur + 0.5, 2)

    clip_vos = script_data.get("clip_vos", {})

    for idx, item in enumerate(selected_items):
        rank_num = total_clips - idx
        seg_out = f"long_seg_{idx}.mp4"
        dur_use = min(58.0, get_media_duration(item["path"]))
        clean_ch, clean_ti = sanitize_creator_and_title(item["channel_name"], item["title"])
        ch_esc = _escape_drawtext(f"#{rank_num}  |  Credit: {clean_ch}")
        badge_col = "#FF1744" if rank_num == 1 else "#00E5FF"

        vf = (
            f"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            f"drawbox=x=48:y=42:w=135:h=68:color={badge_col}@0.92:t=fill,"
            f"drawtext=fontfile={font_bold}:text='#{rank_num}':fontsize=40:fontcolor=white:x=76:y=56,"
            f"drawbox=x=48:y=965:w=760:h=62:color=black@0.75:t=fill,"
            f"drawtext=fontfile={font_bold}:text='{ch_esc}':fontsize=28:fontcolor=#FFD54F:x=72:y=982,"
            f"drawtext=fontfile={font_bold}:text='{wm_label}':fontsize=26:fontcolor=white@0.85:x=w-text_w-48:y=48"
        )
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-ss", "0", "-t", f"{dur_use:.2f}",
                "-i", item["path"], "-vf", vf,
                "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                seg_out,
            ],
            label=f"LongSegRender_{idx}",
            timeout_sec=150,
        )
        actual_d = get_media_duration(seg_out)
        rendered_segs.append(seg_out)
        seg_durs.append(actual_d)

        chapters.append(f"{_fmt_chapter_ts(cum_t)} #{rank_num} - {clean_ch}: {clean_ti[:48]}")

        # Commentary for EVERY single clip
        c_line = str(clip_vos.get(str(rank_num), "")).strip()
        if c_line:
            vo_mp3 = f"long_vo_{idx}.mp3"
            vd = synthesize_voiceover_mp3(c_line, vo_mp3)
            if vd > 0.4 and os.path.exists(vo_mp3):
                v_st = round(max(cum_t + 0.6, intro_end_guard if idx == 0 else cum_t + 0.6), 2)
                vo_events.append({
                    "path": vo_mp3,
                    "start": v_st,
                    "end": round(v_st + vd, 2),
                })

        cum_t += actual_d - (xfade_d if idx < total_clips - 1 else 0.0)

    # Single closing outro + randomized Like & Subscribe CTA at the very end
    outro_mp3 = "long_vo_outro.mp3"
    outro_txt = str(script_data.get("outro_vo", "")).strip()
    odur = synthesize_voiceover_mp3(outro_txt, outro_mp3)
    if odur > 0.4 and os.path.exists(outro_mp3) and cum_t > 12.0:
        last_end = max((v["end"] for v in vo_events), default=2.0)
        o_st = round(max(last_end + 0.4, cum_t - odur - 0.8), 2)
        vo_events.append({
            "path": outro_mp3,
            "start": o_st,
            "end": round(min(cum_t - 0.1, o_st + odur), 2),
        })

    # Stitch segments with randomized transitions
    stitched_long = "long_stitched.mp4"
    _, *rest_s = rendered_segs
    first_d, *rest_d = seg_durs
    cmd = ["ffmpeg", "-y"]
    for rs in rendered_segs:
        cmd.extend(["-i", rs])

    fc = []
    c_pos = first_d
    pv, pa = "[0:v]", "[0:a]"
    for i, d_val in enumerate(rest_d, start=1):
        off = max(1.0, round(c_pos - xfade_d, 3))
        t_style = random.choice(RANDOM_XFADE_STYLES)
        last = i == len(rest_d)
        ov = "[vf]" if last else f"[v{i}]"
        oa = "[af]" if last else f"[a{i}]"
        fc.append(f"{pv}[{i}:v]xfade=transition={t_style}:duration={xfade_d}:offset={off}{ov}")
        fc.append(f"{pa}[{i}:a]acrossfade=d={xfade_d}{oa}")
        c_pos = off + d_val
        pv, pa = ov, oa

    run_cmd_safe(
        cmd + [
            "-filter_complex", ";".join(fc),
            "-map", "[vf]", "-map", "[af]",
            "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "192k", stitched_long,
        ],
        label="StitchLongVideo",
        timeout_sec=300,
    )

    return stitched_long, vo_events, chapters

# ==============================================================================
# PART 34 OF 40: LONG-FORM MASTER AUDIO MIXER & CUSTOM THUMBNAIL ATTACHER
# Subheading: Ducks Game Audio Under Commentary & Generates 1280x720 Cover JPG
# What This Code Does:
#   - Mixes the single opening intro, every clip's commentary, and the single
#     closing outro/CTA cleanly over the stitched 1080p video with audio ducking.
#   - Calls generate_high_ctr_long_thumbnail (Part 25) to create the custom
#     1280x720 thumbnail with boosted colors, red badge, and bold hook text.
# ==============================================================================
def _mix_long_master_and_thumbnail(stitched_long, vo_events, script_data, total_clips):
    final_long_mp4 = "final_weekly_gta6_long.mp4"
    mix_cmd = ["ffmpeg", "-y", "-i", stitched_long]
    for ve in vo_events:
        mix_cmd.extend(["-i", ve["path"]])

    if vo_events:
        d_cond = "+".join(f"between(t,{v['start']},{v['end']})" for v in vo_events)
        m_fc = [f"[0:a]volume='if(gt({d_cond},0),0.40,0.92)':eval=frame[ga]"]
        m_lbls = ["[ga]"]
        for i, ve in enumerate(vo_events, start=1):
            ms = int(round(ve["start"] * 1000))
            m_fc.append(f"[{i}:a]adelay={ms}|{ms},volume=1.35[lv{i}]")
            m_lbls.append(f"[lv{i}]")
        m_fc.append(f"{''.join(m_lbls)}amix=inputs={len(m_lbls)}:duration=first:normalize=0[aout]")
        run_cmd_safe(
            mix_cmd + [
                "-filter_complex", ";".join(m_fc),
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", final_long_mp4,
            ],
            label="MixLongMaster",
            timeout_sec=180,
        )
    else:
        shutil.copy2(stitched_long, final_long_mp4)

    thumb_jpg = generate_high_ctr_long_thumbnail(
        video_path=final_long_mp4,
        hook_headline=script_data.get("thumb_hook", "IMPOSSIBLE ESCAPE?!"),
        badge_text=f"TOP {total_clips} GTA 6",
        out_jpg="thumb_long_16x9.jpg",
    )
    return final_long_mp4, thumb_jpg

# ==============================================================================
# PART 35 OF 40: LONG-FORM YOUTUBE PUBLISHER & THUMBNAIL UPLOADER
# Subheading: Uploads 16:9 Countdown With Chapters, Custom Thumbnail & AI Logging
# What This Code Does:
#   - Publishes the 8-10 minute 16:9 video to YouTube, uploads the custom
#     1280x720 thumbnail via youtube.thumbnails().set(), records the IST publish
#     hour in history.json for AI Brain reach analysis, and cleans up all
#     temporary long-form files so free-tier storage stays spotless.
# ==============================================================================
def build_and_publish_weekly_long_video(history_data):
    if not should_run_long_form_today(history_data):
        return False

    now_ist = datetime.now(IST_TZ)
    iso_year, iso_week, _ = now_ist.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    today_tag = f"{week_key}:{now_ist.strftime('%Y-%m-%d')}"

    print(f"[Long-Form Engine] Building 16:9 countdown for {today_tag}...", flush=True)
    pool_items = _collect_long_form_clips(history_data)
    if len(pool_items) < 6:
        print("[Long-Form Engine] Fewer than 6 watchable clips found; will retry next hour.", flush=True)
        shutil.rmtree("long_pool", ignore_errors=True)
        return False

    pool_items.sort(key=lambda x: x["viral_score"])
    selected = pool_items[-9:]
    total_clips = len(selected)

    script_data = _generate_long_form_script(selected, history_data)
    stitched_long, vo_events, chapters = _render_and_stitch_long_video(selected, script_data)
    final_long_mp4, thumb_jpg = _mix_long_master_and_thumbnail(
        stitched_long, vo_events, script_data, total_clips
    )

    tot_long_d = get_media_duration(final_long_mp4)
    youtube = get_youtube_service()
    long_title = str(script_data.get("title", f"Top {total_clips} Craziest GTA 6 Moments!"))[:98]
    long_desc = (
        f"{long_title}\n\n"
        f"Welcome to {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) — "
        f"your home for the wildest Grand Theft Auto 6 moments with full creator credits and commentary!\n\n"
        f"Chapters:\n" + "\n".join(chapters) + "\n\n"
        f"#GTA6 #GrandTheftAuto6 #ViceCity #GTAVI #Gaming"
    )

    req = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": long_title,
                "description": long_desc,
                "tags": ["GTA 6", "Grand Theft Auto 6", "Vice City", "GTA 6 Best Moments", "GTA VI"],
                "categoryId": "20",
                "defaultLanguage": "en",
                "defaultAudioLanguage": "en",
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        },
        media_body=MediaFileUpload(final_long_mp4, chunksize=-1, resumable=True),
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk()

    long_vid_id = resp.get("id", "")
    if long_vid_id and thumb_jpg and os.path.exists(thumb_jpg):
        try:
            youtube.thumbnails().set(
                videoId=long_vid_id,
                media_body=MediaFileUpload(thumb_jpg),
            ).execute()
            print(f"[Long Thumbnail Uploaded] Attached 1280x720 cover to {long_vid_id}.", flush=True)
        except Exception as e:
            print(f"[Thumbnail Upload Warning] {e}", flush=True)

    long_url = "https://" + "://youtube.com" + "?v=" + long_vid_id
    brain = history_data.setdefault("ai_brain", _default_ai_brain())
    brain.setdefault("longs_published_this_week", []).append(today_tag)

    history_data.setdefault("uploads", []).append({
        "title": long_title,
        "url": long_url,
        "type": "long_16x9",
        "clip_count": total_clips,
        "duration_sec": round(tot_long_d, 2),
        "publish_hour_ist": now_ist.hour,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    })
    save_history(history_data)

    shutil.rmtree("long_pool", ignore_errors=True)
    for fn in (stitched_long, final_long_mp4, "thumb_long_16x9.jpg"):
        if os.path.exists(fn):
            try:
                os.remove(fn)
            except Exception:
                pass

    print(f"[Long-Form Live] Published {tot_long_d/60:.1f} min 16:9 video + thumbnail: {long_url}", flush=True)
    return True

# ==============================================================================
# PART 36 OF 40: HOURLY HARVEST & STREAM EXHAUSTION ENGINE
# Subheading: Harvests 1 New Watchable Clip Per Stream & Retires Exhausted Streams
# What This Code Does:
#   - Loops over eligible original creator streams (both fresh discoveries and
#     partially scanned streams whose previous clip was deleted >= 24 hours ago).
#   - Skips previously checked timestamps; if zero unused climax peaks remain,
#     moves the stream to exhausted_ids and deletes its timestamp memory so
#     history.json never bloats and the bot never revisits that stream again.
#   - Runs Whisper English translation + Gemini Vision watchability gate before
#     adding any clip to clip_vault/.
# ==============================================================================
def run_hourly_harvest_phase(history_data):
    candidates = discover_gta6_candidates(history_data)
    print(f"[Discovery] Found {len(candidates)} candidate GTA 6 streams.", flush=True)

    rpool = history_data.setdefault("revisit_pool", {})
    exhausted = history_data.setdefault("exhausted_ids", [])
    newly_vaulted = 0

    for cand in candidates:
        if newly_vaulted >= int(CHANNEL_CONFIG.get("max_new_clips_per_run", 3)):
            break

        vid_id = cand["video_id"]
        raw_path = f"raw_{vid_id}.mp4"
        prev_ranges = rpool.get(vid_id, {}).get("checked_ranges", [])

        try:
            downloaded = download_candidate_video(cand, raw_path)
            if not downloaded or not os.path.exists(raw_path):
                continue

            tot_dur = get_media_duration(raw_path)
            peaks = detect_audio_video_peaks(raw_path, max_peaks=2, blocked_ranges=prev_ranges)

            # Entire stream scanned with no unused peaks left -> permanently retire
            if not peaks:
                print(f"[Stream Exhausted] All climax moments scanned in {vid_id}. Retiring permanently.", flush=True)
                if vid_id not in exhausted:
                    exhausted.append(vid_id)
                rpool.pop(vid_id, None)
                continue

            slices = harvest_climax_slices_to_vault(raw_path, cand, peaks)
            if not slices:
                continue

            item = next(iter(slices))
            st = float(item.get("source_start_sec", 0.0))
            en = st + float(item.get("duration_sec", 28.0))

            # Record checked window immediately so failed/low-motion windows aren't re-checked
            entry = rpool.setdefault(vid_id, {
                "title": cand.get("title", "GTA 6 Gameplay"),
                "channel_name": cand.get("channel_name", "GTA 6 Creator"),
                "duration": round(tot_dur, 2),
                "checked_ranges": [],
            })
            entry["checked_ranges"].append([round(st, 1), round(en, 1)])

            if not verify_action_motion_and_quality(item["vault_path"]):
                print(f"[Filter] Rejected low-motion clip {item['vault_path']}.", flush=True)
                if os.path.exists(item["vault_path"]):
                    os.remove(item["vault_path"])
                entry["last_deleted_at"] = datetime.now(timezone.utc).isoformat()
                continue

            tx_en, w_events = transcribe_and_translate_clip(item["vault_path"])
            w_score = compute_clip_viral_score(item["vault_path"], tx_en)
            if w_score < 6.8:
                print(f"[Watchability Reject] Clip {item['vault_path']} scored {w_score}; deleting.", flush=True)
                if os.path.exists(item["vault_path"]):
                    os.remove(item["vault_path"])
                entry["last_deleted_at"] = datetime.now(timezone.utc).isoformat()
                continue

            item["transcript_en"] = tx_en
            item["word_events"] = w_events
            item["viral_score"] = w_score
            # Clear last_deleted_at while this active clip sits inside clip_vault/
            entry.pop("last_deleted_at", None)

            history_data.setdefault("vault_queue", []).append(item)
            newly_vaulted += 1
            print(
                f"[Vault Queued] {item['vault_path']} (score={w_score}, vault_count={len(history_data['vault_queue'])})",
                flush=True,
            )
        except Exception as e:
            print(f"[Harvest Error] {vid_id}: {e}", flush=True)
        finally:
            if os.path.exists(raw_path):
                try:
                    os.remove(raw_path)
                except Exception:
                    pass

    prune_vault_and_temp_files(history_data)
    save_history(history_data)
    return newly_vaulted

# ==============================================================================
# PART 37 OF 40: MULTI-CLIP SHORT BUILDER & RENDER ORCHESTRATOR
# Subheading: Builds Dynamic #N -> #1 Countdown, Validates & Renders Final MP4
# What This Code Does:
#   - Plans a 2-to-5 distinct-creator countdown Short from clip_vault/.
#   - Generates the single opening intro, per-clip commentary for every clip,
#     and single closing outro + randomized Like & Subscribe CTA.
#   - Renders vertical segments with randomized transitions and burns synced
#     yellow captions + top gold banner into final_short_vice_city.mp4.
# ==============================================================================
def build_and_render_short_master(history_data):
    short_plan = build_top5_plan_from_vault(history_data)
    if not short_plan or not short_plan.get("segments"):
        print("[Standby] Vault does not have >= 2 distinct creators ready yet.", flush=True)
        return None, None, None

    script_data = generate_scene_grounded_script(short_plan, history_data)
    if not validate_monetization_eligibility(short_plan, script_data):
        print("[Skip] Short plan failed transformative commentary validation.", flush=True)
        return None, None, None

    stitched_video, timeline_meta = render_vertical_segments_and_concat(short_plan)
    final_short_path = "final_short_vice_city.mp4"
    compose_final_short_with_captions_and_audio(
        stitched_video=stitched_video,
        timeline_meta=timeline_meta,
        script_data=script_data,
        output_path=final_short_path,
    )

    if not os.path.exists(final_short_path):
        raise RuntimeError("Final Short render failed — final_short_vice_city.mp4 missing.")

    export_short_cover_thumbnail(final_short_path, out_jpg="thumb_vice_city.jpg")
    final_dur = get_media_duration(final_short_path)
    print(f"[Ready] Rendered {final_short_path} ({final_dur:.2f}s | {len(timeline_meta)} clips).", flush=True)
    return final_short_path, timeline_meta, script_data

# ==============================================================================
# PART 38 OF 40: SHORT PUBLISHER, REACH TRACKER & 24H DELETION STAMPER
# Subheading: Uploads Short, Deletes Used Vault Clips & Starts 24h Cooldown
# What This Code Does:
#   - Uploads final_short_vice_city.mp4 to YouTube and schedules it for the
#     chosen IST slot.
#   - Deletes the consumed clips from clip_vault/ immediately to keep storage
#     free and stamps last_deleted_at in revisit_pool so the 24-hour cooldown
#     starts from the exact moment of deletion.
#   - Logs the scheduled IST hour and hook style in history.json["uploads"] so
#     Sunday's AI Brain review can learn which hours and hooks get wide reach.
# ==============================================================================
def publish_short_and_consume_vault(history_data, final_short_path, timeline_meta, script_data, slot_iso):
    final_dur = get_media_duration(final_short_path)

    if not CHANNEL_CONFIG.get("upload_to_youtube", False):
        used_paths = {seg["video_path"] for seg in timeline_meta}
        for v in history_data.get("vault_queue", []):
            if v.get("vault_path") in used_paths:
                v["test_rendered_count"] = int(v.get("test_rendered_count", 0)) + 1
        save_history(history_data)
        print("[Test Mode] upload_to_youtube=False — rotated test clips.", flush=True)
        return None

    video_url = upload_short_to_youtube(
        video_path=final_short_path,
        script_data=script_data,
        timeline_meta=timeline_meta,
        publish_at_iso=slot_iso,
    )

    used_paths = {seg["video_path"] for seg in timeline_meta}
    remaining_vault = []
    for v in history_data.get("vault_queue", []):
        vp = v.get("vault_path", "")
        if vp in used_paths:
            if os.path.exists(vp):
                try:
                    os.remove(vp)
                except Exception:
                    pass
            _mark_clip_deleted_for_revisit(history_data, v)
        else:
            remaining_vault.append(v)
    history_data["vault_queue"] = remaining_vault

    # Determine scheduled IST hour so AI Brain can correlate reach with publish hour
    sched_ist_hr = datetime.now(IST_TZ).hour
    if slot_iso:
        history_data.setdefault("scheduled_slots", []).append(slot_iso)
        try:
            sched_ist_hr = datetime.fromisoformat(slot_iso.replace("Z", "+00:00")).astimezone(IST_TZ).hour
        except Exception:
            pass

    history_data.setdefault("uploads", []).append({
        "title": script_data.get("title", ""),
        "url": video_url,
        "type": "short",
        "clip_count": len(timeline_meta),
        "duration_sec": round(final_dur, 2),
        "hook_style": script_data.get("hook_style", "shock"),
        "publish_hour_ist": sched_ist_hr,
        "scheduled_for": slot_iso or "immediate",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    })
    save_history(history_data)
    print(f"[Live Success] Uploaded & scheduled for {sched_ist_hr:02d}:xx IST: {video_url}", flush=True)
    return video_url

# ==============================================================================
# PART 39 OF 40: AUTONOMOUS LONG-FORM & WEEKLY AI BRAIN DISPATCHER
# Subheading: Triggers 2x/Week 16:9 Videos & Sunday Audience Reach Self-Tuning
# What This Code Does:
#   - Runs build_and_publish_weekly_long_video (Parts 31-35) whenever today is
#     an active long-form day (default Wed & Sun evening IST).
#   - Runs run_weekly_analytics_tuning (Part 30) on Sunday evenings so the AI
#     Brain updates its peak audience hours, winning hooks, and Shorts/day +
#     Longs/week cadence experiments for the upcoming week.
# ==============================================================================
def run_autonomous_brain_and_long_tasks(history_data):
    try:
        build_and_publish_weekly_long_video(history_data)
    except Exception as e:
        print(f"[Long-Form Dispatcher Warning] {e}", flush=True)

    try:
        run_weekly_analytics_tuning(history_data)
    except Exception as e:
        print(f"[AI Brain Tuning Warning] {e}", flush=True)

# ==============================================================================
# PART 40 OF 40: MASTER 24/7 AUTONOMOUS CREATOR ENTRYPOINT
# Subheading: Coordinates Harvest, Multi-Clip Shorts, Long-Form & AI Brain Tuning
# What This Code Does:
#   - Cleans scratch files, loads history.json + ai_brain state, and prunes
#     stale vault items while respecting the 24-hour post-deletion timer.
#   - Phase 1: Runs hourly harvest to stock clip_vault/ with watchable clips.
#   - Phase 2: Checks adaptive publish cadence (default 3 Shorts/day, >= 2
#     distinct creators) or manual force_upload trigger.
#   - Phase 3 & 4: Renders multi-clip Short with single intro, per-clip
#     commentary, single outro + randomized Like/Subscribe CTA, uploads it,
#     and deletes used vault clips to trigger their 24h revisit timer.
#   - Phase 5: Runs 2x/week 16:9 long-form publisher & Sunday AI Brain tuning.
# ==============================================================================
def main():
    print("============================================================", flush=True)
    print(f"[Boot] {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) — 40-Part AI Creator Engine", flush=True)
    print("============================================================", flush=True)

    cleanup_scratch_render_files()
    history_data = load_history()
    prune_vault_and_temp_files(history_data)
    os.makedirs(CHANNEL_CONFIG.get("vault_dir", "clip_vault"), exist_ok=True)

    brain = history_data.get("ai_brain", {})
    print(
        f"[AI Brain Active] Target: {brain.get('shorts_per_day', 3)} Shorts/day & "
        f"{brain.get('longs_per_week', 2)} Longs/week | Peak Hours IST: {brain.get('top_hours_ist') or 'Exploring Random Slots'}",
        flush=True,
    )

    # PHASE 1: Hourly Harvest (Respects 24h post-deletion timer & exhausts full streams)
    newly_vaulted = run_hourly_harvest_phase(history_data)

    # PHASE 2: Check Adaptive Short Publish Window (or Manual force_upload)
    force_flag = str(os.environ.get("FORCE_UPLOAD", "")).strip().lower() in ("1", "true", "yes")
    should_build, slot_iso = should_trigger_publish_window(history_data, force_flag=force_flag)

    if should_build or not CHANNEL_CONFIG.get("upload_to_youtube", False):
        # PHASE 3 & 4: Build Multi-Clip Short, Render, Upload & Consume Vault
        final_short_path, timeline_meta, script_data = build_and_render_short_master(history_data)
        if final_short_path and timeline_meta and script_data:
            publish_short_and_consume_vault(
                history_data=history_data,
                final_short_path=final_short_path,
                timeline_meta=timeline_meta,
                script_data=script_data,
                slot_iso=slot_iso,
            )
    else:
        print(
            f"[Standby] Harvest complete ({newly_vaulted} new). Waiting for next adaptive Short slot or >= 2 creators.",
            flush=True,
        )

    # PHASE 5: 2x/Week 16:9 Long-Form Video + Sunday AI Brain Reach & Cadence Review
    run_autonomous_brain_and_long_tasks(history_data)

    cleanup_scratch_render_files()
    print("[Done] Cycle complete. Storage & state compacted for free tier.", flush=True)


if __name__ == "__main__":
    main()
