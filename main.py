from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
import yt_dlp
import re

app = FastAPI(
    title="ASTUBE API",
    description="YouTube video URL extractor – returns 360p MP4 direct URL",
    version="1.0.0"
)

def extract_video_id(input_str: str) -> str:
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, input_str)
        if match:
            return match.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{11}$", input_str):
        return input_str
    raise ValueError("Could not extract a valid YouTube video ID.")


def get_360p_url(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]/best",
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded"],
            }
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    chosen = None
    for fmt in info.get("formats", []):
        h = fmt.get("height") or 0
        ext = fmt.get("ext", "")
        if h <= 360 and ext == "mp4" and fmt.get("url"):
            if chosen is None or h > (chosen.get("height") or 0):
                chosen = fmt

    if not chosen:
        for fmt in info.get("formats", []):
            h = fmt.get("height") or 0
            if h <= 360 and fmt.get("url"):
                if chosen is None or h > (chosen.get("height") or 0):
                    chosen = fmt

    if not chosen:
        raise ValueError("No suitable 360p (or lower) format found for this video.")

    return chosen["url"]


@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "message": "ASTUBE API is running 🚀"}


@app.get("/video", tags=["Video"])
def get_video(id: str = Query(..., description="YouTube video ID or full YouTube URL")):
    try:
        video_id = extract_video_id(id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        mp4_url = get_360p_url(video_id)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"yt-dlp error: {str(e)}")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

    return PlainTextResponse(content=mp4_url)
