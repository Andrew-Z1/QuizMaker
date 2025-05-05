"""
converter.py  —  Video‑to‑Quiz generator (progressive fallback, no FFmpeg merge)
────────────────────────────────────────────────────────────────────────────────
* Downloads ≤ 360 p progressive streams when FFmpeg is absent.
* Uses FFmpeg to create an even smaller .mp3 upload when available.
* Uploads to Gemini, waits until ACTIVE, generates a quiz, and saves it.
"""

import os, re, sys, glob, time, shutil, threading, logging, subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
from tkinter.font import Font

import yt_dlp
import google.generativeai as genai
from dotenv import load_dotenv
from tqdm.auto import tqdm    # spinner while polling Gemini

# ────────────────────────── logging ────────────────────────── #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s — %(levelname)s — %(message)s",
    handlers=[
        logging.FileHandler("app.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ──────────────────── GUI application ──────────────────── #
class VideoQuizApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Video Quiz Generator")
        self.root.geometry("900x750")
        self.root.minsize(800, 650)

        # State
        self.video_url = tk.StringVar()
        self.num_q = tk.StringVar(value="5")
        self.out_dir = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
        self.progress_var = tk.DoubleVar()
        self.processing = False
        self.cancel_flag = False

        # Detect FFmpeg once
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg:
            logger.warning("FFmpeg not found — audio‑only conversion disabled")

        # Build UI, load Gemini API key, ensure output dir
        self._build_ui()
        self._load_api_key()
        Path(self.out_dir.get()).mkdir(parents=True, exist_ok=True)

    # ─────────────── UI construction ─────────────── #
    def _build_ui(self):
        font_normal = Font(size=11)

        # URL + question count
        top = ttk.Frame(self.root, padding=10); top.pack(fill=tk.X)
        ttk.Label(top, text="YouTube URL:", font=font_normal).grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.video_url, width=60).grid(row=0, column=1, padx=5, sticky="we")
        ttk.Label(top, text="Questions:", font=font_normal).grid(row=0, column=2, padx=(10,0))
        ttk.Entry(top, textvariable=self.num_q, width=5).grid(row=0, column=3)

        # Output directory
        out = ttk.Frame(self.root, padding=(10,0)); out.pack(fill=tk.X)
        ttk.Label(out, text="Output Dir:", font=font_normal).grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.out_dir, width=60).grid(row=0, column=1, padx=5, sticky="we")
        ttk.Button(out, text="Browse…", command=self._choose_dir).grid(row=0, column=2)

        # Progress
        prog = ttk.Frame(self.root, padding=(10,10)); prog.pack(fill=tk.X)
        ttk.Progressbar(prog, variable=self.progress_var, maximum=100).pack(fill=tk.X, expand=True)
        self.prog_lbl = ttk.Label(prog, text=""); self.prog_lbl.pack(anchor="w")

        # Buttons
        btns = ttk.Frame(self.root, padding=(10,0)); btns.pack()
        ttk.Button(btns, text="Start Analysis", command=self._start).grid(row=0, column=0, padx=5)
        ttk.Button(btns, text="Cancel",         command=self._cancel).grid(row=0, column=1, padx=5)

        # Log window
        log_frame = ttk.Frame(self.root, padding=10); log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_box = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state="disabled")
        self.log_box.pack(fill=tk.BOTH, expand=True)

        self._log("Ready. Enter a YouTube URL and click 'Start Analysis'.", success=True)

    # ─────────────── helpers ─────────────── #
    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)
            self._log(f"Output directory set to: {d}", success=True)

    def _log(self, msg:str, *, error=False, success=False):
        self.log_box["state"]="normal"
        tag = "info"
        if error:   tag = "err"
        elif success: tag = "succ"
        self.log_box.insert(tk.END, msg+"\n", tag)
        self.log_box.see(tk.END)
        self.log_box["state"]="disabled"
        (logger.error if error else logger.info)(msg)

    def _update_prog(self, val, text=""):
        self.progress_var.set(val)
        self.prog_lbl.config(text=text)
        self.root.update_idletasks()

    def _toggle_widgets(self, disable: bool):
        state = "disabled" if disable else "normal"
        for w in self.root.winfo_children():
            if isinstance(w, ttk.Button) and w["text"]!="Cancel":
                w.config(state=state)
        self.processing = disable

    # ─────────────── API key ─────────────── #
    def _load_api_key(self):
        load_dotenv()
        key = os.getenv("API_KEY")
        if not key:
            self._log("API_KEY not found in .env!", error=True)
            messagebox.showerror("Missing key", "Create a .env file with API_KEY=YOUR_KEY")
            return
        try:
            genai.configure(api_key=key)
            self._log("Gemini API configured.", success=True)
        except Exception as e:
            self._log(f"Gemini config error: {e}", error=True)

    # ─────────────── YouTube helpers ─────────────── #
    @staticmethod
    def _yt_id(url:str):
        pats=[
            r"(?:v=|\/)([0-9A-Za-z_-]{11})",
            r"youtu\.be\/([0-9A-Za-z_-]{11})",
            r"embed\/([0-9A-Za-z_-]{11})",
            r"shorts\/([0-9A-Za-z_-]{11})",
        ]
        for p in pats:
            m=re.search(p,url)
            if m: return m.group(1)
        return None

    def _dl_hook(self,d):
        if self.cancel_flag: raise Exception("cancel")
        if d["status"]=="downloading":
            pct=d.get("_percent_str","0%").strip("%")
            try:
                self._update_prog(float(pct), f"Downloading: {float(pct):.1f}%")
            except: pass

    # ─────────────── download video (progressive fallback) ─────────────── #
    def _download_video(self,url:str):
        vid=self._yt_id(url)
        if not vid:
            self._log("Bad YouTube URL.", error=True); return None
        outdir=self.out_dir.get(); Path(outdir).mkdir(parents=True, exist_ok=True)

        # reuse existing
        existing=glob.glob(os.path.join(outdir,f"{vid}_*.*"))
        if existing:
            if messagebox.askyesno("Reuse", f"{Path(existing[0]).name} exists. Re‑use?"):
                self._log(f"Using existing file: {Path(existing[0]).name}", success=True)
                return existing[0]
            for f in existing: os.remove(f)

        self._log(f"Starting light download for {url}")
        self._update_prog(0,"Preparing…")

        if self.ffmpeg:
            # FFmpeg available → can merge, so allow split streams (still ≤360 p)
            fmt="bestvideo[height<=360]+bestaudio/best[height<=360]"
            merge="mp4"
        else:
            # No FFmpeg → **progressive MP4 only**, ≤360 p
            fmt="best[height<=360][ext=mp4]/best[ext=mp4]"
            merge=None

        ydl_opts={
            "format":fmt,
            "quiet":True,
            "paths":{"home":outdir},
            "outtmpl":f"{vid}_orig.%(ext)s",
            "progress_hooks":[self._dl_hook],
            "noplaylist":True,
        }
        if merge: ydl_opts["merge_output_format"]=merge

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info=ydl.extract_info(url,download=True)
            file=glob.glob(os.path.join(outdir,f"{vid}_orig.*"))[0]
            self._log(f"Downloaded: {info.get('title','(title‑unknown)')}", success=True)
            return file
        except Exception as e:
            self._log(f"Download error: {e}", error=True)
            return None

    # ─────────────── optional audio‑only conversion ─────────────── #
    def _maybe_audio_only(self,filepath:str):
        if not self.ffmpeg: return filepath
        audio_path = Path(filepath).with_suffix(".mp3").with_name(Path(filepath).stem.replace("_orig","_audio")+".mp3")
        if audio_path.exists(): return str(audio_path)
        self._log("Extracting audio for smaller upload…")
        cmd=[self.ffmpeg,"-y","-i",filepath,"-vn","-acodec","libmp3lame","-q:a","7",str(audio_path)]
        try:
            subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
            self._log(f"Created audio file: {audio_path}", success=True)
            return str(audio_path)
        except Exception as e:
            self._log(f"FFmpeg error: {e}", error=True)
            return filepath

    # ─────────────── Gemini upload + poll ─────────────── #
    def _upload_and_wait(self,fpath:str, timeout=600, interval=5):
        self._log("Uploading to Gemini…")
        try:
            gfile=genai.upload_file(fpath)
        except Exception as e:
            self._log(f"Upload failed: {e}", error=True); return None
        bar=tqdm(total=timeout//interval, bar_format="{l_bar}{bar}| {remaining}", leave=False)
        start=time.time()
        while time.time()-start<timeout:
            if self.cancel_flag:
                self._log("Cancelled."); bar.close(); return None
            f=genai.get_file(gfile.name)
            st=getattr(f,"state","UNKNOWN")
            if st=="ACTIVE":
                bar.close(); self._log("Gemini file ACTIVE.", success=True); return f
            if st=="FAILED":
                bar.close(); self._log("Gemini processing FAILED.", error=True); return None
            bar.update(1)
            self._update_prog(self.progress_var.get(), f"Gemini: {st}…")
            time.sleep(interval)
        bar.close()
        self._log("Gemini poll timed out.", error=True)
        return None

    # ─────────────── Quiz generation ─────────────── #
    def _make_quiz(self, gfile, n:int):
        prompt=f"Generate {n} diverse quiz questions with answers about the uploaded video or audio."
        try:
            model=genai.GenerativeModel("gemini-1.5-pro-latest")
            rsp=model.generate_content([prompt,gfile])
            return rsp.text
        except Exception as e:
            self._log(f"Gemini generate error: {e}", error=True); return None

    def _save_quiz(self,txt:str,base:str):
        out=Path(self.out_dir.get())/f"{base}_quiz.txt"
        try:
            out.write_text(txt,encoding="utf-8")
            self._log(f"Quiz saved to {out}", success=True)
        except Exception as e:
            self._log(f"Save error: {e}", error=True)

    # ─────────────── threaded pipeline ─────────────── #
    def _start(self):
        if self.processing: return
        if not self.video_url.get().strip():
            messagebox.showwarning("Missing URL","Enter a YouTube URL."); return
        self._toggle_widgets(True)
        self.cancel_flag=False
        threading.Thread(target=self._pipeline, daemon=True).start()

    def _cancel(self):
        if self.processing:
            self.cancel_flag=True
            self._log("User requested cancel…")

    def _pipeline(self):
        url=self.video_url.get().strip()
        base=self._yt_id(url)
        try:
            # 1. download progressive ≤360 p
            vidfile=self._download_video(url)
            if not vidfile: raise RuntimeError("download")
            # 2. maybe convert to mp3
            upfile=self._maybe_audio_only(vidfile)
            # 3. upload + wait
            gfile=self._upload_and_wait(upfile)
            if not gfile: raise RuntimeError("upload")
            # 4. generate quiz
            quiz=self._make_quiz(gfile,int(self.num_q.get()))
            if not quiz: raise RuntimeError("quiz")
            # 5. save
            self._save_quiz(quiz,base)
        except RuntimeError:
            pass
        finally:
            self._update_prog(0,"")
            self._toggle_widgets(False)
            self._log("Process finished.", success=True)

# ──────────────────── main ──────────────────── #
def main():
    root=tk.Tk()
    root.option_add("*Text.err.foreground","red")
    root.option_add("*Text.succ.foreground","green")
    VideoQuizApp(root)
    root.mainloop()

if __name__=="__main__":
    main()
