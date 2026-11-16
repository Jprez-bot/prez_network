"""End-to-end tests for the Jimbo4prezz studio (synthetic media, no network).

Run:  cd studio && python -m pytest -q
"""
import hashlib
import json
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("studio")
    os.environ["JIMBO_DATA_DIR"] = str(base / "data")
    import importlib

    from jimbo import config
    importlib.reload(config)
    from jimbo import db
    db.reset_connection()
    import make_fixtures
    fx = make_fixtures.make(base / "fx")
    config.save_settings({"render_preset": "ultrafast", "llm_enabled": False})
    from fastapi.testclient import TestClient

    from jimbo.api import app
    with TestClient(app) as client:
        yield {"client": client, "fx": fx, "base": base}


def wait_job(client, job_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "interrupted"):
            assert j["status"] == "done", j.get("error")
            return j
        time.sleep(0.3)
    raise TimeoutError(job_id)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- unit level

def test_note_rules_from_spec_examples():
    from jimbo import profiles
    p, labels = profiles.params_from_notes(
        "I like how quickly he gets to the funny moment. I like the zooms but I don't want them overused. "
        "I like how the editor keeps his reactions in. I like how the clips feel natural instead of looking "
        "like an overedited TikTok. I like how the editor removes unnecessary conversation.")
    assert p["structure"] == "hook_first" and p["hook_max_seconds"] <= 1.5
    assert p["zoom_per_minute"] == 2.0  # zooms, but sparingly — not the "likes zooms" rule
    assert p["reaction_preservation"] >= 0.95
    assert p["transitions"] == "none" and p["sfx_frequency"] == 0
    assert p["silence_tolerance"] <= 0.45
    assert len(labels) >= 5


def test_subtitle_parsing_and_sentences():
    from jimbo import transcribe
    segs = transcribe.parse_subtitles("1\n00:00:01,000 --> 00:00:03,000\nHello there chat.\n\n"
                                      "2\n00:00:05,500 --> 00:00:07,000\nNO WAY!\n")
    assert len(segs) == 2 and segs[1]["start"] == 5.5
    sents = transcribe.sentences(segs)
    assert [s["text"] for s in sents] == ["Hello there chat.", "NO WAY!"]
    from jimbo.analyzer import snap_start
    assert snap_start(2.0, sents) == 1.0           # inside a sentence → its start
    assert abs(snap_start(4.0, sents) - 5.2) < 1e-6  # in a pause → just before the next sentence


def test_caption_mapping_follows_the_cut():
    from jimbo import captions
    lines = [{"start": 10, "end": 12, "text": "hook line", "emph": [], "words": []},
             {"start": 2, "end": 4, "text": "setup", "emph": [], "words": []}]
    tl = [{"start": 10, "end": 12, "out_start": 0, "out_end": 2},
          {"start": 1, "end": 13, "out_start": 2, "out_end": 14}]
    out = captions.map_to_output(lines, tl)
    assert [(round(o["start"], 2), o["text"]) for o in out] == [(0, "hook line"), (3, "setup"), (11, "hook line")]


def test_crop_box_stays_inside_frame():
    from jimbo.render import _crop_box
    w, h, x, y = _crop_box(1920, 1080, 0.98, 0.5, 1.0, 9 / 16)
    assert x + w <= 1920 and y + h <= 1080 and abs(w / h - 9 / 16) < 0.01


# ---------------------------------------------------------------- workflow

def test_twitch_clip_workflow(env):
    c, fx = env["client"], env["fx"]
    before = sha(fx["clip"])
    with open(fx["clip"], "rb") as f:
        r = c.post("/api/sources/upload", files=[("files", ("my_twitch_clip.mp4", f, "video/mp4"))],
                   data={"kind": "twitch_clip", "auto_analyze": "false"})
    assert r.status_code == 200, r.text
    sid = r.json()[0]["source_id"]
    with open(fx["clip_srt"], "rb") as f:
        assert c.post(f"/api/sources/{sid}/transcript", files={"file": ("clip.srt", f)}).json()["segments"] == 5
    job = c.post(f"/api/sources/{sid}/analyze", json={"auto_package": True}).json()["job_id"]
    wait_job(c, job)
    src = c.get(f"/api/sources/{sid}").json()
    shorts = [m for m in src["moments"] if m["kind"] == "short"]
    assert len(shorts) == 1
    m = shorts[0]
    assert m["hook_start"] >= 20 and m["payoff_end"] <= 31  # the "NO WAY" reaction
    plans = {p["platform"]: p for p in src["plans"]}
    assert set(plans) == {"tiktok", "youtube_shorts", "instagram_reels", "x"}
    # Platform-specific: not one edit resized four times.
    assert plans["tiktok"]["format"]["aspect"] == "9:16" and plans["x"]["format"]["aspect"] == "16:9"
    assert plans["x"]["structure"] == "context_first" and plans["tiktok"]["structure"] == "hook_first"
    assert plans["x"]["duration"] < plans["tiktok"]["duration"]
    for p in plans.values():
        # dead setup at the start of the clip is trimmed, and nothing past the payoff + tail is kept
        assert p["source_range"][0] >= 10 and p["source_range"][1] <= 31.5
    # Every version references the same original clip.
    assert {p["source_id"] for p in plans.values()} == {sid}
    assert sha(src["path"]) == before  # stored copy is byte-identical to the upload

    # -- review: edit, approve, learn
    tik = c.get(f"/api/plans/{plans['tiktok']['id']}").json()
    segs = tik["plan"]["segments"]
    body = [s for s in segs if s["role"] != "hook"]
    body[0]["start"] -= 1.0  # keep a bit more context
    new = c.patch(f"/api/plans/{tik['id']}", json={"segments": segs, "zooms": [], "title": "He didn't see me coming"}).json()
    assert new["title"] == "He didn't see me coming" and new["plan"]["zooms"] == []
    assert new["plan"]["captions"]["lines"], "captions are rebuilt from the transcript after a re-cut"
    c.post(f"/api/plans/{tik['id']}/status", json={"status": "approved"})
    prof = next(p for p in c.get("/api/profiles").json() if p["id"] == tik["profile_id"])
    assert prof["learned"]["counts"]["approved"] == 1
    assert "context_preroll" in prof["learned"]["adjust"]

    # -- layout override + preview image
    lay = dict(tik["plan"]["layout"], mode="split", face={"cx": 0.85, "cy": 0.8, "zoom": 0.3})
    c.patch(f"/api/plans/{tik['id']}", json={"layout": lay})
    img = c.get(f"/api/plans/{tik['id']}/preview")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    # -- render the real video
    job = c.post(f"/api/plans/{tik['id']}/render").json()["job_id"]
    wait_job(c, job)
    plan = c.get(f"/api/plans/{tik['id']}").json()
    rnd = plan["renders"][0]
    assert rnd["status"] == "done" and (rnd["width"], rnd["height"]) == (1080, 1920)
    assert abs(rnd["duration"] - plan["duration"]) < 0.6
    from jimbo import media
    info = media.probe(rnd["path"])
    assert info["has_audio"] and (info["width"], info["height"]) == (1080, 1920)
    assert Path(rnd["path"]).with_suffix(".srt").exists()

    # -- Premiere hand-off
    prem = c.post(f"/api/plans/{tik['id']}/premiere").json()
    root = ET.parse(prem["xml"]).getroot()
    seq = root.find("sequence")
    assert seq.find(".//samplecharacteristics/width").text == "1080"
    clips = seq.findall("./media/video/track[1]/clipitem")
    assert len(clips) == len([s for s in plan["timeline"]])
    assert "file://localhost" in root.find(".//pathurl").text
    assert len(seq.findall("./media/video/track")) == 2  # split layout → facecam on V2
    assert Path(prem["srt"]).read_text().startswith("1\n")

    # -- thumbnails / covers with Photoshop hand-off
    th = c.post("/api/thumbnails", json={"plan_id": tik["id"]}).json()
    assert th and Path(th[0]["path"]).exists()
    assert Path(th[0]["layers"]["jsx"]).read_text().startswith("// Jimbo4prezz")

    # -- duplicate for another platform + regenerate keep history
    dup = c.post(f"/api/plans/{tik['id']}/duplicate", json={"platform": "youtube"}).json()["plan_id"]
    assert c.get(f"/api/plans/{dup}").json()["plan"]["format"]["aspect"] == "16:9"
    reg = c.post(f"/api/plans/{plans['x']['id']}/regenerate", json={}).json()["plan_id"]
    rp = c.get(f"/api/plans/{reg}").json()
    assert rp["version"] == 2 and rp["parent_id"] == plans["x"]["id"]

    # -- export center: everything approved
    out = c.post("/api/export", json={"scope": "approved", "platforms": ["tiktok"]}).json()
    for j in out["jobs"]:
        wait_job(c, j["job_id"])
    row = next(p for p in c.get("/api/plans?status=approved").json() if p["id"] == tik["id"])
    assert row["render"]["status"] == "done" and row["premiere"] and row["thumbnail"]

    ops = c.get("/api/operations").json()
    engines = {s["engine"] for s in ops["summary"]}
    assert {"ffmpeg", "adobe-premiere-export", "pillow"} <= engines


def test_long_recording_registered_in_place(env):
    c, fx = env["client"], env["fx"]
    before = sha(fx["stream"])
    r = c.post("/api/sources/register", json={"path": str(fx["stream"]), "kind": "recording", "auto_analyze": False}).json()
    sid = r["source_id"]
    with open(fx["stream_srt"], "rb") as f:
        c.post(f"/api/sources/{sid}/transcript", files={"file": ("s.srt", f)})
    wait_job(c, c.post(f"/api/sources/{sid}/analyze", json={"auto_package": True}).json()["job_id"])
    src = c.get(f"/api/sources/{sid}").json()
    assert src["stored_copy"] == 0 and src["path"] == str(Path(fx["stream"]).resolve())
    shorts = [m for m in src["moments"] if m["kind"] == "short"]
    longs = [m for m in src["moments"] if m["kind"] == "long"]
    assert shorts and longs
    assert any(m["start"] <= 150 <= m["end"] for m in shorts), "the 150 s reaction is found"
    # Selective: a 4-minute recording doesn't produce a pile of weak clips.
    assert len(shorts) <= 2
    lf = c.post(f"/api/sources/{sid}/longform", json={"chapter_ids": [longs[0]["id"]], "cold_open": True}).json()["plan_id"]
    p = c.get(f"/api/plans/{lf}").json()
    assert p["form"] == "long" and p["plan"]["format"]["aspect"] == "16:9"
    assert p["plan"]["segments"][0]["role"] == "hook"
    body = [s for s in p["plan"]["segments"] if s["role"] != "hook"]
    assert body == sorted(body, key=lambda s: s["start"]), "long-form keeps chronology"
    assert p["duration"] < 240  # dead air removed
    assert sha(fx["stream"]) == before, "original recording never modified"
    # manual moment
    mm = c.post("/api/moments", json={"source_id": sid, "start": 95, "end": 120, "title": "setup bit"}).json()
    assert mm["overall_score"] > 0 and set(mm["platform_scores"]) == {"youtube_shorts", "tiktok", "instagram_reels", "x"}


def test_references_shape_profiles(env):
    c, fx = env["client"], env["fx"]
    tik = next(p for p in c.get("/api/profiles").json() if p["platform"] == "tiktok")
    before = tik["effective"]
    r = c.post("/api/references", data={
        "level": "clip", "name": "Twitch clip cuts I like", "creator": "Agent",
        "notes": "I like how quickly he gets to the funny moment. I like the zooms but I don't want them overused.",
        "platforms": json.dumps(["tiktok"]), "content_types": "[]", "aspects": "", "profile_ids": "[]"},
        files={"file": ("ref.mp4", open(fx["clip"], "rb"), "video/mp4")}).json()
    wait_job(c, r["job_id"])
    ref = next(x for x in c.get("/api/references").json() if x["id"] == r["ref_id"])
    assert tik["id"] in ref["profile_ids"]
    ex = ref["extracted"]
    assert ex["measured"]["duration"] > 40 and "cuts_per_minute" in ex["measured"]
    assert ex["note_params"]["zoom_per_minute"] == 2.0
    after = next(p for p in c.get("/api/profiles").json() if p["id"] == tik["id"])["effective"]
    learned = after["_layers"]["learned"].get("zoom_per_minute", 0)  # from the earlier approval (zooms removed)
    assert abs(after["zoom_per_minute"] - max(0, 2.0 + learned)) < 1e-6 and after["structure"] == "hook_first"
    assert before["structure"] != "hook_first" or before["zoom_per_minute"] != after["zoom_per_minute"]
    # Written instructions beat what was measured from the video.
    assert after["_layers"]["written"]["zoom_per_minute"] == 2.0
    # Aspect-limited reference only touches caption params.
    r2 = c.post("/api/references", data={"level": "caption", "name": "caption style", "notes": "Big bold captions, one word at a time",
                                         "platforms": json.dumps(["tiktok"]), "content_types": "[]", "aspects": "captions",
                                         "profile_ids": json.dumps([tik["id"]])}).json()
    wait_job(c, r2["job_id"])
    ref2 = next(x for x in c.get("/api/references").json() if x["id"] == r2["ref_id"])
    assert set(ref2["extracted"]["note_params"]) <= {k for k in ref2["extracted"]["note_params"] if k.startswith("caption")}
    assert ref2["extracted"]["note_params"]["caption_words_per_chunk"] == 1
    # Thumbnail reference feeds the thumbnail style profile.
    from jimbo import media
    img = env["base"] / "thumbref.jpg"
    media.extract_frame(fx["clip"], 5, img)
    r3 = c.post("/api/references", data={"level": "thumbnail", "name": "thumb", "notes": "I like how simple it is, face on the left",
                                         "platforms": "[]", "content_types": "[]", "profile_ids": "[]"},
                files={"file": ("t.jpg", open(img, "rb"), "image/jpeg")}).json()
    wait_job(c, r3["job_id"])
    style = c.get("/api/thumbnail-style").json()
    assert style["text_max_words"] == 2 and style["subject_side"] == "left"


def test_restart_keeps_everything(env):
    c = env["client"]
    counts = {k: len(c.get(u).json()) for k, u in (("sources", "/api/sources"), ("plans", "/api/plans"),
                                                   ("refs", "/api/references"), ("lib", "/api/library"))}
    from jimbo import db, jobs
    db.reset_connection()
    jobs.recover_interrupted()
    again = {k: len(c.get(u).json()) for k, u in (("sources", "/api/sources"), ("plans", "/api/plans"),
                                                  ("refs", "/api/references"), ("lib", "/api/library"))}
    assert counts == again and counts["sources"] >= 2


def test_ui_served(env):
    c = env["client"]
    assert "Jimbo4prezz" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/api/files", params={"path": "/etc/passwd"}).status_code == 404


def test_claude_understanding_path(env, monkeypatch):
    """The Claude branch (mocked): proposals are validated, blended with measured quality, and logged."""
    from jimbo import analyzer, db, llm, planner, transcribe
    c, fx = env["client"], env["fx"]
    with open(fx["clip"], "rb") as f:
        sid = c.post("/api/sources/upload", files=[("files", ("claude_clip.mp4", f, "video/mp4"))],
                     data={"kind": "twitch_clip", "auto_analyze": "false"}).json()[0]["source_id"]
    db.upsert("transcripts", "source_id", source_id=sid, engine="imported", language="en",
              segments=transcribe.parse_subtitles(Path(fx["clip_srt"]).read_text()), created_at=db.now())
    scores = {k: 80 for k in analyzer.SCORE_KEYS}
    fake = {"summary": "Jimbo sneaks up on an opponent and loses it.", "content_type": "gaming",
            "moments": [{"start": 11.5, "end": 30.0, "hook_start": 22.0, "hook_end": 25.0, "payoff_end": 29.0,
                         "structure": "hook_first", "title": "He never saw it coming", "why": "Big reaction, clear setup.",
                         "content_type": "gaming", "scores": scores,
                         "platforms": {"youtube_shorts": 88, "tiktok": 91, "instagram_reels": 84, "x": 62},
                         "emphasis_words": ["WAY"]},
                        {"start": 50, "end": 90, "hook_start": 50, "hook_end": 52, "payoff_end": 90,  # out of range → dropped
                         "structure": "context_first", "title": "bogus", "why": "", "content_type": "gaming",
                         "scores": scores, "platforms": {"youtube_shorts": 90, "tiktok": 90, "instagram_reels": 90, "x": 90},
                         "emphasis_words": []}],
            "chapters": []}
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: fake if "moments" in str(a[2]) else
                        {"items": [{"platform": p, "title": "He never saw it coming", "description": "d", "tags": ["t"],
                                    "thumbnail_text": "NEVER SAW IT"} for p in ("youtube_shorts", "tiktok", "instagram_reels")]})
    res = analyzer.analyze_source(sid)
    assert res["engine"] == "claude" and res["moments"] == 1
    m = db.query("SELECT * FROM moments WHERE source_id=? AND kind='short'", (sid,))[0]
    assert m["title"] == "He never saw it coming" and m["engine"] == "claude"
    out = planner.create_package(m["id"])
    plats = {db.get("edit_plans", i)["platform"] for i in out["plans"]}
    assert plats == {"youtube_shorts", "tiktok", "instagram_reels"} and "x" in out["skipped"]  # X scored 62 → no X edit
    tik = next(db.get("edit_plans", i) for i in out["plans"] if db.get("edit_plans", i)["platform"] == "tiktok")
    assert tik["plan"]["thumbnail_text"] == "NEVER SAW IT"
    assert any(0 in ln["emph"] or ln["emph"] for ln in tik["plan"]["captions"]["lines"] if "WAY" in ln["text"])
