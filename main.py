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
MAX_STREAMS_TO_RECORD = 6       # Sweet spot: 6 simultaneous streams (avoids Twitch IP blocks)
CHUNK_SECONDS = 360             # 6 mins/hr in scout mode (216 mins scanned per 6-hr window!)

# --- CLIP & CHANNEL SETTINGS ---
CHANNEL_HANDLE = "@Vice-City_ClipsVault"
SINGLE_CLIP_DUR = 45
MULTI_CLIP_DUR = 19
TRANSITION_DUR = 0.4
HISTORY_FILE = "history.json"
SCHEDULE_HOURS_UTC = [0, 6, 12, 18]  # 5:30 AM, 11:30 AM, 5:30 PM, 11:30 PM IST

PRIORITY_CREATORS = [
    "xqc", "kaicenat", "timthetatman", "nickmercs", "tfue",
    "summit1g", "shroud", "zerkaa", "joshzerker", "lirik",
    "sykkuno", "valkyrae", "hasanabi", "jynxzi", "caseoh_"
]

# ==========================================
# PART 2: HISTORY & EXACT-HOUR SLOT SCHEDULER
# ==========================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("uploaded_ids", [])
                data.setdefault("scheduled_slots", [])
                return data
        except Exception as e:
            print(f"[Warn] Could not parse {HISTORY_FILE}, resetting: {e}", flush=True)
    return {"uploaded_ids": [], "scheduled_slots": []}


def save_history(hist):
    hist["uploaded_ids"] = hist.get("uploaded_ids", [])[-250:]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    hist["scheduled_slots"] = [
        s for s in hist.get("scheduled_slots", [])
        if str(s) >= cutoff
    ][-100:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)


def get_next_available_slot(hist):
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
            if candidate_dt <= now_utc + timedelta(minutes=15):
                continue
            slot_iso = candidate_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if slot_iso not in booked_slots:
                return slot_iso
    return None

# ==========================================
# PART 3: LIVE STREAM DISCOVERY & PARALLEL RECORDING
# ==========================================
TWITCH_GQL_URL = "https://twitch.tv"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


def fetch_twitch_directory_streams(game_name, limit=25):
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
    }
    payload = {
        "query": query,
        "variables": {"gameName": game_name, "limit": limit},
    }
    try:
        resp = requests.post(TWITCH_GQL_URL, json=payload, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        edges = (((data.get("data") or {}).get("game") or {}).get("streams") or {}).get("edges") or []
        results = []
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
        return results
    except Exception as e:
        print(f"[Warn] Twitch GQL error for {game_name}: {e}", flush=True)
        return []


def discover_top_live_streams(hist, max_count=MAX_STREAMS_TO_RECORD):
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
    chosen = [item[2] for item in filtered[:max_count]]
    print(f"[Discovery] Selected {len(chosen)} live streams: {[c['creator'] for c in chosen]}", flush=True)
    return chosen


def record_streams_parallel(streams, duration_sec=CHUNK_SECONDS):
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
        time.sleep(0.6)

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
            print(f"[Recorded] {st['creator']} -> {ts_path} ({os.path.getsize(ts_path) // (1024*1024)} MB)", flush=True)
        else:
            print(f"[Skip] Failed or empty recording for {st['creator']}", flush=True)
    return recorded

# ==========================================
# PART 4: CRASH-PROOF SCORING & HIGHLIGHT SELECTION
# ==========================================
def get_whisperm_model():
    return WhisperModel("base.en", device="cpu", compute_type="int8")


def analyze_batch_for_multiple_shorts(recorded_streams):
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
            print(f"[Skip] {channel_name}: audio extraction failed.", flush=True)
            continue

        try:
            rate, data = wavfile.read(wav_path)
            data = data.astype(np.float32) / 32768.0
        except Exception as e:
            print(f"[Skip] {channel_name}: wav read error {e}", flush=True)
            continue

        num_secs = len(data) // rate
        try:
            probe_out = subprocess.check_output(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", ts_path
                ],
                text=True
            ).strip()
            if probe_out and probe_out != "N/A":
                num_secs = min(num_secs, max(0, int(float(probe_out)) - 3))
        except Exception:
            pass

        if num_secs < 55:
            print(f"[Skip] {channel_name}: valid video too short ({num_secs}s).", flush=True)
            continue

        energies = np.array([np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2)) for i in range(num_secs)])
        median_e = float(np.median(energies)) + 1e-6
        best_sec, best_score = 25, 0.0

        for s in range(25, num_secs - 25):
            win = energies[s - 18 : s + 22]
            active_ratio = float(np.mean(win > (median_e * 0.55)))
            if active_ratio < 0.65:
                continue
            spike = float(np.mean(energies[s - 2 : s + 4])) / (float(np.mean(energies[s - 18 : s - 4])) + 1e-6)
            score = spike * active_ratio
            if score > best_score:
                best_score, best_sec = score, s

        best_jump = best_score
        if best_jump <= 0.0:
            print(f"[Skip] {channel_name}: dead-air / muted stream rejected.", flush=True)
            continue

        win_start = max(0, best_sec - 25)
        win_end = min(num_secs, best_sec + 22)
        sub_wav = ts_path.replace(".ts", "_win.wav")
        wavfile.write(sub_wav, rate, (data[win_start * rate : win_end * rate] * 32767).astype(np.int16))

        lines = []
        try:
            segments, _ = model.transcribe(sub_wav, beam_size=1)
            for seg in segments:
                txt = seg.text.strip()
                if txt:
                    lines.append(f"[{win_start + seg.start:.1f}s] {txt}")
        except Exception as e:
            print(f"[Warn] Whisper transcribe warning for {channel_name}: {e}", flush=True)

        spoken_words = sum(max(0, len(ln.split()) - 1) for ln in lines)
        if spoken_words < 12 and best_jump < 2.1:
            print(f"[Skip] {channel_name}: low speech ({spoken_words}w) and no stunt spike ({best_jump:.2f}x).", flush=True)
            continue

        combined_score = best_jump * (1.0 + min(spoken_words, 60) / 80.0)
        candidates.append({
            "stream_id": st["stream_id"],
            "creator": channel_name,
            "title": st.get("title", ""),
            "game": st.get("game", "Grand Theft Auto VI"),
            "ts_path": ts_path,
            "audio_peak": best_sec,
            "jump_ratio": round(best_jump, 3),
            "spoken_words": spoken_words,
            "combined_score": round(combined_score, 3),
            "transcript_snippet": " | ".join(lines[:14]),
        })
        print(
            f"[Candidate] {channel_name}: peak={best_sec}s, spike={best_jump:.2f}x, "
            f"words={spoken_words}, total={combined_score:.2f}",
            flush=True,
        )

    if not candidates:
        return []

    sorted_c = sorted(candidates, key=lambda x: x["combined_score"], reverse=True)
    if len(sorted_c) >= 3 and random.random() < 0.55:
        top_three = sorted_c[:3]
        multi_specs = [
            (c_item, max(0, c_item["audio_peak"] - 9), c_item["audio_peak"], MULTI_CLIP_DUR)
            for c_item in top_three
        ]
        return [("MULTI", "blur", multi_specs)]

    best_c = next(iter(sorted_c))
    return [("SINGLE", "blur", [(best_c, max(0, best_c["audio_peak"] - 22), best_c["audio_peak"], SINGLE_CLIP_DUR)])]

# ==========================================
# PART 5: VIRAL ENGLISH TITLE & HOOK GENERATOR
# ==========================================
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
    cleaned = re.sub(r"[^\x00-\x7F\U0001F300-\U0001FAFF\u2600-\u27BF]+", "", str(raw_text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) < 12:
        return fallback
    if "#shorts" not in cleaned.lower():
        cleaned = f"{cleaned[:78].strip()} #gta6 #shorts"
    return cleaned[:95]


def generate_viral_metadata(mode, creators_list, transcript_text, game_name="Grand Theft Auto VI"):
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
        f"Vice City Chaos Caught Live on Twitch {e1}{e2} #gta6 #shorts",
        f"GTA 6 Streamers Losing Their Minds {e1}{e2} #gta6 #shorts",
        f"Peak Leonida Madness in 55 Seconds {e1}{e2} #gta6 #shorts",
        f"Best Live GTA 6 Moments You Missed {e1}{e2} #gta6 #shorts",
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
    creators_str = ", ".join(creators_list[:3]) if creators_list else "top Twitch streamers"

    prompt = (
        f"You write high-CTR YouTube Shorts titles in ENGLISH ONLY for '{CHANNEL_HANDLE}'.\n"
        f"Mode: {mode} | Game: {game_name} | Streamer(s): {creators_str}\n"
        f"Angle: {chosen_angle}\n"
        f"Transcript context: {str(transcript_text)[:500]}\n"
        f"Rules:\n"
        f"- Output strictly 2 lines:\n"
        f"  TITLE: <catchy English title under 75 chars + 2 emojis {e1}{e2} + #gta6 #shorts>\n"
        f"  HOOK: <ALL CAPS 3-5 word on-screen banner hook>\n"
        f"- Include the streamer name '{lead_creator}' if SINGLE mode.\n"
        f"- NO profanity, NO swear words, NO generic 'Nobody Expected This' cliches.\n"
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

# ==========================================
# PART 6: ANIMATED .ASS SUBTITLES GENERATOR
# ==========================================
PROFANITY_PATTERN = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|asshole\w*|motherfuck\w*|nigga\w*|nigger\w*|cunt\w*|dick\w*|pussy\w*)\b",
    re.IGNORECASE,
)


def censor_caption_text(text):
    return PROFANITY_PATTERN.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)


def format_ass_time(sec):
    sec = max(0.0, float(sec))
    hrs = int(sec // 3600)
    mins = int((sec % 3600) // 60)
    secs = sec % 60
    return f"{hrs}:{mins:02d}:{secs:05.2f}"


def generate_animated_ass_captions(video_path, ass_path="captions.ass"):
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
            segments, _ = model.transcribe(wav_path, beam_size=1, word_timestamps=True)
            words = []
            for seg in segments:
                if getattr(seg, "words", None):
                    for w in seg.words:
                        clean_w = censor_caption_text(w.word.strip())
                        if clean_w:
                            words.append((float(w.start), float(w.end), clean_w.upper()))
                else:
                    clean_s = censor_caption_text(seg.text.strip())
                    if clean_s:
                        words.append((float(seg.start), float(seg.end), clean_s.upper()))

            chunk_size = 3
            for i in range(0, len(words), chunk_size):
                chunk = words[i : i + chunk_size]
                if not chunk:
                    continue
                for active_idx, (w_start, w_end, _) in enumerate(chunk):
                    next_start = chunk[active_idx + 1][0] if active_idx + 1 < len(chunk) else max(w_end + 0.12, w_start + 0.25)
                    if next_start <= w_start:
                        next_start = w_start + 0.22
                    styled_parts = []
                    for j, (_, _, w_txt) in enumerate(chunk):
                        safe_w = w_txt.replace("{", "").replace("}", "").replace("\\", "")
                        if j == active_idx:
                            styled_parts.append(r"{\c&H0000D7FF&\fscx114\fscy114}" + safe_w + r"{\c&H00FFFFFF&\fscx100\fscy100}")
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

# ==========================================
# PART 7: PRO VIDEO EDITOR & RENDERER
# ==========================================
XFADE_TRANSITIONS = ["fade", "wipeleft", "wiperight", "slideleft", "slideright", "smoothleft", "circleopen"]


def sanitize_ffmpeg_text(text):
    clean = re.sub(r"[^\x20-\x7E]", "", str(text or ""))
    clean = clean.replace("\\", "").replace("'", "").replace('"', "").replace(":", "\\:").replace("%", "\\%")
    return clean.strip()


def build_and_render_short(mode, framing_mode, clip_specs, out_video_name="final_short.mp4", out_thumb_name="final_thumb.jpg"):
    seg_files = []
    credit_windows = []
    current_timeline_sec = 0.0

    for idx, (cand_info, start_s, peak_s, dur_s) in enumerate(clip_specs):
        seg_path = f"seg_{idx}.mp4"
        src_video = cand_info.get("video_path") or cand_info["ts_path"]
        actual_start = 0.0 if cand_info.get("video_path") else float(start_s)

        res = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err",
            "-ss", f"{actual_start:.2f}", "-i", src_video, "-t", f"{float(dur_s):.2f}",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2", seg_path
        ])
        if res.returncode != 0 or not os.path.exists(seg_path) or os.path.getsize(seg_path) < 50000:
            print(f"[Warn] Segment {idx} skipped due to short/corrupt source.", flush=True)
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
    if len(seg_files) == 1:
        shutil.copy(seg_files, merged_raw)
    else:
        inputs = []
        for sf in seg_files:
            inputs.extend(["-i", sf])
        v_chain = ""
        a_chain = ""
        prev_v = "[0:v]"
        prev_a = "[0:a]"
        offset_acc = 0.0
        for i in range(1, len(seg_files)):
            dur_prev = float(clip_specs[i - 1])
            offset_acc += dur_prev - TRANSITION_DUR
            trans = random.choice(XFADE_TRANSITIONS)
            out_v = f"[v{i}]"
            out_a = f"[a{i}]"
            v_chain += f"{prev_v}[{i}:v]xfade=transition={trans}:duration={TRANSITION_DUR}:offset={offset_acc:.2f}{out_v};"
            a_chain += f"{prev_a}[{i}:a]acrossfade=d={TRANSITION_DUR}:c1=tri:c2=tri{out_a};"
            prev_v = out_v
            prev_a = out_a

        filter_complex = f"{v_chain}{a_chain}".rstrip(";")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
                "-filter_complex", filter_complex,
                "-map", prev_v, "-map", prev_a,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", merged_raw
            ],
            check=True,
        )

    # Crash-proof 2.5s opening hook teaser from climax
    first_cand, first_start, first_peak, first_dur = clip_specs
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
        shutil.copy(seg_files, "hook_clip.mp4")

    combined_pre_fx = "combined_pre_fx.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", "hook_clip.mp4", "-i", merged_raw,
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", combined_pre_fx
    ], check=True)

    # Shift credit windows by +2.3s for the opening hook teaser
    hook_offset = 2.3
    shifted_windows = []
    for idx_w, (w_s, w_e, c_nm) in enumerate(credit_windows):
        start_b = 0.0 if idx_w == 0 else (w_s + hook_offset)
        end_b = (w_e + hook_offset) if idx_w < len(credit_windows) - 1 else 999.0
        shifted_windows.append((start_b, end_b, c_nm))

    ass_file = generate_animated_ass_captions(combined_pre_fx, "captions.ass")
    safe_handle = sanitize_ffmpeg_text(CHANNEL_HANDLE)

    credit_filters = []
    for w_start, w_end, c_name in shifted_windows:
        safe_c = sanitize_ffmpeg_text(c_name)
        credit_filters.append(
            f"drawtext=text='Credit\\: {safe_c}':fontcolor=white@0.92:fontsize=26:"
            f"x=(w-text_w)/2:y=220:enable='gte(t,{w_start:.2f})*lt(t,{w_end:.2f})'"
        )
    credits_chain = ",".join(credit_filters)

    # Vertical 1080x1920 composite: blurred BG + color-graded slow-zoom gameplay + neon Vice City borders + ASS captions
    vf_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=24:4,eq=brightness=-0.10:saturation=1.25[bg];"
        "[0:v]eq=contrast=1.08:saturation=1.18:brightness=0.01,"
        "scale='trunc(1080*(1+0.0012*t)/2)*2':'trunc(608*(1+0.0012*t)/2)*2',"
        "crop=1080:608[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[comp1];"
        "[comp1]drawbox=x=0:y=650:w=1080:h=6:color=0x00F5FF@0.95:t=fill,"
        "drawbox=x=0:y=1264:w=1080:h=6:color=0xFF2A85@0.95:t=fill,"
        f"drawtext=text='{safe_handle}':fontcolor=white@0.96:fontsize=30:x=(w-text_w)/2:y=175,"
        f"{credits_chain},"
        f"ass={ass_file}[vout]"
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

    # Extract high-contrast thumbnail frame around 4.5s
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", "4.5", "-i", out_video_name,
        "-vframes", "1", "-q:v", "2", out_thumb_name
    ])
    print(f"[Render] Built {out_video_name} ({os.path.getsize(out_video_name) // (1024*1024)} MB)", flush=True)
    return out_video_name, out_thumb_name

# ==========================================
# PART 8: HOURLY CANDIDATE POOL MANAGER
# ==========================================
def save_hourly_candidate(mode, framing_mode, clip_specs, out_dir="candidate_out"):
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
    winner, *_ = sorted_pool
    print(
        f"[Pool] Picked #1 winner from {len(pool)} hourly candidates "
        f"(mode={winner.get('mode')}, score={winner.get('score')})",
        flush=True,
    )
    return winner

# ==========================================
# PART 9: YOUTUBE OAUTH UPLOADER & SCHEDULER
# ==========================================
def get_youtube_client():
    token_raw = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not token_raw:
        raise RuntimeError("YOUTUBE_TOKEN_JSON environment variable is missing.")
    creds_info = json.loads(token_raw)
    creds = Credentials.from_authorized_user_info(creds_info)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_path, thumb_path, title, creators_list, publish_at_iso=None):
    youtube = get_youtube_client()
    unique_creators = list(dict.fromkeys([c for c in creators_list if c]))
    credits_str = ", ".join(unique_creators) if unique_creators else "Twitch Streamers"

    description = (
        f"{title}\n\n"
        f"Featured Streamer(s): {credits_str}\n"
        f"Subscribe to {CHANNEL_HANDLE} for daily GTA 6 & Vice City live highlights!\n\n"
        f"#gta6 #grandtheftauto6 #vicecity #gta6clips #twitchclips #gaming #shorts"
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
                "GTA 6 Twitch", "GTA 6 Funny Moments", "Shorts", "Gaming Shorts"
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
            print(f"[Warn] Thumbnail set skipped (requires verified channel): {e}", flush=True)

    return video_id

# ==========================================
# PART 10: MAIN ORCHESTRATOR (SCOUT VS PUBLISH)
# ==========================================
def run_scout_mode(hist):
    streams = discover_top_live_streams(hist, max_count=MAX_STREAMS_TO_RECORD)
    if not streams:
        print("[Scout] No live streams discovered.", flush=True)
        return

    recorded = record_streams_parallel(streams, duration_sec=CHUNK_SECONDS)
    if not recorded:
        print("[Scout] No streams recorded successfully.", flush=True)
        return

    short_plans = analyze_batch_for_multiple_shorts(recorded)
    if not short_plans:
        print("[Scout] No viral highlights passed quality filters this hour.", flush=True)
        return

    top_plan = next(iter(short_plans))
    mode, framing_mode, clip_specs = top_plan
    save_hourly_candidate(mode, framing_mode, clip_specs, out_dir="candidate_out")


def run_publish_mode(hist):
    slot_iso = None
    if UPLOAD_TO_YOUTUBE and YOUTUBE_PRIVACY == "public":
        slot_iso = get_next_available_slot(hist)
        if not slot_iso:
            print("[Publish] All upcoming 24h schedule slots are already full. Skipping.", flush=True)
            return

    winner = fetch_and_pick_best_candidate(work_dir="pool_in")
    mode = "SINGLE"
    framing_mode = "blur"
    clip_specs = []
    creators_list = []
    transcript_pieces = []
    game_name = "Grand Theft Auto VI"
    used_stream_ids = []

    if winner:
        mode = winner.get("mode", "SINGLE")
        framing_mode = winner.get("framing_mode", "blur")
        for c_entry in winner.get("clips", []):
            dur_s = float(c_entry.get("duration", SINGLE_CLIP_DUR))
            clip_specs.append((c_entry, 0.0, max(3.0, dur_s - 4.0), dur_s))
            creators_list.append(c_entry.get("creator", "Streamer"))
            transcript_pieces.append(c_entry.get("transcript_snippet", ""))
            game_name = c_entry.get("game", game_name)
            if c_entry.get("stream_id"):
                used_stream_ids.append(c_entry.get("stream_id"))
    else:
        print("[Publish] Candidate pool empty; running live fallback scan now...", flush=True)
        streams = discover_top_live_streams(hist, max_count=MAX_STREAMS_TO_RECORD)
        recorded = record_streams_parallel(streams, duration_sec=CHUNK_SECONDS)
        short_plans = analyze_batch_for_multiple_shorts(recorded)
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
    viral_title, _ = generate_viral_metadata(mode, creators_list, transcript_text, game_name=game_name)
    out_mp4, out_jpg = build_and_render_short(
        mode,
        framing_mode,
        clip_specs,
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
        print(f"[DryRun] Rendered {out_mp4} with title: {viral_title}", flush=True)


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
