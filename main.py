import os
import json
import time
import asyncio
import subprocess
import urllib.parse
import urllib.request
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
MIN_SPIKE_RATIO = 1.8       # Strict energy threshold to skip boring/quiet streams
MIN_WORD_COUNT = 14         # Requires active commentary/dialogue
ENABLE_AI_VOICEOVER = True  # Energetic AI intro hook voiceover

HISTORY_FILE = "history.json"
MAX_CLIPS_PER_STREAM = 6    # Up to 6 clips per multi-hour broadcast
MIN_GAP_SECONDS = 1800      # 30-minute cooldown before clipping the same stream again
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

def search_twitch_game(game_name, keywords, history):
    query = """
    query($name: String!) {
      game(name: $name) {
        streams(first: 20) {
          edges {
            node {
              title
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
            return None, None, None, None

        edges = game_obj["streams"]["edges"]
        print(f"[Twitch Search] Found {len(edges)} live '{game_name}' streams.", flush=True)

        for edge in edges:
            node = edge["node"]
            broadcaster = node["broadcaster"]
            login = broadcaster["login"]
            channel_name = broadcaster["displayName"]
            title = node.get("title", "")
            t_low = title.lower()

            if "rerun" in t_low or "rebroadcast" in t_low or "24/7" in t_low:
                continue
            if not any(kw in t_low for kw in keywords):
                continue

            live_url = urllib.parse.urlunparse(("https", "www.twitch.tv", "/" + login, "", "", ""))
            if can_clip_stream(live_url, history):
                print(f"[Live Match] {game_name} | {channel_name}: {title}", flush=True)
                return live_url, title, channel_name, game_name
    except Exception as e:
        print(f"Twitch search error ({game_name}): {e}", flush=True)

    return None, None, None, None

def find_live_stream():
    history = load_history()
    # 1. Always check GTA 6 first (activates automatically the moment streams go live)
    live_url, title, channel_name, matched_game = search_twitch_game(
        "Grand Theft Auto VI",
        ["gta 6", "gta vi", "grand theft auto vi", "grand theft auto 6", "vice city", "leonida"],
        history
    )
    if live_url:
        return live_url, title, channel_name, matched_game

    # 2. Fallback to GTA 5 until GTA 6 releases so the channel builds momentum now
    print("No GTA 6 streams live yet. Falling back to GTA 5...", flush=True)
    return search_twitch_game(
        "Grand Theft Auto V",
        ["gta", "grand theft auto", "rp", "nopixel", "fiveM", "fivem", "vice city"],
        history
    )

def record_live_chunk(live_url):
    print(f"Recording {CHUNK_SECONDS}s live chunk from Twitch...", flush=True)
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
    avg_energy = np.mean(energies) + 1e-6

    # Use a 5-second rolling average so we pick sustained hype/action (not a 1-second cough)
    kernel = np.ones(5) / 5.0
    sustained_energies = np.convolve(energies, kernel, mode="same")

    valid_window = sustained_energies[28 : max(29, num_secs - 22)]
    peak_sec = 28 + int(np.argmax(valid_window))
    peak_ratio = sustained_energies[peak_sec] / avg_energy

    print(f"Sustained action peak at {peak_sec}s (Ratio: {peak_ratio:.2f}x)", flush=True)
    if peak_ratio < MIN_SPIKE_RATIO:
        return None, None

    start_sec = max(0, peak_sec - 22)
    return start_sec, peak_sec

def transcribe_clip(clip_path, time_offset=HOOK_DURATION):
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_path, word_timestamps=True)

    words = []
    for seg in segments:
        for w in seg.words:
            words.append((w.start, w.end, w.word.strip().upper()))

    if len(words) < MIN_WORD_COUNT:
        return None, "", None

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
        # Shift timestamps forward by HOOK_DURATION (2.5s) so captions never appear early!
        start_t = chunk[0][0] + time_offset
        end_t = chunk[-1][1] + time_offset
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

    return "captions.srt", " ".join(full_text), meme_trigger_sec

def generate_viral_metadata_and_hook(transcript, stream_title, matched_game):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    is_gta6 = "VI" in matched_game
    game_tag = "GTA 6" if is_gta6 else "GTA 5"
    clean_words = [w.capitalize() for w in transcript.split() if len(w) > 3]
    topic = " ".join(clean_words[:4]) if clean_words else "Wild Live Moment"
    fallback_title = f"{game_tag}: {topic} 😱 #gta6 #gta5 #shorts"

    if not api_key:
        print("Warning: GEMINI_API_KEY not found in environment.", flush=True)
        return fallback_title, "Watch what happens right here!"

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30000))
    prompt = f"""You are an expert gaming YouTube Shorts editor for a GTA 6 & GTA 5 channel.
Current Game: {matched_game}
Stream Title: {stream_title}
Spoken Transcript from Clip: {transcript}

Rules:
1. Write a clear, logical, high-CTR YouTube Shorts title (under 68 characters) that accurately reflects what happens in the transcript—do NOT make up random nonsense.
2. Include "{game_tag}" and "#gta6 #gta5 #shorts" in the title.
3. Write a natural 5-to-7 word spoken intro hook for a 2.5-second voiceover that fits the clip.

Reply in this exact 2-line format ONLY:
TITLE: <your sensible title here>
HOOK: <your spoken hook here>"""

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        title = fallback_title
        hook = "Watch what happens right here!"
        for line in resp.text.strip().splitlines():
            if line.upper().startswith("TITLE:"):
                title = line.split(":", 1)[1].strip()
            elif line.upper().startswith("HOOK:"):
                hook = line.split(":", 1)[1].strip()
        print(f"Generated Title: {title}", flush=True)
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
def render_viral_short(raw_path, start_sec, peak_sec, srt_path, voice_hook_text, meme_trigger_sec):
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", raw_path,
        "-t", str(CLIP_DURATION), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "base_clip.mp4"
    ], check=True)

    hook_start = max(0, peak_sec - 1)
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(hook_start), "-i", raw_path,
        "-t", str(HOOK_DURATION), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "hook_clip.mp4"
    ], check=True)

    if ENABLE_AI_VOICEOVER and voice_hook_text:
        asyncio.run(create_voiceover_audio(voice_hook_text, "hook_vo.mp3"))
        subprocess.run([
            "ffmpeg", "-y", "-i", "hook_clip.mp4", "-i", "hook_vo.mp3",
            "-filter_complex", "[0:a]volume=0.35[bg_a];[1:a]volume=1.35[vo_a];[bg_a][vo_a]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "hook_clip_voiced.mp4"
        ], check=True)
        os.replace("hook_clip_voiced.mp4", "hook_clip.mp4")

    with open("concat.txt", "w") as f:
        f.write("file 'hook_clip.mp4'\nfile 'base_clip.mp4'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "combined.mp4"], check=True)

    climax_rel = (peak_sec - start_sec) + HOOK_DURATION
    meme_time = (meme_trigger_sec + HOOK_DURATION) if meme_trigger_sec is not None else climax_rel

    inputs = ["-i", "combined.mp4"]
    has_custom_bg = os.path.exists("assets/bg_neon.png")
    has_meme_img = os.path.exists("assets/wasted.png")
    has_boom_sfx = os.path.exists("assets/boom.mp3")

    if has_custom_bg:
        inputs.extend(["-i", "assets/bg_neon.png"])
        bg_filter = "[1:v]scale=1080:1920[bg];"
    else:
        bg_filter = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:6,eq=brightness=-0.08:saturation=1.3[bg];"

    # Dynamic zoom-ins, impact camera shake, and climax flash via zoompan
    zoom_expr = f"if(between(in_time,{climax_rel-0.25},{climax_rel+0.6}),1.20,if(lt(mod(in_time,4),0.45),1.09,1.0))"
    fg_filter = (
        f"[0:v]scale=1080:608,setsar=1,"
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

    # Clean screen: no owner credit on-screen, only channel handle + neon progress bar + synced captions
    polish_filter = (
        f"{current_v}drawbox=x=0:y=ih-14:w=iw*(t/{CLIP_DURATION + HOOK_DURATION}):h=14:color=#FF00FF@0.85:t=fill,"
        f"drawtext=text='{CHANNEL_HANDLE}':fontcolor=white@0.60:fontsize=28:x=(w-text_w)/2:y=225,"
        f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=20,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=48'[out_v]"
    )

    if has_boom_sfx:
        inputs.extend(["-i", "assets/boom.mp3"])
        delay_ms = int(climax_rel * 1000)
        audio_filter = (
            f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[main_a];"
            f"[{next_idx}:a]adelay={delay_ms}|{delay_ms},volume=0.85[sfx_a];"
            f"[main_a][sfx_a]amix=inputs=2:duration=first[out_a]"
        )
    else:
        audio_filter = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[out_a]"

    full_filter_complex = bg_filter + fg_filter + meme_filter + polish_filter + ";" + audio_filter

    subprocess.run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", full_filter_complex,
        "-map", "[out_v]", "-map", "[out_a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "final_short.mp4"
    ], check=True)
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
                f"🔥 Insane Grand Theft Auto Live Clip!\n\n"
                f"🎮 Twitch Streamer: {channel_name} ({live_url})\n"
                f"📺 Original Broadcast: {stream_title}\n\n"
                f"Subscribe to {CHANNEL_HANDLE} for daily GTA 6 & GTA 5 live highlights, Vice City moments, and viral gaming clips! 🌴🚗💨\n\n"
                "#GTA6 #GrandTheftAuto6 #GrandTheftAutoVI #ViceCity #Leonida #GTA5 #GrandTheftAutoV #GTAV #GTA6Shorts #GTA5Shorts #ViceCityClips #GamingShorts #Shorts"
            ),
            "tags": [
                "GTA 6", "Grand Theft Auto 6", "Grand Theft Auto VI", "GTA VI",
                "Vice City", "Leonida", "GTA 6 Gameplay", "GTA 6 Live", "GTA 6 Clips",
                "GTA 5", "Grand Theft Auto 5", "Grand Theft Auto V", "GTA V", "GTA RP",
                "Vice City Clips", "GTA 6 Shorts", "GTA 5 Shorts", "Gaming Shorts",
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
    print(f"Uploaded PUBLIC Short to {CHANNEL_HANDLE}! Video ID: {response['id']}", flush=True)

if __name__ == "__main__":
    for attempt in range(1, 3):
        print(f"\n--- Attempt {attempt}/2: Searching for high-energy GTA live stream ---", flush=True)
        live_url, stream_title, channel_name, matched_game = find_live_stream()
        if not live_url:
            print("No eligible live streams found right now. Exiting cleanly.", flush=True)
            break

        record_live_chunk(live_url)
        start_sec, peak_sec = find_best_highlight("raw_chunk.ts")
        if start_sec is None:
            print("Stream was too quiet/boring in this window. Skipping to next streamer...", flush=True)
            record_clip_history(live_url, stream_title, channel_name, 0)
            continue

        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_sec), "-i", "raw_chunk.ts",
            "-t", str(CLIP_DURATION), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "temp_transcribe.mp4"
        ], check=True)

        srt_path, transcript, meme_trigger_sec = transcribe_clip("temp_transcribe.mp4", time_offset=HOOK_DURATION)
        if not srt_path:
            print("Not enough spoken dialogue in clip. Skipping to next streamer...", flush=True)
            record_clip_history(live_url, stream_title, channel_name, 0)
            continue

        viral_title, voice_hook = generate_viral_metadata_and_hook(transcript, stream_title, matched_game)
        render_viral_short("raw_chunk.ts", start_sec, peak_sec, srt_path, voice_hook, meme_trigger_sec)
        upload_to_youtube("final_short.mp4", viral_title, stream_title, channel_name, live_url)
        record_clip_history(live_url, stream_title, channel_name, peak_sec)
        break
