import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

app = FastAPI()

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

    try:
        # Pass the YouTube URL directly into the contents list as a Part
        response = client.models.generate_content(
            model='gemini-2.5-flash',
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
        print(f"Gemini processing error: {repr(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to process video with Gemini: {str(e)}"
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
