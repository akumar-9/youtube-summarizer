import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

# Fail fast if the API key isn't configured, instead of a cryptic auth
# error on the first request.
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable not set")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=API_KEY)


class SummarizeRequest(BaseModel):
    url: str


@app.get("/")
def health_check():
    return {"status": "ok", "message": "YouTube Summarizer API is running"}


@app.post("/summarize")
async def summarize_video(req: SummarizeRequest):
    if not req.url or "youtu" not in req.url:
        raise HTTPException(status_code=400, detail="Please provide a valid YouTube URL.")

    prompt = """
    You are an expert summarizer. Analyze this YouTube video and provide a comprehensive summary.
    Structure your answer with:
    - **TL;DR** (1-2 sentences capturing the essence)
    - **Key Takeaways** (Actionable bullet points)
    - **Detailed Summary** (Break down the main topics covered)
    """

    # Free-tier-friendly model lineup, cheapest/fastest first.
    # gemini-3.5-flash-lite and gemini-3.6-flash both have generous free
    # quotas on the Gemini API free tier; gemini-3.8-flash is kept as a
    # stronger fallback if the lighter models are unavailable.
    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]
    last_error = None

    # YouTube URLs are passed via file_data with no mime_type — Gemini
    # auto-detects it. Using Part.from_uri with a wildcard mime type like
    # "video/*" is not valid and will cause a 400 from the API.
    video_part = types.Part(file_data=types.FileData(file_uri=req.url))

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                # Use the async client (client.aio) so this blocking-style
                # call doesn't stall the FastAPI event loop for other
                # requests (including the health check) while it runs.
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=[video_part, prompt],
                )
                return {"summary": response.text}
            except Exception as e:
                err_str = str(e)
                last_error = err_str
                # Server overloaded (503): brief backoff, then retry same model.
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    await asyncio.sleep(2)
                    continue
                # Model deprecated / not found: move to the next candidate immediately.
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    break
                # Anything else (bad request, invalid URL, quota exceeded): fail fast.
                else:
                    raise HTTPException(status_code=400, detail=err_str)

    raise HTTPException(
        status_code=503,
        detail=f"All candidate models failed. Details: {last_error}",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
