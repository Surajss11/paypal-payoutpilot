import json, subprocess
from pathlib import Path
B = Path(__file__).resolve().parent
offs = json.loads((B / "offsets.json").read_text())
ORDER = ["s1_title", "s2_problem", "s3_ask", "s4_judge", "s5_approve", "s6_batch", "s7_ops", "s8_arch", "s9_close"]
SLIDES = {"s1_title", "s2_problem", "s8_arch", "s9_close"}
LEAD = 0.4

def dur(f):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                                capture_output=True, text=True).stdout.strip())

def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)

ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
parts, report = [], []
for n in ORDER:
    vo = B / f"{n}.mp3"
    v = dur(vo)
    out = B / f"seg_{n}.mp4"
    if n in SLIDES:
        D = round(v + LEAD + 0.9, 2)
        vin = ["-loop", "1", "-framerate", "30", "-t", str(D), "-i", str(B / f"{n}.png")]
        vf = f"scale=1920:1080,fade=t=in:st=0:d=0.35,fade=t=out:st={D-0.35}:d=0.35"
    else:
        src = B / f"{n}.webm"
        st = offs[n]["start"] - 0.2
        if n == "s7_ops":  # cut the ~15 s wait while PayPal sandbox keeps answering 500 to the bounded retries
            tmp = B / "s7_cut.mp4"
            run(["ffmpeg", "-y", "-i", str(src), "-filter_complex",
                 f"[0:v]trim={st}:12.5,setpts=PTS-STARTPTS[a];[0:v]trim=26.6,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1[o]",
                 "-map", "[o]", "-c:v", "libx264", "-crf", "16", "-r", "30", str(tmp)])
            src, st = tmp, 0
        vl = dur(src) - st
        D = round(max(vl, v + LEAD + 0.6), 2)
        vin = ["-ss", f"{st:.2f}", "-i", str(src)]
        vf = f"fps=30,scale=1920:1080,tpad=stop_mode=clone:stop_duration={max(0, D - vl) + 0.5:.2f},trim=0:{D},fade=t=in:st=0:d=0.25,fade=t=out:st={D-0.3}:d=0.3"
    run(["ffmpeg", "-y", *vin, "-i", str(vo), "-filter_complex",
         f"[0:v]{vf},setpts=PTS-STARTPTS[v];[1:a]adelay={int(LEAD*1000)}|{int(LEAD*1000)},apad,atrim=0:{D}[a]",
         "-map", "[v]", "-map", "[a]", "-t", str(D), *ENC, str(out)])
    parts.append(out)
    report.append((n, D))
    print(n, D)
(B / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
final = B.parent / "demo.mp4"
run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(B / "list.txt"), "-c", "copy", "-movflags", "+faststart", str(final)])
t = 0
for n, D in report:
    print(f"{int(t//60)}:{t%60:05.2f}  {n}  {D:.1f}s"); t += D
print("total", round(dur(final), 2))
