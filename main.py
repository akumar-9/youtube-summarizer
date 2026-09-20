import os
import asyncio
import re
import httpx
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from google import genai
from google.genai import types
from supabase import create_client, Client
import jwt
from jwt import PyJWKClient

# ---- Config / startup checks ----
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable not set")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")  # service_role key — backend only, never expose
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY environment variables not set")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    # The frontend reads this custom header to know if a streamed response
    # was served from cache — browsers hide non-"simple" response headers
    # from JS unless explicitly exposed here.
    expose_headers=["X-Cache"],
)

client = genai.Client(api_key=API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Supabase signs tokens asymmetrically (RS256/ES256) and publishes the
# public keys at this well-known JWKS endpoint, so verification needs no
# shared secret — just the project's public keys, fetched and cached here.
_jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")

# In-memory response cache, keyed per-user so one person's cache hit never
# leaks another person's summary.
_cache: dict[str, str] = {}


class SummarizeRequest(BaseModel):
    url: str


_VIDEO_ID_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?v=)([\w-]{11})"),
    re.compile(r"(?:youtu\.be/)([\w-]{11})"),
    re.compile(r"(?:youtube\.com/shorts/)([\w-]{11})"),
]


def extract_video_id(url: str) -> str | None:
    for pattern in _VIDEO_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def build_prompt() -> str:
    return """
    You are an expert summarizer. Analyze this YouTube video and provide a comprehensive summary.
    Structure your answer with:
    - **TL;DR** (1-2 sentences capturing the essence)
    - **Key Takeaways** (Actionable bullet points)
    - **Chapters** (A bulleted list of key moments, each formatted exactly as
      "MM:SS — short description", using the actual timestamp from the video.
      Use HH:MM:SS instead if the video is over an hour. Pick 4-8 meaningful
      moments, not every topic change.)
    - **Action Items** (ONLY include this section if the video is genuinely
      instructional, tutorial, or how-to in nature — e.g. a recipe, a
      software walkthrough, a workout routine, a DIY guide. If it is not
      that kind of video — commentary, news, vlogs, interviews, reviews,
      entertainment — omit this section entirely, do not force it. When
      included, list concrete steps as a GitHub-flavored markdown checklist,
      one per line, e.g. "- [ ] Preheat the oven to 350°F". Keep each item
      short and actually actionable, not a restatement of a takeaway.)
    - **Detailed Summary** (Break down the main topics covered)
    """


async def fetch_video_metadata(url: str) -> dict:
    """Best-effort fetch of title/channel via YouTube's public oEmbed
    endpoint (needs no API key). The thumbnail is derived directly from the
    video ID instead, so it's available even if the oEmbed call fails."""
    video_id = extract_video_id(url)
    thumbnail_url = f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg" if video_id else None

    try:
        async with httpx.AsyncClient(
            timeout=5,
            follow_redirects=True,  # oEmbed sometimes 3xx-redirects; without this the body comes back empty
            headers={"User-Agent": "Mozilla/5.0 (compatible; SummarizerBot/1.0)"},
        ) as http_client:
            resp = await http_client.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "title": data.get("title"),
                "channel_name": data.get("author_name"),
                "thumbnail_url": data.get("thumbnail_url") or thumbnail_url,
            }
    except Exception as e:
        print(f"[metadata] oEmbed fetch failed for {url}: {e}")
        return {"title": None, "channel_name": None, "thumbnail_url": thumbnail_url}


def get_current_user(authorization: str = Header(None)) -> str:
    """Verify the Supabase-issued JWT sent by the frontend and return the user's id.
    Works the same regardless of whether they signed up with email/password
    or Google OAuth — Supabase issues the same kind of session token either way."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    token = authorization.split(" ", 1)[1]
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Your session has expired. Please log in again.")
    return payload["sub"]  # Supabase user id (uuid)


@app.get("/")
def health_check():
    return {"status": "ok", "message": "YouTube Summarizer API is running"}


@app.post("/summarize")
async def summarize_video(req: SummarizeRequest, user_id: str = Depends(get_current_user)):
    if not req.url or "youtu" not in req.url:
        raise HTTPException(status_code=400, detail="Please provide a valid YouTube URL.")

    cache_key = f"{user_id}:{req.url}"

    if cache_key in _cache:
        cached_text = _cache[cache_key]

        async def cached_stream():
            yield cached_text

        return StreamingResponse(cached_stream(), media_type="text/plain", headers={"X-Cache": "HIT"})

    prompt = build_prompt()
    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]
    video_part = types.Part(file_data=types.FileData(file_uri=req.url))
    gen_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="low"),
        max_output_tokens=1536,  # a bit more headroom now that chapters are part of the output
    )

    # Fetch title/channel/thumbnail in parallel with the model call —
    # it's an independent network request, so this adds zero extra latency.
    metadata_task = asyncio.create_task(fetch_video_metadata(req.url))

    async def stream_and_save():
        last_error = None
        for model_name in candidate_models:
            started = False
            full_text = ""
            try:
                stream = await client.aio.models.generate_content_stream(
                    model=model_name,
                    contents=[video_part, prompt],
                    config=gen_config,
                )
                async for chunk in stream:
                    if chunk.text:
                        started = True
                        full_text += chunk.text
                        yield chunk.text

                if started:
                    _cache[cache_key] = full_text
                    metadata = await metadata_task
                    try:
                        supabase.table("summaries").insert({
                            "user_id": user_id,
                            "video_url": req.url,
                            "summary": full_text,
                            "title": metadata["title"],
                            "channel_name": metadata["channel_name"],
                            "thumbnail_url": metadata["thumbnail_url"],
                        }).execute()
                    except Exception as e:
                        print(f"[history] failed to save summary: {e}")
                    return  # success — stop trying further models
            except Exception as e:
                last_error = str(e)
                if started:
                    # We already streamed partial content to the client for
                    # this model — switching models now would produce a
                    # garbled, duplicated response, so just stop here.
                    print(f"[summarize] {model_name} failed mid-stream: {last_error}")
                    return
                # Nothing streamed yet for this model — safe to try the next one.
                print(f"[summarize] {model_name} failed before streaming: {last_error}")
                continue

        # Every candidate model failed before producing any content.
        yield f"\n\n⚠️ Sorry, all models are unavailable right now. Details: {last_error}"

    return StreamingResponse(stream_and_save(), media_type="text/plain", headers={"X-Cache": "MISS"})


@app.get("/history")
def get_history(
    user_id: str = Depends(get_current_user),
    q: str | None = Query(default=None),
    sort: str = Query(default="desc", pattern="^(asc|desc)$"),
):
    query = (
        supabase.table("summaries")
        .select("id, video_url, summary, title, channel_name, thumbnail_url, created_at")
        .eq("user_id", user_id)
    )
    if q:
        # Search across title and channel name; video_url as a fallback for
        # entries saved before metadata existed.
        safe_q = q.replace(",", " ").replace("%", "")
        query = query.or_(f"title.ilike.%{safe_q}%,channel_name.ilike.%{safe_q}%,video_url.ilike.%{safe_q}%")

    result = query.order("created_at", desc=(sort == "desc")).limit(50).execute()
    return {"history": result.data}


@app.delete("/history/{summary_id}")
def delete_history_item(summary_id: str, user_id: str = Depends(get_current_user)):
    # Scoping the delete to both id AND user_id means a user can never
    # delete anyone else's row, even if they guessed a valid id.
    result = (
        supabase.table("summaries")
        .delete()
        .eq("id", summary_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Summary not found.")
    return {"deleted": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
