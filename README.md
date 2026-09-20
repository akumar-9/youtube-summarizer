# YouTube, Summarized

Paste a YouTube link, get a structured AI summary — TL;DR, key takeaways,
timestamped chapters you can click to jump to, and a detailed breakdown —
without watching the whole video. Sign in to keep a searchable history of
everything you've summarized, and use the companion browser extension to
summarize straight from YouTube itself.

## Features

- **AI summaries via Gemini**, streamed live as they generate
- **Clickable timestamped chapters** — jump straight to the relevant moment in the video
- **Accounts** — email/password or Google sign-in (Supabase Auth)
- **History** — searchable, sortable (newest/oldest), deletable, with video thumbnail/title/channel
- **Response caching** — instant results for a video you've already summarized
- **Browser extension** — a "Summarize" button right on YouTube's watch page
- **Mobile-friendly** — installable to your phone's home screen (PWA-lite)

## Architecture

This project is intentionally split across a few free-tier services rather
than one big app:

| Piece | Where it lives | Why |
|---|---|---|
| Frontend (`index.html`) | GitHub Pages | Free static hosting |
| Backend API (`main.py`) | Render | Free web service, runs FastAPI |
| Auth + database | Supabase | Free Postgres + auth, handles Google OAuth |
| Transactional email | Resend | Supabase's default mailer is rate-limited to 2 emails/hour |
| AI summarization | Google Gemini API | Free tier covers light personal use |
| YouTube metadata | YouTube oEmbed (public, no key) | Title/channel/thumbnail, no API key needed |

The frontend never talks to Gemini or Supabase's database directly — it
calls your FastAPI backend, which holds the only credentials that actually
need to stay secret (see **Secrets**, below).

## Setup

### 1. Google Gemini API key
Grab a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

### 2. Supabase project
1. Create a project at [supabase.com](https://supabase.com).
2. Run this in the SQL editor to create the history table:
   ```sql
   create table summaries (
     id uuid primary key default gen_random_uuid(),
     user_id uuid references auth.users(id) not null,
     video_url text not null,
     summary text not null,
     title text,
     channel_name text,
     thumbnail_url text,
     created_at timestamptz default now()
   );

   alter table summaries enable row level security;

   create policy "Users can view their own summaries"
     on summaries for select
     using (auth.uid() = user_id);
   ```
3. **Enable Google sign-in** (optional but recommended): Authentication →
   Providers → Google. You'll need an OAuth Client ID/Secret from
   [Google Cloud Console](https://console.cloud.google.com/) with this
   redirect URI authorized:
   ```
   https://<your-project-ref>.supabase.co/auth/v1/callback
   ```
4. **Fix the email rate limit** (needed if you use email/password sign-up
   at all): Supabase's built-in mailer only sends 2 emails/hour. Set up
   [Resend](https://resend.com) (free tier, 3,000 emails/month) as a
   custom SMTP provider under Authentication → Settings → SMTP Settings:
   - Host: `smtp.resend.com`, Port: `587`
   - Username: `resend`, Password: your Resend API key
   - Sender: `onboarding@resend.dev` for testing, or your own verified domain for real users
5. Under Authentication → URL Configuration, add your GitHub Pages URL to
   the **Redirect URLs** allow-list.

### 3. Backend (Render)
1. Push this repo, create a new **Web Service** on [Render](https://render.com) pointing at it.
2. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Add environment variables (Render dashboard → Environment):
   - `GEMINI_API_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (the **service_role** key — not the anon key)
4. Optional: under Settings → Build & Deploy → Build Filters, restrict
   auto-deploys to backend files only (`main.py`, `requirements.txt`) so
   frontend-only commits don't trigger unnecessary rebuilds.

### 4. Frontend (GitHub Pages)
1. In `index.html`, set:
   ```js
   const SUPABASE_URL = "https://your-project-ref.supabase.co";
   const SUPABASE_ANON_KEY = "your-anon-public-key";
   const API_BASE = "https://your-backend.onrender.com";
   ```
2. Enable GitHub Pages on this repo (Settings → Pages).

### 5. Browser extension (optional)
See `extension-README.md` (place it inside your `extension/` folder as
`README.md`) — edit `extension/config.js` with your GitHub Pages URL, then
load it unpacked via `chrome://extensions` → Developer mode → Load unpacked.

## Secrets — what's safe to commit and what isn't

This matters more than usual here because one of the "keys" in this
project is *meant* to be public.

**Safe to commit / already in the code:**
- `SUPABASE_URL` and `SUPABASE_ANON_KEY` in `index.html` — the anon key is
  designed to be exposed client-side. Your data is protected by the Row
  Level Security policy above, not by hiding this key.
- The extension's `APP_URL` — just your public site address.

**Never commit — environment variables only:**
- `GEMINI_API_KEY`
- `SUPABASE_SERVICE_KEY` — this one **bypasses** Row Level Security
  entirely. Treat it like a database root password.
- Your Resend API key and Google OAuth Client Secret (these live in the
  Supabase dashboard, never in this repo at all).

A `.gitignore` and `.env.example` are included so a local `.env` file
(if you use one for local dev) never gets committed by accident — only
copy real values into `.env`, never into `.env.example`.

## Local development

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...
export SUPABASE_URL=...
export SUPABASE_SERVICE_KEY=...
python main.py
```

Then open `index.html` directly in a browser (or serve it with any static
file server) pointed at `http://localhost:8000` as `API_BASE`.

## Known limitations

- In-memory response cache resets whenever Render's free-tier service
  restarts or spins down from inactivity.
- Free-tier cold starts on Render mean the first request after idle time
  can take 30-60 seconds.
- YouTube's watch history isn't accessible via any public API — there's no
  way to auto-import what you've watched, only what you explicitly
  summarize going forward (or import via a Google Takeout export, if you
  build that feature later).
