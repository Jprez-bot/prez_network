"""Generate synthetic test media: a 4-minute 'stream' and a 45 s 'Twitch clip'.

Audio has quiet 'talking' stretches, pauses and loud reaction bursts at known
times, and an SRT transcript is written alongside, so analysis is testable
without speech recognition.
"""
import subprocess
import sys
from pathlib import Path


def make(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stream = out_dir / "stream.mp4"
    clip = out_dir / "clip.mp4"
    # talk = amplitude-modulated tone; pauses where sin<0 slowly; bursts at 150-156s and 30-36s
    expr_stream = ("0.04*sin(2*PI*220*t)*gt(sin(2*PI*t/9),-0.6)"
                   "+0.6*sin(2*PI*330*t)*between(t,150,156)*(0.6+0.4*sin(2*PI*7*t))"
                   "+0.3*sin(2*PI*300*t)*between(t,60,62)")
    expr_clip = ("0.04*sin(2*PI*220*t)*gt(sin(2*PI*t/7),-0.5)"
                 "+0.6*sin(2*PI*330*t)*between(t,22,30)*(0.6+0.4*sin(2*PI*6*t))")
    for path, dur, expr in ((stream, 240, expr_stream), (clip, 45, expr_clip)):
        if path.exists():
            continue
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={dur}",
                        "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={dur}",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-c:a", "aac", "-shortest", str(path)],
                       check=True)
    srt_stream = out_dir / "stream.srt"
    srt_clip = out_dir / "clip.srt"
    srt_stream.write_text(_srt([
        (5, 9, "Alright chat we are back, let's get into it."),
        (20, 26, "So I was telling you about the tournament yesterday."),
        (40, 46, "Honestly I think this game is underrated."),
        (58, 62, "Wait what was that?"),
        (100, 106, "Okay so here's the setup, I need this one kill."),
        (140, 149, "He's low, he's low, I just need to hit this shot."),
        (150, 153, "NO WAY! Let's go!"),
        (153, 157, "Bro I'm dying haha that was insane!"),
        (170, 178, "Chat clip that, clip it right now."),
        (200, 210, "Anyway the best part of this game is the movement."),
    ]))
    srt_clip.write_text(_srt([
        (2, 6, "Okay chat watch this, I got a plan."),
        (12, 18, "He doesn't see me, he doesn't see me."),
        (22, 25, "NO WAY! What the!"),
        (25, 29, "Bro that was insane haha!"),
        (36, 42, "Okay anyway let me go grab some water."),
    ]))
    return {"stream": stream, "clip": clip, "stream_srt": srt_stream, "clip_srt": srt_clip}


def _srt(items):
    def ts(t):
        return f"00:{int(t // 60):02d}:{int(t % 60):02d},000"
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(items, 1))


if __name__ == "__main__":
    print(make(Path(sys.argv[1] if len(sys.argv) > 1 else "fixtures")))
