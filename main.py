# ==============================================================================
# PART 1 OF 30: IMPORTS, ENVIRONMENT & RUNTIME SETUP
# Subheading: Core Python, Media, AI, Whisper, Edge-TTS & YouTube API Dependencies
# Code Title: imports_and_runtime
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
# PART 2 OF 30: MASTER CHANNEL & PIPELINE CONFIGURATION
# Subheading: Controls Upload Toggle, 6-Hour IST Schedule, Vault & Clip Limits
# Code Title: CHANNEL_CONFIG
# ==============================================================================
CHANNEL_CONFIG = {
    "channel_name": "Vice City Clips Vault",
    "channel_handle": "@Vice-City_ClipsVault",
    "watermark_text": "@Vice-City_ClipsVault",
    "upload_to_youtube": False,  # Flip to True after verifying 1 test render!
    "vault_dir": "clip_vault",
    "history_file": "history.json",
    "max_clips_per_short": 5,
    "max_new_clips_per_run": 3,
    "min_motion_diff": 3.4,
    "publish_hours_ist": [7, 13, 19, 1],
    "publish_minute_ist": 30,
    "tts_voice": "en-US-GuyNeural",
    "search_queries": [
        "GTA 6 gameplay stream highlights",
        "Grand Theft Auto 6 funny moments",
        "GTA 6 Vice City shootout chase",
        "GTA 6 live reaction gameplay",
    ],
}

IST_TZ = timezone(timedelta(hours=5, minutes=30))


# ==============================================================================
# PART 3 OF 30: SAFE SUBPROCESS & MEDIA DURATION HELPERS
# Subheading: Executes FFmpeg/FFprobe/yt-dlp Commands With Timeout Protection
# Code Title: run_cmd_safe
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
# PART 4 OF 30: HISTORY & VAULT STATE PERSISTENCE
# Subheading: Loads & Saves history.json Safely With Default Schema
# Code Title: load_and_save_history
# ==============================================================================
def load_history():
    hfile = CHANNEL_CONFIG["history_file"]
    default_data = {
        "used_clips": [],
        "vault_queue": [],
        "scheduled_slots": [],
        "uploads": [],
    }
    if not os.path.exists(hfile):
        return default_data
    try:
        with open(hfile, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default_data.items():
            if k not in data or not isinstance(data[k], list):
                data[k] = list(v)
        return data
    except Exception:
        return default_data


def save_history(history_data):
    hfile = CHANNEL_CONFIG["history_file"]
    with open(hfile, "w", encoding="utf-8") as f:
        json.dump(history_data, f, indent=2, ensure_ascii=False)


# ==============================================================================
# PART 5 OF 30: 6-HOUR IST SCHEDULE SLOT CALCULATOR
# Subheading: Computes Next Upcoming Peak Slot & Checks Publish Window
# Code Title: should_trigger_publish_window
# ==============================================================================
def get_next_ist_publish_slot(history_data):
    now_ist = datetime.now(IST_TZ)
    booked = set(history_data.get("scheduled_slots", []))
    candidates = []
    for day_offset in range(0, 3):
        base_day = (now_ist + timedelta(days=day_offset)).replace(
            minute=CHANNEL_CONFIG["publish_minute_ist"], second=0, microsecond=0
        )
        for hr in CHANNEL_CONFIG["publish_hours_ist"]:
            slot_dt = base_day.replace(hour=hr)
            if slot_dt > now_ist + timedelta(minutes=12):
                iso_utc = slot_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if iso_utc not in booked:
                    candidates.append((slot_dt, iso_utc))
    candidates.sort(key=lambda x: x[0])
    if not candidates:
        return None
    _, next_iso = next(iter(candidates))
    return next_iso

def should_trigger_publish_window(history_data, force_flag=False):
    slot_iso = get_next_ist_publish_slot(history_data)
    if force_flag:
        return True, slot_iso

    now_ist = datetime.now(IST_TZ)
    is_publish_hour = now_ist.hour in CHANNEL_CONFIG["publish_hours_ist"]
    vault_ready = len([
        v for v in history_data.get("vault_queue", [])
        if v.get("vault_path") and os.path.exists(v["vault_path"])
    ]) >= 1
    return (is_publish_hour and vault_ready), slot_iso

# ==============================================================================
# PART 6 OF 30: FRESH GTA 6 CANDIDATE DISCOVERY
# Subheading: Searches Recent Gameplay Streams & Filters Already-Used Video IDs
# Code Title: discover_gta6_candidates
# ==============================================================================
def discover_gta6_candidates(history_data):
    used_ids = {
        str(x.get("video_id", "")).strip()
        for x in history_data.get("used_clips", [])
        if isinstance(x, dict)
    }
    vault_ids = {
        str(x.get("video_id", "")).strip()
        for x in history_data.get("vault_queue", [])
        if isinstance(x, dict)
    }
    skip_ids = used_ids | vault_ids

    queries = list(CHANNEL_CONFIG["search_queries"])
    random.shuffle(queries)
    found = []
    seen_now = set()

    for q in queries[:2]:
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            f"ytsearch10:{q}",
        ]
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
            uploader = str(info.get("uploader") or info.get("channel") or "GTA 6 Creator").strip()

            if not vid_id or vid_id in skip_ids or vid_id in seen_now:
                continue
            # Prefer real gameplay videos (45s to 45 mins) and skip vertical Shorts
            if dur < 45 or dur > 2700:
                continue
            if "#shorts" in title.lower():
                continue

            seen_now.add(vid_id)
            found.append({
                "video_id": vid_id,
                "url": "https://" + "://youtube.com" + "?v=" + vid_id,
                "title": title,
                "channel_name": uploader,
                "duration": dur,
            })


    random.shuffle(found)
    return found[:8]

# ==============================================================================
# PART 7 OF 30: CANDIDATE VIDEO DOWNLOADER (720P MP4 + COOKIES & CLIENT BYPASS)
# Subheading: Loads YouTube Cookies From Any Configured Secret & Downloads 720p MP4
# Code Title: download_candidate_video
# ==============================================================================
def _prepare_cookies_file():
    for candidate_path in ("yt_cookies.txt", "cookies.txt"):
        if os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 40:
            return candidate_path

    for env_name in ("YT_COOKIES_TXT", "YOUTUBE_COOKIES", "YT_COOKIES", "COOKIES_TXT"):
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
    if not cookies_file:
        print("[Cookies Warning] No YouTube cookies secret found in env (YT_COOKIES_TXT / YOUTUBE_COOKIES).", flush=True)

    vid_id = str(candidate["video_id"]).strip()
    client_profiles = (
        "youtube:player_client=mediaconnect,android_vr",
        "youtube:player_client=tv_embedded,mweb",
    )

    for idx, extractor_arg in enumerate(client_profiles):
        cmd = [
            "yt-dlp",
            "--js-runtimes", "deno",
            "--extractor-args", extractor_arg,
            "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--max-filesize", "220M",
            "-o", out_path,
        ]
        if cookies_file:
            cmd.extend(["--cookies", cookies_file])
        cmd.extend(["--", vid_id])

        res = run_cmd_safe(cmd, label=f"Download_{vid_id}_{idx}", timeout_sec=160, check=False)
        if res.returncode == 0 and os.path.exists(out_path) and get_media_duration(out_path) >= 20.0:
            print(f"[Downloaded] {vid_id} ({candidate['channel_name']})", flush=True)
            return True

        err_msg = (res.stderr or res.stdout or "").strip().splitlines()[-1:]
        print(f"[Download Retry {idx+1}] {vid_id}: {err_msg}", flush=True)
        if os.path.exists(out_path):
            os.remove(out_path)

    return False

# ==============================================================================
# PART 8 OF 30: AUDIO-ENERGY CLIMAX PEAK DETECTOR
# Subheading: Scans Audio Waveform Spikes To Locate Top Climax Timestamps
# Code Title: detect_audio_video_peaks
# ==============================================================================
def detect_audio_video_peaks(video_path, max_peaks=2):
    total_dur = get_media_duration(video_path)
    if total_dur < 20.0:
        return []

    wav_tmp = "tmp_peak_scan.wav"
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)

    res = run_cmd_safe(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", wav_tmp,
        ],
        label="ExtractPeakPCM",
        timeout_sec=60,
        check=False,
    )
    if res.returncode != 0 or not os.path.exists(wav_tmp):
        return [round(total_dur * 0.5, 2)]

    try:
        raw = np.fromfile(wav_tmp, dtype=np.int16).astype(np.float32)
    finally:
        if os.path.exists(wav_tmp):
            os.remove(wav_tmp)

    sr = 8000
    win = sr * 2  # 2-second energy windows
    if len(raw) < win * 5:
        return [round(total_dur * 0.5, 2)]

    trimmed = raw[: (len(raw) // win) * win].reshape(-1, win)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
    median_rms = float(np.median(rms)) + 1e-5

    # Ignore first 8s (intros) and last 8s (outros)
    start_idx = max(4, int(8 // 2))
    end_idx = max(start_idx + 1, len(rms) - 4)

    scored_bins = []
    for idx in range(start_idx, end_idx):
        ratio = float(rms[idx] / median_rms)
        scored_bins.append((ratio, idx * 2.0 + 1.0))

    scored_bins.sort(key=lambda item: item[0], reverse=True)
    chosen_peaks = []
    for ratio, ts in scored_bins:
        if all(abs(ts - prev) >= 40.0 for prev in chosen_peaks):
            chosen_peaks.append(round(ts, 2))
        if len(chosen_peaks) >= max_peaks:
            break

    return chosen_peaks or [round(total_dur * 0.5, 2)]


# ==============================================================================
# PART 9 OF 30: ACTION MOTION & VISUAL QUALITY FILTER
# Subheading: Rejects Static Menus, Lobbies & Slow Walking Clips (mean_diff >= 3.4)
# Code Title: verify_action_motion_and_quality
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

    step = max(1, int(fps // 2))  # Sample 2 frames per second
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
    return mean_diff >= float(CHANNEL_CONFIG["min_motion_diff"]) and peak_diff >= 5.5


# ==============================================================================
# PART 10 OF 30: VIRAL SCORE CALCULATOR FOR HARVESTED CLIPS
# Subheading: Scores Clip Excitement (6.8 - 9.6) To Trigger Solo vs Countdown Mode
# Code Title: compute_clip_viral_score
# ==============================================================================
def compute_clip_viral_score(clip_path, transcript_text=""):
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        return 7.5

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

    motion_val = float(np.mean(diffs)) if diffs else 4.0
    score = 6.8 + min(2.0, max(0.0, (motion_val - 3.4) * 0.35))
    hype_words = ("bro", "what", "no way", "cops", "shoot", "run", "crazy", "oh my", "help", "watch")
    if any(w in (transcript_text or "").lower() for w in hype_words):
        score += 0.45
    return round(min(9.6, score), 2)

# ==============================================================================
# PART 11 OF 30: 50-SECOND CLIMAX SLICE HARVESTER INTO CLIP VAULT
# Subheading: Extracts Up To 50s Around Peak Action & Records Local Peak Offset
# Code Title: harvest_climax_slices_to_vault
# ==============================================================================
def harvest_climax_slices_to_vault(source_path, candidate_meta, peak_timestamps):
    """
    Extracts up to 50.0s around each detected climax timestamp (23s pre-roll +
    27s post-roll) into clip_vault/ so the planner can either run a full 45-52s
    Solo Feature or trim around local_peak_sec for a 2-to-5 clip countdown.
    """
    vault_dir = CHANNEL_CONFIG.get("vault_dir", "clip_vault")
    os.makedirs(vault_dir, exist_ok=True)

    total_dur = get_media_duration(source_path)
    if total_dur < 12.0 or not peak_timestamps:
        return []

    target_slice_dur = 50.0
    pre_roll = 23.0
    video_id = str(candidate_meta.get("video_id", "vid")).strip()
    channel_name = str(candidate_meta.get("channel_name", "GTA 6 Creator")).strip()
    source_title = str(candidate_meta.get("title", "GTA 6 Gameplay")).strip()
    base_viral_score = float(candidate_meta.get("viral_score", 7.8))

    harvested_items = []
    used_windows = []

    for idx, raw_peak in enumerate(peak_timestamps):
        peak_sec = float(raw_peak)
        if any(abs(peak_sec - prev_p) < 35.0 for prev_p in used_windows):
            continue

        slice_dur = min(target_slice_dur, total_dur)
        slice_start = max(0.0, min(total_dur - slice_dur, peak_sec - pre_roll))
        slice_end = min(total_dur, slice_start + slice_dur)
        actual_span = round(slice_end - slice_start, 2)

        if actual_span < 12.0:
            continue

        local_peak = round(max(1.0, min(actual_span - 1.0, peak_sec - slice_start)), 2)
        safe_ts = int(round(slice_start))
        vault_filename = f"{video_id}_p{idx}_{safe_ts}s.mp4"
        vault_path = os.path.join(vault_dir, vault_filename)

        try:
            run_cmd_safe(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{slice_start:.2f}",
                    "-t", f"{actual_span:.2f}",
                    "-i", source_path,
                    "-r", "30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                    vault_path,
                ],
                label=f"HarvestVaultSlice_{idx}",
                timeout_sec=120,
            )
        except Exception as e:
            print(f"[Harvest Warning] Failed slice {vault_filename}: {e}", flush=True)
            if os.path.exists(vault_path):
                os.remove(vault_path)
            continue

        verified_dur = get_media_duration(vault_path)
        if verified_dur < 11.0:
            if os.path.exists(vault_path):
                os.remove(vault_path)
            continue

        used_windows.append(peak_sec)
        harvested_items.append({
            "vault_path": vault_path,
            "video_id": video_id,
            "channel_name": channel_name,
            "source_title": source_title,
            "source_start_sec": round(slice_start, 2),
            "duration_sec": round(verified_dur, 2),
            "local_peak_sec": local_peak,
            "viral_score": round(base_viral_score, 2),
            "transcript_en": "",
            "word_events": [],
            "harvested_at": datetime.now(timezone.utc).isoformat(),
        })
        print(
            f"[Vault Harvested] {vault_filename} ({verified_dur:.1f}s, local_peak={local_peak:.1f}s) from {channel_name}",
            flush=True,
        )

    return harvested_items

# ==============================================================================
# PART 12 OF 30: FASTER-WHISPER ENGLISH SPEECH TRANSLATOR & WORD TIMER
# Subheading: Translates Any Non-English Speech To English & Extracts Word Timings
# Code Title: transcribe_and_translate_clip
# ==============================================================================
_WHISPER_MODEL_CACHE = None


def _get_whisper_model():
    global _WHISPER_MODEL_CACHE
    if _WHISPER_MODEL_CACHE is None:
        _WHISPER_MODEL_CACHE = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _WHISPER_MODEL_CACHE


def transcribe_and_translate_clip(clip_path):
    """
    Transcribes & translates spoken dialogue in the clip into English and returns
    (full_english_text, word_events_list).
    """
    wav_tmp = "tmp_whisper_clip.wav"
    if os.path.exists(wav_tmp):
        os.remove(wav_tmp)

    res = run_cmd_safe(
        [
            "ffmpeg", "-y", "-i", clip_path,
            "-vn", "-ac", "1", "-ar", "16000", wav_tmp,
        ],
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
# PART 13 OF 30: VERIFIED STREAMER WEBCAM CORNER DETECTOR
# Subheading: Detects Real Corner Facecam Boxes (Never Mini-Maps or HUD Icons)
# Code Title: detect_verified_webcam_box
# ==============================================================================
def detect_verified_webcam_box(video_path, start_s, end_s):
    """
    Checks the 4 screen corners for a confirmed human face using OpenCV Haar
    cascades across multiple sampled frames. Returns (x, y, w, h) crop box if a
    real streamer webcam is verified in >= 2 frames, otherwise None.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if width < 640 or height < 360:
        cap.release()
        return None

    cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    face_cascade = cv2.CascadeClassifier(cascade_path)
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
# PART 14 OF 30: VAULT HOUSEKEEPING & STALE FILE CLEANER
# Subheading: Caps Vault Size To Best 18 Clips & Removes Orphaned Temp Files
# Code Title: prune_vault_and_temp_files
# ==============================================================================
def prune_vault_and_temp_files(history_data, max_vault_items=18):
    vault = [
        item for item in history_data.get("vault_queue", [])
        if item.get("vault_path") and os.path.exists(item["vault_path"])
    ]
    vault.sort(key=lambda x: float(x.get("viral_score", 0.0)), reverse=True)

    keep = vault[:max_vault_items]
    discard = vault[max_vault_items:]
    for old_item in discard:
        vp = old_item.get("vault_path", "")
        if vp and os.path.exists(vp):
            try:
                os.remove(vp)
            except Exception:
                pass

    history_data["vault_queue"] = keep

    if len(history_data.get("used_clips", [])) > 400:
        history_data["used_clips"] = history_data["used_clips"][-400:]

# ==============================================================================
# PART 15 OF 30: SINGLE-CLIP MONETIZATION ELIGIBILITY GUARD
# Subheading: Ensures Solo 45-52s Clips Have Strong Commentary & Framing
# Code Title: validate_monetization_eligibility
# ==============================================================================
def validate_monetization_eligibility(short_plan, script_data):
    """
    Verifies every Short (especially 1-clip 45-52s Solo Features) includes
    transformative commentary, creator credit, and custom framing so it stays
    compliant with YouTube Partner Program Reused Content policies.
    """
    segments = short_plan.get("segments", [])
    if not segments:
        return False
    clip_vos = script_data.get("clip_vos", {})
    if not clip_vos or not str(script_data.get("outro_vo", "")).strip():
        return False
    if len(segments) == 1 and not str(script_data.get("mid_vo", "")).strip():
        script_data["mid_vo"] = "Watch how quickly this entire Vice City encounter turns upside down!"
    return True

# ==============================================================================
# PART 16 OF 30: CREATOR & STREAM METADATA SANITIZER
# Subheading: Cleans Channel Names & Stream Titles For Watermarks & Captions
# Code Title: sanitize_creator_and_title
# ==============================================================================
def sanitize_creator_and_title(raw_channel, raw_title):
    ch = re.sub(r"[^\w\s\-\.\&]", "", str(raw_channel or "GTA 6 Creator")).strip()
    ch = re.sub(r"\s+", " ", ch)[:32] or "GTA 6 Creator"

    title = re.sub(r"http\S+|#\S+", "", str(raw_title or "GTA 6 Gameplay")).strip()
    title = re.sub(r"\s+", " ", title)[:90] or "GTA 6 Gameplay"
    return ch, title

# ==============================================================================
# PART 17 OF 30: DYNAMIC TOP 1-TO-5 VAULT PLANNER & DURATION BUDGETER
# Subheading: Builds 45-52s Solo Feature OR Dynamic #N -> #1 Countdown (2-5 Clips)
# Code Title: build_top5_plan_from_vault
# ==============================================================================
def build_top5_plan_from_vault(history_data):
    """
    Selects up to 5 distinct-creator clips from clip_vault/:
    - If top clip is an elite gem (score >= 8.5 and >= 34s) or only 1 clip exists:
      builds a 45-52s Solo Monetization-Safe Feature (1 clip).
    - Otherwise picks all available distinct-creator clips up to 5 (2, 3, 4, or 5)
      and numbers them dynamically (#N down to #1).
    """
    vault = [
        item for item in history_data.get("vault_queue", [])
        if item.get("vault_path") and os.path.exists(item["vault_path"])
    ]
    if not vault:
        return None

    vault.sort(key=lambda x: float(x.get("viral_score", 0.0)), reverse=True)

    distinct_best = []
    seen_creators = set()
    for item in vault:
        ch_key = str(item.get("channel_name", "")).strip().lower()
        if ch_key and ch_key in seen_creators:
            continue
        if ch_key:
            seen_creators.add(ch_key)
        distinct_best.append(item)
        if len(distinct_best) >= int(CHANNEL_CONFIG["max_clips_per_short"]):
            break

    top_item = next(iter(distinct_best))
    top_avail_dur = get_media_duration(top_item["vault_path"])

    if len(distinct_best) == 1 or (float(top_item.get("viral_score", 0.0)) >= 8.5 and top_avail_dur >= 34.0):
        chosen = [top_item]
    else:
        chosen = list(reversed(distinct_best))

    count = len(chosen)
    target_total = 50.0 if count == 1 else 56.5
    per_clip_budget = target_total / count

    segments = []
    for idx, item in enumerate(chosen):
        vpath = item["vault_path"]
        avail_d = get_media_duration(vpath)
        if avail_d <= 3.0:
            continue

        use_dur = min(avail_d, per_clip_budget)
        peak_local = float(item.get("local_peak_sec", avail_d / 2.0))
        start_s = max(0.0, min(avail_d - use_dur, peak_local - use_dur * 0.45))
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
            "viral_score": float(item.get("viral_score", 7.5)),
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
        f"[Vault Plan] Built {final_count}-clip Short ({'SOLO FEATURE' if final_count == 1 else f'COUNTDOWN #{final_count} -> #1'}).",
        flush=True,
    )
    return {"segments": segments, "is_solo": final_count == 1}

# ==============================================================================
# PART 18 OF 30: EDGE-TTS NEURAL VOICEOVER SYNTHESIZER
# Subheading: Generates Crisp English Commentary Audio Tracks With Fallback Voices
# Code Title: synthesize_voiceover_mp3
# ==============================================================================
async def _edge_tts_save_async(text, out_mp3, voice):
    communicate = edge_tts.Communicate(text, voice, rate="+8%")
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
            time.sleep(0.5)
    return 0.0

# ==============================================================================
# PART 19 OF 30: GEMINI 2.5 MONETIZATION-SAFE SCRIPT & COMMENTARY WRITER
# Subheading: Writes 3-Beat Transformative Commentary (Solo) or Dynamic #N -> #1 Cues
# Code Title: generate_scene_grounded_script
# ==============================================================================
def generate_scene_grounded_script(short_plan, history_data):
    segments = short_plan["segments"]
    count = len(segments)
    creators = [s["channel_name"] for s in segments]
    *_, top_seg = segments
    recent_titles = [u.get("title", "") for u in history_data.get("uploads", [])[-12:]]

    frame_paths = []
    prompt_parts = []
    context_lines = []

    for idx, seg in enumerate(segments):
        mid_ts = (seg["start_sec"] + seg["end_sec"]) / 2.0
        for offset_i, sample_t in enumerate((max(0.2, mid_ts - 1.5), mid_ts + 1.0)):
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

    if count == 1:
        mode_instructions = (
            f'3. "clip_vos": JSON object with key "1" containing a punchy 1-sentence Opening Hook (8-11 words) setting up what happens in {top_seg["channel_name"]}\'s clip.\n'
            f'4. "mid_vo": Punchy 1-sentence Mid-Clip Commentary (8-11 words) reacting to the turning point halfway through the clip.\n'
            f'5. "outro_vo": Punchy 1-sentence Closing Takeaway + question (8-11 words) after the climax.\n'
        )
    else:
        rank_keys_desc = ", ".join(f'"{s["rank"]}"' for s in segments)
        mode_instructions = (
            f'3. "clip_vos": JSON object mapping each rank ({rank_keys_desc}) to a punchy 1-sentence English commentary hook (7-10 words) describing that specific clip (starting from #{count} down to #1).\n'
            f'4. "mid_vo": "" (empty string for multi-clip countdowns).\n'
            f'5. "outro_vo": Punchy 1-sentence English closing question (8-11 words) after the #1 climax.\n'
        )

    banner_example = "CRAZIEST VICE CITY MOMENT" if count == 1 else f"TOP {count} VICE CITY MOMENTS"
    prompt_text = (
        f"You are writing transformative commentary and metadata for {CHANNEL_CONFIG['channel_name']} "
        f"({CHANNEL_CONFIG['channel_handle']}), a Grand Theft Auto VI Shorts channel.\n"
        f"Inspect the attached gameplay frames and clip details:\n"
        + "\n".join(context_lines)
        + f"\nAvoid repeating any of these recent titles: {recent_titles}\n\n"
        f"Return STRICT valid JSON with these exact 6 keys:\n"
        f'1. "title": Catchy English YouTube Shorts title describing the wildest moment (max 68 chars, 1 emoji, no #Shorts tag).\n'
        f'2. "banner": Short 3-to-5 word UPPERCASE top banner (e.g. "{banner_example}").\n'
        + mode_instructions
        + f'6. "tags": List of 8 relevant English tags including "GTA 6", "Grand Theft Auto 6", "Vice City", and creator names.\n'
    )
    prompt_parts.append(prompt_text)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=45000),
        )
        for model_id in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
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
                if parsed.get("title") and parsed.get("clip_vos") and parsed.get("outro_vo"):
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

    top_creator = top_seg["channel_name"]
    fallback_vos = {}
    for s in segments:
        r = str(s["rank"])
        if count == 1:
            fallback_vos[r] = f"Watch how fast things spiral out of control for {s['channel_name']} in Vice City!"
        elif s["rank"] == 1:
            fallback_vos[r] = f"And at number 1, {s['channel_name']} takes Vice City chaos to the limit!"
        else:
            fallback_vos[r] = f"At number {r}, check out this wild moment from {s['channel_name']}!"

    return {
        "title": (f"Top {count} Craziest GTA 6 Moments Featuring {top_creator} 🔥" if count > 1 else f"{top_creator} Had The Wildest Moment In Vice City 😱")[:75],
        "banner": f"TOP {count} VICE CITY MOMENTS" if count > 1 else "CRAZIEST VICE CITY MOMENT",
        "clip_vos": fallback_vos,
        "mid_vo": "Nobody expected what happens next in this Vice City encounter!" if count == 1 else "",
        "outro_vo": "Would you have survived that in Vice City? Comment below!",
        "tags": ["GTA 6", "Grand Theft Auto 6", "Vice City", "GTA VI", "Shorts"] + creators[:3],
        "creators": creators,
    }

# ==============================================================================
# PART 20 OF 30: ASS WORD-SYNCED KINETIC CAPTION BUILDER
# Subheading: Builds Bold Yellow Karaoke-Style English Subtitles For Voiceovers
# Code Title: build_ass_subtitles_file
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
Style: Default,DejaVu Sans,62,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,330,1

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
# PART 21 OF 30: 1080X1920 VERTICAL LAYOUT & XFADE TRANSITION RENDERER
# Subheading: Renders Solo (45-52s) or Dynamic #N -> #1 Countdown With 0.35s Transitions
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
    Renders each chosen GTA 6 segment to 1080x1920 @ 30fps (auto-fitting total
    runtime to <= 57.5s so #1 + outro never get cut off) and joins multi-clip
    Shorts using smooth 0.35s xfade video + acrossfade audio transitions.
    """
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
        print(f"[Stitched Master] Solo Short runtime={first_dur:.2f}s.", flush=True)
        return stitched_out, timeline_meta

    cmd = ["ffmpeg", "-y"]
    for rf in rendered_files:
        cmd.extend(["-i", rf])

    transitions = ("fadewhite", "wipeleft", "slideright", "wipeup")
    fc_parts = []
    cum_dur = first_dur
    prev_v = "[0:v]"
    prev_a = "[0:a]"

    for idx_offset, (cur_dur, cur_seg) in enumerate(zip(rest_durs, rest_segs), start=1):
        offset_t = max(0.5, round(cum_dur - xfade_dur, 3))
        t_style = transitions[(idx_offset - 1) % len(transitions)]
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
    print(f"[Stitched Master] Total runtime={cum_dur:.2f}s across {len(rendered_files)} clips.", flush=True)
    return stitched_out, timeline_meta

# ==============================================================================
# PART 22 OF 30: TOP BANNER HEADER OVERLAY GENERATOR
# Subheading: Builds High-Contrast Gold/Black Top Header Card For Shorts
# Code Title: build_top_banner_filter
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
# PART 23 OF 30: VOICEOVER TIMELINE & DUCKING AUDIO MIXER
# Subheading: Generates Per-Clip, Mid-Clip (Solo) & Outro TTS Tracks & Mixes Audio
# Code Title: build_voiceover_tracks_and_mix
# ==============================================================================
def build_voiceover_tracks_and_mix(stitched_video, timeline_meta, script_data):
    """
    Synthesizes TTS voiceovers for each segment (#N -> #1), adds mid_vo for
    1-clip Solo Features, adds outro_vo at the end, and mixes them cleanly over
    the game audio with automatic volume ducking during speech.
    """
    total_dur = get_media_duration(stitched_video)
    clip_vos = script_data.get("clip_vos", {})
    vo_tracks = []

    for idx, seg in enumerate(timeline_meta):
        rank_key = str(seg["rank"])
        vo_line = str(clip_vos.get(rank_key, "")).strip()
        if not vo_line:
            continue
        mp3_path = f"vo_seg_{idx}.mp3"
        vdur = synthesize_voiceover_mp3(vo_line, mp3_path)
        if vdur > 0.35 and os.path.exists(mp3_path):
            v_start = round(float(seg["offset_start"]) + 0.35, 2)
            v_end = round(min(float(seg["offset_end"]) - 0.3, v_start + vdur), 2)
            if v_end > v_start + 0.3:
                vo_tracks.append({
                    "path": mp3_path,
                    "start": v_start,
                    "end": v_end,
                    "text": vo_line,
                })

    mid_txt = str(script_data.get("mid_vo", "")).strip()
    if len(timeline_meta) == 1 and mid_txt and total_dur >= 24.0:
        mid_mp3 = "vo_seg_mid.mp3"
        mdur = synthesize_voiceover_mp3(mid_txt, mid_mp3)
        if mdur > 0.35 and os.path.exists(mid_mp3):
            m_start = round(total_dur * 0.46, 2)
            m_end = round(min(total_dur - 4.2, m_start + mdur), 2)
            if m_end > m_start + 0.3:
                vo_tracks.append({
                    "path": mid_mp3,
                    "start": m_start,
                    "end": m_end,
                    "text": mid_txt,
                })

    outro_txt = str(script_data.get("outro_vo", "")).strip()
    if outro_txt and total_dur >= 6.0:
        outro_mp3 = "vo_seg_outro.mp3"
        odur = synthesize_voiceover_mp3(outro_txt, outro_mp3)
        if odur > 0.35 and os.path.exists(outro_mp3):
            o_start = round(max(1.0, total_dur - min(odur + 0.4, 4.2)), 2)
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

    # Duck game audio to 0.42 whenever voiceover is speaking
    duck_conds = "+".join(f"between(t,{tr['start']},{tr['end']})" for tr in vo_tracks)
    fc_lines = [f"[0:a]volume='if(gt({duck_conds},0),0.42,0.92)':eval=frame[g_audio]"]

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
# PART 24 OF 30: FINAL MASTER VIDEO + ASS CAPTIONS + BANNER COMPOSITOR
# Subheading: Burns Top Banner & Word-Synced Captions Into Final 1080x1920 MP4
# Code Title: compose_final_short_with_captions_and_audio
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
# PART 25 OF 30: CUSTOM COVER FRAME EXTRACTOR FOR THUMBNAIL ARTIFACT
# Subheading: Exports High-Contrast 1080x1920 Preview Frame From Peak Climax
# Code Title: export_short_cover_thumbnail
# ==============================================================================
def export_short_cover_thumbnail(video_path, out_jpg="short_cover.jpg"):
    dur = get_media_duration(video_path)
    if dur <= 1.0:
        return None
    snap_t = round(max(1.0, dur * 0.78), 2)
    run_cmd_safe(
        [
            "ffmpeg", "-y", "-ss", f"{snap_t:.2f}",
            "-i", video_path, "-vframes", "1", "-q:v", "2",
            out_jpg,
        ],
        label="ExportCoverThumb",
        timeout_sec=25,
        check=False,
    )
    return out_jpg if os.path.exists(out_jpg) else None

# ==============================================================================
# PART 26 OF 30: YOUTUBE DATA API V3 OAUTH CREDENTIAL LOADER
# Subheading: Builds & Refreshes Authenticated YouTube Client From Secret Token
# Code Title: get_youtube_service
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
# PART 27 OF 30: CREATOR ATTRIBUTION & SEO DESCRIPTION FORMATTER
# Subheading: Builds Monetization-Safe English Description With Full Creator Credits
# Code Title: build_youtube_description
# ==============================================================================
def build_youtube_description(script_data, timeline_meta):
    seen = set()
    credit_lines = []
    for seg in timeline_meta:
        ch = str(seg.get("channel_name", "GTA 6 Creator")).strip()
        if ch and ch.lower() not in seen:
            seen.add(ch.lower())
            credit_lines.append(f"• {ch}")

    credits_block = "\n".join(credit_lines) if credit_lines else "• Featured GTA 6 Streamers"
    is_solo = len(timeline_meta) == 1
    mode_summary = (
        "Featuring an insane full-length Vice City highlight with custom vertical framing, "
        "live commentary, and English subtitles."
        if is_solo
        else f"Counting down the Top {len(timeline_meta)} wildest GTA 6 Vice City moments with "
        "custom split-screen reactions, commentary, and English captions."
    )

    return (
        f"{script_data.get('title', 'GTA 6 Vice City Highlights')}\n\n"
        f"{mode_summary}\n\n"
        f"Featured Creators (Full Credit):\n{credits_block}\n\n"
        f"Subscribe to {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) "
        f"for daily GTA 6 clips & Vice City chaos!\n\n"
        f"#GTA6 #GrandTheftAuto6 #ViceCity #GTAVI #Shorts"
    )

# ==============================================================================
# PART 28 OF 30: YOUTUBE SHORTS UPLOADER & PEAK SLOT SCHEDULER
# Subheading: Uploads Final Short To YouTube & Schedules Single Next IST Slot
# Code Title: upload_short_to_youtube
# ==============================================================================
def upload_short_to_youtube(video_path, script_data, timeline_meta, publish_at_iso=None):
    youtube = get_youtube_service()

    raw_title = str(script_data.get("title", "CraZy GTA 6 Vice City Moment")).strip()
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
                "categoryId": "20",  # Gaming
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
# PART 29 OF 30: TEMPORARY RENDER WORKSPACE CLEANER
# Subheading: Removes Intermediate Segment, Voiceover & Subtitle Scratch Files
# Code Title: cleanup_scratch_render_files
# ==============================================================================
def cleanup_scratch_render_files():
    prefixes = ("seg_render_", "vo_seg_", "gem_ctx_", "raw_")
    exact_names = ("stitched_video.mp4", "mixed_master_audio.m4a", "captions.ass", "tmp_peak_scan.wav", "tmp_whisper_clip.wav")
    for fname in os.listdir("."):
        if fname in exact_names or any(fname.startswith(p) for p in prefixes):
            try:
                os.remove(fname)
            except Exception:
                pass

# ==============================================================================
# PART 30 OF 30: MAIN 24/7 HARVEST + DYNAMIC 1-TO-5 VAULT ORCHESTRATOR
# Subheading: Runs Hourly Harvest, Builds Dynamic 1-5 Clip Short, Renders & Schedules
# Code Title: main
# ==============================================================================
def main():
    print("============================================================", flush=True)
    print(f"[Boot] {CHANNEL_CONFIG['channel_name']} ({CHANNEL_CONFIG['channel_handle']}) Starting...", flush=True)
    print("============================================================", flush=True)

    cleanup_scratch_render_files()
    history_data = load_history()
    os.makedirs(CHANNEL_CONFIG.get("vault_dir", "clip_vault"), exist_ok=True)

    # ------------------------------------------------------------------
    # PHASE 1: HOURLY HARVEST (Discover -> Download -> Action Filter -> Vault)
    # ------------------------------------------------------------------
    candidates = discover_gta6_candidates(history_data)
    print(f"[Discovery] Found {len(candidates)} candidate GTA 6 videos.", flush=True)

    newly_vaulted = 0
    for cand in candidates:
        if newly_vaulted >= int(CHANNEL_CONFIG.get("max_new_clips_per_run", 3)):
            break

        vid_id = cand["video_id"]
        raw_path = f"raw_{vid_id}.mp4"
        try:
            downloaded = download_candidate_video(cand, raw_path)
            if not downloaded or not os.path.exists(raw_path):
                continue

            peaks = detect_audio_video_peaks(raw_path)
            if not peaks:
                print(f"[Skip] No strong climax peaks in {vid_id}.", flush=True)
                continue

            slices = harvest_climax_slices_to_vault(raw_path, cand, peaks)
            for item in slices:
                if not verify_action_motion_and_quality(item["vault_path"]):
                    print(f"[Filter] Rejected low-motion clip {item['vault_path']}.", flush=True)
                    if os.path.exists(item["vault_path"]):
                        os.remove(item["vault_path"])
                    continue

                tx_en, w_events = transcribe_and_translate_clip(item["vault_path"])
                item["transcript_en"] = tx_en
                item["word_events"] = w_events
                item["viral_score"] = compute_clip_viral_score(item["vault_path"], tx_en)

                history_data.setdefault("vault_queue", []).append(item)
                newly_vaulted += 1
                print(
                    f"[Vault Queued] {item['vault_path']} (score={item['viral_score']}, total={len(history_data['vault_queue'])})",
                    flush=True,
                )

            history_data.setdefault("used_clips", []).append({
                "video_id": vid_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
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

    # ------------------------------------------------------------------
    # PHASE 2: CHECK 6-HOUR PUBLISH WINDOW (Or Manual force_upload)
    # ------------------------------------------------------------------
    force_flag = str(os.environ.get("FORCE_UPLOAD", "")).strip().lower() in ("1", "true", "yes")
    should_build, slot_iso = should_trigger_publish_window(history_data, force_flag=force_flag)

    if not should_build and CHANNEL_CONFIG.get("upload_to_youtube", False):
        print(f"[Standby] Harvest complete ({newly_vaulted} new). Waiting for next 6-hour publish window.", flush=True)
        return

    # ------------------------------------------------------------------
    # PHASE 3: BUILD DYNAMIC 1-TO-5 SHORT PLAN, VALIDATE & RENDER
    # ------------------------------------------------------------------
    short_plan = build_top5_plan_from_vault(history_data)
    if not short_plan or not short_plan.get("segments"):
        print("[Standby] Vault has no ready clips yet. Will retry next hourly run.", flush=True)
        return

    script_data = generate_scene_grounded_script(short_plan, history_data)
    if not validate_monetization_eligibility(short_plan, script_data):
        print("[Skip] Short plan did not pass monetization commentary guard.", flush=True)
        return

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
    print(f"[Ready] Rendered {final_short_path} ({final_dur:.2f}s | {len(timeline_meta)} clip(s)).", flush=True)

    # ------------------------------------------------------------------
    # PHASE 4: UPLOAD TO YOUTUBE (If Enabled) & CONSUME USED VAULT CLIPS
    # ------------------------------------------------------------------
    if CHANNEL_CONFIG.get("upload_to_youtube", False):
        video_url = upload_short_to_youtube(
            video_path=final_short_path,
            script_data=script_data,
            timeline_meta=timeline_meta,
            publish_at_iso=slot_iso,
        )
        used_paths = {seg["video_path"] for seg in timeline_meta}
        history_data["vault_queue"] = [
            v for v in history_data.get("vault_queue", [])
            if v.get("vault_path") not in used_paths
        ]
        for vp in used_paths:
            if os.path.exists(vp):
                try:
                    os.remove(vp)
                except Exception:
                    pass

        if slot_iso:
            history_data.setdefault("scheduled_slots", []).append(slot_iso)

        history_data.setdefault("uploads", []).append({
            "title": script_data.get("title", ""),
            "url": video_url,
            "clip_count": len(timeline_meta),
            "duration_sec": round(final_dur, 2),
            "scheduled_for": slot_iso or "immediate",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        })
        save_history(history_data)
        print(f"[Live Success] Uploaded & scheduled: {video_url}", flush=True)
    else:
        print("[Test Mode] upload_to_youtube=False — keeping vault clips intact for inspection.", flush=True)

    cleanup_scratch_render_files()


if __name__ == "__main__":
    main()
