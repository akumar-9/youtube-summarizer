import os
import asyncio
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
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
)

client = genai.Client(api_key=API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Supabase now signs tokens asymmetrically (RS256/ES256) and publishes the
# public keys at this well-known JWKS endpoint, so verification needs no
# shared secret — just the project's public keys, fetched and cached here.
_jwk_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")

# In-memory response cache, keyed per-user so one person's cache hit never
# leaks another person's summary.
_cache: dict[str, str] = {}


class SummarizeRequest(BaseModel):
    url: str


def get_current_user(authorization: str = Header(None)) -> str:
    """Verify the Supabase-issued JWT sent by the frontend and return the user's id."""
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
        return {"summary": _cache[cache_key], "cached": True}

    prompt = """
    You are an expert summarizer. Analyze this YouTube video and provide a comprehensive summary.
    Structure your answer with:
    - **TL;DR** (1-2 sentences capturing the essence)
    - **Key Takeaways** (Actionable bullet points)
    - **Detailed Summary** (Break down the main topics covered)
    """

    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]
    last_error = None
    video_part = types.Part(file_data=types.FileData(file_uri=req.url))
    gen_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="low"),
        max_output_tokens=1024,
    )

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=[video_part, prompt],
                    config=gen_config,
                )
                summary_text = response.text
                _cache[cache_key] = summary_text

                # Persist to history. Non-fatal if this write fails — the
                # user still gets their summary either way.
                try:
                    supabase.table("summaries").insert({
                        "user_id": user_id,
                        "video_url": req.url,
                        "summary": summary_text,
                    }).execute()
                except Exception:
                    pass

                return {"summary": summary_text, "cached": False}
            except Exception as e:
                err_str = str(e)
                last_error = err_str
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    break
                elif "404" in err_str or "NOT_FOUND" in err_str:
                    break
                elif "thinking_level" in err_str or "INVALID_ARGUMENT" in err_str:
                    try:
                        response = await client.aio.models.generate_content(
                            model=model_name, contents=[video_part, prompt],
                        )
                        summary_text = response.text
                        _cache[cache_key] = summary_text
                        try:
                            supabase.table("summaries").insert({
                                "user_id": user_id,
                                "video_url": req.url,
                                "summary": summary_text,
                            }).execute()
                        except Exception:
                            pass
                        return {"summary": summary_text, "cached": False}
                    except Exception as e2:
                        last_error = str(e2)
                        break
                else:
                    raise HTTPException(status_code=400, detail=err_str)

    raise HTTPException(
        status_code=503,
        detail=f"All candidate models failed. Details: {last_error}",
    )


@app.get("/history")
def get_history(user_id: str = Depends(get_current_user)):
    result = (
        supabase.table("summaries")
        .select("id, video_url, summary, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"history": result.data}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
