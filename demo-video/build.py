"""Naan Irukken demo video pipeline.
  python demo-video/build.py                 # audio + record + render (contacts backed up/restored)
  python demo-video/build.py --render-only   # reuse raw recording
  python demo-video/build.py --voice voice.wav   # re-render with your own voice (file, or folder of <scene>.wav)
"""
import argparse, asyncio, json, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJ = ROOT.parent
RAW, WORK, AUD, OUT = ROOT / "raw", ROOT / "work", ROOT / "audio", ROOT / "out"
for d in (RAW, WORK, AUD, OUT, ROOT / "clips"):
    d.mkdir(exist_ok=True)
REGION, TABLE, PARENT = "ap-south-1", "NaanIrukken", "1276258406"
BOT = "naanirukken_thiru_bot"
TG = "https://web.telegram.org/k/"
VP = {"width": 640, "height": 900}
NAVY, ACC = "#0F1B2D", "#FF9900"
FADE, LEAD = 0.3, 0.5
REPO = "https://github.com/Thirumalaiboobathi/Weekend-challenge-Nan-iruken"
API = "https://l7wghl08sj.execute-api.ap-south-1.amazonaws.com"
NARR = json.loads((ROOT / "narration.json").read_text(encoding="utf-8"))
SCENES = NARR["scenes"]


def run(*a):
    r = subprocess.run([str(x) for x in a], capture_output=True, text=True)
    if r.returncode:
        sys.exit("FAILED: " + " ".join(map(str, a))[:200] + "\n" + r.stderr[-1500:])
    return r.stdout


def ffmpeg(*a):
    return run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *a)


def adur(p):
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p).strip())


# ------------------------------------------------------------------ contacts (pre-flight / restore)
def contacts(action):
    import boto3
    from boto3.dynamodb.conditions import Key
    t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    bak = ROOT / "contacts-backup.json"
    if action == "backup":
        items = t.query(KeyConditionExpression=Key("PK").eq(f"USER#{PARENT}") & Key("SK").begins_with("CONTACT#"))["Items"]
        bak.write_text(json.dumps(items, default=str, indent=1), encoding="utf-8")
        for i in items:
            t.delete_item(Key={"PK": i["PK"], "SK": i["SK"]})
        print(f"contacts backed up+removed: {len(items)}")
    else:
        items = json.loads(bak.read_text(encoding="utf-8"))
        from decimal import Decimal
        for i in items:  # numbers were dumped as str by default=str; re-numberize ints
            for k, v in list(i.items()):
                if isinstance(v, str) and re.fullmatch(r"-?\d+", v) and k in ("priority", "chatId"):
                    i[k] = Decimal(v)
            t.put_item(Item=i)
        got = t.query(KeyConditionExpression=Key("PK").eq(f"USER#{PARENT}") & Key("SK").begins_with("CONTACT#"))["Items"]
        print(f"contacts restored: {len(got)} (backup had {len(items)})")


def family_code():
    import boto3
    from boto3.dynamodb.conditions import Attr
    t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    for i in t.scan(FilterExpression=Attr("PK").begins_with("FAMILY#"))["Items"]:
        if str(i.get("userId")) == PARENT:
            return i["PK"].split("#", 1)[1]
    sys.exit("no familyCode")


# ------------------------------------------------------------------ narration audio
async def _tts(text, out):
    import edge_tts
    c = edge_tts.Communicate(text, NARR["voice"], rate=NARR["rate"], boundary="WordBoundary")
    words = []
    with open(out, "wb") as f:
        async for ch in c.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"]))
    return words


def make_audio(voice):
    (ROOT / "narration.txt").write_text("\n\n".join(f"[{s['id'].upper()}]\n{s['text']}" for s in SCENES), encoding="utf-8")
    if voice:
        v = Path(voice)
        if v.is_dir():
            for s in SCENES:
                ffmpeg("-i", v / f"{s['id']}.wav", AUD / f"{s['id']}.wav")
        else:  # one long recording: split at the N-1 longest silences
            log = subprocess.run(["ffmpeg", "-i", str(v), "-af", "silencedetect=noise=-35dB:d=0.7", "-f", "null", "-"],
                                 capture_output=True, text=True).stderr
            st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
            en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
            gaps = sorted(sorted(zip(st, en), key=lambda g: g[0] - g[1])[: len(SCENES) - 1])
            cuts = [0.0] + [(a + b) / 2 for a, b in gaps] + [adur(str(v))]
            if len(cuts) != len(SCENES) + 1:
                sys.exit("voice.wav: could not find scene gaps (leave >=1s silence between scenes)")
            for s, a, b in zip(SCENES, cuts, cuts[1:]):
                ffmpeg("-ss", a, "-to", b, "-i", v, AUD / f"{s['id']}.wav")
        for s in SCENES:
            s["words"] = None
            s["audio"] = str(AUD / f"{s['id']}.wav")
    else:
        for s in SCENES:
            mp3 = AUD / f"{s['id']}.mp3"
            s["words"] = asyncio.run(_tts(s["text"], mp3))
            s["audio"] = str(mp3)
    for s in SCENES:
        s["alen"] = adur(s["audio"])


def timed_tokens(s):
    """[(token, start, end)] aligned to the original text (word boundaries if counts match, else proportional)."""
    toks = s["text"].split()
    idx = [i for i, t in enumerate(toks) if re.search(r"\w", t)]
    w = s["words"]
    out, tot = [], sum(len(t) for t in toks)
    if w and len(w) == len(idx):
        wm = {i: w[k] for k, i in enumerate(idx)}
        last = 0.0
        for i, t in enumerate(toks):
            if i in wm:
                a, d, _ = wm[i]
                out.append((t, a, a + d))
                last = a + d
            else:
                out.append((t, last, last))
    else:
        c = 0
        for t in toks:
            out.append((t, s["alen"] * c / tot, s["alen"] * (c + len(t)) / tot))
            c += len(t) + 1
    return out


def sub_chunks(s):
    chunks, cur = [], []
    for tok in timed_tokens(s):
        cur.append(tok)
        txt = " ".join(t[0] for t in cur)
        if (re.search(r"[.?!]$", tok[0]) and len(txt) >= 18) or (re.search(r"[,;:—]$", tok[0]) and len(txt) >= 50) or len(txt) >= 88:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    res = []
    for k, c in enumerate(chunks):
        a = c[0][1]
        b = chunks[k + 1][0][1] if k + 1 < len(chunks) else s["alen"] + 0.4
        res.append((" ".join(t[0] for t in c), a, b))
    return res


def cue(s, word):
    for t, a, _ in timed_tokens(s):
        if word.lower() in t.lower():
            return a
    return None


# ------------------------------------------------------------------ recording
def record(marks_path):
    from playwright.sync_api import sync_playwright
    code = family_code()
    marks = {}
    with sync_playwright() as p:
        # --- phase A: login check, NOT recorded
        ctx = p.chromium.launch_persistent_context(str(ROOT / ".profile"), headless=False, viewport={"width": 1000, "height": 900})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto(TG)
        pg.wait_for_timeout(6000)
        if not pg.query_selector(".chatlist"):
            print("Scan the QR in the browser, then press Enter  (auto-continues once login is detected)", flush=True)
            pg.wait_for_selector(".chatlist", timeout=900000)
        pg.wait_for_timeout(1500)
        ctx.close()
        # --- phase B: recording
        ctx = p.chromium.launch_persistent_context(str(ROOT / ".profile"), headless=False, viewport=VP,
                                                   record_video_dir=str(RAW / "vid"), record_video_size=VP)
        ctx.add_init_script(r"""
          const go=()=>{const st=document.createElement('style');st.textContent='#column-left{display:none!important} html{zoom:1.3}';
            document.documentElement.appendChild(st);
            setInterval(()=>{const w=document.createTreeWalker(document.documentElement,NodeFilter.SHOW_TEXT);let n;
              while(n=w.nextNode()){if(/\d{6,}-demo-\d+/.test(n.nodeValue))n.nodeValue=n.nodeValue.replace(/\d{6,}-demo-\d+/g,'demo-checkin')}},40)};
          if(document.documentElement)go();else document.addEventListener('DOMContentLoaded',go);""")
        olds = list(ctx.pages)
        pg = ctx.new_page()
        for old in olds:
            old.close()
        t0 = time.time()
        T = lambda: round(time.time() - t0, 2)
        pg.goto(TG + "#@" + BOT)
        pg.wait_for_selector(".input-message-input", timeout=60000)
        pg.wait_for_timeout(2500)
        seen = set()

        def snap():
            return pg.evaluate("""()=>[...document.querySelectorAll('.bubbles-inner .bubble')].map(b=>({mid:b.dataset.mid,text:b.innerText,
                btns:[...b.querySelectorAll('.reply-markup-button')].map(x=>x.innerText)}))""")

        def mark_seen():
            seen.update(b["mid"] for b in snap())

        def wait_new(pat, btn=None, timeout=120):
            end = time.time() + timeout
            while time.time() < end:
                for b in snap():
                    if b["mid"] in seen:
                        continue
                    if re.search(pat, b["text"], re.I) and (btn is None or any(re.search(btn, x, re.I) for x in b["btns"])):
                        seen.add(b["mid"])
                        return b, T()
                pg.wait_for_timeout(120)
            raise TimeoutError(pat)

        def send(text):
            pg.locator(".input-message-container .input-message-input").first.click()
            pg.keyboard.type(text, delay=80)
            pg.wait_for_timeout(300)
            pg.keyboard.press("Enter")

        def tap(b, pat):
            pg.locator(f'.bubble[data-mid="{b["mid"]}"] .reply-markup-button', has_text=re.compile(pat, re.I)).first.click()

        def demo_start(mk):
            mark_seen()
            marks[mk + ".start"] = T()
            send("/demo")

        # DEMO 1
        demo_start("demo1")
        ping, marks["demo1.ping"] = wait_new(r"", btn=r"I'm OK")
        pg.wait_for_timeout(3000)
        rem, marks["demo1.remind"] = wait_new(r"", btn=r"I'm OK", timeout=60)
        pg.wait_for_timeout(2800)
        al, marks["demo1.alert"] = wait_new(r"hasn't responded", btn=r"checking", timeout=60)
        pg.wait_for_timeout(3200)
        marks["demo1.tap_ack"] = T()
        tap(al, r"checking")
        _, marks["demo1.checking"] = wait_new(r"is checking on")
        pg.wait_for_timeout(3000)
        cf, marks["demo1.confirm"] = wait_new(r"", btn=r"fine", timeout=40)
        pg.wait_for_timeout(2500)
        marks["demo1.tap_fine"] = T()
        tap(cf, r"fine")
        try:
            _, marks["demo1.resolved"] = wait_new(r"", timeout=10)
        except TimeoutError:
            marks["demo1.resolved"] = T()
        pg.wait_for_timeout(3500)
        marks["demo1.end"] = T()
        pg.wait_for_timeout(6000)
        # DEMO 2
        demo_start("demo2")
        ping, marks["demo2.ping"] = wait_new(r"", btn=r"I'm OK")
        pg.wait_for_timeout(2500)
        marks["demo2.tap_help"] = T()
        tap(ping, r"Help")
        urg, marks["demo2.urgent"] = wait_new(r"pressed HELP", timeout=40)
        pg.wait_for_timeout(5500)
        marks["demo2.end"] = T()
        # cleanup (not used in video): claim + confirm so the demo execution ends cleanly
        try:
            if any(re.search("checking", x, re.I) for x in urg["btns"]):
                tap(urg, r"checking")
            else:
                raise TimeoutError
            cf, _ = wait_new(r"", btn=r"fine", timeout=20)
            tap(cf, r"fine")
            pg.wait_for_timeout(4000)
        except Exception:
            pg.wait_for_timeout(55000)
        pg.wait_for_timeout(4000)
        # DEMO 3
        demo_start("demo3")
        ping, marks["demo3.ping"] = wait_new(r"", btn=r"I'm OK")
        pg.wait_for_timeout(3000)
        marks["demo3.type"] = T()
        send("ok")
        wait_new(r"Got it")
        marks["demo3.got"] = T()
        pg.wait_for_timeout(5000)
        marks["demo3.end"] = T()
        chat_video = pg.video
        # --- dashboard (separate page => separate video)
        dp = ctx.new_page()
        d0 = time.time()
        D = lambda: round(time.time() - d0, 2)
        dp.goto(f"{API}/d/{code}")
        dp.wait_for_selector("#cost", timeout=30000)
        dp.wait_for_timeout(2500)
        dmarks = {"start": D()}
        card = lambda sel: dp.evaluate(f"document.querySelector('{sel}').closest('.card').offsetTop-18")
        smooth = lambda y, s: dp.evaluate("""([y,s])=>new Promise(r=>{const y0=scrollY,t0=performance.now();(function f(t){const k=Math.min(1,(t-t0)/(s*1000));
            const e=k<.5?2*k*k:1-Math.pow(-2*k+2,2)/2;scrollTo(0,y0+(y-y0)*e);k<1?requestAnimationFrame(f):r()})(t0)})""", [y, s])
        dmarks["today"] = D()
        dp.wait_for_timeout(1500)
        smooth(card("#streak"), 1.4)
        dmarks["streak"] = D()
        dp.wait_for_timeout(2200)
        smooth(card("#strip"), 1.4)
        dp.wait_for_timeout(1200)
        try:
            btns = dp.query_selector_all("#strip button.has")
            btns[-1].click()
        except Exception:
            pass
        dmarks["strip"] = D()
        dp.wait_for_timeout(1000)
        smooth(dp.evaluate("document.querySelector('#strip').offsetTop-10"), 1.2)
        dp.wait_for_timeout(3200)
        smooth(card("#routine"), 1.4)
        dmarks["routine"] = D()
        dp.wait_for_timeout(3000)
        smooth(card("#cost"), 1.4)
        dmarks["cost"] = D()
        dp.wait_for_timeout(3500)
        dmarks["end"] = D()
        dash_video = dp.video
        ctx.close()
        marks["_chat"] = str(Path(chat_video.path()))
        marks["_dash"] = str(Path(dash_video.path()))
        marks["_dm"] = dmarks
    marks_path.write_text(json.dumps(marks, indent=1), encoding="utf-8")
    print("recorded:", {k: v for k, v in marks.items() if not k.startswith("_")})


# ------------------------------------------------------------------ HTML -> PNG
FONT = "Inter,'DejaVu Sans','Segoe UI','Nirmala UI','Segoe UI Emoji',sans-serif"
HEAD = f"""<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box}}body{{margin:0;font-family:{FONT};color:#fff}}.acc{{color:{ACC}}}.mut{{color:#9fb0c8}}
.full{{width:1920px;height:1080px;background:{NAVY};display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:34px}}
.box{{border:3px solid {ACC};border-radius:28px;padding:34px 44px;font-size:44px;font-weight:600;background:#16294400}}
.row{{display:flex;align-items:center;gap:60px}}.arrow{{font-size:80px;color:{ACC}}}</style>"""


class Shots:
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.p = sync_playwright().start()
        self.b = self.p.chromium.launch()
        self.pg = self.b.new_page(viewport={"width": 1920, "height": 1080})
        return self

    def __exit__(self, *a):
        self.b.close()
        self.p.stop()

    def png(self, body, path, w=1920, h=1080, transparent=False):
        self.pg.set_viewport_size({"width": w, "height": h})
        self.pg.set_content(f"<html><head>{HEAD}</head><body>{body}</body></html>")
        self.pg.evaluate("document.fonts.ready")
        self.pg.wait_for_timeout(350)
        self.pg.screenshot(path=str(path), omit_background=transparent, clip={"x": 0, "y": 0, "width": w, "height": h})
        return str(path)


def full(inner):
    return f'<div class="full">{inner}</div>'


def sub_png(sh, text, path):
    return sh.png(f'<div style="text-align:center;padding-top:8px"><div style="display:inline-block;background:rgba(0,0,0,.66);'
                  f'font-size:42px;font-weight:600;line-height:1.3;padding:12px 30px;border-radius:16px;max-width:1560px">{text}</div></div>',
                  path, 1920, 150, True)


def cap_png(sh, step, text, path):
    return sh.png(f'<div style="padding:10px 20px"><div class="acc" style="font-size:30px;font-weight:800;letter-spacing:3px">{step}</div>'
                  f'<div style="font-size:50px;font-weight:800;line-height:1.15;margin-top:10px">{text}</div></div>', path, 580, 300, True)


def badge_png(sh, path):
    return sh.png(f'<div style="display:inline-block;background:{ACC};color:#0F1B2D;font-size:46px;font-weight:800;padding:18px 34px;border-radius:60px">'
                  f'⏩ 25 seconds later</div>', path, 580, 120, True)


# ------------------------------------------------------------------ ffmpeg scene builder
class FF:
    def __init__(self):
        self.args, self.f, self.n = [], [], 0

    def inp(self, *a):
        self.args += list(a)
        self.n += 1
        return self.n - 1

    def add(self, s):
        self.f.append(s)

    def xchain(self, labels, lens, tag):
        cur, L = labels[0], lens[0]
        for i in range(1, len(labels)):
            out = f"{tag}{i}"
            self.add(f"[{cur}][{labels[i]}]xfade=transition=fade:duration={FADE}:offset={L - FADE:.3f}[{out}]")
            cur, L = out, L + lens[i] - FADE
        return cur


def finish(ff, base, D, s, subs_png, caps, out, extra_over=()):
    """overlay subtitles/captions on `base`, add voice, write mp4."""
    cur = base
    items = [(i, 0, 930, a, b) for i, a, b in subs_png] + list(caps) + list(extra_over)
    for k, (idx, x, y, a, b) in enumerate(items):
        ff.add(f"[{cur}][{idx}:v]overlay={x}:{y}:enable='between(t,{a:.2f},{b:.2f})'[ov{k}]")
        cur = f"ov{k}"
    ai = ff.inp("-i", s["audio"])
    ff.add(f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={int(LEAD * 1000)}|{int(LEAD * 1000)},apad=whole_dur={D:.3f}[aout]")
    ff.add(f"[{cur}]fps=30,format=yuv420p,setsar=1[vout]")
    ffmpeg(*ff.args, "-filter_complex", ";".join(ff.f), "-map", "[vout]", "-map", "[aout]", "-t", f"{D:.3f}", "-r", "30",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", out)


def sub_inputs(ff, sh, s):
    res = []
    for k, (txt, a, b) in enumerate(sub_chunks(s)):
        p = sub_png(sh, txt, WORK / f"sub_{s['id']}_{k}.png")
        res.append((ff.inp("-i", p), a + LEAD, b + LEAD))
    return res


def scene_dur(s, nat=0):
    return max(s["min"], s["alen"] + LEAD + 0.7, nat)


# ---- card scenes
def cards_scene(sh, s, pages, splits):
    D = scene_dur(s)
    ff = FF()
    labels, lens = [], []
    for i, html in enumerate(pages):
        p = sh.png(full(html), WORK / f"card_{s['id']}_{i}.png")
        a = splits[i]
        b = splits[i + 1] if i + 1 < len(pages) else D
        L = b - a + (FADE if i + 1 < len(pages) else 0)
        k = ff.inp("-loop", 1, "-framerate", 30, "-t", f"{L:.3f}", "-i", p)
        ff.add(f"[{k}:v]fps=30,format=yuv420p,setsar=1[c{i}]")
        labels.append(f"c{i}")
        lens.append(L)
    base = ff.xchain(labels, lens, "cx") if len(pages) > 1 else labels[0]
    out = str(WORK / f"scene_{s['id']}.mp4")
    finish(ff, base, D, s, sub_inputs(ff, sh, s), [], out)
    return out, D


# ---- architecture
def arch_scene(sh, s):
    from PIL import Image, ImageDraw
    D = scene_dur(s)
    src = Image.open(PROJ / "docs" / "architecture.png").convert("RGB")
    big = src.resize((3200, 2000), Image.LANCZOS)
    boxes = {"sched": [(958, 108, 1262, 192)], "sfn": [(940, 224, 1562, 626)],
             "api": [(620, 140, 880, 270), (620, 330, 880, 440)], "dyn": [(940, 700, 1230, 830)], "s3": [(1270, 700, 1562, 830)]}
    wins = {"sched": (533, 40, 1067, 600), "sfn": (533, 170, 1067, 600), "api": (420, 110, 1067, 600),
            "dyn": (380, 450, 960, 540), "s3": (640, 450, 960, 540)}
    order = ["sched", "sfn", "api", "dyn", "s3"]
    cues = [cue(s, "EventBridge"), cue(s, "Every"), cue(s, "Button"), cue(s, "DynamoDB"), cue(s, "S3")]
    if None in cues:
        t = [0.18, 0.3, 0.55, 0.72, 0.86]
        cues = [x * s["alen"] for x in t]
    starts = [0.0] + [c + LEAD - 0.4 for c in cues]  # overview first, then each highlight
    starts[1] = max(starts[1], 1.0)
    keys = ["full"] + order
    end_hold = min(s["alen"] + LEAD + 0.6, D - 2.0)
    starts_t = starts + [max(end_hold, starts[-1] + 3.0)]  # final overview
    keys.append("full")
    ff = FF()
    labels, lens = [], []
    for i, key in enumerate(keys):
        a = starts_t[i]
        b = starts_t[i + 1] if i + 1 < len(keys) else D
        L = b - a + (FADE if i + 1 < len(keys) else 0)
        img = big.copy()
        if key != "full":
            ov = Image.new("RGBA", img.size, (5, 10, 20, 150))
            base = img.convert("RGBA")
            comp = Image.alpha_composite(base, ov)
            for (x0, y0, x1, y1) in boxes[key]:
                r = (x0 * 2 - 14, y0 * 2 - 14, x1 * 2 + 14, y1 * 2 + 14)
                comp.paste(img.crop(r), r[:2])
                ImageDraw.Draw(comp).rounded_rectangle(r, radius=18, outline=ACC, width=10)
            img = comp.convert("RGB")
        pth = WORK / f"arch_{i}.png"
        img.save(pth)
        k = ff.inp("-loop", 1, "-framerate", 30, "-t", f"{L:.3f}", "-i", str(pth))
        if key == "full":  # whole diagram, fit to 1920x1080 with navy margins, slow zoom-in
            ff.add(f"[{k}:v]scale=1728:1080,pad=1920:1080:96:0:color=0x0F1B2D,fps=30,format=yuv420p,setsar=1[a{i}]")
        else:
            x, y, w, h = wins[key]
            dx = 40
            ff.add(f"[{k}:v]crop=w={w * 2}:h={h * 2}:x='{x * 2}+{dx * 2}*t/{L:.2f}':y={y * 2},scale=1920:1080,fps=30,format=yuv420p,setsar=1[a{i}]")
        labels.append(f"a{i}")
        lens.append(L)
    base = ff.xchain(labels, lens, "ax")
    out = str(WORK / "scene_arch.mp4")
    finish(ff, base, D, s, sub_inputs(ff, sh, s), [], out)
    return out, D


# ---- screen-recording scenes (chat / dashboard)
def seg_fit(segs, D):
    for sn in (1.0, 1.1, 1.2, 1.3, 1.4):
        for sf in (6, 7, 8):
            N = sum((b - a) / (sf if fast else sn) for a, b, fast in segs)
            if N <= D:
                return sf, sn, N
    N = sum((b - a) / (8 if fast else 1.4) for a, b, fast in segs)
    return 8, 1.4, N


def screen_scene(sh, s, raw, segs, caps, pip=False):
    """segs: [(raw_a, raw_b, fast)], caps: [(raw_t, step, text)]; returns scene mp4."""
    D0 = scene_dur(s)
    sf, sn, N = seg_fit(segs, D0)
    D = max(D0, N)
    # raw->scene time map
    tmap, acc = [], 0.0
    for a, b, fast in segs:
        sp = sf if fast else sn
        tmap.append((a, b, acc, sp))
        acc += (b - a) / sp

    def to_scene(t):
        for a, b, o, sp in tmap:
            if t <= b:
                return o + max(0, t - a) / sp
        return acc

    ff = FF()
    bg = ff.inp("-f", "lavfi", "-i", f"color=c=0x0F1B2D:s=1920x1080:r=30:d={D:.3f}")
    rv = ff.inp("-i", raw)
    K = len(segs)
    ff.add(f"[{rv}:v]split={K}" + "".join(f"[r{i}]" for i in range(K)))
    for i, (a, b, fast) in enumerate(segs):
        sp = sf if fast else sn
        ff.add(f"[r{i}]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{sp},fps=30,scale=640:900,setsar=1[g{i}]")
    ff.add("".join(f"[g{i}]" for i in range(K)) + f"concat=n={K}:v=1:a=0[scr]")
    ff.add("[bg0][scr]overlay=640:10[base]".replace("bg0", str(bg) + ":v"))
    cur = "base"
    extra = []
    if pip and (ROOT / "clips" / "sfn.mp4").exists():
        pi = ff.inp("-stream_loop", -1, "-i", str(ROOT / "clips" / "sfn.mp4"))
        ff.add(f"[{pi}:v]scale=576:-2,setsar=1[pip]")
        ff.add(f"[{cur}][pip]overlay=1920-576-40:1080-340-150[pipo]")
        cur = "pipo"
    capi = []
    for k, (t, step, text) in enumerate(caps):
        a = to_scene(t) + LEAD * 0
        b = to_scene(caps[k + 1][0]) if k + 1 < len(caps) else D
        p = cap_png(sh, step, text, WORK / f"cap_{s['id']}_{k}.png")
        capi.append((ff.inp("-i", p), 40, 380, a, b))
    for k, (a, b, fast) in enumerate(segs):
        if fast:
            bp = badge_png(sh, WORK / "badge.png")
            capi.append((ff.inp("-i", bp), 1300, 450, to_scene(a), to_scene(b)))
    out = str(WORK / f"scene_{s['id']}.mp4")
    finish(ff, cur, D, s, sub_inputs(ff, sh, s), capi, out)
    return out, D


# ------------------------------------------------------------------ main render
def render(marks, voice):
    S = {s["id"]: s for s in SCENES}
    m = marks
    dm = m["_dm"]
    scenes = []
    with Shots() as sh:
        scenes.append(cards_scene(sh, S["hook"], [
            '<div style="font-size:92px;font-weight:800;line-height:1.2;max-width:1500px">If Amma falls at home,<br>who finds out — and <span class="acc">when</span>?</div>',
            '<div style="font-size:130px;font-weight:800" class="acc">Naan Irukken</div><div style="font-size:92px;font-weight:600">நான் இருக்கேன்</div>'
            '<div style="font-size:56px" class="mut">“I\'m here”</div>'], [0, 4.6]))
        scenes.append(cards_scene(sh, S["problem"], [
            '<div style="font-size:70px;font-weight:800">Parents live alone.<br><span class="acc">Kids live in other cities.</span></div>'
            '<div class="row"><div class="box">🏠 Amma<br><span class="mut" style="font-size:32px">home alone</span></div>'
            '<div style="width:320px;border-top:6px dashed #FF9900"></div>'
            '<div class="box">💼 Kids<br><span class="mut" style="font-size:32px">far away</span></div></div>',
            '<div style="font-size:70px;font-weight:800">A daily call gets missed.<br><span class="acc">Nothing escalates.</span></div>'
            '<div class="row"><div class="box">📞 Call missed</div><div class="arrow">→</div><div class="box">🤷 Nobody knows</div><div class="arrow">→</div>'
            '<div class="box" style="border-color:#ff5a5a">⏳ Hours pass</div></div>'], [0, S["problem"]["alen"] * 0.5 + 1.2]))
        scenes.append(screen_scene(sh, S["demo1"], m["_chat"], [
            (m["demo1.start"] + 0.2, m["demo1.ping"] + 3.2, False),
            (m["demo1.ping"] + 3.2, m["demo1.remind"] - 1.0, True),
            (m["demo1.remind"] - 1.0, m["demo1.remind"] + 3.4, False),
            (m["demo1.remind"] + 3.4, m["demo1.alert"] - 1.0, True),
            (m["demo1.alert"] - 1.0, m["demo1.end"], False)],
            [(m["demo1.start"], "STEP 1", "Daily check-in:<br>one button"), (m["demo1.remind"] - 1.0, "STEP 2", "Gentle<br>reminder"),
             (m["demo1.alert"] - 1.0, "STEP 3", "Family alerted<br>one by one"), (m["demo1.tap_ack"], "STEP 4", "“I'm checking” —<br>everyone told"),
             (m["demo1.confirm"], "STEP 5", "Waits for<br>“They're fine”"), (m["demo1.tap_fine"], "RESOLVED", "Confirmed safe ✔")], pip=True))
        scenes.append(screen_scene(sh, S["demo2"], m["_chat"], [(m["demo2.start"] + 0.2, m["demo2.end"], False)],
                                   [(m["demo2.start"], "SOS", "One tap on<br>🆘 Help"), (m["demo2.urgent"] - 0.3, "URGENT", "Everyone alerted<br>at once")]))
        scenes.append(screen_scene(sh, S["demo3"], m["_chat"], [(m["demo3.start"] + 0.2, m["demo3.end"], False)],
                                   [(m["demo3.start"], "ANY MESSAGE", "Just type<br>“ok”"), (m["demo3.got"] - 0.3, "CHECKED IN", "“I'm here” ✔")]))
        scenes.append(arch_scene(sh, S["arch"]))
        scenes.append(screen_scene(sh, S["dash"], m["_dash"], [(dm["start"] + 0.2, dm["end"], False)],
                                   [(dm["start"], "DASHBOARD", "Private family<br>dashboard"), (dm["strip"] - 0.5, "30 DAYS", "Tap a day →<br>its timeline"),
                                    (dm["routine"], "ROUTINE", "Routine changes"), (dm["cost"], "COST", "Real cost<br>per family")]))
        scenes.append(cards_scene(sh, S["close"], [
            '<div style="font-size:110px;font-weight:800" class="acc">Naan Irukken</div><div style="font-size:60px">நான் இருக்கேன்</div>'
            f'<div style="font-size:46px;font-weight:600;margin-top:20px">{REPO}</div>'
            '<div style="font-size:38px" class="mut">Lambda · API Gateway · DynamoDB · S3 · Step Functions · EventBridge Scheduler</div>'
            '<div style="font-size:42px;font-weight:600;margin-top:10px">Scales to zero — ~22 families inside the Step Functions free tier</div>'
            '<div style="font-size:42px;font-weight:600" class="acc">One SAM template · one deploy command</div>'], [0]))
    files = [x[0] for x in scenes]
    lens = [x[1] for x in scenes]
    ff = FF()
    vl, al = [], []
    for i, f in enumerate(files):
        k = ff.inp("-i", f)
        ff.add(f"[{k}:v]fps=30,format=yuv420p,setsar=1[v{i}]")
        ff.add(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        vl.append(f"v{i}")
        al.append(f"a{i}")
    v = ff.xchain(vl, lens, "vx")
    cur, L = al[0], lens[0]
    for i in range(1, len(al)):
        ff.add(f"[{cur}][{al[i]}]acrossfade=d={FADE}[ac{i}]")
        cur = f"ac{i}"
    out = OUT / "naan-irukken-demo.mp4"
    ffmpeg(*ff.args, "-filter_complex", ";".join(ff.f), "-map", f"[{v}]", "-map", f"[{cur}]", "-r", "30", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out)
    total = sum(lens) - FADE * (len(lens) - 1)
    print("scene lengths:", [round(x, 1) for x in lens], "expected total", round(total, 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice")
    ap.add_argument("--render-only", action="store_true")
    a = ap.parse_args()
    mp = RAW / "marks.json"
    make_audio(a.voice)
    if not (a.render_only or a.voice):
        contacts("backup")
        try:
            record(mp)
        finally:
            contacts("restore")
    out = render(json.loads(mp.read_text(encoding="utf-8")), a.voice)
    print("OUT", out)


if __name__ == "__main__":
    main()
