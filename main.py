import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

# Enable CORS for browser requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

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

    # Pool of active models to try in case of 503 capacity spikes
    candidate_models = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.6-flash"]

    last_error = None
    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_uri(
                            file_uri=req.url,
                            mime_type="video/*"
                        ),
                        prompt
                    ]
                )
                return {"summary": response.text}
            except Exception as e:
                err_str = str(e)
                last_error = err_str
                # If server is overloaded (503), wait 2s and retry or switch model
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    await asyncio.sleep(2)
                    continue
                else:
                    # Fail fast on bad requests (invalid URL, permissions, etc.)
                    raise HTTPException(status_code=400, detail=err_str)

    raise HTTPException(
        status_code=503,
        detail=f"All models temporarily unavailable. Details: {last_error}"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
