import os
import json
import re
import urllib.request
import time
import asyncio
import subprocess
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

WATCHLIST = [
    "https://youtube.com/@TypicalGamer/live",
    "https://youtube.com/@DarkViperAU/live",
    "https://youtube.com/@TGG/live",
    "https://youtube.com/@Valkyrae/live",
    "https://youtube.com/@Sykkuno/live",
    "https://youtube.com/@xQcOW/live",
    "https://youtube.com/@KaiCenatLive/live",
    "https://youtube.com/@IShowSpeed/live",
]

CHUNK_SECONDS = 300         # Record 15-minute live buffer
CLIP_DURATION = 40          # Core highlight duration (seconds)
MIN_SPIKE_RATIO = 2.3       # Climax must be 2.3x louder than average
MIN_WORD_COUNT = 12         # Minimum spoken words to avoid silent clips
ENABLE_AI_VOICEOVER = True  # Adds energetic 2.5s AI hook voiceover

HISTORY_FILE = "history.json"
MAX_CLIPS_PER_STREAM = 6    # Allow up to 6 Shorts from a single multi-hour stream
MIN_GAP_SECONDS = 1800      # Wait at least 30 mins (1800s) before clipping the same stream again

def get_cookies_arg():
    base_args = ["--extractor-args", "youtube:player_client=tv_embedded,mweb"]
    return (["--cookies", "cookies.txt"] + base_args) if os.path.exists("cookies.txt") else base_args

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
    last_time = entry.get("last_clipped_at", 0)
    elapsed = int(time.time() - last_time)
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

def find_live_stream():
    history = load_history()
    cookies = get_cookies_arg()
    # 1. Check curated watchlist first
    for url in WATCHLIST:
        cmd = ["yt-dlp", "--print", "%(is_live)s|%(title)s|%(channel)s|%(webpage_url)s", "--no-warnings"] + cookies + [url]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = res.stdout.strip()
    if out.startswith("True|"):
         _, title, channel_name, live_url = out.split("|", 3)
         if can_clip_stream(live_url, history):
             print(f"[Watchlist Hit] Live stream by {channel_name}: {title} ({live_url})", flush=True)
             return live_url, title, channel_name

    # 2. Fallback: Full free YouTube search for active LIVE streams (0 API quota)
    # Change "GTA+5+live" to "GTA+6+live" before November 19 launch!
    import urllib.parse
    query = "GTA+5+live"
    search_page = urllib.parse.urlunparse(("https", "youtube.com", "/results", "", "search_query=" + query, ""))
    req = urllib.request.Request(
        search_page,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        # Extract only video IDs that have YouTube's active LIVE badge nearby
        live_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})".{0,1500}?"BADGE_STYLE_TYPE_LIVE_NOW"', html)
        # Remove duplicates while keeping top-ranked search order
        seen = set()
        unique_live_ids = [x for x in live_ids if not (x in seen or seen.add(x))]
        print(f"[Full Search] Found {len(unique_live_ids)} active live streams on YouTube.", flush=True)

        for vid_id in unique_live_ids[:15]:
            vid_url = urllib.parse.urlunparse(("https", "youtube.com", "/watch", "", "v=" + vid_id, ""))
            cmd_chk = [
                "yt-dlp", "--print", "%(is_live)s|%(title)s|%(channel)s",
                "--no-warnings",
            ] + cookies + [vid_url]
            chk = subprocess.run(cmd_chk, capture_output=True, text=True).stdout.strip()
            res_chk = subprocess.run(cmd_chk, capture_output=True, text=True)
            chk = res_chk.stdout.strip()
            print(f"Checking {vid_id}: stdout='{chk}' | stderr='{res_chk.stderr.strip()[:120]}'", flush=True)

            if chk.startswith("True|"):
                _, title, channel_name = chk.split("|", 2)
                if can_clip_stream(vid_url, history):
                    print(f"[Full Search Hit] Live stream by {channel_name}: {title} ({vid_url})", flush=True)
                    return vid_url, title, channel_name
    except Exception as e:
        print(f"Full search error: {e}", flush=True)

    return None, None, None
    
    

def record_live_chunk(live_url):
    cookies = get_cookies_arg()
    print(f"Recording {CHUNK_SECONDS}s live chunk via yt-dlp...", flush=True)
    cmd = [
        "yt-dlp",
        "-f", "best[height<=720]",
        "--downloader", "ffmpeg",
        "--downloader-args", f"ffmpeg_i:-t {CHUNK_SECONDS}",
        "-o", "raw_chunk.mp4",
        "--no-part", "--no-warnings"
    ] + cookies + [live_url]
    subprocess.run(cmd, check=True, timeout=CHUNK_SECONDS + 180)
    print("Finished recording live chunk!", flush=True)



def find_best_highlight(video_path):
    subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "audio.wav"], check=True)
    rate, data = wavfile.read("audio.wav")
    data = np.abs(data.astype(np.float32))

    num_secs = len(data) // rate
    if num_secs < 60:
        return None, None

    energies = np.array([
        np.sqrt(np.mean(data[i * rate : (i + 1) * rate] ** 2))
        for i in range(num_secs)
    ])
    avg_energy = np.mean(energies) + 1e-6

    valid_energies = energies[28 : max(29, num_secs - 22)]
    peak_sec = 28 + int(np.argmax(valid_energies))
    peak_ratio = energies[peak_sec] / avg_energy

    print(f"Peak audio at {peak_sec}s (Ratio: {peak_ratio:.2f}x)", flush=True)
    if peak_ratio < MIN_SPIKE_RATIO:
        return None, None

    start_sec = max(0, peak_sec - 22)
    return start_sec, peak_sec

def transcribe_clip(clip_path):
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_path, word_timestamps=True)

    words = []
    for seg in segments:
        for w in seg.words:
            words.append((w.start, w.end, w.word.strip().upper()))

    if len(words) < MIN_WORD_COUNT:
        return None, "", None

    meme_trigger_sec = None
    trigger_keywords = {"COPS", "POLICE", "WASTED", "DEAD", "WHAT", "BRO", "CRAZY"}
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
        start_t = chunk[0][0]
        end_t = chunk[-1][1]
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

def generate_viral_metadata_and_hook(transcript, stream_title):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "You won't believe what just happened in GTA 6! 😱 #gta6 #shorts", "Wait till you see what happens next!"

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30000))
    prompt = f"""You are a viral gaming YouTube Shorts editor for GTA 6.
Stream Title: {stream_title}
Clip Transcript: {transcript}

Return ONLY valid JSON with two keys:
1. "title": A high-CTR curiosity-gap YouTube Shorts title under 65 characters including 1 emoji and #gta6 #shorts.
2. "voice_hook": An energetic 6-to-8 word spoken hook for a 2.5-second intro voiceover that builds suspense without spoiling the ending."""

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(resp.text)
        return data.get("title", "INSANE GTA 6 Moment! 😱 #gta6 #shorts"), data.get("voice_hook", "Watch what happens right here!")
    except Exception as e:
        print(f"Gemini fallback triggered: {e}", flush=True)
        return "INSANE GTA 6 Live Moment! 😱 #gta6 #shorts", "Watch what happens right here!"

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
def render_viral_short(raw_path, start_sec, peak_sec, srt_path, voice_hook_text, meme_trigger_sec, channel_name):
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", raw_path,
        "-t", str(CLIP_DURATION), "-c", "copy", "base_clip.mp4"
    ], check=True)

    hook_start = max(0, peak_sec - 1)
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(hook_start), "-i", raw_path,
        "-t", "2.5", "-c", "copy", "hook_clip.mp4"
    ], check=True)

    if ENABLE_AI_VOICEOVER and voice_hook_text:
        asyncio.run(create_voiceover_audio(voice_hook_text, "hook_vo.mp3"))
        subprocess.run([
            "ffmpeg", "-y", "-i", "hook_clip.mp4", "-i", "hook_vo.mp3",
            "-filter_complex", "[0:a]volume=0.35[bg_a];[1:a]volume=1.3[vo_a];[bg_a][vo_a]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "hook_clip_voiced.mp4"
        ], check=True)
        os.replace("hook_clip_voiced.mp4", "hook_clip.mp4")

    with open("concat.txt", "w") as f:
        f.write("file 'hook_clip.mp4'\nfile 'base_clip.mp4'\n")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "combined.mp4"], check=True)

    climax_rel = (peak_sec - start_sec) + 2.5
    meme_time = (meme_trigger_sec + 2.5) if meme_trigger_sec is not None else climax_rel

    inputs = ["-i", "combined.mp4"]
    has_custom_bg = os.path.exists("assets/bg_neon.png")
    has_meme_img = os.path.exists("assets/wasted.png")
    has_boom_sfx = os.path.exists("assets/boom.mp3")

    if has_custom_bg:
        inputs.extend(["-i", "assets/bg_neon.png"])
        bg_filter = "[1:v]scale=1080:1920[bg];"
    else:
        bg_filter = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:6,eq=brightness=-0.08:saturation=1.3[bg];"

    fg_filter = (
        "[0:v]scale=1080:-2,eq=contrast=1.15:saturation=1.25,"
        f"crop=iw*if(between(t\\,{climax_rel-0.2}\\,{climax_rel+0.45})\\,0.91\\,if(lt(mod(t\\,5)\\,0.35)\\,0.94\\,1.0)):"
        f"ih*if(between(t\\,{climax_rel-0.2}\\,{climax_rel+0.45})\\,0.91\\,if(lt(mod(t\\,5)\\,0.35)\\,0.94\\,1.0)):"
        f"(iw-ow)/2+if(between(t\\,{climax_rel-0.2}\\,{climax_rel+0.45})\\,20*sin(t*65)\\,0):"
        f"(ih-oh)/2+if(between(t\\,{climax_rel-0.2}\\,{climax_rel+0.45})\\,20*cos(t*65)\\,0),"
        "scale=1080:-2[fg];"
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

        safe_creator = channel_name.replace("'", "").replace(":", "")
    polish_filter = (
        f"{current_v}drawbox=x=0:y=ih-14:w=iw*(t/{CLIP_DURATION + 2.5}):h=14:color=#FF00FF@0.85:t=fill,"
        f"drawtext=text='{CHANNEL_HANDLE}':fontcolor=white@0.55:fontsize=28:x=(w-text_w)/2:y=215,"
        f"drawtext=text='Credit\\: {safe_creator}':fontcolor=white@0.80:fontsize=24:x=(w-text_w)/2:y=255,"
        f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=19,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=48'[out_v]"
    )


    polish_filter = (
        f"{current_v}drawbox=x=0:y=ih-14:w=iw*(t/{CLIP_DURATION + 2.5}):h=14:color=#FF00FF@0.85:t=fill,"
        f"drawtext=text='{CHANNEL_HANDLE}':fontcolor=white@0.50:fontsize=28:x=(w-text_w)/2:y=230,"
        f"subtitles={srt_path}:force_style='FontName=Arial,FontSize=19,Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,Outline=2,Alignment=2,MarginV=48'[out_v]"
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
                f"🎥 Credit: {channel_name}\n"
                f"🔗 Original Stream: {live_url}\n"
                f"📺 Stream Title: {stream_title}\n\n"
                f"Subscribe to {CHANNEL_HANDLE} for 6 daily GTA 6 live clips! 🔥🌴\n\n"
                "#GTA6 #GrandTheftAuto6 #ViceCityClips #GTA6Shorts #GamingShorts #Leonida"
            ),
            "tags": ["GTA 6", "Grand Theft Auto 6", "Vice City Clips", channel_name, "GTA 6 Shorts", "Gaming Shorts"],
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
    live_url, stream_title, channel_name = find_live_stream()
    if not live_url:
        print("No live streams found right now. Exiting cleanly.", flush=True)
        exit(0)

    record_live_chunk(live_url)
    start_sec, peak_sec = find_best_highlight("raw_chunk.mp4")
    if start_sec is None:
        print("No major hype spike in this window. Skipping upload.", flush=True)
        exit(0)

    subprocess.run([
        "ffmpeg", "-y", "-ss", str(start_sec), "-i", "raw_chunk.mp4",
        "-t", str(CLIP_DURATION), "-c", "copy", "temp_transcribe.mp4"
    ], check=True)

    srt_path, transcript, meme_trigger_sec = transcribe_clip("temp_transcribe.mp4")
    if not srt_path:
        print("Not enough spoken dialogue in clip. Skipping upload.", flush=True)
        exit(0)

    viral_title, voice_hook = generate_viral_metadata_and_hook(transcript, stream_title)
    render_viral_short("raw_chunk.mp4", start_sec, peak_sec, srt_path, voice_hook, meme_trigger_sec, channel_name)
    upload_to_youtube("final_short.mp4", viral_title, stream_title, channel_name, live_url)
    record_clip_history(live_url, stream_title, channel_name, peak_sec)


