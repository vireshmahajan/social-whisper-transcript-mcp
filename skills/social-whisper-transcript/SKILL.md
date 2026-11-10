---
name: social-whisper-transcript
description: Transcribe the speech in a video or audio link (YouTube, Instagram, TikTok, X/Twitter, Facebook, Vimeo, podcasts) with a self-hosted Whisper server, then summarise, quote, translate or save it. Use when the user shares a video/audio URL and wants a transcript, summary, quotes, subtitles or notes, or asks "what does this video say".
---

# Social Whisper Transcript

Uses the `social-whisper-transcript` MCP server (tools: `transcribe_url`, `get_transcript`,
`get_transcript_file`, `list_transcripts`). Setup: https://github.com/vireshmahajan/social-whisper-transcript-mcp

## How to use it

1. Call `transcribe_url(url)` with the single video link the user gave (not a playlist or channel).
   - Pass `language` (e.g. `"hi"`, `"de"`) when you know it; leave it empty to auto-detect.
   - Use `model="large-v3"` for non-English speech, strong accents, names or song lyrics. It's slower but more accurate.
2. Short clips come back finished. For long videos you get a job id and progress: tell the user roughly how long it
   will take, then call `get_transcript(job_id, wait_seconds=300)` until it is done. Don't start a second job for the same URL;
   repeated requests reuse the earlier transcript.
3. Work from the transcript: summarise, answer questions, pull quotes with timestamps (`output="timestamped"`).
4. When the user wants a file (notes, subtitles, sharing), call `get_transcript_file(job_id, format=...)`:
   `txt` (readable, default), `md` (notes), `srt`/`vtt` (subtitles), `json` (everything). Give the user the download link;
   it works for 24 hours without a token.

## Notes

- Long transcripts are paged: follow the `offset=` hint in the result to read the rest.
- "(No speech was detected…)" means the audio really has no words (music only, silence).
- Errors such as "Sign in to confirm you're not a bot" or "login required" mean the server needs a `cookies.txt` or a restart;
  tell the user instead of retrying in a loop.
