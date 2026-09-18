import os
import re
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai

app = FastAPI()

# Allow cross-origin requests from your frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Gemini Client (set GEMINI_API_KEY in your environment)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class SummarizeRequest(BaseModel):
    url: str

def extract_video_id(url: str) -> str:
    """Extracts the 11-character video ID from common YouTube URL formats."""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'youtu\.be\/([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError("Invalid YouTube URL")

@app.post("/summarize")
async def summarize_video(req: SummarizeRequest):
    try:
        video_id = extract_video_id(req.url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL provided.")

    # 1. Fetch transcript from YouTube
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        transcript_text = " ".join([entry['text'] for entry in transcript_list])
    except Exception as e:
        raise HTTPException(
            status_code=400, 
            detail="Could not retrieve transcript. The video may not have captions enabled."
        )

    # 2. Summarize using Gemini
    prompt = f"""
    You are an expert summarizer. Provide a clear, concise summary of the following YouTube video transcript.
    Structure your answer with:
    - A brief TL;DR (1-2 sentences)
    - Key Takeaways (bullet points)
    - Detailed Summary

    Transcript:
    {transcript_text}
    """

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return {"summary": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Summarization failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
