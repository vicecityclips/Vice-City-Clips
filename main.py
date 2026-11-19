import os
import json
import time
import random
import asyncio
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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

CHUNK_SECONDS = 300         # Record 5 streams x 5 mins simultaneously (25 mins scanned per run)
SINGLE_CLIP_DUR = 40        # Tier-1 single story clip length (seconds)
MULTI_CLIP_DUR = 16         # Tier-2 compilation clip length (3 clips x 16s = ~47s)
HOOK_DURATION = 2.5         # Cold-open climax preview length (seconds)
TRANSITION_DUR = 0.4        # Smooth xfade / acrossfade duration (seconds)
MIN_WORD_COUNT = 10         # Minimum spoken words
ENABLE_AI_VOICEOVER = True  # Energetic AI intro hook voiceover

HISTORY_FILE = "history.json"
MAX_CLIPS_PER_STREAM = 6    # Up to 6 clips per multi-hour broadcast
MIN_GAP_SECONDS = 1800      # 30-minute cooldown before clipping the same stream again

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
                return json.load(f)
        except Exception:
            return {}
    return {}

def can_clip_stream(stream_url, history):
    entry = history.get(stream_url)
    if not entry:
        return True
    if entry.get("count", 0) >= MAX_CLIPS_PER_STREAM:
        return False
    elapsed = int(time.time() - entry.get("last_clipped_at", 0))
    return elapsed >= MIN_GAP_SECONDS

def record_clip_history(stream_url, stream_title, channel_name, peak_sec):
    history = load_history()
    entry = history.get(stream_url, {
        "title": stream_title,
        "channel": channel_name,
        "count": 0,
        "clips": []
    })
    entry["count"] += 1
    entry["last_clipped_at"] = int(time.time())
    entry["clips"].append({
        "utc_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "chunk_peak_sec": int(peak_sec)
    })
    history[stream_url] = entry
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"Logged clip history for {channel_name} (Clip #{entry['count']}).", flush=True)

def find_top_live_streams(max_streams=5):
    history = load_history()
    now_utc = datetime.now(timezone.utc)
    if now_utc < datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc):
        print("Waiting for GTA 6 official launch. Exiting cleanly.", flush=True)
        return []

    game_name = "Grand Theft Auto VI"
    keywords = ["gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6", "vice city", "leonida"]
    query = """
    query($name: String!) {
      game(name: $name) {
        streams(first: 50) {
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
            if can_clip_stream(live_url, history):
                item = (live_url, title, channel_name)
                if login in CLIP_FRIENDLY_CREATORS:
                    whitelisted.append(item)
                else:
                    others.append(item)

        selected = (whitelisted + others)[:max_streams]
        print(f"[Parallel Scout] Recording {len(selected)} live GTA 6 streams simultaneously!", flush=True)
        return selected
    except Exception as e:
        print(f"Twitch search error: {e}", flush=True)
        return []

def record_single_stream(idx_and_stream):
    idx, (live_url, title, channel_name) = idx_and_stream
    out_file = f"raw_{idx}.ts"
    print(f"[Stream {idx+1}] Recording {CHUNK_SECONDS}s from {channel_name}...", flush=True)
    cmd = [
        "yt-dlp", "-f", "best[height<=720]",
        "--downloader", "ffmpeg", "--downloader-args", f"ffmpeg_i:-t {CHUNK_SECONDS}",
        "--fixup", "never", "-o", out_file, "--no-part", "--no-warnings", live_url
    ]
    try:
        subprocess.run(cmd, check=True, timeout=CHUNK_SECONDS + 180)
        if os.path.exists(out_file) and os.path.getsize(out_file) > 1_000_000:
            return (idx, out_file, live_url, title, channel_name)
    except Exception as e:
        print(f"[Stream {idx+1}] Skipped ({channel_name}): {e}", flush=True)
    return None

def analyze_streams_hybrid(recorded_list):
    candidates = []
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")

    for idx, video_path, live_url, stream_title, channel_name in recorded_list:
        wav_path = f"audio_{idx}.wav"
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", wav_path], check=True)
        rate, data = wavfile.read(wav_path)
        data = np.abs(data.astype(np.float32))
        num_secs = len(data) // rate
        if num_secs < 70:
            continue

        energies = np.array([np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2)) for i in range(num_secs)])
        jump_scores = np.zeros(num_secs)
        for s in range(25, num_secs - 22):
            jump_scores[s] = np.mean(energies[s - 1 : s + 3]) / (np.mean(energies[s - 14 : s - 2]) + 1e-6)

        best_sec = int(np.argmax(jump_scores))
        best_jump = float(jump_scores[best_sec])

        segments, _ = model.transcribe(wav_path)
        lines = [f"[{int(seg.start)}s] {seg.text.strip()}" for seg in segments if 15 <= seg.start <= (num_secs - 20) and seg.text.strip()]
        candidates.append({
            "idx": idx, "video_path": video_path, "live_url": live_url,
            "stream_title": stream_title, "channel_name": channel_name,
            "num_secs": num_secs, "audio_peak": best_sec, "jump_ratio": best_jump,
            "transcript": "\n".join(lines[:130])
        })

    if not candidates:
        return None

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=40000))
        blocks = []
        for c in candidates:
            blocks.append(
                f"=== STREAM {c['idx']} ({c['channel_name']} | Spike: {c['jump_ratio']:.2f}x at {c['audio_peak']}s | MaxSec: {c['num_secs']-22}) ===\n"
                f"Title: {c['stream_title']}\nTranscript:\n{c['transcript']}\n"
            )

        prompt = f"""You are a viral YouTube Shorts producer for a GTA 6 channel.
Analyze these {len(candidates)} simultaneous live streams and choose between TWO modes:
- MODE SINGLE: Use whenever ONE stream has a complete, engaging 40-second story, funny conversation, police chase, or stunt that holds attention for a full Short.
- MODE MULTI: Use when individual moments are short 15-second bursts, picking the TOP 3 moments across these streams to merge into a fast-paced highlight reel.

{''.join(blocks)}

Reply using this exact format ONLY:
MODE: <SINGLE or MULTI>
FRAMING: <crop or blur>
PICK1: <stream_idx>,<peak_sec>
PICK2: <stream_idx>,<peak_sec>
PICK3: <stream_idx>,<peak_sec>"""

        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.7),
            )
            mode = "SINGLE"
            framing = "blur"
            picks = []
            for line in resp.text.strip().splitlines():
                up = line.upper()
                val = line.split(":", 1)[-1].strip()
                if up.startswith("MODE:"):
                    mode = "MULTI" if "MULTI" in val.upper() else "SINGLE"
                elif up.startswith("FRAMING:"):
                    framing = "crop" if "crop" in val.lower() else "blur"
                elif up.startswith("PICK") and "," in val:
                    p_left, _, p_right = val.partition(",")
                    s_digits = "".join(ch for ch in p_left if ch.isdigit())
                    t_digits = "".join(ch for ch in p_right if ch.isdigit())
                    if s_digits and t_digits:
                        s_i, p_s = int(s_digits), int(t_digits)
                        for c in candidates:
                            if c["idx"] == s_i and 22 <= p_s <= (c["num_secs"] - 20):
                                picks.append((c, p_s))

            if mode == "SINGLE" and picks:
                c_win, p_win = next(iter(picks))
                print(f"[Smart Hybrid: SINGLE FULL SHORT] {c_win['channel_name']} at {p_win}s ({framing.upper()})", flush=True)
                return ("SINGLE", framing, [(c_win, max(0, p_win - 22), p_win, SINGLE_CLIP_DUR)])

            if len(picks) >= 2:
                selected_picks = picks[:3]
                print(f"[Smart Hybrid: MULTI COMPILATION] Merging {len(selected_picks)} moments ({framing.upper()})!", flush=True)
                formatted = [(c_item, max(0, p_item - 10), p_item, MULTI_CLIP_DUR) for c_item, p_item in selected_picks]
                return ("MULTI", framing, formatted)
        except Exception as e:
            print(f"Hybrid AI judge fallback: {e}", flush=True)

    sorted_c = sorted(candidates, key=lambda x: x["jump_ratio"], reverse=True)
    best_c = next(iter(sorted_c))
    if best_c["jump_ratio"] >= 1.85 or len(sorted_c) < 3:
        print(f"[Fallback Single Short] {best_c['channel_name']} at {best_c['audio_peak']}s", flush=True)
        return ("SINGLE", "blur", [(best_c, max(0, best_c["audio_peak"] - 22), best_c["audio_peak"], SINGLE_CLIP_DUR)])

    top3 = sorted_c[:3]
    print("[Fallback Multi Compilation] Merging top 3 audio reaction moments!", flush=True)
    return ("MULTI", "blur", [(c_item, max(0, c_item["audio_peak"] - 10), c_item["audio_peak"], MULTI_CLIP_DUR) for c_item in top3])

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
2. BANNER: Write a 3-to-4 word ALL-CAPS on-screen hook headline (e.g., "WAIT FOR THE COPS", "1 IN A MILLION", "PURE VICE CITY CHAOS").
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
        print(f"Generated Title: {title} | Top Hook Banner: {banner}", flush=True)
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

def build_and_render_short(mode, framing_mode, clip_specs):
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

        seg_start_t = 0.0 if (mode == "SINGLE" and idx == 0) else current_timeline_sec
        seg_end_t = current_timeline_sec + seg_dur
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

        v_filters = []
        a_filters = []
        prev_v = "[0:v]"
        prev_a = "[0:a]"
        running_offset = 0.0

        chosen_transitions = random.sample(XFADE_TRANSITIONS, k=min(len(XFADE_TRANSITIONS), len(concat_list) - 1))
        for step_idx in range(1, len(concat_list)):
            running_offset += concat_durs[step_idx - 1] - TRANSITION_DUR
            t_style = chosen_transitions[step_idx - 1]
            out_v_pad = "[vout]" if step_idx == len(concat_list) - 1 else f"[v{step_idx}]"
            out_a_pad = "[aout]" if step_idx == len(concat_list) - 1 else f"[a{step_idx}]"

            v_filters.append(
                f"{prev_v}[{step_idx}:v]xfade=transition={t_style}:duration={TRANSITION_DUR}:offset={running_offset:.2f}{out_v_pad}"
            )
            a_filters.append(
                f"{prev_a}[{step_idx}:a]acrossfade=d={TRANSITION_DUR}{out_a_pad}"
            )
            prev_v = out_v_pad
            prev_a = out_a_pad

        print(f"Applied dynamic transitions: {', '.join(chosen_transitions)}", flush=True)
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
            f"drawtext=text='Credit\\: {safe_c}':fontcolor=white@0.92:fontsize=25:x=(w-text_w)/2:y=225:enable='between(t,{w_start:.2f},{end_bound:.2f})'"
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
        "final_short.mp4"
    ], check=True)

    return viral_title, banner_hook, primary_climax, creators_used

def create_short_thumbnail(video_path, peak_rel_sec, banner_text):
    safe_txt = banner_text.replace("'", "").replace(":", "")[:24] or "NO WAY!"
    cmd = [
        "ffmpeg", "-y", "-ss", f"{max(0.5, peak_rel_sec):.2f}", "-i", video_path,
        "-vframes", "1",
        "-vf", (
            "eq=contrast=1.22:saturation=1.35,"
            f"drawtext=text='{safe_txt}':fontcolor=#FFFF00:bordercolor=black:borderw=9:"
            "fontsize=88:x=(w-text_w)/2:y=300"
        ),
        "thumb.jpg"
    ]
    subprocess.run(cmd, check=True)
    return "thumb.jpg"

def upload_to_youtube(video_path, title, clip_specs):
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
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response["id"]
    print(f"Uploaded PUBLIC GTA 6 Short to {CHANNEL_HANDLE}! Video ID: {video_id}", flush=True)

    if os.path.exists("thumb.jpg"):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload("thumb.jpg", mimetype="image/jpeg")
            ).execute()
            print("Custom vertical thumbnail uploaded!", flush=True)
        except Exception as e:
            print(f"Thumbnail upload skipped: {e}", flush=True)

if __name__ == "__main__":
    streams = find_top_live_streams(max_streams=5)
    if not streams:
        print("No eligible GTA 6 live streams found right now. Exiting cleanly.", flush=True)
        exit(0)

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(record_single_stream, enumerate(streams)))

    recorded = [r for r in results if r is not None]
    if not recorded:
        print("Failed to record any streams. Exiting cleanly.", flush=True)
        exit(0)

    hybrid_plan = analyze_streams_hybrid(recorded)
    if not hybrid_plan:
        print("No usable dialogue or highlights found across streams. Exiting.", flush=True)
        exit(0)

    mode, framing_mode, clip_specs = hybrid_plan
    viral_title, banner_hook, primary_climax, _ = build_and_render_short(mode, framing_mode, clip_specs)
    create_short_thumbnail("final_short.mp4", primary_climax, banner_hook)
    upload_to_youtube("final_short.mp4", viral_title, clip_specs)

    for cand, _, peak_sec, _ in clip_specs:
        record_clip_history(cand["live_url"], cand["stream_title"], cand["channel_name"], peak_sec)
