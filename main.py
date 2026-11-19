import os
import json
import time
import asyncio
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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

CHANNEL_HANDLE = "@Vice-City-Clips-Vault"

CHUNK_SECONDS = 300         # 5-minute live recording buffer
CLIP_DURATION = 40          # Core highlight length (seconds)
HOOK_DURATION = 2.5         # Cold-open climax preview length (seconds)
MIN_SPIKE_RATIO = 1.65      # Sudden-jump reaction threshold (ignores steady loud engines)
MIN_WORD_COUNT = 12         # Requires active commentary/dialogue
ENABLE_AI_VOICEOVER = True  # Energetic AI intro hook voiceover

HISTORY_FILE = "history.json"
MAX_CLIPS_PER_STREAM = 6    # Up to 6 clips per multi-hour broadcast
MIN_GAP_SECONDS = 1800      # 30-minute cooldown before clipping the same stream again

# Top creators known to welcome community clip channels (checked first before general search)
CLIP_FRIENDLY_CREATORS = [
    "kaicenat", "xqc", "shroud", "sykkuno", "valkyrae",
    "zerkaa", "summit1g", "lirik", "timthetatman", "nickmercs",
    "caseoh_", "jynxzi", "agent00", "fanum", "duke"
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
        first_char = clean[0]
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
        print(f"Skipping {stream_url}: reached max {MAX_CLIPS_PER_STREAM} clips.", flush=True)
        return False
    elapsed = int(time.time() - entry.get("last_clipped_at", 0))
    if elapsed < MIN_GAP_SECONDS:
        mins_left = (MIN_GAP_SECONDS - elapsed) // 60
        print(f"Skipping {stream_url}: cooldown active ({mins_left}m remaining).", flush=True)
        return False
    return True

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
    print(f"Saved timestamp to {HISTORY_FILE} (Clip #{entry['count']} for this stream).", flush=True)

def search_twitch_gta6(history):
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
    gql_payload = json.dumps({
        "query": query,
        "variables": {"name": game_name}
    }).encode("utf-8")

    gql_url = urllib.parse.urlunparse(("https", "gql.twitch.tv", "/gql", "", "", ""))
    req = urllib.request.Request(
        gql_url,
        data=gql_payload,
        headers={
            "Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko",
            "Content-Type": "application/json"
        }
    )

    try:
        raw_resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        resp_data = json.loads(raw_resp)
        game_obj = resp_data.get("data", {}).get("game")
        if not game_obj:
            return None, None, None

        edges = game_obj["streams"]["edges"]
        print(f"[Twitch Search] Found {len(edges)} live '{game_name}' streams.", flush=True)

        valid_streams = []
        for edge in edges:
            node = edge["node"]
            broadcaster = node["broadcaster"]
            login = broadcaster["login"].lower()
            channel_name = broadcaster["displayName"]
            title = node.get("title", "")

            lang = (broadcaster.get("broadcastSettings") or {}).get("language", "en")
            if lang and lang.lower() != "en":
                continue
            if not title.isascii():
                continue

            t_low = title.lower()
            if "rerun" in t_low or "rebroadcast" in t_low or "24/7" in t_low or "fake" in t_low:
                continue
            if not any(kw in t_low for kw in keywords):
                continue

            live_url = urllib.parse.urlunparse(("https", "www.twitch.tv", "/" + login, "", "", ""))
            if can_clip_stream(live_url, history):
                valid_streams.append((login, live_url, title, channel_name))

        # Pass 1: Prioritize clip-friendly whitelisted creators first
        for login, live_url, title, channel_name in valid_streams:
            if login in CLIP_FRIENDLY_CREATORS:
                print(f"[Whitelist GTA 6 Hit] {channel_name}: {title}", flush=True)
                return live_url, title, channel_name

        # Pass 2: Fallback to highest-viewer eligible English GTA 6 stream
        if valid_streams:
            login, live_url, title, channel_name = valid_streams[0]
            print(f"[Top GTA 6 Hit] {channel_name}: {title}", flush=True)
            return live_url, title, channel_name

    except Exception as e:
        print(f"Twitch search error: {e}", flush=True)

    return None, None, None

def find_live_stream():
    history = load_history()
    now_utc = datetime.now(timezone.utc)
    if now_utc < datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc):
        print("Waiting for GTA 6 official launch (Nov 19, 2026 00:00 UTC). Exiting cleanly.", flush=True)
        return None, None, None

    return search_twitch_gta6(history)

def record_live_chunk(live_url):
    print(f"Recording {CHUNK_SECONDS}s live GTA 6 chunk from Twitch...", flush=True)
    cmd = [
        "yt-dlp",
        "-f", "best[height<=720]",
        "--downloader", "ffmpeg",
        "--downloader-args", f"ffmpeg_i:-t {CHUNK_SECONDS}",
        "--fixup", "never",
        "-o", "raw_chunk.ts",
        "--no-part",
        "--no-warnings",
        live_url
    ]
    subprocess.run(cmd, check=True, timeout=CHUNK_SECONDS + 180)
    print("Finished recording live chunk!", flush=True)

def find_best_highlight(video_path):
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "audio.wav"], check=True)
    rate, data = wavfile.read("audio.wav")
    data = np.abs(data.astype(np.float32))

    num_secs = len(data) // rate
    if num_secs < 65:
        return None, None

    energies = np.array([
        np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2))
        for i in range(num_secs)
    ])

    jump_scores = np.zeros(num_secs)
    for s in range(28, num_secs - 22):
        action_avg = np.mean(energies[s - 1 : s + 3])
        before_avg = np.mean(energies[s - 14 : s - 2]) + 1e-6
        jump_scores[s] = action_avg / before_avg

    peak_sec = int(np.argmax(jump_scores))
    best_jump = jump_scores[peak_sec]

    print(f"Sharpest reaction jump at {peak_sec}s (Jump Ratio: {best_jump:.2f}x)", flush=True)
    if best_jump < MIN_SPIKE_RATIO:
        return None, None

    start_sec = max(0, peak_sec - 22)
    return start_sec, peak_sec

def transcribe_clip(clip_path, time_offset=HOOK_DURATION):
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_path, word_timestamps=True)

    words = []
    climax_words = []
    bad_word_intervals = []
    for seg in segments:
        for w in seg.words:
            raw_upper = w.word.strip().upper()
            clean_alpha = "".join(c for c in raw_upper if c.isalpha())
            if clean_alpha in PROFANITY_WORDS:
                bad_word_intervals.append((max(0.0, w.start + time_offset - 0.05), w.end + time_offset + 0.05))
            censored_txt = censor_word_text(raw_upper)
            words.append((w.start, w.end, censored_txt))
            if 15.0 <= w.start <= 36.0 and "*" not in censored_txt:
                climax_words.append(censored_txt)

    if len(words) < MIN_WORD_COUNT:
        return None, "", "", None, []

    meme_trigger_sec = None
    trigger_keywords = {"COPS", "POLICE", "WASTED", "DEAD", "WHAT", "BRO", "CRAZY", "INSANE", "NO"}
    for start_t, _, word_txt in words:
        clean_w = "".join(c for c in word_txt if c.isalpha())
        if clean_w in trigger_keywords:
            meme_trigger_sec = start_t
            break

    srt_lines = []
    full_text = []
    idx = 1
    for i in range(0, len(words), 2):
        chunk = words[i : i + 2]
        first_word = chunk[0]
        last_word = chunk[-1]
        start_t = first_word[0] + time_offset
        end_t = last_word[1] + time_offset
        text = " ".join(w[2] for w in chunk)
        full_text.append(text)

        def fmt_time(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = int(t % 60)
            ms = int((t - int(t)) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        srt_lines.append(f"{idx}\n{fmt_time(start_t)} --> {fmt_time(end_t)}\n{text}\n")
        idx += 1

    with open("captions.srt", "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    print(f"Detected {len(bad_word_intervals)} restricted words to mute & censor.", flush=True)
    return "captions.srt", " ".join(full_text), " ".join(climax_words), meme_trigger_sec, bad_word_intervals

def generate_viral_metadata_and_hook(transcript, climax_transcript, stream_title):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    clean_stream = "".join(c for c in stream_title if c.isascii() and (c.isalnum() or c.isspace())).strip()
    stream_topic = " ".join(clean_stream.split()[:4]).title() if clean_stream else "Vice City Chaos"
    fallback_title = f"GTA 6: {stream_topic} 😱 #gta6 #shorts"

    if not api_key:
        print("Warning: GEMINI_API_KEY is missing! Using English fallback title.", flush=True)
        return fallback_title, "Watch what happens right here!"

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30000))
    prompt = f"""You are a viral gaming YouTube Shorts editor for an English-speaking GTA 6 channel.
Stream Title: {stream_title}
Setup Dialogue (Beginning): {transcript[:250]}
CLIMAX MOMENT DIALOGUE (Main Action): {climax_transcript or transcript[-250:]}

Rules:
1. Write the title EXCLUSIVELY in English (never use Japanese, Chinese, or any non-English words or characters).
2. Keep the title under 68 characters, family-friendly, and directly tied to the CLIMAX MOMENT.
3. Include "GTA 6" and "#gta6 #shorts" in the title.
4. Write a natural, exciting 5-to-7 word English spoken intro hook that teases the climax.

Reply in this exact 2-line format ONLY:
TITLE: <your English climax-focused title here>
HOOK: <your English spoken hook here>"""

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        title = fallback_title
        hook = "Watch what happens right here!"
        for line in resp.text.strip().splitlines():
            if line.upper().startswith("TITLE:"):
                candidate = line.partition(":")[2].strip()
                if candidate:
                    title = candidate
            elif line.upper().startswith("HOOK:"):
                candidate_hook = line.partition(":")[2].strip()
                if candidate_hook:
                    hook = candidate_hook
        print(f"Gemini English Title: {title}", flush=True)
        return title, hook
    except Exception as e:
        print(f"Gemini API error: {e}", flush=True)
        return fallback_title, "Watch what happens right here!"

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

def render_viral_short(raw_path, start_sec, peak_sec, srt_path, voice_hook_text, meme_trigger_sec, bad_word_intervals):
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", raw_path,
        "-t", str(CLIP_DURATION), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", "-ac", "2", "base_clip.mp4"
    ], check=True)

    hook_start = max(0, peak_sec - 3.5)
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(hook_start), "-i", raw_path,
        "-t", str(HOOK_DURATION), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", "-ac", "2", "hook_clip.mp4"
    ], check=True)

    if ENABLE_AI_VOICEOVER and voice_hook_text:
        asyncio.run(create_voiceover_audio(voice_hook_text, "hook_vo.mp3"))
        subprocess.run([
            "ffmpeg", "-y", "-i", "hook_clip.mp4", "-i", "hook_vo.mp3",
            "-filter_complex", "[0:a]volume=0.25[bg_a];[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.35[vo_a];[bg_a][vo_a]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "hook_clip_voiced.mp4"
        ], check=True)
        os.replace("hook_clip_voiced.mp4", "hook_clip.mp4")

    with open("concat.txt", "w") as f:
        f.write("file 'hook_clip.mp4'\nfile 'base_clip.mp4'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "combined.mp4"], check=True)

    climax_rel = (peak_sec - start_sec) + HOOK_DURATION
    meme_time = (meme_trigger_sec + HOOK_DURATION) if meme_trigger_sec is not None else climax_rel
    total_dur = CLIP_DURATION + HOOK_DURATION

    inputs = ["-i", "combined.mp4"]
    has_custom_bg = os.path.exists("assets/bg_neon.png")
    has_meme_img = os.path.exists("assets/wasted.png")
    has_boom_sfx = os.path.exists("assets/boom.mp3")

    if has_custom_bg:
        inputs.extend(["-i", "assets/bg_neon.png"])
        bg_filter = "[1:v]scale=1080:1920[bg];"
    else:
        bg_filter = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:6,eq=brightness=-0.08:saturation=1.3[bg];"

    zoom_expr = f"if(between(in_time,{climax_rel-0.25},{climax_rel+0.6}),1.20,if(lt(mod(in_time,4),0.45),1.09,1.0))"
    fg_filter = (
        f"[0:v]fps=30,scale=1080:608,setsar=1,"
        f"eq=contrast=1.18:saturation=1.30:brightness='if(between(t,{climax_rel-0.1},{climax_rel+0.15}),0.20,0)':eval=frame,"
        f"zoompan=z='{zoom_expr}':"
        f"x='iw/2-(iw/zoom/2)+if(between(in_time,{climax_rel-0.25},{climax_rel+0.6}),22*sin(in_time*75),0)':"
        f"y='ih/2-(ih/zoom/2)+if(between(in_time,{climax_rel-0.25},{climax_rel+0.6}),22*cos(in_time*75),0)':"
        f"d=1:s=1080x608:fps=30[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[base_v];"
    )

    current_v = "[base_v]"
    next_idx = 2 if has_custom_bg else 1

    meme_filter = ""
    if has_meme_img:
        inputs.extend(["-i", "assets/wasted.png"])
        meme_filter = (
            f"[{next_idx}:v]scale=520:-1[meme];"
            f"{current_v}[meme]overlay=(W-w)/2:280:enable='between(t,{meme_time:.2f},{meme_time+1.6:.2f})'[meme_v];"
        )
        current_v = "[meme_v]"
        next_idx += 1

    polish_filter = (
        f"{current_v}drawbox=x=0:y=ih-14:w=iw*(t/{total_dur}):h=14:color=#FF00FF@0.85:t=fill,"
        f"drawtext=text='{CHANNEL_HANDLE}':fontcolor=white@0.60:fontsize=28:x=(w-text_w)/2:y=225,"
        f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=20,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=48'[out_v]"
    )

    if bad_word_intervals:
        enable_expr = "+".join([f"between(t,{s:.2f},{e:.2f})" for s, e in bad_word_intervals])
        censor_chain = f"[0:a]aresample=async=1,loudnorm=I=-14:TP=-1.5:LRA=11,volume=0:enable='{enable_expr}'[norm_a];"
    else:
        censor_chain = "[0:a]aresample=async=1,loudnorm=I=-14:TP=-1.5:LRA=11[norm_a];"

    if has_boom_sfx:
        inputs.extend(["-i", "assets/boom.mp3"])
        delay_ms = int(climax_rel * 1000)
        audio_filter = (
            censor_chain +
            f"[{next_idx}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay_ms}|{delay_ms},volume=0.85[sfx_a];"
            f"[norm_a][sfx_a]amix=inputs=2:duration=first[out_a]"
        )
    else:
        audio_filter = censor_chain.replace("[norm_a];", "[out_a]")

    full_filter_complex = bg_filter + fg_filter + meme_filter + polish_filter + ";" + audio_filter

    subprocess.run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", full_filter_complex,
        "-map", "[out_v]", "-map", "[out_a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "final_short.mp4"
    ], check=True)

def create_short_thumbnail(video_path, peak_rel_sec, title_text):
    hook_words = [w.upper() for w in title_text.split() if w.isalpha() and w.upper() not in {"GTA", "SHORTS"}][:3]
    banner_txt = " ".join(hook_words) if hook_words else "NO WAY!"
    safe_txt = banner_txt.replace("'", "").replace(":", "")

    cmd = [
        "ffmpeg", "-y", "-ss", f"{max(0.5, peak_rel_sec):.2f}", "-i", video_path,
        "-vframes", "1",
        "-vf", (
            "eq=contrast=1.22:saturation=1.35,"
            f"drawtext=text='{safe_txt}':fontcolor=#FFFF00:bordercolor=black:borderw=8:"
            "fontsize=96:x=(w-text_w)/2:y=290"
        ),
        "thumb.jpg"
    ]
    subprocess.run(cmd, check=True)
    return "thumb.jpg"

def upload_to_youtube(video_path, title, stream_title, channel_name, live_url):
    creds_data = json.loads(os.environ["YOUTUBE_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(creds_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title[:95],
            "description": (
                f"🔥 Insane Grand Theft Auto VI Live Highlight!\n\n"
                f"🎮 Twitch Streamer: {channel_name} ({live_url})\n"
                f"📺 Original Broadcast: {stream_title}\n\n"
                f"Subscribe to {CHANNEL_HANDLE} for daily GTA 6 live highlights, Vice City moments, and viral gaming clips! 🌴🚗💨\n\n"
                "#GTA6 #GrandTheftAuto6 #GrandTheftAutoVI #ViceCity #Leonida #GTA6Shorts #ViceCityClips #GamingShorts #Shorts"
            ),
            "tags": [
                "GTA 6", "Grand Theft Auto 6", "Grand Theft Auto VI", "GTA VI",
                "Vice City", "Leonida", "GTA 6 Gameplay", "GTA 6 Live", "GTA 6 Clips",
                "Vice City Clips", "GTA 6 Shorts", "Gaming Shorts",
                channel_name
            ],
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
    for attempt in range(1, 5):
        print(f"\n--- Attempt {attempt}/4: Searching for high-energy English GTA 6 live stream ---", flush=True)
        live_url, stream_title, channel_name = find_live_stream()
        if not live_url:
            print("No eligible GTA 6 live streams found right now. Exiting cleanly.", flush=True)
            break

        record_live_chunk(live_url)
        start_sec, peak_sec = find_best_highlight("raw_chunk.ts")
        if start_sec is None:
            print("No sharp reaction jump in this stream. Moving on to next streamer...", flush=True)
            record_clip_history(live_url, stream_title, channel_name, 0)
            continue

        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_sec), "-i", "raw_chunk.ts",
            "-t", str(CLIP_DURATION), "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "48000", "-ac", "2", "temp_transcribe.mp4"
        ], check=True)

        srt_path, transcript, climax_transcript, meme_trigger_sec, bad_word_intervals = transcribe_clip("temp_transcribe.mp4", time_offset=HOOK_DURATION)
        if not srt_path:
            print("Not enough spoken dialogue in clip. Moving on to next streamer...", flush=True)
            record_clip_history(live_url, stream_title, channel_name, 0)
            continue

        viral_title, voice_hook = generate_viral_metadata_and_hook(transcript, climax_transcript, stream_title)
        render_viral_short("raw_chunk.ts", start_sec, peak_sec, srt_path, voice_hook, meme_trigger_sec, bad_word_intervals)

        climax_rel = (peak_sec - start_sec) + HOOK_DURATION
        create_short_thumbnail("final_short.mp4", climax_rel, viral_title)

        upload_to_youtube("final_short.mp4", viral_title, stream_title, channel_name, live_url)
        record_clip_history(live_url, stream_title, channel_name, peak_sec)
        break
