# Jimbo4prezz AI Content Studio

A personal AI content editing and repurposing studio. One source (livestream, VOD, Twitch clip, YouTube
video, gameplay) goes in. Out come intentional **long-form** and **short-form** edits, each built
separately for its destination: YouTube, YouTube Shorts, TikTok, Instagram Reels and X. You review and
approve every decision before export.

```
Source → Analyze → Understand → Long-form segments + short-form moments → Platform decision
      → Per-platform edit plans → Captions → Thumbnail/cover → Review → Render / Premiere / Photoshop
```

## Run it

Requirements: Python 3.10+, FFmpeg (with libass). Optional: Adobe Premiere Pro and Photoshop.

```bash
cd studio
pip install -r requirements.txt
pip install faster-whisper          # recommended: speech-to-text with word timestamps
export ANTHROPIC_API_KEY=...        # optional: Claude content understanding
python run.py                       # → http://127.0.0.1:8765
```

Everything is stored under `studio/data/`; use `--data-dir` to put it elsewhere, for example on a big
drive for 6-hour streams. That folder holds the SQLite library, renders, thumbnails and the Adobe
hand-off files. Restarting the app loses nothing. Jobs that were interrupted mid-run can be retried
from the dashboard.

## What each part does

| Page | Purpose |
|---|---|
| **Dashboard** | Active projects, recent recordings, processing jobs, strongest clips, review queues per platform, thumbnail and Adobe status. |
| **Long-Form Studio** | Upload or register 2–6 h recordings. Pick detected long-form segments and build a YouTube edit. The edit keeps the original order and removes only dead air, with an optional cold open. |
| **Shorts Studio** | The strongest standalone moments across all recordings, scored per platform. |
| **Twitch Clip Studio** | Drop one or many Twitch clips. Each clip's dead setup is trimmed and the reaction is kept. The edit ends on the payoff, and separate TikTok, Shorts, Reels and X versions all reference the same original clip. |
| **Social Clip Studio** | Review queue per destination (draft / approved / rejected). |
| **Plan editor** | Approve or reject, change timestamps (or set them from the playhead), restore removed sections, edit captions, change framing (crop / split-screen / picture-in-picture / blurred fill) with a live preview, edit punch-ins, title and description, platform or style. Also: regenerate, duplicate for another platform, render, export to Premiere, make a thumbnail. "Play the edit" previews the cut on the original without rendering. |
| **Thumbnail Studio** | Frames scored for sharpness, exposure, face and reaction loudness. Generates YouTube 1280×720 or vertical covers plus a Photoshop hand-off. |
| **Editing References** | Teach the studio your style from examples (see below). |
| **Style Profiles** | Jimbo4prezz – YouTube Long Form / TikTok / YouTube Shorts / Instagram Reels / X Clips, plus your own profiles. |
| **Content Library** | Every asset as a database record linked to its source. |
| **Export Center** | Source, content type, platform, format, resolution, duration, source timestamps, style, AI confidence, review status, thumbnail, captions, Premiere sequence and export method. Export one, several, or all approved. |
| **Adobe Integration** | What's installed, and a log of which engine handled every operation. |

## How editing decisions are made

* **Signals (always measured):** a loudness curve relative to a rolling baseline, sudden rises, silences,
  scene changes, faces, and transcript cues (laughter, exclamations, opinions, speech density).
* **Understanding:** with Claude configured, the timestamped transcript plus the signal peaks are
  sent to Claude. Claude proposes moments, hook / payoff timestamps, *Context → Build-up → Payoff* vs
  *Hook → Context → Payoff*, 12 quality scores, platform fit and long-form chapters. Without Claude, a
  heuristic engine produces the same structures from the signals. The engine is recorded on every
  moment.
* **Selective:** moments below the minimum score are dropped, and there's a per-hour ceiling (Settings).
  A stream with six good moments gives six.
* **Platform decision:** a moment becomes an edit only for platforms it fits, so it can land on one,
  several or none. Each platform edit is built from scratch:
  * its own trim to the profile's target length
  * its own structure
  * silence removal at the profile's pause tolerance (loud non-speech reactions are kept)
  * its own aspect and layout (X can stay 16:9 or go 1:1)
  * its own caption style and punch-in density
* **Framing:**
  * Face-camera content gets a crop that follows the face.
  * A small facecam over gameplay gets a split screen.
  * Gameplay without a facecam gets a centred action crop.
  * Anything unclear gets the full frame over a blurred fill, so nothing is cropped out.
  * Everything can be overridden.
* **Scores** are recommendations, not virality guarantees.

## Teaching it your style

Add references at any level: creator, video, section (`00:00–00:30`), technique, Twitch clip,
short-form example, thumbnail, caption, Premiere example, or just written instructions. Each one is
stored separately with the platforms, content types, aspects (pacing, cuts, hook, reactions, captions,
zooms, framing, audio, graphics) and profiles it applies to.

* **Measured from the file:** cuts per minute, shot length, longest pause, silence ratio, first-cut
  timing, burned-in caption presence and position, split-screen layout, face presence. For
  thumbnails: face side, saturation, contrast, accent colour.
* **Your notes** ("I like how quickly he gets to the funny moment", "zooms but not overused", "keeps
  his reactions in") become parameters. Claude does this when available, with keyword rules as a
  fallback. **Written notes always override measured values.**
* **Approvals and edits teach it too.** When you approve after changing length, start point, punch-ins
  or captions, the profile gets a small learned adjustment. You can see it, and reset it, on the
  profile page.

Only editing principles are extracted. No creator identity, branding, assets or exact edits are
copied.

## Adobe vs FFmpeg (what actually happens)

| Operation | Engine |
|---|---|
| Final video render (trim, layout, punch-ins, captions, loudness) | **FFmpeg** (labelled in the log) |
| Premiere Pro project | **FCP7 XML sequence**. Import with File › Import or `import_into_premiere.jsx`. Each cut references the *original* file at its source timecode. Includes basic-motion framing, a facecam on V2 for split / PiP, hook / payoff / punch-in markers, and SRT captions. |
| Photoshop thumbnail | Pillow composite + layer PNGs + `build_thumbnail.jsx`, which builds an editable layered **PSD** with a live text layer when run in Photoshop (File › Scripts › Browse…). |
| Lightroom | Extracted stills are full-quality files you can add to a catalog. No automation, because there's no supported local scripting API for this. |

Adobe apps can't be driven headlessly from this server. The studio generates files they open as
editable projects, and the Adobe page shows exactly which engine did each step. Premiere's framing
from XML is an approximation, so check it once in Premiere.

## Notes and limits

* **Transcripts:** captions and the best titles need a transcript. Install `faster-whisper`, or import
  an SRT/VTT per source (Twitch and YouTube can export these).
* **Reference links:** a link alone is stored with your notes. To have the video measured, upload the
  file (or the section you mean).
* **Render speed:** depends on the preset. `medium` runs at roughly real time for 1080p long-form on
  a typical CPU. Use `veryfast` in Settings for quick drafts.
* **Tests:** `python -m pytest -q` builds synthetic media and runs the whole pipeline, including
  real renders.
