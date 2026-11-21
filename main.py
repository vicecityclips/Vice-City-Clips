# ==============================================================================
# PART 1 OF 30: CORE IMPORTS & ENVIRONMENT GUARDRAILS
# Subheading: Standard Library, Vision, Audio, Whisper & YouTube/Gemini Clients
# Code Title: Module Loader & Clean Runtime Environment Initializer
# ==============================================================================
import os
import re
import sys
import json
import math
import time
import wave
import shutil
import random
import asyncio
import warnings
import subprocess
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
warnings.filterwarnings("ignore")

import cv2
import torch
import open_clip
import numpy as np
from PIL import Image
import yt_dlp
import edge_tts
from gtts import gTTS
from faster_whisper import WhisperModel

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ==============================================================================
# PART 2 OF 30: MASTER CHANNEL CONFIGURATION & 6-HOUR SLOT CONTROLS
# Subheading: Upload Safety Switches, 6-Hour Publishing Cadence & Quality Bar
# Code Title: CHANNEL_CONFIG Master Control Dictionary
# ==============================================================================
def _resolve_upload_flag(default_val=False):
    raw = os.environ.get("FORCE_UPLOAD_OVERRIDE", "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    return bool(default_val)


CHANNEL_CONFIG = {
    # Channel Identity
    "channel_name": "Vice City Clips Vault",
    "channel_handle": "@Vice-City_ClipsVault",
    "game_title": "Grand Theft Auto VI",
    "watermark_text": "@Vice-City_ClipsVault",

    # Upload & Scheduling Master Controls (Keep upload_to_youtube=False during artifact tests)
    "upload_to_youtube": _resolve_upload_flag(False),
    "youtube_privacy": "public",         # "public" | "private" | "unlisted"
    "enable_scheduling": True,           # Schedules 1 Short for the upcoming 6-hour slot
    "max_daily_uploads": 4,              # Matches 4 slots/day (every 6 hours)

    # 6-Hour Slot Schedule (UTC): 02:00, 08:00, 14:00, 20:00 UTC
    # Compiler runs 1 hour earlier at: 01:00, 07:00, 13:00, 19:00 UTC
    "slot_hours_utc": (2, 8, 14, 20),
    "compile_offset_hours": 0.65,

    # Vault & Compilation Quality Rules
    "slice_duration_sec": 26.0,          # Raw 26s buffer harvested per heatmap peak
    "max_clips_per_short": 5,            # Compiles up to 5 top distinct clips into 1 Short
    "min_heatmap_score": 0.68,           # Crowd replay proof threshold (0.0 - 1.0)
    "min_viral_score": 7.0,              # Strict quality gate (0.0 - 10.0)
    "max_vault_age_hours": 18,           # Expires stale uncompiled clips older than 18h

    # Audio & Narration Settings
    "enable_ai_voiceover": True,
    "voice_name": "en-US-AndrewNeural",
}

# ==============================================================================
# PART 3 OF 30: VERIFIED STRIKE-SAFE ORIGINAL CREATOR DIRECTORY
# Subheading: 100% Equal-Weight Random Pool of Official Streamers (Zero Re-Uploaders)
# Code Title: VERIFIED_ORIGINAL_CREATORS Registry
# ==============================================================================
VERIFIED_ORIGINAL_CREATORS = (
    # Global Strike-Safe Original Streamers & Creators (Equal Weight = 1.0)
    {"name": "IShowSpeed", "handle": "@IShowSpeed"},
    {"name": "Kai Cenat", "handle": "@KaiCenatLive"},
    {"name": "xQc", "handle": "@xQcOW"},
    {"name": "Valkyrae", "handle": "@Valkyrae"},
    {"name": "Typical Gamer", "handle": "@TypicalGamer"},
    {"name": "Sykkuno", "handle": "@Sykkuno"},
    {"name": "Ludwig", "handle": "@ludwig"},
    {"name": "MoistCr1TiKaL", "handle": "@penguinz0"},
    {"name": "DarkViperAU", "handle": "@DarkViperAU"},
    {"name": "Tarik", "handle": "@tarik"},
    {"name": "CaseOh", "handle": "@caseoh_"},
    {"name": "Jynxzi", "handle": "@Jynxzi"},
    {"name": "GTA Series Videos", "handle": "@GTASeriesVideos"},

    # Indian Strike-Safe Original Streamers & Creators (Equal Weight = 1.0)
    {"name": "CarryIsLive", "handle": "@CarryIsLive"},
    {"name": "Live Insaan", "handle": "@LiveInsaan"},
    {"name": "The RawKnee Games", "handle": "@TheRawKneeGames"},
    {"name": "BeastBoyShub", "handle": "@BeastBoyShub"},
    {"name": "Techno Gamerz", "handle": "@TechnoGamerzOfficial"},
    {"name": "Mythpat", "handle": "@Mythpat"},
)

# ==============================================================================
# PART 4 OF 30: PERSISTENT HISTORY, VAULT QUEUE & FINGERPRINT MANAGER
# Subheading: Cross-Run Memory for Used Timestamps, Banked Clips & Published Titles
# Code Title: State Persistence & Duplicate Timestamp Guard
# ==============================================================================
HISTORY_FILE = "history.json"
VAULT_DIR = "clip_vault"


def ensure_vault_dir():
    os.makedirs(VAULT_DIR, exist_ok=True)


def load_history():
    ensure_vault_dir()
    default_state = {
        "used_clips": [],
        "vault_queue": [],
        "scheduled_slots": [],
        "uploads": [],
    }
    if not os.path.exists(HISTORY_FILE):
        return default_state
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default_state.items():
            if k not in data or not isinstance(data[k], list):
                data[k] = v
        return data
    except Exception:
        return default_state


def save_history(history_data):
    history_data["used_clips"] = history_data.get("used_clips", [])[-600:]
    history_data["scheduled_slots"] = history_data.get("scheduled_slots", [])[-120:]
    history_data["uploads"] = history_data.get("uploads", [])[-150:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2)


def build_clip_fingerprint(video_id, peak_sec):
    bucket = int(round(float(peak_sec) / 35.0))
    return f"{video_id}:{bucket}"


def is_clip_already_used_or_banked(history_data, video_id, peak_sec):
    target_sec = float(peak_sec)
    for fp in history_data.get("used_clips", []):
        if ":" not in fp:
            continue
        vid, b_str = fp.split(":", 1)
        if vid == video_id:
            try:
                if abs((int(b_str) * 35.0) - target_sec) <= 35.0:
                    return True
            except ValueError:
                pass
    for item in history_data.get("vault_queue", []):
        if item.get("video_id") == video_id:
            if abs(float(item.get("origin_peak_sec", 0.0)) - target_sec) <= 35.0:
                return True
    return False

# ==============================================================================
# PART 5 OF 30: SAFE SUBPROCESS RUNNER & MEDIA DURATION PROBE
# Subheading: Timeout-Protected FFmpeg Executor & Exact Stream Length Inspector
# Code Title: run_cmd_safe & get_media_duration Helpers
# ==============================================================================
def run_cmd_safe(cmd, label="CMD", timeout_sec=120):
    """Runs FFmpeg/system commands with strict timeout & clear error reporting."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-450:].strip()
        raise RuntimeError(f"[{label}] failed (code {proc.returncode}): {tail}")
    return proc.stdout


def get_media_duration(media_path):
    """Returns exact media duration in seconds via ffprobe, or 0.0 on failure."""
    if not media_path or not os.path.exists(media_path):
        return 0.0
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                media_path,
            ],
            text=True,
            timeout=20,
        ).strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0

# ==============================================================================
# PART 6 OF 30: YT-DLP COOKIE & DENO EJS CHALLENGE CONFIGURATION
# Subheading: Netscape Cookie Auto-Repair & Remote YouTube Player JS Solver
# Code Title: _yt_dlp_base_opts Session Builder
# ==============================================================================
def _yt_dlp_base_opts():
    """Builds clean yt-dlp options with Deno EJS solver & valid Netscape cookie file."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "nocheckcertificate": True,
        "remote_components": ["ejs:github"],
    }
    cookie_txt = os.environ.get("YOUTUBE_COOKIES_TXT", "").strip()
    if cookie_txt and "\t" in cookie_txt:
        if not cookie_txt.startswith("# Netscape HTTP Cookie File"):
            cookie_txt = "# Netscape HTTP Cookie File\n" + cookie_txt
        with open("yt_cookies.txt", "w", encoding="utf-8") as cf:
            cf.write(cookie_txt + "\n")
        opts["cookiefile"] = "yt_cookies.txt"
    return opts

# ==============================================================================
# PART 7 OF 30: STRICT GTA 6 GAME GATE & 2ND-PARTY CLIPPER BLOCKER
# Subheading: Enforces Pure GTA 6 Streams & Rejects Other Games or Compilations
# Code Title: is_strictly_original_gta6_stream Validator
# ==============================================================================
def is_strictly_original_gta6_stream(raw_title):
    """
    Returns True ONLY if the title confirms Grand Theft Auto VI / Vice City / Leonida
    and does NOT belong to another game or a 2nd-party compilation/re-upload.
    """
    t = str(raw_title or "").lower()
    has_gta6 = any(
        k in t
        for k in (
            "gta 6",
            "gta vi",
            "grand theft auto 6",
            "grand theft auto vi",
            "vice city",
            "leonida",
        )
    )
    blocked_terms = (
        "gta 5",
        "gta v",
        "gta5",
        "gtav",
        "san andreas",
        "gta 4",
        "gta iv",
        "minecraft",
        "valorant",
        "free fire",
        "bgmi",
        "fortnite",
        "roblox",
        "donut",
        "compilation",
        "reupload",
        "re-upload",
        "fan channel",
        "best moments of",
    )
    has_blocked = any(b in t for b in blocked_terms)
    return has_gta6 and not has_blocked

# ==============================================================================
# PART 8 OF 30: OPEN WORLDWIDE + EQUAL-WEIGHT CREATOR DISCOVERY ENGINE
# Subheading: Open Global YouTube Search + Official Creator Channels (Zero Clippers)
# Code Title: discover_equal_weight_original_streams
# ==============================================================================
WORLDWIDE_GTA6_QUERIES = (
    "GTA 6 gameplay",
    "Grand Theft Auto VI gameplay",
    "GTA 6 live stream",
    "GTA VI Vice City gameplay",
    "Grand Theft Auto 6 stream",
    "GTA 6 Hindi live",
)

BLOCKED_CLIPPER_CHANNEL_WORDS = (
    "clips",
    "clipper",
    "highlights",
    "moments",
    "compilation",
    "shorts",
    "gaming edits",
    "fan",
    "daily",
    "vault",
)


def _is_original_creator_channel(channel_name):
    ch = str(channel_name or "").lower()
    return not any(w in ch for w in BLOCKED_CLIPPER_CHANNEL_WORDS)


def discover_equal_weight_original_streams():
    """
    Discovers strictly GTA 6 streams & full gameplay videos worldwide using:
    1) Open Worldwide YouTube Search (across all global & Indian creators)
    2) Equal-weight random rotation of verified strike-safe official creators
    Strictly blocks 2nd/3rd-party clippers and compilation channels.
    """
    discovered = {}
    base_yt = "https://youtube.com"

    # 1) Open Worldwide YouTube Search (3 rotating global queries x 20 results = 60 candidates)
    queries = list(WORLDWIDE_GTA6_QUERIES)
    random.shuffle(queries)

    def _run_open_search(q):
        opts = _yt_dlp_base_opts()
        opts["extract_flat"] = "in_playlist"
        found = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                res = ydl.extract_info(f"ytsearch20:{q}", download=False)
            for entry in (res or {}).get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                vid_id = str(entry["id"]).strip()
                if len(vid_id) != 11:
                    continue
                dur = float(entry.get("duration") or 0)
                # Require full gameplay/stream (>= 4 mins) so 2nd-party short clips are skipped
                if dur and dur < 240:
                    continue
                title_str = str(entry.get("title") or "").strip()
                uploader = str(entry.get("uploader") or entry.get("channel") or "").strip()
                if not is_strictly_original_gta6_stream(title_str):
                    continue
                if not _is_original_creator_channel(uploader):
                    continue
                found.append({
                    "video_id": vid_id,
                    "url": f"{base_yt}/watch?v={vid_id}",
                    "title": title_str,
                    "channel_name": uploader or "YouTube Creator",
                    "weight": 1.0,
                })
        except Exception as e:
            print(f"[Open Search Warning] '{q}': {e}", flush=True)
        return found

    # 2) Equal-Weight Random Official Creator Tabs (10 shuffled creators x /streams + /videos)
    pool = list(VERIFIED_ORIGINAL_CREATORS)
    random.shuffle(pool)

    def _fetch_creator_tab(creator, tab):
        raw_h = str(creator["handle"]).strip().lstrip("/")
        clean_h = raw_h if raw_h.startswith("@") else f"@{raw_h}"
        tab_url = f"{base_yt}/{clean_h}/{tab}"
        opts = _yt_dlp_base_opts()
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = 15
        found = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(tab_url, download=False)
            for entry in (info or {}).get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                vid_id = str(entry["id"]).strip()
                if len(vid_id) != 11:
                    continue
                dur = float(entry.get("duration") or 0)
                if dur and dur < 180:
                    continue
                title_str = str(entry.get("title") or "").strip()
                if not is_strictly_original_gta6_stream(title_str):
                    continue
                found.append({
                    "video_id": vid_id,
                    "url": f"{base_yt}/watch?v={vid_id}",
                    "title": title_str,
                    "channel_name": creator["name"],
                    "weight": 1.0,
                })
        except Exception:
            pass
        return found

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_run_open_search, q) for q in queries[:3]]
        for c in pool[:10]:
            futures.append(executor.submit(_fetch_creator_tab, c, "streams"))
            futures.append(executor.submit(_fetch_creator_tab, c, "videos"))

        for fut in as_completed(futures):
            for item in fut.result() or []:
                if item["video_id"] not in discovered:
                    discovered[item["video_id"]] = item

    items = list(discovered.values())
    random.shuffle(items)
    print(
        f"[Worldwide Discovery] Found {len(items)} original GTA 6 streams/VODs worldwide (zero 2nd-party clippers).",
        flush=True,
    )
    return items

# ==============================================================================
# PART 9 OF 30: PARALLEL 6-WORKER MOST REPLAYED HEATMAP SCANNER
# Subheading: Concurrent Crowd-Replay Inspector Before Any Video Download
# Code Title: scan_heatmaps_in_parallel
# ==============================================================================
def scan_heatmaps_in_parallel(discovered_items, history_data, max_inspect=60):
    """
    Inspects up to 60 official GTA 6 streams concurrently (6 workers) to find
    authentic 'Most Replayed' crowd heatmap spikes (>= min_heatmap_score).
    """
    batch = list(discovered_items)[:max_inspect]
    heatmap_peaks = []
    fresh_vods = []

    def _inspect_worker(meta):
        opts = _yt_dlp_base_opts()
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(meta["url"], download=False)
            if not info:
                return None
            full_title = str(info.get("title") or meta["title"])
            if not is_strictly_original_gta6_stream(full_title):
                return None
            return (meta, info)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(_inspect_worker, item) for item in batch]
        for fut in as_completed(futures):
            res = fut.result()
            if not res:
                continue
            meta, info = res
            vid_id = meta["video_id"]
            is_live = bool(info.get("is_live"))
            duration = float(info.get("duration") or 0)
            heatmap = info.get("heatmap") or []

            if heatmap and not is_live:
                valid_bins = [
                    b
                    for b in heatmap
                    if float(b.get("start_time", 0)) >= 25.0
                    and float(b.get("value", 0)) >= float(CHANNEL_CONFIG["min_heatmap_score"])
                ]
                valid_bins.sort(key=lambda b: float(b.get("value", 0)), reverse=True)

              picked_peaks_for_vid = []
              for b in valid_bins:
                  peak_mid = (float(b["start_time"]) + float(b["end_time"])) / 2.0
                  if is_clip_already_used_or_banked(history_data, vid_id, peak_mid):
                      continue
                  # Ensure distinct peaks from the same video are at least 45s apart
                  if any(abs(peak_mid - prev_p) < 45.0 for prev_p in picked_peaks_for_vid):
                      continue
                  heatmap_peaks.append({
                      "video_id": vid_id,
                      "url": meta["url"],
                      "video_title": str(info.get("title") or meta["title"]),
                      "channel_name": meta["channel_name"],
                      "peak_sec": peak_mid,
                      "heatmap_score": float(b["value"]),
                      "duration": duration,
                      "source_type": "YouTube-Heatmap",
                  })
                  picked_peaks_for_vid.append(peak_mid)
                  if len(picked_peaks_for_vid) >= 6:
                      break

            elif not is_live and duration >= 180 and not is_clip_already_used_or_banked(history_data, vid_id, 0):
                fresh_vods.append({
                    "video_id": vid_id,
                    "url": meta["url"],
                    "video_title": str(info.get("title") or meta["title"]),
                    "channel_name": meta["channel_name"],
                    "duration": duration,
                    "source_type": "YouTube-FreshVOD",
                })

    heatmap_peaks.sort(key=lambda x: x["heatmap_score"], reverse=True)
    random.shuffle(fresh_vods)
    print(
        f"[Parallel Heatmap Scan] Inspected {len(batch)} official GTA 6 videos -> "
        f"{len(heatmap_peaks)} Most-Replayed spikes + {len(fresh_vods)} fresh VODs.",
        flush=True,
    )
    return heatmap_peaks, fresh_vods

# ==============================================================================
# PART 10 OF 30: SURGICAL 26-SECOND CLIMAX DOWNLOADER
# Subheading: Byte-Range Window Extractor for High-Heatmap Peaks
# Code Title: harvest_26s_climax_slices
# ==============================================================================
def harvest_26s_climax_slices(heatmap_peaks, max_slices=8):
    """
    Downloads only the 26-second climax window around the top Most-Replayed spikes
    (19s buildup before peak + 7s aftermath) without downloading full multi-hour VODs.
    """
    slice_len = float(CHANNEL_CONFIG["slice_duration_sec"])
    harvested = []

    for idx, target in enumerate(heatmap_peaks[:max_slices]):
        vid_id = target["video_id"]
        peak = float(target["peak_sec"])
        total_dur = float(target.get("duration") or 0)

        start_s = max(0.0, peak - (slice_len * 0.72))
        end_s = start_s + slice_len
        if total_dur > 0 and end_s > total_dur:
            end_s = total_dur
            start_s = max(0.0, end_s - slice_len)

        raw_out = f"raw_hm_{idx}_{vid_id}.mp4"
        if os.path.exists(raw_out):
            os.remove(raw_out)

        dl_opts = _yt_dlp_base_opts()
        dl_opts.update({
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": raw_out,
            "download_ranges": yt_dlp.utils.download_range_func(None, [(start_s, end_s)]),
            "force_keyframes_at_cuts": True,
        })

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([target["url"]])
            dur = get_media_duration(raw_out)
            if dur >= 12.0:
                harvested.append({
                    "video_path": raw_out,
                    "video_id": vid_id,
                    "channel_name": target["channel_name"],
                    "source_title": target["video_title"],
                    "source_type": target["source_type"],
                    "heatmap_score": target["heatmap_score"],
                    "origin_peak_sec": peak,
                    "slice_start_origin": start_s,
                    "duration": dur,
                })
                print(
                    f"[Surgical Slice] {target['channel_name']} @ {peak:.1f}s "
                    f"(heatmap={target['heatmap_score']:.2f}, dur={dur:.1f}s)",
                    flush=True,
                )
        except Exception as e:
            print(f"[Slice Warning] {target['channel_name']}: {e}", flush=True)

    return harvested

# ==============================================================================
# PART 11 OF 30: FRESH OFFICIAL VOD BACKUP HARVESTER
# Subheading: Samples Brand-New Official Streams Before Heatmap Data Appears
# Code Title: harvest_fresh_official_vods
# ==============================================================================
def harvest_fresh_official_vods(fresh_vod_pool, max_samples=3):
    """
    Backup harvester for brand-new official creator streams that do not yet have
    YouTube heatmap graphs. Samples a 50-second mid-stream window at <=720p.
    """
    harvested = []
    for idx, vod in enumerate(fresh_vod_pool[:max_samples]):
        vid_id = vod["video_id"]
        total_dur = float(vod.get("duration") or 0)
        if total_dur < 240:
            continue

        # Pick a random high-action point in the middle 25%-75% of the stream
        mid_anchor = total_dur * random.uniform(0.28, 0.72)
        start_s = max(30.0, mid_anchor - 25.0)
        end_s = min(total_dur - 10.0, start_s + 50.0)

        raw_out = f"raw_fresh_{idx}_{vid_id}.mp4"
        if os.path.exists(raw_out):
            os.remove(raw_out)

        dl_opts = _yt_dlp_base_opts()
        dl_opts.update({
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": raw_out,
            "download_ranges": yt_dlp.utils.download_range_func(None, [(start_s, end_s)]),
            "force_keyframes_at_cuts": True,
        })

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([vod["url"]])
            dur = get_media_duration(raw_out)
            if dur >= 15.0:
                harvested.append({
                    "video_path": raw_out,
                    "video_id": vid_id,
                    "channel_name": vod["channel_name"],
                    "source_title": vod["video_title"],
                    "source_type": vod["source_type"],
                    "heatmap_score": 0.0,
                    "origin_peak_sec": start_s + (dur / 2.0),
                    "slice_start_origin": start_s,
                    "duration": dur,
                })
                print(
                    f"[Fresh VOD Sample] {vod['channel_name']} @ {start_s:.0f}s (dur={dur:.1f}s)",
                    flush=True,
                )
        except Exception as e:
            print(f"[Fresh VOD Warning] {vod['channel_name']}: {e}", flush=True)

    return harvested

# ==============================================================================
# PART 12 OF 30: VOCAL & EXPLOSION RMS AUDIO SPIKE DETECTOR
# Subheading: Pinpoints Exact Laughter, Scream & Crash Peaks in 0.5s Windows
# Code Title: detect_audio_reaction_peak
# ==============================================================================
def detect_audio_reaction_peak(video_path):
    """
    Analyzes audio energy in 0.5s blocks to locate the sharpest reaction/explosion
    timestamp and computes its spike ratio above the median baseline volume.
    """
    wav_tmp = f"probe_aud_{random.randint(10000, 99999)}.wav"
    try:
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-ac", "1", "-ar", "16000",
                wav_tmp,
            ],
            label="AudioSpikeProbe",
            timeout_sec=35,
        )
        with wave.open(wav_tmp, "rb") as wf:
            sr = wf.getframerate()
            raw_bytes = wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
        if len(samples) < sr * 4:
            return 10.0, 1.0

        win = int(sr * 0.5)
        step = int(sr * 0.25)
        rms_vals = []
        times = []
        for idx in range(0, len(samples) - win, step):
            seg = samples[idx : idx + win]
            rms_vals.append(float(np.sqrt(np.mean(seg * seg))))
            times.append((idx + win * 0.5) / sr)

        if not rms_vals:
            return 10.0, 1.0

        arr = np.array(rms_vals)
        # Keep peak away from the very first 3.5s or final 1.5s so cuts stay clean
        valid_mask = [(t >= 3.5 and t <= (times[-1] - 1.5)) for t in times]
        if any(valid_mask):
            masked_arr = np.where(valid_mask, arr, -1.0)
            best_i = int(np.argmax(masked_arr))
        else:
            best_i = int(np.argmax(arr))

        baseline = max(float(np.median(arr)), 1.0)
        spike_ratio = round(float(arr[best_i]) / baseline, 2)
        return round(float(times[best_i]), 2), spike_ratio
    except Exception:
        return 10.0, 1.0
    finally:
        if os.path.exists(wav_tmp):
            try:
                os.remove(wav_tmp)
            except Exception:
                pass

# ==============================================================================
# PART 13 OF 30: OPENCV MOTION INTENSITY & STATIC/AD-SCREEN FILTER
# Subheading: Frame-Difference Action Scorer & Static/Loading Rejector
# Code Title: extract_motion_and_key_frames
# ==============================================================================
def extract_motion_and_key_frames(video_path, center_sec):
    """
    Samples 9 frames across [center_sec - 5.5s, center_sec + 2.5s], computes real
    pixel motion score (0-10), rejects frozen/loading screens, and returns PIL frames.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False, 0.0, []

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = total_frames / max(fps, 1.0)
    if dur < 6.0:
        cap.release()
        return False, 0.0, []

    start_t = max(0.5, center_sec - 5.5)
    end_t = min(dur - 0.5, center_sec + 2.5)
    if end_t <= start_t:
        start_t, end_t = 0.5, max(1.5, dur - 0.5)

    sample_ts = np.linspace(start_t, end_t, num=9)
    gray_prev = None
    diffs = []
    pil_frames = []

    for ts in sample_ts:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(ts * fps))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue

        # Reject pitch-black loading screens
        if float(np.mean(frame)) < 12.0:
            continue

        small_gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        if gray_prev is not None:
            diffs.append(float(np.mean(cv2.absdiff(small_gray, gray_prev))))
        gray_prev = small_gray

        rgb = cv2.cvtColor(cv2.resize(frame, (384, 216)), cv2.COLOR_BGR2RGB)
        pil_frames.append(Image.fromarray(rgb))

    cap.release()

    if len(pil_frames) < 4 or not diffs:
        return False, 0.0, []

    mean_diff = float(np.mean(diffs))
    if mean_diff < 1.8:
        return False, 0.0, []

    motion_score = round(min(10.0, (mean_diff / 18.0) * 10.0), 2)
    return True, motion_score, pil_frames

# ==============================================================================
# PART 14 OF 30: MULTI-SIGNAL NEURAL + CROWD CLIP RATER
# Subheading: Combines OpenCLIP Action Semantics, Crowd Heatmaps, Motion & Audio
# Code Title: rate_gta6_candidates
# ==============================================================================
_OPENCLIP_CACHE = None


def _get_openclip_rater():
    global _OPENCLIP_CACHE
    if _OPENCLIP_CACHE is not None:
        return _OPENCLIP_CACHE

    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k"
    )
    model.eval()
    tokenizer = open_clip.get_tokenizer("ViT-B-32")

    pos_prompts = [
        "intense video game car crash explosion mid air stunt or high speed police chase",
        "chaotic video game shootout helicopter crash ragdoll fail or hilarious physics glitch",
        "dramatic speedboat ocean crash street race collision or epic heist escape in video game",
    ]
    neg_prompts = [
        "video game pause menu settings screen map navigation or inventory UI",
        "video game character calmly walking on empty sidewalk or standing still talking",
        "loading screen black screen or static stream starting soon banner",
    ]

    with torch.no_grad():
        pos_emb = model.encode_text(tokenizer(pos_prompts))
        pos_emb /= pos_emb.norm(dim=-1, keepdim=True)
        neg_emb = model.encode_text(tokenizer(neg_prompts))
        neg_emb /= neg_emb.norm(dim=-1, keepdim=True)

    _OPENCLIP_CACHE = (model, preprocess, pos_emb, neg_emb)
    return _OPENCLIP_CACHE


def rate_gta6_candidates(candidates):
    """
    Scores each harvested GTA 6 slice across 4 signals (0-10 scale) and keeps
    only clips meeting CHANNEL_CONFIG['min_viral_score'].
    """
    model, preprocess, pos_emb, neg_emb = _get_openclip_rater()
    winners = []

    for cand in candidates:
        peak_sec, jump_ratio = detect_audio_reaction_peak(cand["video_path"])
        cand["audio_peak"] = peak_sec
        cand["jump_ratio"] = jump_ratio

        ok_motion, motion_score, pil_frames = extract_motion_and_key_frames(
            cand["video_path"], peak_sec
        )
        if not ok_motion or not pil_frames:
            print(f"[Rater REJECT] {cand['channel_name']} -> Frozen/Low-Motion screen", flush=True)
            continue

        try:
            tensors = torch.stack([preprocess(im) for im in pil_frames])
            with torch.no_grad():
                img_emb = model.encode_image(tensors)
                img_emb /= img_emb.norm(dim=-1, keepdim=True)

                frame_pos = (img_emb @ pos_emb.T).max(dim=-1).values
                frame_neg = (img_emb @ neg_emb.T).max(dim=-1).values
                margins = (frame_pos - frame_neg).cpu().numpy()

                peak_margin = float(np.max(margins))
                mean_margin = float(np.mean(margins))

            # Reject routine walking, maps, or menu screens
            if peak_margin < -0.005 or mean_margin < -0.02:
                print(
                    f"[Rater REJECT] {cand['channel_name']} -> Menu/Map/Calm filler (margin={peak_margin:.3f})",
                    flush=True,
                )
                continue

            action_10 = max(0.0, min(10.0, ((peak_margin * 0.65 + mean_margin * 0.35) + 0.05) * 70.0))
            hm_10 = float(cand.get("heatmap_score", 0.0)) * 10.0
            audio_10 = min(10.0, float(jump_ratio) * 2.5)

            if hm_10 > 0:
                final_score = (action_10 * 0.35) + (hm_10 * 0.35) + (motion_score * 0.15) + (audio_10 * 0.15)
            else:
                final_score = (action_10 * 0.45) + (motion_score * 0.30) + (audio_10 * 0.25)

            final_score = round(min(10.0, final_score), 2)
            print(
                f"[Clip Rater] {cand['channel_name']} ({cand['source_type']}) -> "
                f"Score={final_score}/10 (action={action_10:.1f}, hm={hm_10:.1f}, "
                f"motion={motion_score:.1f}, audio={audio_10:.1f})",
                flush=True,
            )

            if final_score >= float(CHANNEL_CONFIG["min_viral_score"]):
                cand["viral_score"] = final_score
                cand["motion_score"] = motion_score
                winners.append(cand)
        except Exception as e:
            print(f"[Rater Warning] {cand['channel_name']}: {e}", flush=True)

    winners.sort(key=lambda x: x["viral_score"], reverse=True)
    return winners

# ==============================================================================
# PART 15 OF 30: PERSISTENT VAULT BANKER
# Subheading: Banks High-Scoring 26s Clips Across Hours 1–5 & Purges Stale Files
# Code Title: bank_winners_into_vault & prune_expired_vault_items
# ==============================================================================
def prune_expired_vault_items(history_data):
    """Removes uncompiled vault clips older than max_vault_age_hours or missing on disk."""
    max_age_sec = float(CHANNEL_CONFIG["max_vault_age_hours"]) * 3600.0
    now_ts = time.time()
    kept = []

    for item in history_data.get("vault_queue", []):
        vpath = item.get("vault_path", "")
        added_ts = float(item.get("added_epoch", 0))
        if not vpath or not os.path.exists(vpath):
            continue
        if (now_ts - added_ts) > max_age_sec:
            try:
                os.remove(vpath)
            except Exception:
                pass
            continue
        kept.append(item)

    history_data["vault_queue"] = kept


def bank_winners_into_vault(scored_winners, history_data):
    """
    Copies winning 26s slices into clip_vault/ and registers them in history_data['vault_queue']
    so Hours 1-5 build up a multi-creator pool for the T-1h Top-5 Compiler.
    """
    ensure_vault_dir()
    prune_expired_vault_items(history_data)
    added_count = 0

    for cand in scored_winners:
        vid_id = cand["video_id"]
        peak_sec = float(cand["origin_peak_sec"])
        if is_clip_already_used_or_banked(history_data, vid_id, peak_sec):
            continue

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        vault_fname = f"vault_{stamp}_{vid_id}_{int(round(peak_sec))}.mp4"
        vault_path = os.path.join(VAULT_DIR, vault_fname)
        shutil.copy2(cand["video_path"], vault_path)

        history_data["vault_queue"].append({
            "vault_path": vault_path,
            "video_id": vid_id,
            "channel_name": cand["channel_name"],
            "source_title": cand["source_title"],
            "source_type": cand["source_type"],
            "heatmap_score": float(cand.get("heatmap_score", 0.0)),
            "viral_score": float(cand["viral_score"]),
            "audio_peak": float(cand["audio_peak"]),
            "origin_peak_sec": peak_sec,
            "added_epoch": time.time(),
        })
        added_count += 1

    print(
        f"[Vault Banker] Banked {added_count} new GTA 6 gem(s) -> "
        f"Total ready in Vault: {len(history_data['vault_queue'])} clip(s).",
        flush=True,
    )

# ==============================================================================
# PART 16 OF 30: T-MINUS 1 HOUR SLOT GATE & DAILY QUOTA GUARD
# Subheading: Triggers Compilation 1 Hour Before Each 6-Hour Slot & Guards Quota
# Code Title: check_compile_trigger_and_slot
# ==============================================================================
def count_todays_uploads(history_data):
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(
        1
        for u in history_data.get("uploads", [])
        if u.get("uploaded") and str(u.get("created_at", "")).startswith(today_prefix)
    )


def get_next_unscheduled_6h_slot(history_data, now_utc=None):
    """Finds the single closest upcoming 6-hour peak slot not yet scheduled."""
    now = now_utc or datetime.now(timezone.utc)
    scheduled_set = set(history_data.get("scheduled_slots", []))
    slot_hours = sorted(int(h) for h in CHANNEL_CONFIG["slot_hours_utc"])

    candidates = []
    for day_offset in (0, 1):
        base_day = (now + timedelta(days=day_offset)).replace(minute=0, second=0, microsecond=0)
        for hr in slot_hours:
            cand_dt = base_day.replace(hour=hr)
            if cand_dt > (now + timedelta(minutes=15)):
                candidates.append(cand_dt)

    candidates.sort()
    for slot_dt in candidates:
        iso_str = slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if iso_str not in scheduled_set:
            return slot_dt, iso_str
    return None, None


def check_compile_trigger_and_slot(history_data):
    """
    Decides whether this hourly run should compile & schedule a Short right now:
    - Always harvests first (Hours 1-5 + Compile Hour).
    - Compiles when within ~1 hour of the next unscheduled 6h slot (or in local test mode).
    """
    do_upload = bool(CHANNEL_CONFIG["upload_to_youtube"])
    do_schedule = bool(CHANNEL_CONFIG["enable_scheduling"])

    if not do_upload:
        # Local artifact test mode: compile whenever Vault has >= 1 clip
        return True, None

    if count_todays_uploads(history_data) >= int(CHANNEL_CONFIG["max_daily_uploads"]):
        print("[Quota Guard] Daily upload cap reached. Banking clips only.", flush=True)
        return False, None

    if not do_schedule:
        return True, None

    now = datetime.now(timezone.utc)
    slot_dt, slot_iso = get_next_unscheduled_6h_slot(history_data, now)
    if not slot_dt:
        return False, None

    hours_until_slot = (slot_dt - now).total_seconds() / 3600.0
    offset_hr = float(CHANNEL_CONFIG["compile_offset_hours"])

    # Trigger compilation when we are <= (offset_hr + 0.35h) away from the target slot
    if hours_until_slot <= (offset_hr + 0.35):
        print(
            f"[T-1h Compiler Trigger] Target slot {slot_iso} is {hours_until_slot:.2f}h away -> Compiling Top Vault Clips!",
            flush=True,
        )
        return True, slot_iso

    print(
        f"[Harvest-Only Hour] Next slot {slot_iso} is {hours_until_slot:.2f}h away -> Clips banked in Vault.",
        flush=True,
    )
    return False, None

# ==============================================================================
# PART 17 OF 30: TOP-5 DISTINCT-CREATOR VAULT CHOOSER & PACING BALANCER
# Subheading: Picks Up to 5 Top Distinct Creators & Trims Crisp Climax Windows
# Code Title: build_top5_plan_from_vault
# ==============================================================================
def build_top5_plan_from_vault(history_data):
    """
    Reviews all clips banked in history_data['vault_queue'], picks up to
    CHANNEL_CONFIG['max_clips_per_short'] (max 5) distinct-creator winners,
    and trims crisp climax durations:
    - 5 clips: 12.5s each (~62.5s Short | #5 -> #4 -> #3 -> #2 -> #1)
    - 4 clips: 13.0s each (~52.0s Short | #4 -> #3 -> #2 -> #1)
    - 3 clips: 14.0s each (~42.0s Short | #3 -> #2 -> #1)
    - 2 clips: 16.0s each (~32.0s Short | #2 -> #1)
    - 1 clip:  21.5s single viral climax (#1)
    """
    prune_expired_vault_items(history_data)
    vault_items = list(history_data.get("vault_queue", []))
    if not vault_items:
        print("[Vault Chooser] Vault queue is empty—nothing to compile yet.", flush=True)
        return None

    vault_items.sort(key=lambda x: float(x.get("viral_score", 0.0)), reverse=True)
    max_clips = min(5, max(1, int(CHANNEL_CONFIG["max_clips_per_short"])))

    chosen = []
    seen_creators = set()
    for item in vault_items:
        cname = str(item.get("channel_name", "")).strip().lower()
        if cname in seen_creators:
            continue
        if not os.path.exists(item.get("vault_path", "")):
            continue
        chosen.append(item)
        seen_creators.add(cname)
        if len(chosen) >= max_clips:
            break

    if not chosen:
        return None

    count = len(chosen)
    dur_map = {1: 21.5, 2: 16.0, 3: 14.0, 4: 13.0, 5: 12.5}
    seg_dur = dur_map[count]

    # Order ascending by score so the #1 best clip always plays LAST
    ordered_asc = list(reversed(chosen))
    ranks = tuple(range(count, 0, -1))
    segments = []

    for rank_num, item in zip(ranks, ordered_asc):
        vpath = item["vault_path"]
        clip_dur = get_media_duration(vpath)
        peak = float(item.get("audio_peak", clip_dur * 0.65))
        end_t = min(clip_dur - 0.25, peak + (seg_dur * 0.28))
        start_t = max(0.0, end_t - seg_dur)
        actual_dur = round(end_t - start_t, 2)

        # Transcribe & translate spoken audio inside the exact trimmed climax window
        eng_text, word_events = transcribe_window_to_english(vpath, start_t, end_t)

        segments.append({
            "rank": rank_num,
            "vault_item": item,
            "video_path": vpath,
            "channel_name": item["channel_name"],
            "source_title": item["source_title"],
            "viral_score": item["viral_score"],
            "start_sec": round(start_t, 2),
            "end_sec": round(end_t, 2),
            "duration": actual_dur,
            "transcript_en": eng_text,
            "word_events": word_events,
        })

    mode_label = "SINGLE" if count == 1 else f"TOP_{count}_COUNTDOWN"
    print(
        f"[Vault Chooser] Selected {count} distinct creator(s) for {mode_label}: "
        + ", ".join(f"#{s['rank']} {s['channel_name']} ({s['viral_score']}/10)" for s in segments),
        flush=True,
    )
    return {"mode": mode_label, "segments": segments}

# ==============================================================================
# PART 18 OF 30: FASTER-WHISPER MULTILINGUAL-TO-ENGLISH TRANSLATOR
# Subheading: Translates Streamer Speech Into Synced English Word Timestamps
# Code Title: transcribe_window_to_english
# ==============================================================================
_WHISPER_CACHE = None


def _get_whisper_model():
    global _WHISPER_CACHE
    if _WHISPER_CACHE is None:
        _WHISPER_CACHE = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _WHISPER_CACHE


def transcribe_window_to_english(video_path, start_sec, end_sec):
    """
    Translates spoken dialogue in [start_sec, end_sec] into English text and
    returns (full_english_sentence, list_of_word_dicts_relative_to_window_start).
    """
    wav_tmp = f"whisper_win_{random.randint(10000, 99999)}.wav"
    try:
        dur = max(1.0, float(end_sec) - float(start_sec))
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-ss", f"{start_sec:.2f}", "-t", f"{dur:.2f}",
                "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
                wav_tmp,
            ],
            label="WhisperWindowWav",
            timeout_sec=35,
        )
        model = _get_whisper_model()
        segments, _ = model.transcribe(
            wav_tmp,
            task="translate",
            beam_size=1,
            word_timestamps=True,
            vad_filter=True,
        )
        words_out = []
        full_parts = []
        for seg in segments:
            txt = seg.text.strip()
            if txt:
                full_parts.append(txt)
            for w in getattr(seg, "words", []) or []:
                clean_w = w.word.strip()
                if clean_w:
                    words_out.append({
                        "word": clean_w,
                        "start": round(float(w.start), 2),
                        "end": round(float(w.end), 2),
                    })
        return " ".join(full_parts).strip(), words_out
    except Exception:
        return "", []
    finally:
        if os.path.exists(wav_tmp):
            try:
                os.remove(wav_tmp)
            except Exception:
                pass

# ==============================================================================
# PART 19 OF 30: GEMINI 2.5 SCENE-GROUNDED TITLE, BANNER & 3-ACT SCRIPT WRITER
# Subheading: Inspects Climax Frames to Write Accurate English Hook & Punchline
# Code Title: generate_scene_grounded_script
# ==============================================================================
def generate_scene_grounded_script(short_plan, history_data):
    """
    Uses Gemini 2.5 Flash vision on the actual winning GTA 6 frames to generate
    a scene-grounded English title, top banner, Act 1 hook, and Act 3 punchline.
    """
    segments = short_plan["segments"]
    creators = [s["channel_name"] for s in segments]
    top_seg = segments[-1]  # #1 climax plays last
    recent_titles = [u.get("title", "") for u in history_data.get("uploads", [])[-12:]]

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
            f"- Rank #{seg['rank']} | Creator: {seg['channel_name']} | "
            f"Stream Title: {seg['source_title']} | "
            f"Translated Dialogue: '{seg['transcript_en'] or 'intense gameplay reaction'}'"
        )

    prompt_text = (
        f"You are writing metadata and voiceover lines for {CHANNEL_CONFIG['channel_name']} "
        f"({CHANNEL_CONFIG['channel_handle']}), a Grand Theft Auto VI Shorts channel.\n"
        f"Inspect the attached gameplay frames and clip details:\n"
        + "\n".join(context_lines)
        + f"\nAvoid repeating any of these recent titles: {recent_titles}\n\n"
        f"Return STRICT valid JSON with these exact 5 keys:\n"
        f'1. "title": Catchy, accurate English YouTube Shorts title describing what actually happens (max 68 chars, 1 emoji, no #Shorts tag yet).\n'
        f'2. "banner": Short 3-to-5 word UPPERCASE hook banner for the top of the screen (e.g. "5 INSANE VICE CITY FAILS").\n'
        f'3. "intro_vo": Energetic 1-sentence English spoken hook (10-14 words, ~3.5s) setting up the clips.\n'
        f'4. "outro_vo": Punchy 1-sentence English closing reaction/question (9-13 words, ~3.2s) after the #1 climax.\n'
        f'5. "tags": List of 8 relevant English tags including "GTA 6", "Grand Theft Auto 6", "Vice City", and creator names.\n'
    )
    prompt_parts.append(prompt_text)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=45000),
        )
        for model_id in ("gemini-2.5-flash", "gemini-3.5-flash-lite"):
            try:
                resp = client.models.generate_content(
                    model=model_id,
                    contents=prompt_parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.75,
                    ),
                )
                parsed = json.loads(resp.text.strip())
                if parsed.get("title") and parsed.get("intro_vo") and parsed.get("outro_vo"):
                    parsed["creators"] = creators
                    print(f"[Gemini Scene Writer] Generated via {model_id}: '{parsed['title']}'", flush=True)
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

    count = len(segments)
    top_creator = top_seg["channel_name"]
    if count > 1:
        fallback_title = f"Top {count} Craziest GTA 6 Moments Featuring {top_creator} 🔥"
        fallback_banner = f"TOP {count} VICE CITY MOMENTS"
        fallback_intro = f"Here are the top {count} wildest Vice City moments caught on stream today!"
    else:
        fallback_title = f"{top_creator} Had The Wildest Moment In Vice City 😱"
        fallback_banner = "PURE VICE CITY CHAOS"
        fallback_intro = f"Watch how fast things spiral out of control for {top_creator} in Vice City!"

    return {
        "title": fallback_title[:75],
        "banner": fallback_banner,
        "intro_vo": fallback_intro,
        "outro_vo": f"Rate {top_creator}'s Vice City moment from 1 to 10 in the comments!",
        "tags": ["GTA 6", "Grand Theft Auto 6", "Vice City", "GTA VI", "Shorts"] + creators[:3],
        "creators": creators,
    }

# ==============================================================================
# PART 20 OF 30: VERIFIED WEBCAM CORNER DETECTOR
# Subheading: Safe OpenCV Face Verification for Splitcam vs. Cinema Framing
# Code Title: detect_verified_webcam_box
# ==============================================================================
def detect_verified_webcam_box(video_path, start_sec, end_sec):
    """
    Safely checks 5 frames in [start_sec, end_sec] for a consistent streamer facecam
    in one of the 4 corners. Returns (x, y, w, h) in source pixels or None.
    """
    try:
        cascade_cls = getattr(cv2, "CascadeClassifier", None)
        cv2_data = getattr(cv2, "data", None)
        if cascade_cls is None or cv2_data is None:
            return None

        cascade_path = cv2_data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cascade_cls(cascade_path)
        if face_cascade.empty():
            return None

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)

        sample_ts = np.linspace(start_sec + 0.5, max(start_sec + 1.0, end_sec - 0.5), num=5)
        hits = []

        for ts in sample_ts:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(ts * fps))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48))
            for (fx, fy, fw, fh) in faces:
                cx = (fx + fw * 0.5) / src_w
                cy = (fy + fh * 0.5) / src_h
                in_left = cx <= 0.28
                in_right = cx >= 0.72
                in_top_or_bot = cy <= 0.36 or cy >= 0.64
                if (in_left or in_right) and in_top_or_bot:
                    hits.append((cx, cy, fx, fy, fw, fh))
                    break

        cap.release()

        if len(hits) < 3:
            return None

        xs = [cx for (cx, _, _, _, _, _) in hits]
        ys = [cy for (_, cy, _, _, _, _) in hits]
        if (max(xs) - min(xs)) > 0.08 or (max(ys) - min(ys)) > 0.08:
            return None

        med_cx = float(np.median(xs)) * src_w
        med_cy = float(np.median(ys)) * src_h
        cam_w = int(src_w * 0.26)
        cam_h = int(src_h * 0.28)

        box_x = int(max(0, min(src_w - cam_w, med_cx - cam_w * 0.5)))
        box_y = int(max(0, min(src_h - cam_h, med_cy - cam_h * 0.5)))
        return (box_x, box_y, cam_w, cam_h)
    except Exception:
        return None

# ==============================================================================
# PART 21 OF 30: 1080X1920 VERTICAL LAYOUT RENDERER
# Subheading: Renders Verified Splitcam or Full 16:9 Cinema Center With Rank Badges
# Code Title: render_vertical_segments_and_concat
# ==============================================================================
def _escape_drawtext(txt):
    return (
        str(txt or "")
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def render_vertical_segments_and_concat(short_plan):
    """
    Renders each chosen GTA 6 segment to 1080x1920 @ 30fps with dynamic creator
    credit + rank badge, then concatenates all segments into 'stitched_video.mp4'.
    Returns (stitched_path, timeline_segments_with_offsets).
    """
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    segments = short_plan["segments"]
    is_multi = len(segments) > 1

    rendered_files = []
    timeline_meta = []
    cursor_sec = 0.0

    for idx, seg in enumerate(segments):
        seg_out = f"seg_render_{idx}.mp4"
        if os.path.exists(seg_out):
            os.remove(seg_out)

        start_s = float(seg["start_sec"])
        dur_s = float(seg["duration"])
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

        # Top watermark + dynamic per-clip creator credit
        text_chain = (
            f"[canvas]drawtext=fontfile={font_bold}:text='{wm_label}':"
            f"fontsize=26:fontcolor=white@0.88:x=(w-text_w)/2:y=56:"
            f"shadowcolor=black@0.85:shadowx=2:shadowy=2,"
            f"drawtext=fontfile={font_bold}:text='{creator_label}':"
            f"fontsize=25:fontcolor=#FFD54F@0.95:x=(w-text_w)/2:y=94:"
            f"shadowcolor=black@0.90:shadowx=2:shadowy=2"
        )

        # Add rank badge (#5 -> #1) when compiling 2-5 clips
        if is_multi:
            badge_color = "#FF1744" if rank_num == 1 else "#00E5FF"
            text_chain += (
                f",drawbox=x=42:y=146:w=118:h=58:color={badge_color}@0.92:t=fill,"
                f"drawtext=fontfile={font_bold}:text='#{rank_num}':"
                f"fontsize=36:fontcolor=white:x=68:y=158:"
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
        timeline_meta.append({
            **seg,
            "offset_start": round(cursor_sec, 3),
            "offset_end": round(cursor_sec + actual_d, 3),
            "rendered_dur": round(actual_d, 3),
        })
        cursor_sec += actual_d
        print(f"[Segment #{rank_num}] Rendered {seg['channel_name']} ({layout_mode}, {actual_d:.1f}s)", flush=True)

    stitched_out = "stitched_video.mp4"
    if os.path.exists(stitched_out):
        os.remove(stitched_out)

    if len(rendered_files) == 1:
        shutil.copy2(rendered_files[0], stitched_out)
    else:
        concat_txt = "concat_list.txt"
        with open(concat_txt, "w", encoding="utf-8") as cf:
            for rf in rendered_files:
                cf.write(f"file '{os.path.abspath(rf)}'\n")
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_txt, "-c", "copy", stitched_out,
            ],
            label="ConcatSegments",
            timeout_sec=90,
        )

    return stitched_out, timeline_meta

# ==============================================================================
# PART 22 OF 30: ZERO-FILE SYNTHESIZED SUB-BASS COUNTDOWN SFX GENERATOR
# Subheading: Generates Punchy Transition Impact Drops via FFmpeg Audio Math
# Code Title: generate_countdown_impact_sfx
# ==============================================================================
def generate_countdown_impact_sfx(out_wav="impact_drop.wav"):
    """Synthesizes a clean 0.42s sub-bass impact drop using FFmpeg aevalsrc."""
    if os.path.exists(out_wav):
        os.remove(out_wav)
    expr = "0.75*sin(2*PI*(95*t - 55*t*t))*exp(-7.5*t)"
    run_cmd_safe(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"aevalsrc='{expr}':s=44100:d=0.42",
            "-af", "afade=t=in:st=0:d=0.015,afade=t=out:st=0.22:d=0.20",
            out_wav,
        ],
        label="SynthImpactSFX",
        timeout_sec=25,
    )
    return out_wav

# ==============================================================================
# PART 23 OF 30: NEURAL ENGLISH VOICEOVER SYNTHESIZER
# Subheading: Synthesizes Crisp Act 1 Intro & Act 3 Outro Voice Tracks
# Code Title: synthesize_voiceover_mp3
# ==============================================================================
async def _edge_tts_save(text, voice, out_path):
    comm = edge_tts.Communicate(text, voice=voice, rate="+8%")
    await comm.save(out_path)


def synthesize_voiceover_mp3(text, out_path):
    """Generates clean neural English speech MP3 and returns its duration in seconds."""
    if os.path.exists(out_path):
        os.remove(out_path)
    clean_txt = re.sub(r"[^\w\s.,!?'-]", "", str(text or "")).strip()
    if not clean_txt:
        return 0.0

    try:
        asyncio.run(_edge_tts_save(clean_txt, CHANNEL_CONFIG["voice_name"], out_path))
        dur = get_media_duration(out_path)
        if dur > 0.4:
            return dur
    except Exception:
        pass

    try:
        tts = gTTS(text=clean_txt, lang="en", slow=False)
        tts.save(out_path)
        return get_media_duration(out_path)
    except Exception:
        return 0.0

# ==============================================================================
# PART 24 OF 30: 3-ACT AUDIO TIMELINE MIXER & COPYRIGHT AUDIO SHIELD
# Subheading: Silent Narrator During Climaxes + Impact SFX + Loudness Normalization
# Code Title: mix_three_act_audio_master
# ==============================================================================
def mix_three_act_audio_master(stitched_video, timeline_meta, script_data):
    """
    Builds the 3-Act audio master:
    - Act 1 (0.0s -> ~3.4s): Intro hook voiceover (game audio gently ducked)
    - Act 2 (Middle climaxes): Narrator 100% SILENT so streamer reactions shine;
      sub-bass impact drops fire at each rank transition (#5 -> #1)
    - Act 3 (Final ~3.3s): Outro punchline voiceover
    """
    total_dur = get_media_duration(stitched_video)
    mixed_out = "audio_mixed_video.mp4"
    if os.path.exists(mixed_out):
        os.remove(mixed_out)

    intro_mp3 = "vo_intro.mp3"
    outro_mp3 = "vo_outro.mp3"
    intro_dur = synthesize_voiceover_mp3(script_data.get("intro_vo", ""), intro_mp3) if CHANNEL_CONFIG["enable_ai_voiceover"] else 0.0
    outro_dur = synthesize_voiceover_mp3(script_data.get("outro_vo", ""), outro_mp3) if CHANNEL_CONFIG["enable_ai_voiceover"] else 0.0

    has_intro = intro_dur > 0.4 and os.path.exists(intro_mp3)
    has_outro = outro_dur > 0.4 and os.path.exists(outro_mp3) and total_dur > (intro_dur + outro_dur + 4.0)
    outro_start = max(intro_dur + 2.0, total_dur - outro_dur - 0.15) if has_outro else total_dur

    # Collect transition timestamps for multi-clip countdown impact drops
    transition_times = [
        round(seg["offset_start"], 3)
        for seg in timeline_meta[1:]
        if seg["offset_start"] > 1.0
    ]
    has_sfx = len(transition_times) > 0
    sfx_wav = generate_countdown_impact_sfx() if has_sfx else None

    cmd = ["ffmpeg", "-y", "-i", stitched_video]
    in_idx = 1
    intro_idx = outro_idx = sfx_idx = None

    if has_intro:
        cmd.extend(["-i", intro_mp3])
        intro_idx = in_idx
        in_idx += 1
    if has_outro:
        cmd.extend(["-i", outro_mp3])
        outro_idx = in_idx
        in_idx += 1
    if has_sfx and sfx_wav and os.path.exists(sfx_wav):
        cmd.extend(["-i", sfx_wav])
        sfx_idx = in_idx
        in_idx += 1

    # Duck game audio only during Act 1 intro and Act 3 outro; 100% full volume in Act 2
    duck_conds = []
    if has_intro:
        duck_conds.append(f"between(t,0,{intro_dur + 0.15:.2f})")
    if has_outro:
        duck_conds.append(f"between(t,{outro_start:.2f},{total_dur:.2f})")

    if duck_conds:
        expr = "+".join(duck_conds)
        game_filter = f"[0:a]highpass=f=45,volume='if(gt({expr},0),0.32,1.0)':eval=frame[agame]"
    else:
        game_filter = "[0:a]highpass=f=45,volume=1.0[agame]"

    filter_parts = [game_filter]
    mix_inputs = ["[agame]"]

    if has_intro:
        filter_parts.append(f"[{intro_idx}:a]adelay=0|0,volume=1.25[aintro]")
        mix_inputs.append("[aintro]")

    if has_outro:
        delay_ms = int(round(outro_start * 1000))
        filter_parts.append(f"[{outro_idx}:a]adelay={delay_ms}|{delay_ms},volume=1.25[aoutro]")
        mix_inputs.append("[aoutro]")

    if sfx_idx is not None:
        splits = "".join(f"[sfx{i}]" for i in range(len(transition_times)))
        filter_parts.append(f"[{sfx_idx}:a]asplit={len(transition_times)}{splits}")
        for i, t_sec in enumerate(transition_times):
            t_ms = max(0, int(round((t_sec - 0.12) * 1000)))
            filter_parts.append(f"[sfx{i}]adelay={t_ms}|{t_ms},volume=0.85[sfxd{i}]")
            mix_inputs.append(f"[sfxd{i}]")

    if len(mix_inputs) == 1:
        filter_parts.append("[agame]loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    else:
        joined = "".join(mix_inputs)
        filter_parts.append(
            f"{joined}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0:normalize=0,"
            f"loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )

    run_cmd_safe(
        cmd + [
            "-filter_complex", ";".join(filter_parts),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            mixed_out,
        ],
        label="ThreeActAudioMix",
        timeout_sec=110,
    )

    print(
        f"[3-Act Audio] Intro 0.0-{intro_dur:.1f}s | SILENT CLIMAX {intro_dur:.1f}-{outro_start:.1f}s | "
        f"Outro {outro_start:.1f}-{total_dur:.1f}s | Transition SFX={len(transition_times)}",
        flush=True,
    )
    return mixed_out, round(intro_dur, 2), round(outro_start, 2)

# ==============================================================================
# PART 25 OF 30: TOP HOOK BANNER & SYNCED ENGLISH ASS SUBTITLE BURNER
# Subheading: Burns Clean Top Hook Banner & Word-Synced English Captions
# Code Title: burn_banner_and_english_captions
# ==============================================================================
def _fmt_ass_time(sec):
    s = max(0.0, float(sec))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    rem = s % 60
    return f"{h}:{m:02d}:{rem:05.2f}"


def _chunk_words_to_events(text_or_words, start_offset, max_dur, is_word_list=False):
    """Groups words into crisp 3-word caption chunks with exact start/end timestamps."""
    events = []
    if is_word_list:
        buf = []
        for w in text_or_words:
            buf.append(w)
            if len(buf) >= 3:
                t0 = start_offset + float(buf[0]["start"])
                t1 = start_offset + max(float(buf[-1]["end"]), float(buf[0]["start"]) + 0.35)
                phrase = " ".join(x["word"] for x in buf).upper()
                events.append((t0, t1, phrase))
                buf = []
        if buf:
            t0 = start_offset + float(buf[0]["start"])
            t1 = start_offset + max(float(buf[-1]["end"]), float(buf[0]["start"]) + 0.35)
            phrase = " ".join(x["word"] for x in buf).upper()
            events.append((t0, t1, phrase))
        return events

    words = [w.strip() for w in str(text_or_words or "").split() if w.strip()]
    if not words or max_dur <= 0.3:
        return events
    chunks = [" ".join(words[i : i + 3]).upper() for i in range(0, len(words), 3)]
    step = max_dur / len(chunks)
    for idx, ch in enumerate(chunks):
        events.append((start_offset + idx * step, start_offset + (idx + 1) * step, ch))
    return events


def burn_banner_and_english_captions(audio_mixed_video, timeline_meta, script_data, intro_dur, outro_start):
    """
    Burns the top hook banner + bold yellow/white English captions onto the Short
    and outputs 'final_short_<timestamp>.mp4'.
    """
    total_dur = get_media_duration(audio_mixed_video)
    ass_path = "captions_master.ass"
    if os.path.exists(ass_path):
        os.remove(ass_path)

    caption_events = []
    # Act 1 exact English hook captions
    if intro_dur > 0.4:
        caption_events.extend(_chunk_words_to_events(script_data.get("intro_vo", ""), 0.05, intro_dur - 0.1))

    # Act 2 translated English captions from each streamer's climax window
    for seg in timeline_meta:
        seg_off = float(seg["offset_start"])
        seg_end = float(seg["offset_end"])
        w_events = seg.get("word_events") or []
        for (t0, t1, phrase) in _chunk_words_to_events(w_events, seg_off, seg_end - seg_off, is_word_list=True):
            # Keep middle captions clear of Act 1 intro and Act 3 outro voiceovers
            if t0 >= (intro_dur + 0.15) and t1 <= (outro_start - 0.15):
                caption_events.append((t0, t1, phrase))

    # Act 3 exact English outro captions
    if outro_start < total_dur - 0.4:
        caption_events.extend(
            _chunk_words_to_events(script_data.get("outro_vo", ""), outro_start, total_dur - outro_start - 0.1)
        )

    ass_header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: ViralCap,DejaVu Sans,54,&H0018E5FF,&H00FFFFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,1,0,1,4,2,2,65,65,330,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [ass_header]
    for (t0, t1, txt) in caption_events:
        clean_ass_txt = txt.replace("{", "").replace("}", "").replace("\n", " ")
        lines.append(f"Dialogue: 0,{_fmt_ass_time(t0)},{_fmt_ass_time(t1)},ViralCap,,0,0,0,,{clean_ass_txt}\n")

    with open(ass_path, "w", encoding="utf-8") as af:
        af.writelines(lines)

    banner_txt = _escape_drawtext(str(script_data.get("banner", "VICE CITY CHAOS")).upper()[:34])
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    vf_chain = (
        f"drawbox=x=95:y=222:w=890:h=84:color=#0B0E17@0.88:t=fill,"
        f"drawbox=x=95:y=222:w=890:h=4:color=#FFD54F@0.95:t=fill,"
        f"drawbox=x=95:y=302:w=890:h=4:color=#FFD54F@0.95:t=fill,"
        f"drawtext=fontfile={font_bold}:text='{banner_txt}':"
        f"fontsize=38:fontcolor=#FFD54F:x=(w-text_w)/2:y=246:"
        f"shadowcolor=black@0.9:shadowx=2:shadowy=2,"
        f"subtitles={ass_path}"
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    final_mp4 = f"final_short_{stamp}.mp4"

    run_cmd_safe(
        [
            "ffmpeg", "-y", "-i", audio_mixed_video,
            "-vf", vf_chain,
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "copy",
            final_mp4,
        ],
        label="BurnBannerAndSubs",
        timeout_sec=160,
    )
    print(f"[Final Render] Created '{final_mp4}' ({get_media_duration(final_mp4):.1f}s)", flush=True)
    return final_mp4

# ==============================================================================
# PART 26 OF 30: CUSTOM 1080X1920 THUMBNAIL FRAME EXTRACTOR
# Subheading: Extracts High-Impact Cover Image From the #1 Climax Finale
# Code Title: extract_short_thumbnail
# ==============================================================================
def extract_short_thumbnail(final_mp4):
    """Extracts a sharp 1080x1920 JPEG thumbnail from the #1 climax finale."""
    total_dur = get_media_duration(final_mp4)
    grab_ts = max(1.0, total_dur * 0.82)
    thumb_jpg = final_mp4.replace(".mp4", "_thumb.jpg")
    try:
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-ss", f"{grab_ts:.2f}",
                "-i", final_mp4, "-vframes", "1", "-q:v", "2",
                thumb_jpg,
            ],
            label="ExtractThumb",
            timeout_sec=25,
        )
        if os.path.exists(thumb_jpg):
            return thumb_jpg
    except Exception as e:
        print(f"[Thumbnail Warning] {e}", flush=True)
    return None

# ==============================================================================
# PART 27 OF 30: YOUTUBE OAUTH & SCHEDULED UPLOADER
# Subheading: Schedules Short for Upcoming 6h Slot, Sets Thumbnail & Posts Credits
# Code Title: upload_or_schedule_short_to_youtube
# ==============================================================================
def _get_youtube_service():
    raw_token = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not raw_token:
        raise RuntimeError("Missing YOUTUBE_TOKEN_JSON secret.")
    token_dict = json.loads(raw_token)
    creds = Credentials.from_authorized_user_info(
        token_dict,
        scopes=[
            "https://googleapis.com",
            "https://googleapis.com",
        ],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_or_schedule_short_to_youtube(final_mp4, thumb_jpg, script_data, timeline_meta, slot_iso=None):
    """
    Uploads final_mp4 to @Vice-City_ClipsVault:
    - If slot_iso is provided, schedules it as private with publishAt=slot_iso.
    - Otherwise uses CHANNEL_CONFIG['youtube_privacy'].
    """
    if not CHANNEL_CONFIG["upload_to_youtube"]:
        print(
            f"[Test Mode] upload_to_youtube=False -> Saved '{final_mp4}' locally for artifact preview.",
            flush=True,
        )
        return None

    youtube = _get_youtube_service()
    raw_title = str(script_data.get("title", "Wildest GTA 6 Moments 🔥")).strip()
    if "#shorts" not in raw_title.lower():
        raw_title = f"{raw_title} #Shorts"
    final_title = raw_title[:98]

    credit_lines = [
        f"• #{seg['rank']} Featured Creator: {seg['channel_name']} ({seg['source_title']})"
        for seg in reversed(timeline_meta)
    ]
    description = (
        f"{script_data.get('intro_vo', '')}\n\n"
        f"🎮 Game: {CHANNEL_CONFIG['game_title']} (Vice City / Leonida)\n"
        f"🎬 Curated & Edited by {CHANNEL_CONFIG['channel_handle']}\n\n"
        f"Featured Official Creators:\n" + "\n".join(credit_lines) + "\n\n"
        f"#GTA6 #GrandTheftAuto6 #ViceCity #GTAVI #Shorts"
    )

    status_body = {"selfDeclaredMadeForKids": False}
    if slot_iso and CHANNEL_CONFIG["enable_scheduling"]:
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = slot_iso
    else:
        status_body["privacyStatus"] = CHANNEL_CONFIG["youtube_privacy"]

    body = {
        "snippet": {
            "title": final_title,
            "description": description,
            "tags": script_data.get("tags", ["GTA 6", "Vice City", "Shorts"]),
            "categoryId": "20",  # Gaming
        },
        "status": status_body,
    }

    media = MediaFileUpload(final_mp4, mimetype="video/mp4", chunksize=-1, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = req.next_chunk()

    video_id = response["id"]
    mode_desc = f"SCHEDULED for {slot_iso}" if slot_iso else f"PUBLISHED ({status_body['privacyStatus']})"
    print(f"[YouTube Upload] {mode_desc} -> https://youtu.be{video_id}", flush=True)

    if thumb_jpg and os.path.exists(thumb_jpg):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_jpg, mimetype="image/jpeg"),
            ).execute()
        except Exception as e:
            print(f"[Thumbnail Upload Note] {e}", flush=True)

    try:
        creators_str = ", ".join(dict.fromkeys(seg["channel_name"] for seg in reversed(timeline_meta)))
        comment_txt = (
            f"Which Vice City moment was your #1? Drop your rating (1-10) below! 👇🔥\n"
            f"Full credit to the original creators: {creators_str}"
        )
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": comment_txt}},
                }
            },
        ).execute()
    except Exception:
        pass

    return video_id

# ==============================================================================
# PART 28 OF 30: VAULT CLEANUP & USED-FINGERPRINT COMMITTER
# Subheading: Marks Compiled Clips as Used, Purges Consumed MP4s & Logs Slot
# Code Title: finalize_vault_and_history_after_compile
# ==============================================================================
def finalize_vault_and_history_after_compile(history_data, timeline_meta, script_data, yt_video_id, slot_iso):
    """
    Updates history.json after compilation:
    - Adds fingerprints of all used segments to 'used_clips'
    - Removes consumed clips from 'vault_queue' and deletes their files in clip_vault/
    - Logs the scheduled slot & upload metadata
    """
    consumed_paths = set()
    for seg in timeline_meta:
        v_item = seg.get("vault_item") or {}
        vid_id = v_item.get("video_id", "")
        peak_s = float(v_item.get("origin_peak_sec", 0.0))
        if vid_id:
            fp = build_clip_fingerprint(vid_id, peak_s)
            if fp not in history_data["used_clips"]:
                history_data["used_clips"].append(fp)
        vpath = v_item.get("vault_path", "")
        if vpath:
            consumed_paths.add(vpath)
            if os.path.exists(vpath):
                try:
                    os.remove(vpath)
                except Exception:
                    pass

    history_data["vault_queue"] = [
        item for item in history_data.get("vault_queue", [])
        if item.get("vault_path") not in consumed_paths
    ]

    if slot_iso and yt_video_id:
        if slot_iso not in history_data["scheduled_slots"]:
            history_data["scheduled_slots"].append(slot_iso)

    history_data["uploads"].append({
        "video_id": yt_video_id or "LOCAL_PREVIEW",
        "title": script_data.get("title", ""),
        "clip_count": len(timeline_meta),
        "creators": [s["channel_name"] for s in reversed(timeline_meta)],
        "scheduled_for": slot_iso,
        "uploaded": bool(yt_video_id),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

# ==============================================================================
# PART 29 OF 30: TEMPORARY WORKSPACE CLEANER
# Subheading: Purges Intermediate Files While Preserving clip_vault & Final Shorts
# Code Title: cleanup_temp_workspace
# ==============================================================================
def cleanup_temp_workspace():
    """Deletes temporary working files while keeping clip_vault/ and final_short_*.mp4."""
    prefixes = ("raw_hm_", "raw_fresh_", "seg_render_", "probe_aud_", "whisper_win_", "gem_ctx_")
    exact_files = (
        "stitched_video.mp4",
        "audio_mixed_video.mp4",
        "vo_intro.mp3",
        "vo_outro.mp3",
        "impact_drop.wav",
        "captions_master.ass",
        "concat_list.txt",
        "yt_cookies.txt",
    )
    for fname in os.listdir("."):
        if fname.startswith(prefixes) or fname in exact_files:
            try:
                os.remove(fname)
            except Exception:
                pass

# ==============================================================================
# PART 30 OF 30: MASTER 2-STAGE PIPELINE ORCHESTRATOR (__MAIN__)
# Subheading: Auto-Expanding Multi-Wave Vault Harvest + T-1h Top-5 Compiler
# Code Title: run_master_pipeline Entrypoint
# ==============================================================================
def run_master_pipeline():
    t0 = time.time()
    target_vault_clips = int(CHANNEL_CONFIG["max_clips_per_short"])
    print(
        f"=== {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) | "
        f"upload_to_youtube={CHANNEL_CONFIG['upload_to_youtube']} | TargetVault={target_vault_clips} ===",
        flush=True,
    )
    history_data = load_history()

    try:
        # ------------------------------------------------------------------
        # STAGE 1: AUTO-EXPANDING MULTI-WAVE VAULT HARVEST (UNTIL >= 5 GEMS)
        # ------------------------------------------------------------------
        discovered = discover_equal_weight_original_streams()
        heatmap_peaks, fresh_vods = scan_heatmaps_in_parallel(discovered, history_data, max_inspect=65)

        hm_cursor = 0
        vod_cursor = 0
        wave_configs = [
            ("Wave 1 (Primary Heatmap + Fresh)", 8, 3),
            ("Wave 2 (Expanded Heatmap + VODs)", 10, 5),
            ("Wave 3 (Deep Reserve Search)", 12, 6),
        ]

        for wave_label, hm_take, vod_take in wave_configs:
            if len(history_data.get("vault_queue", [])) >= target_vault_clips:
                print(
                    f"[Vault Ready] Already have {len(history_data['vault_queue'])} banked clips -> Stopping harvest early!",
                    flush=True,
                )
                break

            # If Wave 3 still needs more candidates, run a fresh deep search pass
            if wave_label.startswith("Wave 3") and hm_cursor >= len(heatmap_peaks) and vod_cursor >= len(fresh_vods):
                print("[Wave 3 Expand] Pool exhausted -> Running deep worldwide expansion scan...", flush=True)
                extra_disc = discover_equal_weight_original_streams()
                seen_ids = {d["video_id"] for d in discovered}
                new_only = [d for d in extra_disc if d["video_id"] not in seen_ids]
                if new_only:
                    extra_hm, extra_vod = scan_heatmaps_in_parallel(new_only, history_data, max_inspect=50)
                    heatmap_peaks.extend(extra_hm)
                    fresh_vods.extend(extra_vod)

            hm_batch = heatmap_peaks[hm_cursor : hm_cursor + hm_take]
            hm_cursor += len(hm_batch)

            vod_batch = fresh_vods[vod_cursor : vod_cursor + vod_take]
            vod_cursor += len(vod_batch)

            if not hm_batch and not vod_batch:
                break

            print(
                f"[{wave_label}] Harvesting {len(hm_batch)} heatmap peaks + {len(vod_batch)} VOD samples "
                f"(Vault={len(history_data.get('vault_queue', []))}/{target_vault_clips})...",
                flush=True,
            )
            candidates = harvest_26s_climax_slices(hm_batch, max_slices=len(hm_batch))
            if vod_batch:
                candidates.extend(harvest_fresh_official_vods(vod_batch, max_samples=len(vod_batch)))

            if candidates:
                winners = rate_gta6_candidates(candidates)
                if winners:
                    bank_winners_into_vault(winners, history_data)
                    save_history(history_data)

            # Clean up temp wave slices before starting next wave
            cleanup_temp_workspace()

        save_history(history_data)

        # ------------------------------------------------------------------
        # STAGE 2: T-MINUS 1 HOUR TOP-5 COMPILER & SLOT SCHEDULER
        # ------------------------------------------------------------------
        should_compile, target_slot_iso = check_compile_trigger_and_slot(history_data)
        if not should_compile:
            print(f"[Complete] Harvest-only run finished in {time.time() - t0:.1f}s.", flush=True)
            return

        short_plan = build_top5_plan_from_vault(history_data)
        if not short_plan:
            print(f"[Complete] No banked clips ready to compile ({time.time() - t0:.1f}s).", flush=True)
            return

        script_data = generate_scene_grounded_script(short_plan, history_data)
        stitched_vid, timeline_meta = render_vertical_segments_and_concat(short_plan)
        mixed_vid, intro_dur, outro_start = mix_three_act_audio_master(
            stitched_vid, timeline_meta, script_data
        )
        final_mp4 = burn_banner_and_english_captions(
            mixed_vid, timeline_meta, script_data, intro_dur, outro_start
        )
        thumb_jpg = extract_short_thumbnail(final_mp4)

        uploaded_id = None
        final_title = script_data["title"]
        if CHANNEL_CONFIG["upload_to_youtube"]:
            uploaded_id, final_title = upload_and_schedule_youtube_short(
                final_mp4, thumb_jpg, script_data, timeline_meta, publish_at_iso=target_slot_iso
            )
            commit_compiled_clips_to_history(
                history_data, timeline_meta, final_title, uploaded_id, target_slot_iso
            )
        else:
            print(
                f"[Test Mode] upload_to_youtube=False -> Saved '{final_mp4}' for artifact preview "
                f"(Vault clips kept intact until live upload).",
                flush=True,
            )

        save_history(history_data)
        print(f"[Complete] 2-Stage pipeline finished cleanly in {time.time() - t0:.1f}s!", flush=True)
    finally:
        cleanup_temp_workspace()


if __name__ == "__main__":
    run_master_pipeline()
    
