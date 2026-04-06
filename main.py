
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
import yt_dlp
import re
import os

app = FastAPI(
    title="ASTUBE API",
    description="YouTube video URL extractor – returns MP4 direct URL",
    version="1.0.0"
)

COOKIES_PATH = os.path.join(os.path.dirname(__file__), "cookies.txt")

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


def get_video_url(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best",
        "cookiefile": COOKIES_PATH if os.path.exists(COOKIES_PATH) else None,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    video_url = info.get("url") or info.get("formats", [{}])[-1].get("url")
    if not video_url:
        raise ValueError("No URL found.")
    return video_url


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
        mp4_url = get_video_url(video_id)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"yt-dlp error: {str(e)}")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

    return PlainTextResponse(content=mp4_url)
