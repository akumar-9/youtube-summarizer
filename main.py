import asyncio
from google.genai.errors import APIError

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

    # Primary and fallback models if one cluster hits 503
    candidate_models = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.8-flash"]

    last_error = None
    for model_name in candidate_models:
        for attempt in range(2):  # Try each model up to twice
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
                # If high demand / 503, wait briefly and retry or try next model
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    await asyncio.sleep(2)
                    continue
                else:
                    # If it's a 400 (e.g. invalid video), don't retry
                    raise HTTPException(status_code=400, detail=err_str)

    raise HTTPException(
        status_code=503, 
        detail=f"All available Gemini models are currently experiencing high demand. Details: {last_error}"
    )
