import os
import json
import time
import random
import asyncio
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
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

CHANNEL_HANDLE = "@Vice-City_ClipsVault"

# --- EASY CONTROL TOGGLES ---
UPLOAD_TO_YOUTUBE = True        # True = upload/schedule to YouTube | False = skip YouTube & save local artifact
YOUTUBE_PRIVACY = "public"      # "public" (uses exact scheduled slots) or "private" (immediate private test)
MAX_UPLOADS_PER_DAY = 4         # Strict cap: maximum 4 Shorts scheduled/uploaded per day
MAX_STREAMS_TO_RECORD = 6       # Sweet spot: 6 simultaneous streams (avoids Twitch IP rate limits)
CHUNK_SECONDS = 720             # 12 minutes (720s) per stream (72 total mins scanned per batch)
# ----------------------------

SINGLE_CLIP_DUR = 45
MULTI_CLIP_DUR = 19
HOOK_DURATION = 2.5
TRANSITION_DUR = 0.4
ENABLE_AI_VOICEOVER = True

HISTORY_FILE = "history.json"
MAX_CLIPS_PER_STREAM = 2        # Up to 2 clips per multi-hour stream (at least 2 hours apart)
MIN_GAP_SECONDS = 7200          # 2-hour cooldown before revisiting a long stream

CLIP_FRIENDLY_CREATORS = [
    "kaicenat", "xqc", "shroud", "sykkuno", "valkyrae",
    "zerkaa", "summit1g", "lirik", "timthetatman", "nickmercs",
    "caseoh_", "jynxzi", "agent00", "fanum", "duke"
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
        first_char = clean[:1]
        masked = first_char + "*" * (len(clean) - 1)
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

def can_clip_stream(stream_url, history_data):
    streams_map = history_data.get("streams", {})
    entry = streams_map.get(stream_url)
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
    remaining_daily_quota = max(0, MAX_UPLOADS_PER_DAY - len(booked_next_24h))

    if remaining_daily_quota == 0:
        print(f"Daily cap reached ({MAX_UPLOADS_PER_DAY}/{MAX_UPLOADS_PER_DAY} slots booked in next 24h).", flush=True)
        return []

    base_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate_slots = []
    for day_offset in range(0, 3):
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
        url = cand["live_url"]
        entry = streams_map.get(url, {
            "title": cand["stream_title"],
            "channel": cand["channel_name"],
            "count": 0,
            "clips": []
        })
        entry["count"] += 1
        entry["last_clipped_at"] = int(time.time())
        entry["clips"].append({
            "scheduled_utc": slot_iso or "immediate",
            "chunk_peak_sec": int(peak_sec)
        })
        streams_map[url] = entry

    if slot_iso and slot_iso not in history_data["scheduled_slots"]:
        history_data["scheduled_slots"].append(slot_iso)
    save_history(history_data)

def find_top_live_streams(max_streams=MAX_STREAMS_TO_RECORD):
    history_data = load_history()
    now_utc = datetime.now(timezone.utc)
    if now_utc < datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc):
        print("Waiting for GTA 6 official launch. Exiting cleanly.", flush=True)
        return []

    game_name = "Grand Theft Auto VI"
    keywords = ["gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6", "vice city", "leonida"]
    query = """
    query($name: String!) {
      game(name: $name) {
        streams(first: 60) {
          edges {
            node {
              title
              broadcaster {
                login
                displayName
                broadcastSettings {
                  language
                }
              }
            }
          }
        }
      }
    }
    """
    gql_payload = json.dumps({"query": query, "variables": {"name": game_name}}).encode("utf-8")
    gql_url = urllib.parse.urlunparse(("https", "gql.twitch.tv", "/gql", "", "", ""))
    req = urllib.request.Request(
        gql_url,
        data=gql_payload,
        headers={"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko", "Content-Type": "application/json"}
    )

    try:
        raw_resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        resp_data = json.loads(raw_resp)
        edges = resp_data.get("data", {}).get("game", {}).get("streams", {}).get("edges", [])
        whitelisted, others = [], []

        for edge in edges:
            node = edge["node"]
            broadcaster = node["broadcaster"]
            login = broadcaster["login"].lower()
            channel_name = broadcaster["displayName"]
            title = node.get("title", "")

            lang = (broadcaster.get("broadcastSettings") or {}).get("language", "en")
            if (lang and lang.lower() != "en") or not title.isascii():
                continue
            t_low = title.lower()
            if any(bad in t_low for bad in ["rerun", "rebroadcast", "24/7", "fake"]):
                continue
            if not any(kw in t_low for kw in keywords):
                continue

            live_url = urllib.parse.urlunparse(("https", "www.twitch.tv", "/" + login, "", "", ""))
            if can_clip_stream(live_url, history_data):
                item = (live_url, title, channel_name)
                if login in CLIP_FRIENDLY_CREATORS:
                    whitelisted.append(item)
                else:
                    others.append(item)

        selected = (whitelisted + others)[:max_streams]
        print(f"[6-Stream Scout] Found {len(selected)} eligible GTA 6 streams to record simultaneously!", flush=True)
        return selected
    except Exception as e:
        print(f"Twitch search error: {e}", flush=True)
        return []

def record_single_stream(idx_and_stream):
    idx, (live_url, title, channel_name) = idx_and_stream
    out_file = f"raw_{idx}.ts"
    print(f"[Stream {idx+1}] Recording {CHUNK_SECONDS}s (12 mins) from {channel_name}...", flush=True)
    cmd = [
        "yt-dlp", "-f", "best[height<=720]",
        "--downloader", "ffmpeg", "--downloader-args", f"ffmpeg_i:-t {CHUNK_SECONDS}",
        "--fixup", "never", "-o", out_file, "--no-part", "--no-warnings", live_url
    ]
    try:
        subprocess.run(cmd, check=True, timeout=CHUNK_SECONDS + 240)
        if os.path.exists(out_file) and os.path.getsize(out_file) > 1_000_000:
            return (idx, out_file, live_url, title, channel_name)
    except Exception as e:
        print(f"[Stream {idx+1}] Skipped ({channel_name}): {e}", flush=True)
    return None

def analyze_batch_for_multiple_shorts(recorded_list, max_shorts=3):
    candidates = []
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")

    for idx, video_path, live_url, stream_title, channel_name in recorded_list:
        wav_path = f"audio_{idx}.wav"
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", wav_path], check=True)
        rate, data = wavfile.read(wav_path)
        data = np.abs(data.astype(np.float32))
        num_secs = len(data) // rate
        if num_secs < 90:
            continue

        energies = np.array([np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2)) for i in range(num_secs)])
        jump_scores = np.zeros(num_secs)
        for s in range(25, num_secs - 22):
            jump_scores[s] = np.mean(energies[s - 1 : s + 3]) / (np.mean(energies[s - 14 : s - 2]) + 1e-6)

        best_sec = int(np.argmax(jump_scores))
        best_jump = float(jump_scores[best_sec])

        segments, _ = model.transcribe(wav_path)
        lines = [f"[{int(seg.start)}s] {seg.text.strip()}" for seg in segments if 20 <= seg.start <= (num_secs - 22) and seg.text.strip()]
        candidates.append({
            "idx": idx, "video_path": video_path, "live_url": live_url,
            "stream_title": stream_title, "channel_name": channel_name,
            "num_secs": num_secs, "audio_peak": best_sec, "jump_ratio": best_jump,
            "transcript": "\n".join(lines[:180])
        })

    if not candidates:
        return []

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=45000))
        blocks = []
        for c in candidates:
            blocks.append(
                f"=== STREAM {c['idx']} ({c['channel_name']} | Spike: {c['jump_ratio']:.2f}x at {c['audio_peak']}s | MaxSec: {c['num_secs']-22}) ===\n"
                f"Title: {c['stream_title']}\nTranscript:\n{c['transcript']}\n"
            )

        prompt = f"""You are the head producer for a viral GTA 6 YouTube Shorts channel.
We recorded {len(candidates)} live streams (12 minutes each). Find up to {max_shorts} SEPARATE high-quality Shorts so zero viral moments are wasted!
For each Short you design:
- Use MODE SINGLE if one stream has a standout 40-second funny moment, police chase, stunt, or plot twist.
- Use MODE MULTI if 3 quick 16-second moments from different streams work best together as a compilation.
- NEVER reuse the same stream_idx across different Shorts.

{''.join(blocks)}

Reply with up to {max_shorts} Shorts using this exact format ONLY:
SHORT1: <SINGLE or MULTI> | <crop or blur> | <idx1>,<peak1> ; <idx2>,<peak2> ; <idx3>,<peak3>
SHORT2: <SINGLE or MULTI> | <crop or blur> | <idx1>,<peak1>
SHORT3: <SINGLE or MULTI> | <crop or blur> | <idx1>,<peak1>"""

        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.75),
            )
            planned_shorts = []
            used_stream_ids = set()

            for line in resp.text.strip().splitlines():
                up = line.upper()
                if not up.startswith("SHORT") or ":" not in line:
                    continue
                body = line.split(":", 1)[-1].strip()
                parts = [p.strip() for p in body.split("|")]
                if len(parts) < 3:
                    continue

                m_raw, f_raw, picks_raw = parts[0].upper(), parts[1].lower(), parts[-1]
                mode = "MULTI" if "MULTI" in m_raw else "SINGLE"
                framing = "crop" if "crop" in f_raw else "blur"

                parsed_specs = []
                for pair in picks_raw.split(";"):
                    if "," not in pair:
                        continue
                    left_s, _, right_s = pair.partition(",")
                    s_dig = "".join(ch for ch in left_s if ch.isdigit())
                    p_dig = "".join(ch for ch in right_s if ch.isdigit())
                    if s_dig and p_dig:
                        s_i, p_s = int(s_dig), int(p_dig)
                        if s_i in used_stream_ids:
                            continue
                        for c in candidates:
                            if c["idx"] == s_i and 22 <= p_s <= (c["num_secs"] - 22):
                                dur = SINGLE_CLIP_DUR if mode == "SINGLE" else MULTI_CLIP_DUR
                                pre = 22 if mode == "SINGLE" else 10
                                parsed_specs.append((c, max(0, p_s - pre), p_s, dur))
                                used_stream_ids.add(s_i)
                                break

                if mode == "SINGLE" and parsed_specs:
                    planned_shorts.append(("SINGLE", framing, [next(iter(parsed_specs))]))
                elif mode == "MULTI" and len(parsed_specs) >= 2:
                    planned_shorts.append(("MULTI", framing, parsed_specs[:3]))

            if planned_shorts:
                print(f"[Batch AI Producer] Built {len(planned_shorts)} distinct Shorts from this 6-stream batch!", flush=True)
                return planned_shorts[:max_shorts]
        except Exception as e:
            print(f"Batch AI producer fallback: {e}", flush=True)

    sorted_c = sorted(candidates, key=lambda x: x["jump_ratio"], reverse=True)
    best_c = next(iter(sorted_c))
    return [("SINGLE", "blur", [(best_c, max(0, best_c["audio_peak"] - 22), best_c["audio_peak"], SINGLE_CLIP_DUR)])]

def transcribe_segment(clip_path, time_offset=0.0):
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

    srt_entries = []
    full_text = []
    for i in range(0, len(words), 2):
        chunk = words[i : i + 2]
        first_w = next(iter(chunk))
        last_w = chunk[-1]
        s_t, _, _ = first_w
        _, e_t, _ = last_w
        txt = " ".join(w_str for _, _, w_str in chunk)
        full_text.append(txt)
        srt_entries.append((s_t, e_t, txt))

    return srt_entries, " ".join(full_text), bad_intervals

def write_srt_file(all_entries, out_path="captions.srt"):
    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for idx, (s_t, e_t, txt) in enumerate(all_entries, start=1):
        lines.append(f"{idx}\n{fmt_time(s_t)} --> {fmt_time(e_t)}\n{txt}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path

def generate_viral_metadata(transcript, mode, creators_list):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    emoji_pool = ["💀", "🚨", "🤯", "🔥", "😭", "⚡", "👀", "💥", "🌴", "🚓"]
    e1, e2 = random.sample(emoji_pool, 2)

    styles = [
        "Shocking plot twist curiosity hook",
        "Pure Vice City comedy / NPC chaos",
        "1-in-a-million stunt or lucky escape",
        "Instant karma in 5 seconds",
        "Unbelievable police chase ending"
    ]
    chosen_style = random.choice(styles)
    fallback_title = (
        f"GTA 6 Daily Chaos Compilation {e1}{e2} #gta6 #shorts"
        if mode == "MULTI"
        else f"Nobody Expected This in GTA 6 {e1}{e2} #gta6 #shorts"
    )

    if not api_key:
        return fallback_title, "WAIT FOR THE END!", "Watch what happens right here!"

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30000))
    prompt = f"""You are a top viral gaming YouTube Shorts editor for an English GTA 6 channel.
Clip Mode: {"3-Clip Highlight Compilation" if mode == "MULTI" else "Single Viral Highlight"}
Creators Featured: {", ".join(creators_list)}
Spoken Dialogue: {transcript[:450]}
Creative Angle: {chosen_style}

Rules:
1. TITLE: Write a WILD, PUNCHY, completely UNIQUE English title (under 66 characters, NO profanity). Never repeat "GTA 6: Gta 6" or use generic "Gone Wrong" clichés. Include 1-2 emojis ({e1}, {e2}) and "#gta6 #shorts".
2. BANNER: Write a 3-to-4 word ALL-CAPS on-screen hook headline.
3. HOOK: Write a 5-to-6 word spoken intro voiceover line.

Reply in this exact 3-line format ONLY:
TITLE: <your unique title>
BANNER: <3-4 word ALL CAPS hook>
HOOK: <spoken intro line>"""

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=1.1),
        )
        title = fallback_title
        banner = "WAIT FOR THE END!"
        hook = "Wait till you see this!"
        for line in resp.text.strip().splitlines():
            up = line.upper()
            val = line.split(":", 1)[-1].strip()
            if up.startswith("TITLE:") and val:
                title = val
            elif up.startswith("BANNER:") and val:
                banner = "".join(c for c in val.upper() if c.isalnum() or c in {" ", "!", "?"})[:26]
            elif up.startswith("HOOK:") and val:
                hook = val
        print(f"Generated Title: {title} | Banner: {banner}", flush=True)
        return title, banner, hook
    except Exception as e:
        print(f"Gemini metadata error: {e}", flush=True)
        return fallback_title, "WAIT FOR THE END!", "Wait till you see this!"

async def create_voiceover_audio(text, filename="hook_vo.mp3"):
    for voice in ["en-US-GuyNeural", "en-US-ChristopherNeural"]:
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

def build_and_render_short(mode, framing_mode, clip_specs, out_video_name="final_short.mp4"):
    segment_files = []
    segment_durations = []
    all_srt_entries = []
    all_bad_intervals = []
    all_transcripts = []
    creators_used = []
    climax_markers = []
    credit_windows = []

    current_timeline_sec = (HOOK_DURATION - TRANSITION_DUR) if mode == "SINGLE" else 0.0

    for idx, (cand, start_sec, peak_sec, seg_dur) in enumerate(clip_specs):
        seg_mp4 = f"seg_{idx}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_sec), "-i", cand["video_path"],
            "-t", str(seg_dur), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", seg_mp4
        ], check=True)

        srt_entries, seg_txt, bad_ints = transcribe_segment(seg_mp4, time_offset=current_timeline_sec)
        all_srt_entries.extend(srt_entries)
        all_bad_intervals.extend(bad_ints)
        all_transcripts.append(seg_txt)

        c_name = cand["channel_name"]
        if c_name not in creators_used:
            creators_used.append(c_name)

        seg_start_t = 0.0 if idx == 0 else current_timeline_sec
        seg_end_t = current_timeline_sec + seg_dur - (TRANSITION_DUR if idx < len(clip_specs) - 1 else 0.0)
        credit_windows.append((seg_start_t, seg_end_t, c_name))

        rel_peak = current_timeline_sec + max(1.0, min(seg_dur - 1.5, peak_sec - start_sec))
        climax_markers.append(rel_peak)
        segment_files.append(seg_mp4)
        segment_durations.append(float(seg_dur))

        current_timeline_sec += (seg_dur - TRANSITION_DUR)

    srt_path = write_srt_file(all_srt_entries, "captions.srt")
    viral_title, banner_hook, voice_hook = generate_viral_metadata(" ".join(all_transcripts), mode, creators_used)

    if mode == "SINGLE":
        cand0, _, peak0, _ = next(iter(clip_specs))
        hook_start = max(0, peak0 - 3.0)
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(hook_start), "-i", cand0["video_path"],
            "-t", str(HOOK_DURATION), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "hook_clip.mp4"
        ], check=True)

        if ENABLE_AI_VOICEOVER and voice_hook:
            asyncio.run(create_voiceover_audio(voice_hook, "hook_vo.mp3"))
            subprocess.run([
                "ffmpeg", "-y", "-i", "hook_clip.mp4", "-i", "hook_vo.mp3",
                "-filter_complex", "[0:a]volume=0.25[bg_a];[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.35[vo_a];[bg_a][vo_a]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "hook_voiced.mp4"
            ], check=True)
            os.replace("hook_voiced.mp4", "hook_clip.mp4")

        concat_list = ["hook_clip.mp4"] + segment_files
        concat_durs = [float(HOOK_DURATION)] + segment_durations
    else:
        concat_list = segment_files
        concat_durs = segment_durations

    if len(concat_list) == 1:
        os.replace(next(iter(concat_list)), "combined.mp4")
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
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", "-ac", "2", "combined.mp4"
        ], check=True)
        total_dur = sum(concat_durs) - TRANSITION_DUR * (len(concat_list) - 1)

    primary_climax = next(iter(climax_markers)) if climax_markers else 20.0

    inputs = ["-i", "combined.mp4"]
    has_custom_bg = os.path.exists("assets/bg_neon.png")
    has_boom_sfx = os.path.exists("assets/boom.mp3")

    climax_cond = "+".join([f"between(in_time,{cm-0.2:.2f},{cm+0.55:.2f})" for cm in climax_markers])
    shake_x = "+".join([f"if(between(in_time,{cm-0.2:.2f},{cm+0.55:.2f}),18*sin(in_time*80),0)" for cm in climax_markers])
    shake_y = "+".join([f"if(between(in_time,{cm-0.2:.2f},{cm+0.55:.2f}),18*cos(in_time*80),0)" for cm in climax_markers])
    flash_cond = "+".join([f"between(t,{cm-0.08:.2f},{cm+0.12:.2f})" for cm in climax_markers])
    zoom_expr = f"if({climax_cond},1.18,1.0+0.09*(mod(in_time,16)/16))"

    if framing_mode == "crop":
        video_base_filter = (
            f"[0:v]fps=30,scale=-2:1920,crop=1080:1920:(iw-1080)/2:0,setsar=1,"
            f"eq=contrast=1.16:saturation=1.28:brightness='if({flash_cond},0.18,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':"
            f"x='iw/2-(iw/zoom/2)+{shake_x}':"
            f"y='ih/2-(ih/zoom/2)+{shake_y}':"
            f"d=1:s=1080x1920:fps=30[base_v];"
        )
        next_idx = 1
    else:
        if has_custom_bg:
            inputs.extend(["-i", "assets/bg_neon.png"])
            bg_filter = "[1:v]scale=1080:1920[bg];"
            next_idx = 2
        else:
            bg_filter = "[0:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=32:8,eq=brightness=-0.12:saturation=1.25[bg];"
            next_idx = 1

        fg_filter = (
            f"[0:v]fps=30,scale=1080:608,setsar=1,"
            f"eq=contrast=1.16:saturation=1.28:brightness='if({flash_cond},0.18,0)':eval=frame,"
            f"zoompan=z='{zoom_expr}':"
            f"x='iw/2-(iw/zoom/2)+{shake_x}':"
            f"y='ih/2-(ih/zoom/2)+{shake_y}':"
            f"d=1:s=1080x608:fps=30[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base_v];"
        )
        video_base_filter = bg_filter + fg_filter

    credit_filters = []
    for win_idx, (w_start, w_end, c_name) in enumerate(credit_windows):
        safe_c = "".join(c for c in c_name if c.isalnum() or c in {"_", "-", " "}).strip()
        end_bound = total_dur + 1.0 if win_idx == len(credit_windows) - 1 else w_end
        credit_filters.append(
            f"drawtext=text='Credit\\: {safe_c}':fontcolor=white@0.92:fontsize=25:x=(w-text_w)/2:y=225:enable='gte(t,{w_start:.2f})*lt(t,{end_bound:.2f})'"
        )
    dynamic_credits_chain = ",".join(credit_filters)

    safe_banner = banner_hook.replace("'", "").replace(":", "")
    polish_filter = (
        f"[base_v]drawbox=x=110:y=290:w=860:h=96:color=#E50914@0.92:t=fill:enable='between(t,0,4.5)',"
        f"drawtext=text='{safe_banner}':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=316:enable='between(t,0,4.5)',"
        f"drawbox=x=0:y=ih-14:w=iw*(t/{total_dur}):h=14:color=#FF00FF@0.90:t=fill,"
        f"drawtext=text='{CHANNEL_HANDLE}':fontcolor=white@0.75:fontsize=28:x=(w-text_w)/2:y=185,"
        f"{dynamic_credits_chain},"
        f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=23,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BackColour=&H80000000,Outline=3,Shadow=2,Alignment=2,MarginV=68'[out_v]"
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
        "-vframes", "1",
        "-vf", (
            "eq=contrast=1.22:saturation=1.35,"
            f"drawtext=text='{safe_txt}':fontcolor=#FFFF00:bordercolor=black:borderw=9:"
            "fontsize=88:x=(w-text_w)/2:y=300"
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
        prefix = f"Clip #{clip_num} — " if len(clip_specs) > 1 else ""
        credit_lines.append(f"🎮 {prefix}{c_name}: {cand['live_url']} ({cand['stream_title']})")
        if c_name not in seen:
            seen.add(c_name)
            creator_tags.append(c_name)

    status_obj = {"selfDeclaredMadeForKids": False}
    if YOUTUBE_PRIVACY == "public" and publish_at_iso:
        status_obj["privacyStatus"] = "private"
        status_obj["publishAt"] = publish_at_iso
    else:
        status_obj["privacyStatus"] = YOUTUBE_PRIVACY

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:95],
            "description": (
                f"🔥 Insane Grand Theft Auto VI Live Highlights!\n\n"
                f"Featured Twitch Streamers & Credits:\n" + "\n".join(credit_lines) + "\n\n"
                f"Subscribe to {CHANNEL_HANDLE} for daily GTA 6 live highlights, Vice City moments, and viral gaming clips! 🌴🚗💨\n\n"
                "#GTA6 #GrandTheftAuto6 #GrandTheftAutoVI #ViceCity #Leonida #GTA6Shorts #ViceCityClips #GamingShorts #Shorts"
            ),
            "tags": [
                "GTA 6", "Grand Theft Auto 6", "Grand Theft Auto VI", "GTA VI",
                "Vice City", "Leonida", "GTA 6 Gameplay", "GTA 6 Live", "GTA 6 Clips",
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
    sched_msg = f"SCHEDULED for {publish_at_iso}" if publish_at_iso and YOUTUBE_PRIVACY == "public" else f"uploaded as {YOUTUBE_PRIVACY.upper()}"
    print(f"Success! Video ID {video_id} ({sched_msg}) on {CHANNEL_HANDLE}!", flush=True)

    if os.path.exists(thumb_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg")
            ).execute()
            print(f"Custom thumbnail ({thumb_path}) attached!", flush=True)
        except Exception as e:
            print(f"Thumbnail upload skipped: {e}", flush=True)

if __name__ == "__main__":
    history_data = load_history()

    if UPLOAD_TO_YOUTUBE and YOUTUBE_PRIVACY == "public":
        open_slots = get_available_publish_slots(history_data, max_needed=3)
        if not open_slots:
            print("All 4 daily slots for the next 24 hours are already scheduled! Skipping run cleanly.", flush=True)
            exit(0)
    else:
        open_slots = [None, None, None]

    streams = find_top_live_streams(max_streams=MAX_STREAMS_TO_RECORD)
    if not streams:
        print("No eligible fresh GTA 6 live streams found right now. Exiting cleanly.", flush=True)
        exit(0)

    with ThreadPoolExecutor(max_workers=MAX_STREAMS_TO_RECORD) as executor:
        results = list(executor.map(record_single_stream, enumerate(streams)))

    recorded = [r for r in results if r is not None]
    if not recorded:
        print("Failed to record any streams. Exiting cleanly.", flush=True)
        exit(0)

    planned_shorts = analyze_batch_for_multiple_shorts(recorded, max_shorts=len(open_slots))
    if not planned_shorts:
        print("No standout moments found across the recorded streams. Exiting cleanly.", flush=True)
        exit(0)

    os.makedirs("artifacts", exist_ok=True)
    for idx, (mode, framing_mode, clip_specs) in enumerate(planned_shorts):
        slot_iso = open_slots[idx] if idx < len(open_slots) else None
        out_mp4 = f"artifacts/short_{idx+1}.mp4"
        out_jpg = f"artifacts/thumb_{idx+1}.jpg"

        print(f"\n=== Rendering Batch Short #{idx+1}/{len(planned_shorts)} ({mode} | {framing_mode.upper()}) ===", flush=True)
        viral_title, banner_hook, primary_climax = build_and_render_short(mode, framing_mode, clip_specs, out_video_name=out_mp4)
        create_short_thumbnail(out_mp4, primary_climax, banner_hook, out_thumb=out_jpg)

        if UPLOAD_TO_YOUTUBE:
            upload_to_youtube(out_mp4, out_jpg, viral_title, clip_specs, publish_at_iso=slot_iso)
            record_scheduled_upload(clip_specs, slot_iso)
        else:
            print(f"Saved {out_mp4} and {out_jpg} locally (UPLOAD_TO_YOUTUBE=False).", flush=True)
