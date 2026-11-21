# ==============================================================================
# PART 1 OF 25: CORE & AI LIBRARY IMPORTS
# Title: Standard Utilities, Video/Vision Engines & YouTube API Clients
# Subtitle: Foundation imports for ViceCityClipsVault Auto-Clipper
# ==============================================================================
import os
import re
import sys
import json
import time
import math
import wave
import shutil
import random
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import requests
import torch
import open_clip
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
# PART 2 OF 25: MASTER CHANNEL CONFIG & CREATOR ROSTER
# Title: Upload/Privacy/Schedule Controls & Indian + Global Priority Channels
# Subtitle: Edit these toggles to switch between local testing and live publishing
# ==============================================================================
CHANNEL_CONFIG = {
    "channel_name": "Vice City Clips Vault",
    "channel_handle": "@Vice-City_ClipsVault",
    "game_title": "Grand Theft Auto VI",
    "niche_keywords": ["GTA 6", "Grand Theft Auto 6", "Vice City", "GTA VI"],

    # --- 3 EXPLICIT UPLOAD, PRIVACY & SCHEDULING CONTROLS ---
    "upload_to_youtube": False,      # True = upload to YouTube | False = render & save artifact only
    "youtube_privacy": "private",    # "public" | "private" | "unlisted" (used when enable_scheduling=False)
    "enable_scheduling": False,      # True = schedule into 1 upcoming peak slot | False = immediate upload
    "max_videos_per_run": 1,         # Hard lock: never create/schedule more than 1 Short per run
    "max_daily_uploads": 5,          # Protects free 10,000-unit YouTube Data API daily quota

    # --- QUALITY & FRAMING SETTINGS ---
    "min_heatmap_score": 0.76,       # Minimum YouTube 'Most Replayed' spike (0.0 - 1.0)
    "min_visual_viral_score": 7.2,   # Minimum local OpenCLIP + OpenCV action score (0.0 - 10.0)
    "enable_ai_voiceover": True,     # 3-Act English narrator (Intro Hook -> Silent Climax -> Outro)
    "voice_name": "en-US-AndrewNeural",
    "default_framing": "cinema_blur", # "cinema_blur" (16:9 sharp center + blur bg) or "splitcam" (when webcam found)
    "watermark_text": "@Vice-City_ClipsVault",

    # --- PEAK PUBLISHING SLOTS (IST Hours converted cleanly in Part 5) ---
    "peak_hours_utc":,
    "history_file": "history.json",
}

# Apply optional manual workflow_dispatch override ONLY if explicitly entered
_force_override = os.environ.get("FORCE_UPLOAD_OVERRIDE", "").strip().lower()
if _force_override in ("true", "1", "yes"):
    CHANNEL_CONFIG["upload_to_youtube"] = True
elif _force_override in ("false", "0", "no"):
    CHANNEL_CONFIG["upload_to_youtube"] = False

# Priority Indian + Global YouTube Creators (checked alongside wide YouTube search)
PRIORITY_YT_CREATORS = [
    {"name": "CarryIsLive", "handle": "@CarryisLive", "region": "IN"},
    {"name": "Triggered Insaan", "handle": "@triggeredinsaan", "region": "IN"},
    {"name": "The RawKnee Show", "handle": "@TheRawKneeShow", "region": "IN"},
    {"name": "Techno Gamerz", "handle": "@TechnoGamerzOfficial", "region": "IN"},
    {"name": "Mythpat", "handle": "@Mythpat", "region": "IN"},
    {"name": "Live Insaan", "handle": "@LiveInsaan", "region": "IN"},
    {"name": "IShowSpeed", "handle": "@IShowSpeed", "region": "GLOBAL"},
    {"name": "xQc", "handle": "@xQcOW", "region": "GLOBAL"},
    {"name": "ElRubius", "handle": "@elrubiusOMG", "region": "GLOBAL"},
    {"name": "IlloJuan", "handle": "@IlloJuan", "region": "GLOBAL"},
]

# Rotating Wide YouTube Search Queries (3 run per hour to cover all ended lives & uploads with 0 throttling)
WIDE_YT_SEARCH_QUERIES = [
    "GTA 6 live stream India CarryIsLive Triggered Insaan RawKnee",
    "GTA 6 funny moments rage crash Hindi gameplay",
    "Grand Theft Auto 6 livestream highlights Vice City",
    "GTA 6 crazy moments fails stunts live",
    "GTA VI gameplay full stream India Hindi",
    "GTA 6 viral clips moments stream",
]

# ==============================================================================
# PART 3 OF 25: HISTORY & DEDUPLICATION MANAGER
# Title: Persistent JSON Tracker for Used Clips, Titles & Daily Quota Guard
# Subtitle: Prevents duplicate timestamps, repeated titles & API quota overruns
# ==============================================================================
def load_history():
    """Loads history.json safely with default schema if missing or corrupted."""
    path = CHANNEL_CONFIG["history_file"]
    default_data = {"used_clips": [], "uploads": [], "scheduled_slots": []}
    if not os.path.exists(path):
        return default_data
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, val in default_data.items():
            if key not in data or not isinstance(data[key], list):
                data[key] = val
        return data
    except Exception as e:
        print(f"[History Warning] Resetting unreadable history.json: {e}", flush=True)
        return default_data


def save_history(history_data):
    """Writes trimmed history.json atomically so git commits stay clean."""
    history_data["used_clips"] = history_data["used_clips"][-400:]
    history_data["uploads"] = history_data["uploads"][-150:]
    history_data["scheduled_slots"] = history_data["scheduled_slots"][-60:]
    path = CHANNEL_CONFIG["history_file"]
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def build_clip_fingerprint(video_id, peak_sec):
    """Buckets timestamps into 30-second windows so near-identical peaks aren't re-clipped."""
    bucket = int(float(peak_sec) // 30)
    return f"{video_id}::{bucket}"


def is_clip_already_used(history_data, video_id, peak_sec):
    """Returns True if this video + 30s window was already processed."""
    fp = build_clip_fingerprint(video_id, peak_sec)
    return fp in set(history_data.get("used_clips", []))


def count_todays_uploads(history_data):
    """Counts how many Shorts were uploaded/scheduled in the current UTC date."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    for item in history_data.get("uploads", []):
        if str(item.get("created_at", "")).startswith(today_str):
            count += 1
    return count

# ==============================================================================
# PART 4 OF 25: MEDIA PROBE & CRASH-SAFE FFMPEG HELPERS
# Title: Stream Validator, Duration Reader & Safe Subprocess Runner
# Subtitle: Eliminates broken downloads, missing audio tracks & FFmpeg exit 234 errors
# ==============================================================================
def run_cmd_safe(cmd, label="FFmpeg", timeout_sec=180):
    """Runs a subprocess cleanly and prints readable stderr tail on failure."""
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if res.returncode != 0:
        err_tail = (res.stderr or "")[-450:].strip()
        raise RuntimeError(f"[{label} Failed | code={res.returncode}] {err_tail}")
    return res


def get_media_duration(media_path):
    """Returns exact media duration in seconds via ffprobe (0.0 if invalid)."""
    if not media_path or not os.path.exists(media_path):
        return 0.0
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                media_path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        val = float(res.stdout.strip())
        return val if math.isfinite(val) and val > 0 else 0.0
    except Exception:
        return 0.0


def has_audio_stream(video_path):
    """Checks whether a downloaded video file actually contains an audio track."""
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return "audio" in res.stdout.lower()
    except Exception:
        return False


def ensure_valid_clip_with_audio(raw_path, clean_path):
    """Normalizes a raw slice to 30fps H.264 + 48kHz stereo AAC (adds silent audio if missing)."""
    dur = get_media_duration(raw_path)
    if dur < 6.0:
        return False
    if has_audio_stream(raw_path):
        cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            clean_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            clean_path,
        ]
    try:
        run_cmd_safe(cmd, label="NormalizeClip", timeout_sec=120)
        return get_media_duration(clean_path) >= 6.0
    except Exception as e:
        print(f"[Normalize Warning] {e}", flush=True)
        return False

# ==============================================================================
# PART 5 OF 25: SINGLE-SLOT PEAK SCHEDULER
# Title: Next-Upcoming Peak Slot Finder & Daily Quota Guard
# Subtitle: Locks scheduling to at most 1 upcoming slot so queue never overflows
# ==============================================================================
def get_next_single_peak_slot(history_data):
    """
    Finds the single next upcoming peak UTC timestamp (>= 35 mins from now).
    Returns None if the immediate next upcoming slot is already occupied.
    """
    now_utc = datetime.now(timezone.utc)
    min_publish_dt = now_utc + timedelta(minutes=35)

    candidate_slots = []
    for day_offset in (0, 1):
        base_day = (now_utc + timedelta(days=day_offset)).replace(
            minute=0, second=0, microsecond=0
        )
        for hr in sorted(CHANNEL_CONFIG["peak_hours_utc"]):
            slot_dt = base_day.replace(hour=int(hr))
            if slot_dt >= min_publish_dt:
                candidate_slots.append(slot_dt)

    if not candidate_slots:
        return None

    next_slot_dt = next(iter(sorted(candidate_slots)))
    next_slot_iso = next_slot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # If this exact upcoming slot is already scheduled in history.json, do not stack more
    booked_slots = set(history_data.get("scheduled_slots", []))
    if next_slot_iso in booked_slots:
        return None

    return next_slot_iso

# ==============================================================================
# PART 6 OF 25: WIDE YOUTUBE + INDIAN CREATOR DISCOVERY ENGINE
# Title: Zero-Bandwidth Metadata Scanner for Ended Lives, Uploads & Live Streams
# Subtitle: Prioritizes Indian & global GTA 6 YouTube videos before touching Twitch
# ==============================================================================
def _yt_dlp_base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }


def discover_wide_youtube_videos():
    """
    Discovers candidate GTA 6 YouTube ended lives & uploads across:
    1) Priority Indian + Global creators (/streams and /videos tabs)
    2) Wide YouTube searches (rotated per hour to stay well under rate limits)
    Downloads 0 MB of video during discovery.
    """
    discovered = {}
    flat_opts = _yt_dlp_base_opts()
    flat_opts["extract_flat"] = "in_playlist"
    flat_opts["playlistend"] = 6

    # 1) Check rotating subset of priority Indian + global creators each hour
    hour_seed = datetime.now(timezone.utc).hour
    shuffled_creators = list(PRIORITY_YT_CREATORS)
    random.Random(hour_seed).shuffle(shuffled_creators)
    selected_creators = shuffled_creators[:5]

    for creator in selected_creators:
        handle = creator["handle"]
        for tab in ("streams", "videos"):
            url = f"https://youtube.com{handle}/{tab}"
            try:
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                entries = (info or {}).get("entries") or []
                for entry in entries:
                    if not entry or not entry.get("id"):
                        continue
                    vid_id = entry["id"]
                    title_low = str(entry.get("title") or "").lower()
                    dur = float(entry.get("duration") or 0)
                    # Keep GTA-related videos >= 90s (skip Shorts)
                    if dur and dur < 90:
                        continue
                    if not any(k in title_low for k in ("gta", "grand theft", "vice city", "leonida")):
                        continue
                    discovered[vid_id] = {
                        "video_id": vid_id,
                        "url": f"https://youtube.comwatch?v={vid_id}",
                        "title": entry.get("title") or "GTA 6 Stream",
                        "channel_name": creator["name"],
                        "region": creator["region"],
                        "platform": "YouTube",
                        "priority_boost": 1.15 if creator["region"] == "IN" else 1.05,
                    }
            except Exception:
                pass
            time.sleep(0.5)

    # 2) Wide YouTube Search (3 rotating queries per hour -> ~30 fresh videos)
    q_idx = hour_seed % len(WIDE_YT_SEARCH_QUERIES)
    queries_this_hour = [
        WIDE_YT_SEARCH_QUERIES[q_idx],
        WIDE_YT_SEARCH_QUERIES[(q_idx + 1) % len(WIDE_YT_SEARCH_QUERIES)],
        WIDE_YT_SEARCH_QUERIES[(q_idx + 2) % len(WIDE_YT_SEARCH_QUERIES)],
    ]

    for query in queries_this_hour:
        try:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                res = ydl.extract_info(f"ytsearch12:{query}", download=False)
            for entry in (res or {}).get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                vid_id = entry["id"]
                dur = float(entry.get("duration") or 0)
                if dur and dur < 90:
                    continue
                if vid_id not in discovered:
                    is_indian = any(
                        w in str(entry.get("title") or "").lower()
                        for w in ("hindi", "india", "carry", "insaan", "rawknee", "techno", "mythpat")
                    )
                    discovered[vid_id] = {
                        "video_id": vid_id,
                        "url": f"https://youtube.comwatch?v={vid_id}",
                        "title": entry.get("title") or "GTA 6 Gameplay",
                        "channel_name": entry.get("uploader") or entry.get("channel") or "YouTube Creator",
                        "region": "IN" if is_indian else "GLOBAL",
                        "platform": "YouTube",
                        "priority_boost": 1.12 if is_indian else 1.0,
                    }
        except Exception as e:
            print(f"[YT Discovery Warning] Query '{query}': {e}", flush=True)
        time.sleep(0.6)

    items = list(discovered.values())
    print(f"[YT Discovery] Found {len(items)} unique GTA 6 videos/ended lives to inspect.", flush=True)
    return items

# ==============================================================================
# PART 7 OF 25: PRE-DOWNLOAD "MOST REPLAYED" HEATMAP EXTRACTOR
# Title: Crowd-Validated Viral Spike Detector Before Any Video Download
# Subtitle: Reads yt-dlp heatmap markers (0.0-1.0) & ranks top climax timestamps
# ==============================================================================
def inspect_videos_for_heatmap_peaks(discovered_items, history_data, max_inspect=24):
    """
    Inspects full video metadata (download=False) to read YouTube's 'Most Replayed' heatmap.
    Returns ranked viral timestamp targets BEFORE downloading any video files.
    """
    inspect_opts = _yt_dlp_base_opts()
    inspect_opts["skip_download"] = True

    heatmap_targets = []
    no_heatmap_pool = []

    # Prioritize Indian & creator-boosted videos first
    ordered = sorted(discovered_items, key=lambda x: x.get("priority_boost", 1.0), reverse=True)

    for meta in ordered[:max_inspect]:
        vid_id = meta["video_id"]
        try:
            with yt_dlp.YoutubeDL(inspect_opts) as ydl:
                info = ydl.extract_info(meta["url"], download=False)
            if not info:
                continue

            is_live = bool(info.get("is_live"))
            duration = float(info.get("duration") or 0)
            channel_name = info.get("uploader") or info.get("channel") or meta["channel_name"]
            heatmap = info.get("heatmap") or []

            if heatmap and not is_live:
                # Find strongest non-intro peaks (skip first 20s intro spike)
                valid_bins = [
                    b for b in heatmap
                    if float(b.get("start_time", 0)) >= 20.0
                    and float(b.get("value", 0)) >= CHANNEL_CONFIG["min_heatmap_score"]
                ]
                valid_bins.sort(key=lambda b: float(b.get("value", 0)), reverse=True)

                picked_for_vid = 0
                for b in valid_bins:
                    peak_mid = (float(b["start_time"]) + float(b["end_time"])) / 2.0
                    if is_clip_already_used(history_data, vid_id, peak_mid):
                        continue
                    raw_heat = float(b["value"])
                    weighted_score = raw_heat * meta.get("priority_boost", 1.0)
                    heatmap_targets.append({
                        "video_id": vid_id,
                        "url": meta["url"],
                        "video_title": info.get("title") or meta["title"],
                        "channel_name": channel_name,
                        "region": meta["region"],
                        "platform": "YouTube-Heatmap",
                        "peak_sec": peak_mid,
                        "heatmap_score": raw_heat,
                        "weighted_score": weighted_score,
                        "duration": duration,
                    })
                    picked_for_vid += 1
                    if picked_for_vid >= 2:
                        break
            else:
                # Brand-new ended live or active live stream (no heatmap yet)
                if not is_clip_already_used(history_data, vid_id, 0):
                    no_heatmap_pool.append({
                        "video_id": vid_id,
                        "url": meta["url"],
                        "video_title": info.get("title") or meta["title"],
                        "channel_name": channel_name,
                        "region": meta["region"],
                        "platform": "YouTube-Live" if is_live else "YouTube-Fresh",
                        "is_live": is_live,
                        "duration": duration,
                        "priority_boost": meta.get("priority_boost", 1.0),
                    })
        except Exception:
            pass
        time.sleep(0.45)

    heatmap_targets.sort(key=lambda x: x["weighted_score"], reverse=True)
    print(
        f"[Heatmap Pre-Check] Found {len(heatmap_targets)} Most-Replayed spikes "
        f"+ {len(no_heatmap_pool)} fresh/live YouTube streams.",
        flush=True,
    )
    return heatmap_targets, no_heatmap_pool

# ==============================================================================
# PART 8 OF 25: SURGICAL 25-SECOND SECTION DOWNLOADER
# Title: High-Speed Climax Slice Downloader via yt-dlp --download-sections
# Subtitle: Downloads only the 26s window around verified Most-Replayed peaks
# ==============================================================================
def download_surgical_heatmap_slices(heatmap_targets, max_slices=8):
    """
    Downloads ONLY the 26-second climax window around the highest-ranked heatmap spikes.
    Returns normalized candidate dicts ready for local OpenCLIP visual judging.
    """
    candidates = []
    for idx, target in enumerate(heatmap_targets[:max_slices]):
        peak = float(target["peak_sec"])
        total_dur = float(target.get("duration") or 0)
        start_s = max(0.0, peak - 17.0)
        end_s = min(total_dur - 1.0, peak + 9.0) if total_dur > 0 else (peak + 9.0)
        if (end_s - start_s) < 12.0:
            continue

        raw_out = f"raw_hm_{idx}_{target['video_id']}.mp4"
        clean_out = f"clip_hm_{idx}_{target['video_id']}.mp4"

        ydl_opts = _yt_dlp_base_opts()
        ydl_opts.update({
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "outtmpl": raw_out,
            "merge_output_format": "mp4",
            "download_ranges": yt_dlp.utils.download_range_func(None, [(start_s, end_s)]),
            "force_keyframes_at_cuts": True,
        })

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target["url"]])

            if ensure_valid_clip_with_audio(raw_out, clean_out):
                clip_dur = get_media_duration(clean_out)
                # Climax peak sits ~17s into this 26s slice
                local_peak = min(clip_dur - 2.0, max(8.0, peak - start_s))
                candidates.append({
                    "video_id": target["video_id"],
                    "video_path": clean_out,
                    "channel_name": target["channel_name"],
                    "region": target["region"],
                    "platform": target["platform"],
                    "source_title": target["video_title"],
                    "origin_peak_sec": peak,
                    "audio_peak": local_peak,
                    "heatmap_score": target["heatmap_score"],
                    "jump_ratio": round(target["heatmap_score"] * 5.0, 2),
                })
                print(
                    f"[Surgical Slice] Downloaded {target['channel_name']} "
                    f"(heatmap={target['heatmap_score']:.2f} @ {int(peak)}s)",
                    flush=True,
                )
        except Exception as e:
            print(f"[Slice Warning] {target['video_id']}: {e}", flush=True)
        finally:
            if os.path.exists(raw_out):
                os.remove(raw_out)

    return candidates

# ==============================================================================
# PART 9 OF 25: FRESH / LIVE YOUTUBE SAMPLER & RMS AUDIO SPIKE DETECTOR
# Title: Secondary YouTube Harvester for Brand-New Uploads & Active Live Streams
# Subtitle: Captures 90s windows & locates vocal/explosion audio reaction spikes
# ==============================================================================
def find_audio_spike_timestamp(video_path):
    """Scans 1-second RMS audio energy to locate the sharpest reaction spike."""
    wav_path = f"probe_{os.getpid()}_{random.randint(100,999)}.wav"
    try:
        run_cmd_safe(
            ["ffmpeg", "-y", "-i", video_path, "-ac", "1", "-ar", "16000", wav_path],
            label="AudioProbe",
            timeout_sec=45,
        )
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            total_frames = wf.getnframes()
            dur_sec = total_frames // sr
            if dur_sec < 12:
                return 10.0, 1.0
            energies = []
            for sec in range(dur_sec):
                raw = wf.readframes(sr)
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
                energies.append(rms)

        med = float(np.median(energies)) + 1e-5
        # Focus after first 8 seconds so intro greetings aren't picked
        search_slice = energies[8: max(9, len(energies) - 3)]
        best_rel_idx = int(np.argmax(search_slice))
        best_sec = float(best_rel_idx + 8)
        jump_ratio = float(search_slice[best_rel_idx] / med)
        return best_sec, jump_ratio
    except Exception:
        return 15.0, 1.0
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def harvest_fresh_or_live_youtube(no_heatmap_pool, max_streams=3):
    """Captures 75s samples from brand-new YouTube uploads or active YouTube lives."""
    candidates = []
    ordered = sorted(no_heatmap_pool, key=lambda x: x.get("priority_boost", 1.0), reverse=True)

    for idx, item in enumerate(ordered[:max_streams]):
        raw_out = f"raw_ytlive_{idx}_{item['video_id']}.mp4"
        clean_out = f"clip_ytlive_{idx}_{item['video_id']}.mp4"

        ydl_opts = _yt_dlp_base_opts()
        ydl_opts.update({
            "format": "best[height<=720][ext=mp4]/best[height<=720]",
            "outtmpl": raw_out,
            "live_from_start": False,
        })
        if not item.get("is_live") and float(item.get("duration") or 0) > 120:
            mid = float(item["duration"]) * 0.45
            ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(mid, mid + 75.0)])
        else:
            # Cap live stream recording cleanly at 65 seconds via external downloader args
            ydl_opts["external_downloader"] = "ffmpeg"
            ydl_opts["external_downloader_args"] = {"ffmpeg_i": ["-t", "65"]}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([item["url"]])

            if ensure_valid_clip_with_audio(raw_out, clean_out):
                peak_sec, jump_ratio = find_audio_spike_timestamp(clean_out)
                candidates.append({
                    "video_id": item["video_id"],
                    "video_path": clean_out,
                    "channel_name": item["channel_name"],
                    "region": item["region"],
                    "platform": item["platform"],
                    "source_title": item["video_title"],
                    "origin_peak_sec": peak_sec,
                    "audio_peak": peak_sec,
                    "heatmap_score": 0.0,
                    "jump_ratio": round(jump_ratio, 2),
                })
        except Exception as e:
            print(f"[YT Fresh/Live Warning] {item['video_id']}: {e}", flush=True)
        finally:
            if os.path.exists(raw_out):
                os.remove(raw_out)

    return candidates

# ==============================================================================
# PART 10 OF 25: BACKUP TWITCH/KICK FALLBACK (ONLY IF YOUTUBE POOL IS EMPTY)
# Title: Emergency Secondary Stream Harvester
# Subtitle: Runs ONLY when YouTube yields < 2 candidates; heavily filtered in Part 11
# ==============================================================================
def harvest_backup_twitch_streams(max_streams=2):
    """Emergency fallback if YouTube has zero available GTA 6 videos/streams."""
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    csec = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        return []

    try:
        tok_res = requests.post(
            "https://twitch.tv",
            params={"client_id": cid, "client_secret": csec, "grant_type": "client_credentials"},
            timeout=15,
        ).json()
        token = tok_res.get("access_token")
        if not token:
            return []

        headers = {"Client-ID": cid, "Authorization": f"Bearer {token}"}
        game_res = requests.get(
            "https://twitch.tv",
            headers=headers,
            params={"name": "Grand Theft Auto VI"},
            timeout=15,
        ).json()
        game_data = game_res.get("data") or []
        if not game_data:
            return []
        game_id = next(iter(game_data)).get("id")

        streams_res = requests.get(
            "https://twitch.tv",
            headers=headers,
            params={"game_id": game_id, "first": 6},
            timeout=15,
        ).json()
        streams = streams_res.get("data") or []
    except Exception as e:
        print(f"[Twitch Backup Warning] {e}", flush=True)
        return []

    candidates = []
    for idx, st in enumerate(streams[:max_streams]):
        login = st.get("user_login")
        disp = st.get("user_name") or login
        if not login:
            continue
        raw_out = f"raw_tw_{idx}_{login}.mp4"
        clean_out = f"clip_tw_{idx}_{login}.mp4"
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "best[height<=720]",
            "outtmpl": raw_out,
            "external_downloader": "ffmpeg",
            "external_downloader_args": {"ffmpeg_i": ["-t", "55"]},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://twitch.tv{login}"])
            if ensure_valid_clip_with_audio(raw_out, clean_out):
                peak_sec, jump_ratio = find_audio_spike_timestamp(clean_out)
                candidates.append({
                    "video_id": f"tw_{login}_{int(time.time())}",
                    "video_path": clean_out,
                    "channel_name": disp,
                    "region": "GLOBAL",
                    "platform": "Twitch-Backup",
                    "source_title": st.get("title") or "GTA 6 Live",
                    "origin_peak_sec": peak_sec,
                    "audio_peak": peak_sec,
                    "heatmap_score": 0.0,
                    "jump_ratio": round(jump_ratio, 2),
                })
        except Exception:
            pass
        finally:
            if os.path.exists(raw_out):
                os.remove(raw_out)

    return candidates

# ==============================================================================
# PART 11 OF 25: AD-SCREEN, FROZEN-FRAME & VISUAL MOTION GATE (OPENCV)
# Title: Fast Pixel-Level Filter for Commercial Breaks, Pause Menus & Static Filler
# Subtitle: Runs locally in <0.2s per clip with zero API calls
# ==============================================================================
def analyze_clip_motion_and_ads(video_path, peak_sec):
    """
    Inspects 6 frames around the climax window for:
    1) Flat/solid ad screens (e.g. Twitch 'Commercial break in progress')
    2) Frozen/pause screens (near-zero frame-to-frame difference)
    3) Visual motion score (0.0 - 10.0)
    Returns (is_valid, motion_score, sampled_pil_frames)
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_frames < 30:
        cap.release()
        return False, 0.0, []

    dur_sec = total_frames / fps
    start_s = max(0.0, min(dur_sec - 12.0, peak_sec - 10.0))
    end_s = min(dur_sec - 0.5, start_s + 14.0)

    pil_frames = []
    gray_thumbs = []
    for idx in range(6):
        ts = start_s + (end_s - start_s) * (idx / 5.0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(ts * fps))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue

        small = cv2.resize(frame, (192, 108))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        # Reject flat low-detail screens (Twitch purple/blue commercial banner or black loading screen)
        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if lap_var < 38.0:
            cap.release()
            return False, 0.0, []

        gray_thumbs.append(gray.astype(np.float32))
        rgb = cv2.cvtColor(cv2.resize(frame, (384, 216)), cv2.COLOR_BGR2RGB)
        pil_frames.append(Image.fromarray(rgb))

    cap.release()
    if len(gray_thumbs) < 4:
        return False, 0.0, []

    # Measure real on-screen movement across consecutive frames
    diffs = [
        float(np.mean(np.abs(gray_thumbs[i] - gray_thumbs[i - 1])))
        for i in range(1, len(gray_thumbs))
    ]
    avg_diff = float(np.mean(diffs))
    peak_diff = float(np.max(diffs))

    # Reject static pause menus / map screens that barely move
    if peak_diff < 2.8 or avg_diff < 1.8:
        return False, 0.0, []

    # Normalize visual motion into 0.0 - 10.0
    motion_score = min(10.0, max(0.0, (avg_diff * 0.45) + (peak_diff * 0.25)))
    return True, round(motion_score, 2), pil_frames

# ==============================================================================
# PART 12 OF 25: UNLIMITED LOCAL OPENCLIP VISUAL ACTION JUDGE
# Title: Zero-Limit Neural Vision Scorer for Viral Gameplay vs. Filler Screens
# Subtitle: Runs 100% locally on CPU; auto-rejects maps, menus, walking & ad screens
# ==============================================================================
_CLIP_BUNDLE = None


def get_local_openclip_judge():
    """Loads and caches OpenCLIP ViT-B-32 once per run (0 API calls, unlimited runs)."""
    global _CLIP_BUNDLE
    if _CLIP_BUNDLE is not None:
        return _CLIP_BUNDLE

    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k"
    )
    model.eval()
    tokenizer = open_clip.get_tokenizer("ViT-B-32")

    positive_prompts = [
        "exciting video game car crash explosion stunt or high speed police chase",
        "intense video game gunfight shootout chaos or hilarious ragdoll fail",
        "dramatic airplane helicopter boat crash or near miss stunt in video game",
    ]
    negative_prompts = [
        "video game pause menu settings screen or mini map navigation UI",
        "twitch commercial break in progress purple blue banner screen",
        "video game character calmly walking jogging on empty road or standing still",
    ]

    with torch.no_grad():
        pos_tokens = tokenizer(positive_prompts)
        neg_tokens = tokenizer(negative_prompts)
        pos_emb = model.encode_text(pos_tokens)
        pos_emb /= pos_emb.norm(dim=-1, keepdim=True)
        neg_emb = model.encode_text(neg_tokens)
        neg_emb /= neg_emb.norm(dim=-1, keepdim=True)

    _CLIP_BUNDLE = (model, preprocess, pos_emb, neg_emb)
    return _CLIP_BUNDLE


def score_candidates_with_local_vision(candidates):
    """
    Evaluates all harvested candidates locally using OpenCV motion + OpenCLIP vision
    + YouTube Most-Replayed heatmap proof. Zero API limits!
    """
    model, preprocess, pos_emb, neg_emb = get_local_openclip_judge()
    scored_winners = []

    for cand in candidates:
        is_valid, motion_score, pil_frames = analyze_clip_motion_and_ads(
            cand["video_path"], cand["audio_peak"]
        )
        if not is_valid or not pil_frames:
            print(
                f"[Local Judge REJECT] {cand['channel_name']} -> Static/Ad/Low-Motion screen",
                flush=True,
            )
            continue

        try:
            img_tensors = torch.stack([preprocess(im) for im in pil_frames])
            with torch.no_grad():
                img_emb = model.encode_image(img_tensors)
                img_emb /= img_emb.norm(dim=-1, keepdim=True)
                pos_sim = float((img_emb @ pos_emb.T).max(dim=-1).values.mean().item())
                neg_sim = float((img_emb @ neg_emb.T).max(dim=-1).values.mean().item())

            # Reject if frames look more like a map/menu/walking/ad than real action
            if neg_sim > (pos_sim + 0.015):
                print(
                    f"[Local Judge REJECT] {cand['channel_name']} -> Menu/Map/Walking detected "
                    f"(pos={pos_sim:.3f} vs neg={neg_sim:.3f})",
                    flush=True,
                )
                continue

            # Combine Visual Action (0-10), Motion (0-10), and YouTube Heatmap Proof (0-10)
            clip_delta = max(0.0, min(10.0, (pos_sim - neg_sim + 0.06) * 65.0))
            hm_bonus = float(cand.get("heatmap_score", 0.0)) * 10.0
            audio_bonus = min(10.0, float(cand.get("jump_ratio", 1.0)) * 2.2)

            if hm_bonus > 0:
                total_score = (clip_delta * 0.45) + (motion_score * 0.25) + (hm_bonus * 0.30)
            else:
                total_score = (clip_delta * 0.50) + (motion_score * 0.30) + (audio_bonus * 0.20)

            if cand.get("region") == "IN":
                total_score += 0.45  # Slight priority boost for Indian creators

            total_score = round(min(10.0, total_score), 2)
            print(
                f"[Local Judge] {cand['channel_name']} ({cand['platform']}) -> "
                f"Score={total_score}/10 (action={clip_delta:.1f}, motion={motion_score:.1f}, hm={hm_bonus:.1f})",
                flush=True,
            )

            if total_score >= float(CHANNEL_CONFIG["min_visual_viral_score"]):
                cand["viral_score"] = total_score
                cand["motion_score"] = motion_score
                scored_winners.append(cand)
        except Exception as e:
            print(f"[OpenCLIP Warning] {cand['channel_name']}: {e}", flush=True)

    scored_winners.sort(key=lambda x: x["viral_score"], reverse=True)
    return scored_winners

# ==============================================================================
# PART 13 OF 25: MULTI-FRAME VERIFIED WEBCAM DETECTOR
# Title: Corner Facecam Locator for True Split-Screen Layout
# Subtitle: Prevents false locks on in-game characters, billboards & HUD icons
# ==============================================================================
def detect_verified_webcam_box(video_path, peak_sec):
    """
    Checks 4 frames around the climax window for a stationary corner webcam.
    Returns (crop_w, crop_h, x0, y0) if a real webcam is verified, else None.
    """
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames < 20:
            cap.release()
            return None

        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        dur_sec = total_frames / fps
        start_s = max(0.0, min(dur_sec - 10.0, peak_sec - 8.0))

        hits = []
        for idx in range(4):
            ts = start_s + (idx * 2.5)
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(ts * fps))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.18, minNeighbors=7, minSize=(55, 55)
            )
            for (fx, fy, fw, fh) in faces:
                cx = fx + (fw / 2.0)
                cy = fy + (fh / 2.0)
                in_outer_corner = (cx < w * 0.28 or cx > w * 0.72) and (
                    cy < h * 0.36 or cy > h * 0.64
                )
                if in_outer_corner:
                    hits.append((cx, cy, fw, fh, w, h))
                    break
        cap.release()

        if len(hits) < 2:
            return None

        xs = [item for item in hits]
        ys = [item for item in hits]
        if (max(xs) - min(xs)) > 55 or (max(ys) - min(ys)) > 55:
            return None

        first_hit = next(iter(hits))
        cx, cy, fw, fh, w, h = first_hit
        crop_w = int(min(w * 0.34, max(fw * 3.8, w * 0.25)))
        crop_h = int(min(h * 0.42, crop_w * (660.0 / 1080.0)))
        x0 = int(max(0, min(w - crop_w, cx - (crop_w / 2.0))))
        y0 = int(max(0, min(h - crop_h, cy - (crop_h / 2.0))))
        return (crop_w, crop_h, x0, y0)
    except Exception:
        return None

# ==============================================================================
# PART 14 OF 25: FASTER-WHISPER AUTO-ENGLISH TRANSCRIBER
# Title: Multilingual-to-English Speech-to-Text Engine
# Subtitle: Translates Hindi/Spanish/English streamer reactions into English text
# ==============================================================================
_WHISPER_MODEL = None


def get_whisper_model():
    """Loads Faster-Whisper 'base' model once per run (cached on CPU)."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        _WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def transcribe_clip_to_english(video_path):
    """
    Transcribes and translates any spoken language (Hindi, Spanish, English)
    into clean English text + timestamped word segments for captions.
    """
    try:
        model = get_whisper_model()
        segments, _ = model.transcribe(
            video_path,
            task="translate",
            beam_size=1,
            vad_filter=True,
        )
        lines = []
        word_events = []
        for seg in segments:
            txt = seg.text.strip()
            if txt:
                lines.append(txt)
                word_events.append((float(seg.start), float(seg.end), txt))
        return " ".join(lines).strip(), word_events
    except Exception as e:
        print(f"[Whisper Warning] {e}", flush=True)
        return "", []

# ==============================================================================
# PART 15 OF 25: TOP 3-2-1 COUNTDOWN & SINGLE CLIMAX PLANNER
# Title: High-Retention Short Sequencer (#3 -> #2 -> #1)
# Subtitle: Orders approved winners so the #1 highest-scoring moment hits last
# ==============================================================================
def build_short_plan(scored_winners):
    """
    Selects up to 3 distinct-creator winners and builds:
    - MULTI (3-2-1 Countdown): #3 (11s) -> #2 (12s) -> #1 (14s) = ~37s Short
    - SINGLE (1-2 winners): #1 (20s) high-impact climax Short
    Returns None if zero clips passed the quality bar.
    """
    if not scored_winners:
        print("[Quality Gate] Zero clips met the visual quality bar—skipping run.", flush=True)
        return None

    distinct_winners = []
    seen_creators = set()
    for cand in scored_winners:
        cname = cand["channel_name"].strip().lower()
        if cname in seen_creators:
            continue
        distinct_winners.append(cand)
        seen_creators.add(cname)
        if len(distinct_winners) >= 3:
            break

    # Enrich winning 1-3 clips with English transcripts
    for w in distinct_winners:
        eng_text, word_events = transcribe_clip_to_english(w["video_path"])
        w["transcript_en"] = eng_text
        w["word_events"] = word_events

    if len(distinct_winners) >= 3:
        # Ascending order so rank #3 plays first, #2 second, and #1 climax plays LAST!
        ordered_321 = list(reversed(distinct_winners[:3]))
        durations = [11.0, 12.5, 14.5]
        ranks = [3, 2, 1]
        specs = []
        for rank_num, cand, seg_dur in zip(ranks, ordered_321, durations):
            clip_dur = get_media_duration(cand["video_path"])
            peak = float(cand["audio_peak"])
            # Place the climax peak ~3.5s before the end of each segment
            end_t = min(clip_dur - 0.3, peak + 3.5)
            start_t = max(0.0, end_t - seg_dur)
            specs.append({
                "rank": rank_num,
                "candidate": cand,
                "start_sec": round(start_t, 2),
                "end_sec": round(end_t, 2),
                "duration": round(end_t - start_t, 2),
            })
        return {"mode": "MULTI", "segments": specs}

    # Single mode when 1 or 2 clips passed
    top_one = next(iter(distinct_winners))
    clip_dur = get_media_duration(top_one["video_path"])
    peak = float(top_one["audio_peak"])
    end_t = min(clip_dur - 0.3, peak + 4.5)
    start_t = max(0.0, end_t - 20.0)
    return {
        "mode": "SINGLE",
        "segments": [{
            "rank": 1,
            "candidate": top_one,
            "start_sec": round(start_t, 2),
            "end_sec": round(end_t, 2),
            "duration": round(end_t - start_t, 2),
        }],
    }

# ==============================================================================
# PART 16 OF 25: SCENE-GROUNDED ENGLISH TITLE & 3-ACT COMMENTARY WRITER
# Title: Multimodal Gemini Writer Inspecting Winning Keyframes + Transcript
# Subtitle: Generates scene-accurate English titles, 3.5s intro hook & climax outro
# ==============================================================================
def _extract_winner_frames_for_gemini(segments):
    """Extracts 2 crisp JPEG keyframes around each winning clip's climax."""
    image_parts = []
    for seg in segments:
        cand = seg["candidate"]
        vpath = cand["video_path"]
        mid_t = max(0.5, seg["end_sec"] - 2.5)
        for offset_idx, ts in enumerate((max(0.2, mid_t - 2.5), mid_t)):
            tmp_jpg = f"gem_f_{os.getpid()}_{seg['rank']}_{offset_idx}.jpg"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", vpath,
                    "-vframes", "1", "-vf", "scale=512:-2", "-q:v", "6", tmp_jpg,
                ],
                capture_output=True,
            )
            if os.path.exists(tmp_jpg):
                with open(tmp_jpg, "rb") as f:
                    image_parts.append(
                        types.Part.from_bytes(data=f.read(), mime_type="image/jpeg")
                    )
                os.remove(tmp_jpg)
    return image_parts


def generate_scene_grounded_script(short_plan, history_data):
    """
    Inspects the winning keyframes + English transcripts via Gemini to write:
    1) title: Scene-accurate English YouTube Short title (< 65 chars)
    2) banner: 2-4 ALL-CAPS hook words tied to the actual scene
    3) intro_vo: 3.0-3.8s energetic English opening hook describing what's happening
    4) outro_vo: 2.5-3.2s English punchline reacting to the #1 climax ending
    5) tags: Relevant English SEO tags
    """
    segments = short_plan["segments"]
    mode = short_plan["mode"]
    recent_titles = [
        u.get("title", "") for u in history_data.get("uploads", [])[-20:] if u.get("title")
    ]

    scene_notes = []
    creators = []
    for seg in segments:
        c = seg["candidate"]
        creators.append(c["channel_name"])
        tr = (c.get("transcript_en") or "")[:240]
        scene_notes.append(
            f"Rank #{seg['rank']} ({c['channel_name']} [{c['region']}]): "
            f"Source='{c.get('source_title', '')[:70]}' | Speech='{tr}'"
        )

    lead_creator = next(iter(reversed(creators)))  # Rank #1 creator
    avoid_block = (
        "NEVER repeat or mimic these recent titles:\n- " + "\n- ".join(recent_titles[-12:])
        if recent_titles else ""
    )

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        frame_parts = _extract_winner_frames_for_gemini(segments)
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=25000)
        )
        prompt = (
            f"You are the head YouTube Shorts producer for 'Vice City Clips Vault' (GTA 6 highlights).\n"
            f"Format: {'Top 3-2-1 Countdown' if mode == 'MULTI' else 'Single Viral Climax'}\n"
            f"Clips included:\n" + "\n".join(scene_notes) + "\n\n"
            f"{avoid_block}\n\n"
            f"Look closely at the attached frames from these exact clips and return valid JSON:\n"
            f'1. "title": Specific English title under 65 chars describing the EXACT crash, stunt, police chase, or reaction visible in the frames + 1 emoji. BANNED: generic clickbait like "Watch Closely" or "Nobody Expected".\n'
            f'2. "banner": 2 to 4 ALL-CAPS words matching the exact scene (e.g. "SEAPLANE WIPEOUT", "COP CHASE GONE WRONG", "INSANE HIGHWAY SAVE").\n'
            f'3. "intro_vo": 1 punchy 9-13 word English narrator intro setting up the exact action in these frames (remember: narrator will go completely silent during the climax!).\n'
            f'4. "outro_vo": 1 witty 7-10 word English narrator punchline reacting to the #1 ending.\n'
            f'5. "tags": list of 8 short English tags.'
        )

        for model_id in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
            try:
                resp = client.models.generate_content(
                    model=model_id,
                    contents=[*frame_parts, prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.85,
                        response_mime_type="application/json",
                    ),
                )
                data = json.loads(resp.text.strip())
                title = str(data.get("title", "")).strip()
                banner = str(data.get("banner", "")).strip().upper()[:24]
                intro_vo = str(data.get("intro_vo", "")).strip()
                outro_vo = str(data.get("outro_vo", "")).strip()
                tags = data.get("tags") or ["GTA 6", "Vice City", "GTA 6 Clips", "Shorts"]

                if title and title not in recent_titles and len(title) >= 12:
                    print(f"[Gemini Scene Writer] Title via {model_id}: {title}", flush=True)
                    return {
                        "title": title,
                        "banner": banner or "VICE CITY CHAOS",
                        "intro_vo": intro_vo,
                        "outro_vo": outro_vo,
                        "tags": [str(t) for t in tags[:12]],
                        "creators": creators,
                    }
            except Exception as e:
                print(f"[Gemini Writer Warning] {model_id}: {e}", flush=True)

    # Scene-aware fallback if API key is missing or offline
    fallback_title = (
        f"Top 3 Wildest GTA 6 Moments Featuring {lead_creator} 🔥"
        if mode == "MULTI"
        else f"{lead_creator}'s Craziest Vice City Moment Caught Live 🚨"
    )
    return {
        "title": fallback_title,
        "banner": "TOP 3 CHAOS" if mode == "MULTI" else "PURE MADNESS",
        "intro_vo": (
            f"Three insane Vice City clips—wait until you see {lead_creator} at number one!"
            if mode == "MULTI"
            else f"Watch how fast things go wrong for {lead_creator} in Vice City!"
        ),
        "outro_vo": f"Rate {lead_creator}'s clutch from one to ten in the comments!",
        "tags": ["GTA 6", "Grand Theft Auto 6", "Vice City", "GTA 6 Clips", "Shorts"],
        "creators": creators,
    }

# ==============================================================================
# PART 17 OF 25: EDGE-TTS NEURAL VOICEOVER GENERATOR
# Title: High-Energy English Narrator Synthesizer with Silence Trimming
# Subtitle: Generates crisp Act 1 Hook & Act 3 Punchline audio tracks
# ==============================================================================
async def _synthesize_edge_tts_async(text, out_mp3):
    communicate = edge_tts.Communicate(
        text=text,
        voice=CHANNEL_CONFIG["voice_name"],
        rate="+12%",
        pitch="+2Hz",
    )
    await communicate.save(out_mp3)


def generate_clean_tts_clip(text, final_wav_path):
    """Generates clean 48kHz stereo WAV narration with trailing silence trimmed."""
    clean_text = re.sub(r"[^\w\s.,!?'\-]", "", str(text or "")).strip()
    if not clean_text:
        return 0.0

    raw_mp3 = f"tts_raw_{os.getpid()}_{random.randint(100,999)}.mp3"
    try:
        asyncio.run(_synthesize_edge_tts_async(clean_text, raw_mp3))
    except Exception:
        try:
            gTTS(text=clean_text, lang="en", slow=False).save(raw_mp3)
        except Exception as e:
            print(f"[TTS Warning] Both Edge-TTS and gTTS failed: {e}", flush=True)
            return 0.0

    try:
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-i", raw_mp3,
                "-af", "silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=-1:stop_duration=0.15:stop_threshold=-45dB",
                "-ar", "48000", "-ac", "2",
                final_wav_path,
            ],
            label="CleanTTS",
            timeout_sec=30,
        )
        return get_media_duration(final_wav_path)
    except Exception:
        return 0.0
    finally:
        if os.path.exists(raw_mp3):
            os.remove(raw_mp3)

# ==============================================================================
# PART 18 OF 25: SMART SPLITCAM / 16:9 CINEMA & 3-2-1 RANK RENDERER
# Title: Adaptive Vertical Frame Compositor with Rank Badges & Creator Credits
# Subtitle: Uses Splitcam for verified webcams; 16:9 Cinema + Blur BG otherwise
# ==============================================================================
def _escape_drawtext(txt):
    s = str(txt or "").replace("\\", "\\\\")
    s = s.replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")
    return s


def render_formatted_segment(seg, seg_index, total_segments, out_seg_path):
    """
    Slices [start_sec, end_sec] and renders a crisp 1080x1920 30fps segment with:
    - Verified Splitcam (top webcam + bottom gameplay) OR 16:9 Cinema Center + Blur BG
    - Bold #3 / #2 / #1 Countdown Badge (when total_segments > 1)
    - Creator Credit & @Vice-City_ClipsVault Watermark
    """
    cand = seg["candidate"]
    src_path = cand["video_path"]
    start_s = float(seg["start_sec"])
    dur_s = max(5.0, float(seg["duration"]))
    rank_num = int(seg["rank"])
    creator_label = _escape_drawtext(f"Credit: {cand['channel_name']}")
    wm_label = _escape_drawtext(CHANNEL_CONFIG["watermark_text"])

    webcam_box = detect_verified_webcam_box(src_path, cand["audio_peak"])

    if webcam_box is not None:
        cw, ch, x0, y0 = webcam_box
        # Top 720px = verified streamer facecam | Bottom 1200px = sharp gameplay + neon divider
        base_graph = (
            f"[0:v]split=2[cam_in][game_in];"
            f"[cam_in]crop={cw}:{ch}:{x0}:{y0},scale=1080:720:force_original_aspect_ratio=increase,"
            f"crop=1080:720,unsharp=5:5:0.6[top_cam];"
            f"[game_in]scale=1080:1200:force_original_aspect_ratio=increase,"
            f"crop=1080:1200,unsharp=5:5:0.75[bot_game];"
            f"[top_cam][bot_game]vstack=inputs=2,"
            f"drawbox=x=0:y=717:w=1080:h=6:color=#ff00cc@0.95:t=fill[framed]"
        )
        layout_used = "SPLITCAM"
    else:
        # 16:9 Cinema Center (1080x940) over darkened blurred backdrop (keeps 100% of HUD visible)
        base_graph = (
            f"[0:v]split=2[bg_in][fg_in];"
            f"[bg_in]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,gblur=sigma=24,eq=brightness=-0.16[bg];"
            f"[fg_in]scale=1080:940:force_original_aspect_ratio=decrease,"
            f"unsharp=5:5:0.80[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2+40,"
            f"drawbox=x=0:y=528:w=1080:h=4:color=#00f0ff@0.85:t=fill,"
            f"drawbox=x=0:y=1472:w=1080:h=4:color=#ff00cc@0.85:t=fill[framed]"
        )
        layout_used = "CINEMA_BLUR"

    # Add Watermark, Creator Credit, and Bold #3 -> #2 -> #1 Rank Badge
    overlays = (
        f"[framed]"
        f"drawtext=text='{wm_label}':fontcolor=white@0.88:fontsize=28:"
        f"x=(w-text_w)/2:y=58:box=1:boxcolor=black@0.45:boxborderw=8,"
        f"drawtext=text='{creator_label}':fontcolor=#ffe600:fontsize=30:"
        f"x=(w-text_w)/2:y=104:box=1:boxcolor=black@0.55:boxborderw=8"
    )

    if total_segments > 1:
        rank_color = "#ff2a55" if rank_num == 1 else ("#ffaa00" if rank_num == 2 else "#00e5ff")
        overlays += (
            f",drawtext=text='\\#{rank_num}':fontcolor={rank_color}:fontsize=76:"
            f"x=54:y=165:box=1:boxcolor=black@0.75:boxborderw=14"
        )

    overlays += "[vout]"
    full_vf = f"{base_graph};{overlays}"

    run_cmd_safe(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_s:.2f}", "-t", f"{dur_s:.2f}", "-i", src_path,
            "-filter_complex", full_vf,
            "-map", "[vout]", "-map", "0:a",
            "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            out_seg_path,
        ],
        label=f"RenderSeg_{rank_num}",
        timeout_sec=120,
    )
    print(
        f"[Segment #{rank_num}] Rendered {cand['channel_name']} ({layout_used}, {dur_s:.1f}s)",
        flush=True,
    )

# ==============================================================================
# PART 19 OF 25: TIMELINE CONCATENATOR & 3-ACT AUDIO DUCKER
# Title: Crash-Safe Xfade Joiner + Hook -> Silent Climax -> Punchline Mixer
# Subtitle: Narrator goes 100% silent during main climax so raw reaction shines
# ==============================================================================
def assemble_timeline_with_3act_audio(seg_files, script_bundle, merged_out_path):
    """
    1) Joins 1 or 3 segments using safe-clamped xfade transitions (no exit 234 crash)
    2) Mixes 3-Act Audio:
       - Act 1 (0 -> ~3.8s): Intro Hook VO (game ducked to 15%)
       - Act 2 (middle climax): Narrator 100% SILENT (game/streamer at 100%)
       - Act 3 (final ~3.5s): Outro Punchline VO (game ducked to 18%)
    """
    durs = [get_media_duration(p) for p in seg_files]
    if len(seg_files) == 1:
        shutil.copyfile(next(iter(seg_files)), "timeline_raw.mp4")
    else:
        min_seg_dur = min(durs)
        safe_xfade = round(min(0.35, max(0.15, min_seg_dur * 0.25)), 2)

        inputs = []
        for p in seg_files:
            inputs.extend(["-i", p])

        v_chain = ""
        a_chain = ""
        curr_v = "[0:v]"
        curr_a = "[0:a]"
        running_offset = 0.0

        for idx in range(1, len(seg_files)):
            running_offset += durs[idx - 1] - safe_xfade
            next_v = f"[v{idx}]"
            next_a = f"[a{idx}]"
            trans_type = "wipeleft" if idx == 1 else "smoothleft"
            v_chain += (
                f"{curr_v}[{idx}:v]xfade=transition={trans_type}:"
                f"duration={safe_xfade:.2f}:offset={running_offset:.2f}{next_v};"
            )
            a_chain += f"{curr_a}[{idx}:a]acrossfade=d={safe_xfade:.2f}{next_a};"
            curr_v = next_v
            curr_a = next_a

        filter_complex = f"{v_chain}{a_chain}".rstrip(";")
        run_cmd_safe(
            [
                "ffmpeg", "-y", *inputs,
                "-filter_complex", filter_complex,
                "-map", curr_v, "-map", curr_a,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
                "timeline_raw.mp4",
            ],
            label="XfadeMerge",
            timeout_sec=150,
        )

    total_dur = get_media_duration("timeline_raw.mp4")
    if not CHANNEL_CONFIG["enable_ai_voiceover"]:
        os.replace("timeline_raw.mp4", merged_out_path)
        return total_dur

    intro_dur = generate_clean_tts_clip(script_bundle.get("intro_vo", ""), "vo_act1.wav")
    outro_dur = generate_clean_tts_clip(script_bundle.get("outro_vo", ""), "vo_act3.wav")

    # Ensure Act 2 climax always gets at least 55% of the video in 100% raw silence
    if intro_dur > 0 and outro_dur > 0 and total_dur > (intro_dur + outro_dur + 5.0):
        outro_start = max(intro_dur + 4.5, total_dur - outro_dur - 0.35)
        outro_delay_ms = int(outro_start * 1000)
        duck_expr = (
            f"if(lt(t,{intro_dur + 0.30:.2f}),0.15,"
            f"if(between(t,{outro_start:.2f},{outro_start + outro_dur + 0.30:.2f}),0.18,1.0))"
        )
        run_cmd_safe(
            [
                "ffmpeg", "-y",
                "-i", "timeline_raw.mp4", "-i", "vo_act1.wav", "-i", "vo_act3.wav",
                "-filter_complex",
                f"[0:a]volume='{duck_expr}':eval=frame[ga];"
                "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.35[v1];"
                f"[2:a]aresample=48000,aformat=channel_layouts=stereo,adelay={outro_delay_ms}|{outro_delay_ms},volume=1.35[v3];"
                "[ga][v1][v3]amix=inputs=3:duration=first:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                merged_out_path,
            ],
            label="3ActAudioMix",
            timeout_sec=90,
        )
        print(
            f"[3-Act Audio] Intro 0.0-{intro_dur:.1f}s | SILENT CLIMAX {intro_dur:.1f}-{outro_start:.1f}s | Outro {outro_start:.1f}-{total_dur:.1f}s",
            flush=True,
        )
    elif intro_dur > 0:
        duck_expr = f"if(lt(t,{intro_dur + 0.30:.2f}),0.15,1.0)"
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-i", "timeline_raw.mp4", "-i", "vo_act1.wav",
                "-filter_complex",
                f"[0:a]volume='{duck_expr}':eval=frame[ga];"
                "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.35[v1];"
                "[ga][v1]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                merged_out_path,
            ],
            label="IntroOnlyAudioMix",
            timeout_sec=90,
        )
    else:
        os.replace("timeline_raw.mp4", merged_out_path)

    return total_dur

# ==============================================================================
# PART 20 OF 25: DYNAMIC ENGLISH SUBTITLES (ASS POP-IN) & HOOK BANNER BURNER
# Title: Synced English Caption Generator & Neon Progress Bar Overlay
# Subtitle: Guarantees 100% English subtitles regardless of streamer language
# ==============================================================================
def _format_ass_time(sec):
    cs = max(0, int(round(sec * 100)))
    hrs = cs // 360000
    mins = (cs % 360000) // 6000
    secs = (cs % 6000) // 100
    rem = cs % 100
    return f"{hrs}:{mins:02d}:{secs:02d}.{rem:02d}"


def burn_hook_banner_and_english_captions(merged_path, banner_text, final_short_path):
    """
    1) Transcribes & translates final merged track to English for exact timing
    2) Writes Vice City styled .ass pop-in subtitles (yellow highlight + black outline)
    3) Burns top ALL-CAPS Hook Banner + bottom retention progress bar
    """
    _, word_events = transcribe_clip_to_english(merged_path)
    total_dur = max(1.0, get_media_duration(merged_path))

    ass_path = "captions_en.ass"
    ass_header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,DejaVu Sans,58,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,"
        "-1,0,0,0,100,100,0,0,1,4,2,2,60,60,310,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogue_lines = []
    for (st, en, raw_line) in word_events:
        words = raw_line.upper().split()
        if not words:
            continue
        # Chunk into punchy 3-4 word bursts for high retention
        chunk_size = 4
        chunks = [words[i : i + chunk_size] for i in range(0, len(words), chunk_size)]
        span = max(0.25, (en - st) / len(chunks))
        for c_idx, chunk_words in enumerate(chunks):
            c_start = st + (c_idx * span)
            c_end = min(en, c_start + span)
            if (c_end - c_start) < 0.12:
                continue
            phrase = " ".join(chunk_words).replace("{", "").replace("}", "")
            dialogue_lines.append(
                f"Dialogue: 0,{_format_ass_time(c_start)},{_format_ass_time(c_end)},Default,,0,0,0,,"
                f"{{\\fscx92\\fscy92\\t(0,80,\\fscx104\\fscy104)\\t(80,150,\\fscx100\\fscy100)}}{phrase}"
            )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(dialogue_lines) + "\n")

    safe_banner = _escape_drawtext(banner_text.upper()[:26])
    vf_final = (
        f"ass={ass_path},"
        f"drawbox=x=120:y=248:w=840:h=76:color=black@0.82:t=fill,"
        f"drawbox=x=120:y=248:w=840:h=4:color=#ffe600@0.95:t=fill,"
        f"drawbox=x=120:y=320:w=840:h=4:color=#ffe600@0.95:t=fill,"
        f"drawtext=text='{safe_banner}':fontcolor=#ffe600:fontsize=40:"
        f"x=(w-text_w)/2:y=267,"
        f"drawbox=x=0:y=1906:w='iw*(t/{total_dur:.2f})':h=14:color=#ff00cc@0.95:t=fill"
    )

    run_cmd_safe(
        [
            "ffmpeg", "-y", "-i", merged_path,
            "-vf", vf_final,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "copy",
            final_short_path,
        ],
        label="BurnCaptionsAndBanner",
        timeout_sec=120,
    )
    print(f"[Final Render] Saved {final_short_path} ({total_dur:.1f}s)", flush=True)

# ==============================================================================
# PART 21 OF 25: HIGH-CONTRAST CLIMAX THUMBNAIL GENERATOR
# Title: Peak-Action Frame Extractor for Custom YouTube Shorts Cover
# Subtitle: Captures a sharp, vibrant JPEG from the #1 climax segment
# ==============================================================================
def generate_short_thumbnail(final_short_path, thumb_out_path):
    """Extracts a sharp, high-contrast frame near the #1 climax of the rendered Short."""
    dur = get_media_duration(final_short_path)
    grab_ts = max(1.0, dur - 4.2)
    try:
        run_cmd_safe(
            [
                "ffmpeg", "-y", "-ss", f"{grab_ts:.2f}", "-i", final_short_path,
                "-vframes", "1",
                "-vf", "eq=contrast=1.10:saturation=1.18,unsharp=5:5:0.9",
                "-q:v", "2",
                thumb_out_path,
            ],
            label="MakeThumb",
            timeout_sec=30,
        )
        return os.path.exists(thumb_out_path)
    except Exception as e:
        print(f"[Thumbnail Warning] {e}", flush=True)
        return False

# ==============================================================================
# PART 22 OF 25: YOUTUBE DATA API V3 CLIENT & SINGLE-SLOT UPLOADER
# Title: OAuth Token Refresher & Privacy/Schedule-Controlled Video Publisher
# Subtitle: Respects upload_to_youtube, youtube_privacy & enable_scheduling strictly
# ==============================================================================
def get_youtube_service():
    """Authenticates YouTube Data API v3 from YOUTUBE_TOKEN_JSON secret."""
    raw_token = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not raw_token:
        raise RuntimeError("YOUTUBE_TOKEN_JSON secret is missing.")
    token_info = json.loads(raw_token)
    creds = Credentials.from_authorized_user_info(
        token_info,
        scopes=[
            "https://googleapis.com",
            "https://googleapis.com",
        ],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_short_to_youtube(youtube, video_path, thumb_path, script_bundle, publish_at_iso=None):
    """
    Uploads 1 Short with:
    - Scene-grounded English title + #Shorts tag
    - Creator credits in description
    - Exact privacyStatus ('public' | 'private' | 'unlisted') or scheduled publishAt
    """
    title = script_bundle["title"][:95]
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"[:100]

    creators_txt = ", ".join(script_bundle.get("creators", []))
    description = (
        f"{script_bundle['title']}\n\n"
        f"Featured Creators: {creators_txt}\n"
        f"Game: {CHANNEL_CONFIG['game_title']}\n"
        f"Subscribe to {CHANNEL_CONFIG['channel_handle']} for daily Vice City & GTA 6 highlights!\n\n"
        f"#GTA6 #GrandTheftAuto6 #ViceCity #GTA6Clips #Shorts"
    )

    do_schedule = bool(CHANNEL_CONFIG["enable_scheduling"]) and bool(publish_at_iso)
    status_body = {
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": bool(CHANNEL_CONFIG["enable_ai_voiceover"]),
    }
    if do_schedule:
        # YouTube requires privacyStatus='private' alongside publishAt; auto-publishes at publishAt
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = publish_at_iso
    else:
        status_body["privacyStatus"] = CHANNEL_CONFIG["youtube_privacy"]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": script_bundle.get("tags", ["GTA 6", "Vice City", "Shorts"]),
            "categoryId": "20",  # Gaming
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": status_body,
    }

    media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id")
    if video_id and thumb_path and os.path.exists(thumb_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
            ).execute()
        except Exception as e:
            print(f"[Thumb Upload Info] Custom thumbnail skipped: {e}", flush=True)

    mode_desc = f"SCHEDULED ({publish_at_iso})" if do_schedule else f"UPLOADED ({status_body['privacyStatus']})"
    print(f"[YouTube Success] Video ID={video_id} | {mode_desc} | Title='{title}'", flush=True)
    return video_id

# ==============================================================================
# PART 23 OF 25: ENGAGEMENT PINNED COMMENTER
# Title: Post-Upload Community Question Commenter
# Subtitle: Skips private drafts automatically; boosts comments on public/unlisted
# ==============================================================================
def post_first_engagement_comment(youtube, video_id, script_bundle):
    """Posts an interactive English comment on public/unlisted uploads."""
    if not video_id:
        return
    if not CHANNEL_CONFIG["enable_scheduling"] and CHANNEL_CONFIG["youtube_privacy"] == "private":
        return

    creators = script_bundle.get("creators", [])
    top_creator = next(iter(reversed(creators)), "that clip")
    comment_text = (
        f"Rate {top_creator}'s Vice City moment from 1 to 10! "
        f"Which creator should we feature next? 👇🔥"
    )
    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": comment_text}},
                }
            },
        ).execute()
        print(f"[Comment Posted] Added engagement question on {video_id}", flush=True)
    except Exception as e:
        print(f"[Comment Info] Skipped comment: {e}", flush=True)

# ==============================================================================
# PART 24 OF 25: TEMPORARY MEDIA & WORKSPACE CLEANUP
# Title: Safe Artifact Preserver & Temp File Sweeper
# Subtitle: Cleans intermediate clips while keeping final_short_*.mp4 & history.json
# ==============================================================================
def cleanup_temp_workspace():
    """Deletes intermediate clips/audio/subtitles while keeping final Shorts & thumbnails."""
    prefixes = ("raw_", "clip_", "seg_", "timeline_", "merged_", "vo_", "tts_", "probe_", "gem_f_", "judge_f_")
    exact_files = ("captions_en.ass",)
    for fname in os.listdir("."):
        if fname.startswith(prefixes) or fname in exact_files:
            try:
                os.remove(fname)
            except Exception:
                pass

# ==============================================================================
# PART 25 OF 25: MASTER HOURLY ORCHESTRATOR (__MAIN__)
# Title: End-to-End ViceCityClipsVault Automated Production Pipeline
# Subtitle: YouTube-First Heatmap Harvest -> Local OpenCLIP Judge -> 3-Act Render
# ==============================================================================
def run_hourly_pipeline():
    start_wall = time.time()
    print("============================================================", flush=True)
    print(
        f"[Boot] {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) | "
        f"upload={CHANNEL_CONFIG['upload_to_youtube']} | "
        f"privacy={CHANNEL_CONFIG['youtube_privacy']} | "
        f"schedule={CHANNEL_CONFIG['enable_scheduling']}",
        flush=True,
    )
    print("============================================================", flush=True)

    history_data = load_history()
    do_upload = bool(CHANNEL_CONFIG["upload_to_youtube"])
    do_schedule = bool(CHANNEL_CONFIG["enable_scheduling"])

    # 1) Quota & Single-Slot Gate (only enforced when uploading to YouTube)
    next_slot_iso = None
    if do_upload:
        todays_count = count_todays_uploads(history_data)
        if todays_count >= int(CHANNEL_CONFIG["max_daily_uploads"]):
            print(
                f"[Quota Guard] Daily upload cap reached ({todays_count}/{CHANNEL_CONFIG['max_daily_uploads']}). Exiting cleanly.",
                flush=True,
            )
            return
        if do_schedule:
            next_slot_iso = get_next_single_peak_slot(history_data)
            if not next_slot_iso:
                print(
                    "[Slot Guard] Next upcoming peak slot is already scheduled! Skipping until next slot opens.",
                    flush=True,
                )
                return
            print(f"[Slot Guard] Target single upcoming slot: {next_slot_iso}", flush=True)

    # 2) Wide YouTube + Indian Creator Discovery -> Pre-Download Heatmap Check
    discovered = discover_wide_youtube_videos()
    heatmap_targets, no_heatmap_pool = inspect_videos_for_heatmap_peaks(
        discovered, history_data, max_inspect=24
    )

    # 3) Surgical 26s Climax Slices from Top Heatmap Peaks
    candidates = download_surgical_heatmap_slices(heatmap_targets, max_slices=8)

    # 4) If fewer than 4 heatmap slices found, sample Fresh / Live YouTube streams
    if len(candidates) < 4 and no_heatmap_pool:
        extra_yt = harvest_fresh_or_live_youtube(no_heatmap_pool, max_streams=3)
        candidates.extend(extra_yt)

    # 5) Last-Resort Twitch Fallback ONLY if YouTube yielded < 2 candidates
    if len(candidates) < 2:
        print("[Source Fallback] YouTube pool < 2; checking Twitch backup...", flush=True)
        candidates.extend(harvest_backup_twitch_streams(max_streams=2))

    if not candidates:
        print("[Harvest Info] No valid GTA 6 candidates available this hour.", flush=True)
        return

    # 6) Unlimited Local OpenCLIP + OpenCV Visual Action Judge (0 API calls!)
    scored_winners = score_candidates_with_local_vision(candidates)
    short_plan = build_short_plan(scored_winners)
    if not short_plan:
        cleanup_temp_workspace()
        return

    # 7) Scene-Grounded English Title & 3-Act Commentary (Gemini sees winning frames)
    script_bundle = generate_scene_grounded_script(short_plan, history_data)

    # 8) Render Formatted Segments (#3 -> #2 -> #1 or #1 Single)
    segments = short_plan["segments"]
    seg_files = []
    for idx, seg in enumerate(segments):
        seg_out = f"seg_{idx}_rank{seg['rank']}.mp4"
        render_formatted_segment(seg, idx, len(segments), seg_out)
        seg_files.append(seg_out)

    # 9) Join Timeline + 3-Act Audio (Intro Hook -> Silent Climax -> Outro Punchline)
    merged_path = "merged_3act.mp4"
    assemble_timeline_with_3act_audio(seg_files, script_bundle, merged_path)

    # 10) Burn Top Hook Banner + Synced English Subtitles
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    final_short_path = f"final_short_{stamp}.mp4"
    thumb_path = f"thumb_{stamp}.jpg"
    burn_hook_banner_and_english_captions(
        merged_path, script_bundle["banner"], final_short_path
    )
    generate_short_thumbnail(final_short_path, thumb_path)

    # 11) Upload / Schedule Single Video (ONLY if upload_to_youtube == True)
    uploaded_vid_id = None
    if do_upload:
        yt_service = get_youtube_service()
        uploaded_vid_id = upload_short_to_youtube(
            yt_service,
            final_short_path,
            thumb_path,
            script_bundle,
            publish_at_iso=next_slot_iso,
        )
        post_first_engagement_comment(yt_service, uploaded_vid_id, script_bundle)
        if do_schedule and next_slot_iso:
            history_data["scheduled_slots"].append(next_slot_iso)
    else:
        print(
            f"[Test Mode] upload_to_youtube=False -> Saved '{final_short_path}' locally for artifact preview.",
            flush=True,
        )

    # 12) Record Used Fingerprints & Title in history.json
    for seg in segments:
        c = seg["candidate"]
        fp = build_clip_fingerprint(c["video_id"], c["origin_peak_sec"])
        if fp not in history_data["used_clips"]:
            history_data["used_clips"].append(fp)

    history_data["uploads"].append({
        "video_id": uploaded_vid_id or f"local_{stamp}",
        "title": script_bundle["title"],
        "mode": short_plan["mode"],
        "creators": script_bundle.get("creators", []),
        "scheduled_for": next_slot_iso,
        "uploaded": do_upload,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    save_history(history_data)
    cleanup_temp_workspace()

    elapsed = round(time.time() - start_wall, 1)
    print(f"[Complete] Pipeline finished cleanly in {elapsed}s!", flush=True)


if __name__ == "__main__":
    try:
        run_hourly_pipeline()
    except Exception as fatal_err:
        print(f"[Fatal Error] {fatal_err}", flush=True)
        cleanup_temp_workspace()
        sys.exit(1)
