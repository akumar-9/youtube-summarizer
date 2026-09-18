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

# Simple in-process cache so repeat requests for the same video are instant.
# Resets on restart / free-tier cold start — fine for cutting down duplicate
# calls within a session, not meant as durable storage.
_cache: dict[str, str] = {}


class SummarizeRequest(BaseModel):
    url: str


@app.get("/")
def health_check():
    return {"status": "ok", "message": "YouTube Summarizer API is running"}


@app.post("/summarize")
async def summarize_video(req: SummarizeRequest):
    if not req.url or "youtu" not in req.url:
        raise HTTPException(status_code=400, detail="Please provide a valid YouTube URL.")

    if req.url in _cache:
        return {"summary": _cache[req.url], "cached": True}

    prompt = """
    You are an expert summarizer. Analyze this YouTube video and provide a comprehensive summary.
    Structure your answer with:
    - **TL;DR** (1-2 sentences capturing the essence)
    - **Key Takeaways** (Actionable bullet points)
    - **Detailed Summary** (Break down the main topics covered)
    """

    # Free-tier-friendly model lineup, cheapest/fastest first.
    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]
    last_error = None

    video_part = types.Part(file_data=types.FileData(file_uri=req.url))

    # Turn "thinking" down as low as each model allows. These are Gemini 3
    # series models that do an internal reasoning pass by default — for a
    # straightforward summarization task that reasoning mostly adds latency,
    # not quality. "low"/"minimal" cuts response time noticeably.
    gen_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="low"),
        max_output_tokens=1024,  # shorter cap = faster generation
    )

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=[video_part, prompt],
                    config=gen_config,
                )
                _cache[req.url] = response.text
                return {"summary": response.text, "cached": False}
            except Exception as e:
                err_str = str(e)
                last_error = err_str
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    # Short backoff, single retry — don't compound latency.
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    break
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    break
                elif "thinking_level" in err_str or "INVALID_ARGUMENT" in err_str:
                    # A candidate model may not support thinking_level (e.g.
                    # if it's not a Gemini 3-series model) — retry it once
                    # without the config rather than burning the fallback chain.
                    try:
                        response = await client.aio.models.generate_content(
                            model=model_name,
                            contents=[video_part, prompt],
                        )
                        _cache[req.url] = response.text
                        return {"summary": response.text, "cached": False}
                    except Exception as e2:
                        last_error = str(e2)
                        break
                else:
                    raise HTTPException(status_code=400, detail=err_str)

    raise HTTPException(
        status_code=503,
        detail=f"All candidate models failed. Details: {last_error}",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
