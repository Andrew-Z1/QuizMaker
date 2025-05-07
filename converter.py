import os
import re
import sys
import glob
import time
import json
import shutil
import threading
import logging
import subprocess
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
class TranscriptQuizApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("YouTube Transcript Quiz Generator")
        self.root.geometry("900x750")
        self.root.minsize(800, 650)

        # State
        self.video_url = tk.StringVar()
        self.num_q = tk.StringVar(value="5")
        self.out_dir = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
        self.progress_var = tk.DoubleVar()
        self.download_video_var = tk.BooleanVar(value=True)
        self.transcript_only_var = tk.BooleanVar(value=False)
        self.offline_mode_var = tk.BooleanVar(value=False)
        self.processing = False
        self.cancel_flag = False
        self.retry_count = 0
        self.max_retries = 3
        
        # Configure backoff parameters for API requests
        self.initial_backoff = 5  # seconds
        self.max_backoff = 60  # seconds

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

        # Options
        opt = ttk.Frame(self.root, padding=(10,5)); opt.pack(fill=tk.X)
        ttk.Checkbutton(opt, text="Download Video", variable=self.download_video_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opt, text="Transcript Only (No Video Upload)", variable=self.transcript_only_var, 
                      command=self._toggle_transcript_only).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Checkbutton(opt, text="Offline Mode (No API Calls)", variable=self.offline_mode_var).grid(row=0, column=2, sticky="w", padx=10)

        # Progress
        prog = ttk.Frame(self.root, padding=(10,10)); prog.pack(fill=tk.X)
        ttk.Progressbar(prog, variable=self.progress_var, maximum=100).pack(fill=tk.X, expand=True)
        self.prog_lbl = ttk.Label(prog, text=""); self.prog_lbl.pack(anchor="w")

        # Buttons
        btns = ttk.Frame(self.root, padding=(10,0)); btns.pack()
        ttk.Button(btns, text="Start Analysis", command=self._start).grid(row=0, column=0, padx=5)
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=1, padx=5)

        # Log window
        log_frame = ttk.Frame(self.root, padding=10); log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_box = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, state="disabled")
        self.log_box.pack(fill=tk.BOTH, expand=True)
        
        # Configure tags for coloring
        self.log_box.tag_configure("err", foreground="red")
        self.log_box.tag_configure("succ", foreground="green")
        self.log_box.tag_configure("info", foreground="black")

        self._log("Ready. Enter a YouTube URL and click 'Start Analysis'.", success=True)

    # ─────────────── helpers ─────────────── #
    def _toggle_transcript_only(self):
        if self.transcript_only_var.get():
            self.download_video_var.set(False)
        
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
            if isinstance(w, ttk.Frame):
                for child in w.winfo_children():
                    if isinstance(child, ttk.Button) and child["text"] != "Cancel":
                        child.config(state=state)
                    elif isinstance(child, ttk.Entry) or isinstance(child, ttk.Checkbutton):
                        child.config(state=state)
        self.processing = disable

    # ─────────────── API key ─────────────── #
    def _load_api_key(self):
        load_dotenv()
        key = os.getenv("API_KEY")
        if not key:
            self._log("API_KEY not found in .env! Enabling offline mode.", error=True)
            self.offline_mode_var.set(True)
            messagebox.showwarning("Missing API Key", "No API_KEY found in .env file. Enabling offline mode. For better results, create a .env file with API_KEY=YOUR_KEY")
            return
        try:
            genai.configure(api_key=key)
            self._log("Gemini API configured.", success=True)
        except Exception as e:
            self._log(f"Gemini config error: {e}. Enabling offline mode.", error=True)
            self.offline_mode_var.set(True)

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

    # ─────────────── download transcript ─────────────── #
    def _download_transcript(self, url:str):
        vid = self._yt_id(url)
        if not vid:
            self._log("Bad YouTube URL.", error=True)
            return None
            
        outdir = self.out_dir.get()
        Path(outdir).mkdir(parents=True, exist_ok=True)
        transcript_path = os.path.join(outdir, f"{vid}_transcript.txt")
        
        # Check for existing transcript
        if os.path.exists(transcript_path):
            if messagebox.askyesno("Reuse", f"Transcript exists. Re‑use?"):
                self._log(f"Using existing transcript: {Path(transcript_path).name}", success=True)
                return transcript_path
            os.remove(transcript_path)
        
        self._log("Downloading transcript...")
        self._update_prog(20, "Downloading transcript...")
        
        # Configure yt-dlp to get transcript
        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "best",
            "quiet": True,
            "paths": {"home": outdir},
            "outtmpl": f"{vid}_sub"
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_title = info.get('title', 'Untitled')
            
            # Find and process the subtitle file
            subtitle_files = glob.glob(os.path.join(outdir, f"{vid}_sub*.vtt")) + \
                             glob.glob(os.path.join(outdir, f"{vid}_sub*.srt"))
            
            if not subtitle_files:
                self._log("No transcript found. Will generate quiz without transcript.", error=True)
                return None
                
            # Process the first found subtitle file
            subtitle_file = subtitle_files[0]
            
            # Extract text from subtitle file (basic processing)
            with open(subtitle_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Very basic VTT/SRT processing - extract text only
            # Remove timestamps and formatting
            lines = content.split('\n')
            clean_lines = []
            skip_line = False
            
            for line in lines:
                # Skip metadata, timestamps, and empty lines
                if line.strip() == '' or '-->' in line or line.strip().isdigit() or line.startswith('WEBVTT'):
                    skip_line = True
                    continue
                    
                # If we're in a block to skip, check if we can exit that state
                if skip_line:
                    if line.strip() and not '-->' in line and not line.strip().isdigit():
                        skip_line = False
                    else:
                        continue
                
                # Clean the line of HTML-like tags
                clean_line = re.sub(r'<[^>]+>', '', line)
                if clean_line.strip():
                    clean_lines.append(clean_line.strip())
            
            # Write cleaned transcript
            with open(transcript_path, 'w', encoding='utf-8') as f:
                f.write(f"Title: {video_title}\n\n")
                f.write('\n'.join(clean_lines))
            
            # Clean up subtitle file
            try:
                os.remove(subtitle_file)
            except:
                pass
                
            self._log(f"Transcript downloaded and saved to {Path(transcript_path).name}", success=True)
            return transcript_path
            
        except Exception as e:
            self._log(f"Transcript download error: {e}", error=True)
            return None

    # ─────────────── download video (progressive fallback) ─────────────── #
    def _download_video(self, url:str):
        if not self.download_video_var.get():
            self._log("Video download skipped per user settings.", success=True)
            return None
            
        vid = self._yt_id(url)
        if not vid:
            self._log("Bad YouTube URL.", error=True)
            return None
            
        outdir = self.out_dir.get()
        Path(outdir).mkdir(parents=True, exist_ok=True)

        # reuse existing
        existing = glob.glob(os.path.join(outdir, f"{vid}_*.*"))
        existing = [f for f in existing if not f.endswith("_transcript.txt") and not f.endswith("_quiz.txt")]
        
        if existing:
            if messagebox.askyesno("Reuse", f"{Path(existing[0]).name} exists. Re‑use?"):
                self._log(f"Using existing file: {Path(existing[0]).name}", success=True)
                return existing[0]
            for f in existing:
                os.remove(f)

        self._log(f"Starting light download for {url}")
        self._update_prog(30, "Preparing video download...")

        if self.ffmpeg:
            # FFmpeg available → can merge, so allow split streams (still ≤360 p)
            fmt = "bestvideo[height<=360]+bestaudio/best[height<=360]"
            merge = "mp4"
        else:
            # No FFmpeg → **progressive MP4 only**, ≤360 p
            fmt = "best[height<=360][ext=mp4]/best[ext=mp4]"
            merge = None

        ydl_opts = {
            "format": fmt,
            "quiet": True,
            "paths": {"home": outdir},
            "outtmpl": f"{vid}_orig.%(ext)s",
            "progress_hooks": [self._dl_hook],
            "noplaylist": True,
        }
        if merge:
            ydl_opts["merge_output_format"] = merge

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            file = glob.glob(os.path.join(outdir, f"{vid}_orig.*"))[0]
            self._log(f"Downloaded: {info.get('title','(title‑unknown)')}", success=True)
            return file
        except Exception as e:
            self._log(f"Download error: {e}", error=True)
            return None

    # ─────────────── optional audio‑only conversion ─────────────── #
    def _maybe_audio_only(self, filepath:str):
        if not filepath or not self.ffmpeg:
            return filepath
            
        audio_path = Path(filepath).with_suffix(".mp3").with_name(Path(filepath).stem.replace("_orig", "_audio")+".mp3")
        if audio_path.exists():
            return str(audio_path)
            
        self._log("Extracting audio for smaller upload...")
        cmd = [self.ffmpeg, "-y", "-i", filepath, "-vn", "-acodec", "libmp3lame", "-q:a", "7", str(audio_path)]
        
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            self._log(f"Created audio file: {audio_path}", success=True)
            return str(audio_path)
        except Exception as e:
            self._log(f"FFmpeg error: {e}", error=True)
            return filepath

    # ─────────────── Gemini upload + poll ─────────────── #
    def _upload_and_wait(self, fpath:str, timeout=600, interval=5):
        self._log("Uploading to Gemini...")
        self._update_prog(60, "Uploading to Gemini...")
        
        try:
            gfile = genai.upload_file(fpath)
        except Exception as e:
            self._log(f"Upload failed: {e}", error=True)
            return None
            
        bar = tqdm(total=timeout//interval, bar_format="{l_bar}{bar}| {remaining}", leave=False)
        start = time.time()
        
        while time.time() - start < timeout:
            if self.cancel_flag:
                self._log("Cancelled.")
                bar.close()
                return None
                
            f = genai.get_file(gfile.name)
            st = getattr(f, "state", "UNKNOWN")
            
            if st == "ACTIVE":
                bar.close()
                self._log("Gemini file ACTIVE.", success=True)
                return f
                
            if st == "FAILED":
                bar.close()
                self._log("Gemini processing FAILED.", error=True)
                return None
                
            bar.update(1)
            self._update_prog(70, f"Gemini: {st}...")
            time.sleep(interval)
            
        bar.close()
        self._log("Gemini poll timed out.", error=True)
        return None

    # ─────────────── Quiz generation ─────────────── #
    def _make_quiz_from_transcript(self, transcript_path, n:int):
        if not transcript_path:
            self._log("No transcript available for quiz generation", error=True)
            return None
            
        self._log("Generating quiz from transcript...")
        self._update_prog(80, "Generating quiz from transcript...")
        
        try:
            # Read transcript content
            with open(transcript_path, 'r', encoding='utf-8') as f:
                transcript_text = f.read()
                
            # Try with a smaller model first to avoid quota issues
            try:
                model = genai.GenerativeModel("gemini-1.0-pro")
                
                # Reduce transcript size to avoid token limits and quota issues
                # Keep first 15000 chars which should be enough for most videos
                transcript_excerpt = transcript_text[:15000]
                
                prompt = f"""
                Generate {n} diverse quiz questions with answers based on the following transcript from a YouTube video:
                
                {transcript_excerpt}
                
                Format each question as:
                
                ## Question X: [question text]
                **Answer:** [detailed answer]
                
                Make sure questions cover different topics and areas of the content. Include a mix of:
                - Factual recall questions
                - Concept understanding questions
                - Application questions where appropriate
                
                Aim for clear, concise questions that test important concepts from the transcript.
                """
                
                # Use temperature to reduce token usage
                rsp = model.generate_content(prompt, generation_config={"temperature": 0.2})
                return rsp.text
                
            except Exception as e:
                self._log(f"Warning: Gemini API error: {e}")
                self._log("Trying offline quiz generation...", success=True)
                
                # Offline fallback - basic question generation
                return self._generate_basic_questions(transcript_text, n)
            
        except Exception as e:
            self._log(f"Quiz generation error: {e}", error=True)
            return None

    def _make_quiz_from_media(self, gfile, transcript_path, n:int):
        self._log("Generating quiz from video/audio...")
        self._update_prog(90, "Generating quiz from media...")
        
        # Load transcript text if available
        transcript_text = ""
        if transcript_path:
            try:
                with open(transcript_path, 'r', encoding='utf-8') as f:
                    transcript_text = f.read()[:25000]  # Limit size
            except Exception as e:
                self._log(f"Warning: couldn't read transcript: {e}", error=True)
        
        # Create prompt
        if transcript_text:
            prompt = f"""
            Generate {n} diverse quiz questions with answers about the uploaded video/audio.
            
            Use both the media content and this transcript excerpt to create accurate questions:
            
            TRANSCRIPT EXCERPT:
            {transcript_text[:10000]}  # Reduced excerpt size
            
            Format each question as:
            
            ## Question X: [question text]
            **Answer:** [detailed answer]
            """
        else:
            prompt = f"""
            Generate {n} diverse quiz questions with answers about the uploaded video or audio.
            
            Format each question as:
            
            ## Question X: [question text]
            **Answer:** [detailed answer]
            
            Make questions diverse and cover the key concepts from the content.
            """
            
        try:
            # Try with smaller model first to avoid quota issues
            model = genai.GenerativeModel("gemini-1.0-pro")
            rsp = model.generate_content([prompt, gfile], generation_config={"temperature": 0.2})
            return rsp.text
        except Exception as e:
            self._log(f"Warning: Gemini API error: {e}")
            if transcript_text:
                self._log("Falling back to offline quiz generation using transcript", success=True)
                return self._generate_basic_questions(transcript_text, n)
            else:
                self._log("No transcript available for fallback", error=True)
                return None
        except Exception as e:
            self._log(f"Gemini generate error: {e}", error=True)
            return None

    def _save_quiz(self, txt:str, base:str):
        out = Path(self.out_dir.get()) / f"{base}_quiz.txt"
        try:
            out.write_text(txt, encoding="utf-8")
            self._log(f"Quiz saved to {out}", success=True)
        except Exception as e:
            self._log(f"Save error: {e}", error=True)

    # ─────────────── threaded pipeline ─────────────── #
    def _start(self):
        if self.processing: 
            return
            
        if not self.video_url.get().strip():
            messagebox.showwarning("Missing URL", "Enter a YouTube URL.")
            return
            
        self._toggle_widgets(True)
        self.cancel_flag = False
        threading.Thread(target=self._pipeline, daemon=True).start()

    def _cancel(self):
        if self.processing:
            self.cancel_flag = True
            self._log("User requested cancel...")

    def _generate_basic_questions(self, transcript_text, n):
        """Generate basic quiz questions from transcript without using API"""
        self._log("Generating basic questions from transcript text...")
        
        # Only use the first portion of transcript to ensure we focus on the main content
        text = transcript_text[:20000]
        
        # Extract sentences and normalize
        import re
        sentences = re.split(r'(?<=[.!?])\s+', text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 20]  # Only meaningful sentences
        
        if not sentences:
            return "Failed to extract meaningful content from transcript."
        
        # Identify key sentences (every Nth sentence based on desired question count)
        n = min(n, len(sentences) // 2)  # Don't try to make more questions than we have material for
        n = max(3, n)  # At least 3 questions
        
        # Get evenly spaced sentences through the content
        step = max(1, len(sentences) // (n + 1))
        key_sentences = [sentences[i] for i in range(step, len(sentences), step)][:n]
        
        # Generate questions (simple transformation of statements to questions)
        questions = []
        for i, sentence in enumerate(key_sentences, 1):
            # Clean up the sentence
            sentence = re.sub(r'\s+', ' ', sentence).strip()
            
            # Convert statement to question using basic transformations
            question = sentence
            
            # Replace pronouns with "what/who" question words
            question = re.sub(r'^(I|We|They|He|She|It)\s+(\w+)', r'Who \2', question, flags=re.IGNORECASE)
            
            # Add question word at beginning for sentences with "is/are/was/were"
            if not re.match(r'^(What|Who|How|Why|When|Where)', question, re.IGNORECASE):
                for verb in ['is', 'are', 'was', 'were', 'has', 'have', 'had']:
                    pattern = f'\\b{verb}\\b'
                    if re.search(pattern, question, re.IGNORECASE):
                        question = f"What {question.lower()}?"
                        break
                else:
                    # If no linking verb found, make it a "what about" question
                    question = f"What can you say about: {question}?"
            
            # Ensure it ends with question mark
            if not question.endswith('?'):
                question = question + '?'
            
            # Create answer (simplified - using original sentence and surrounding context)
            context_start = max(0, sentences.index(sentence) - 1)
            context_end = min(len(sentences), sentences.index(sentence) + 2)
            answer = ' '.join(sentences[context_start:context_end])
            
            questions.append(f"## Question {i}: {question}\n**Answer:** {answer}\n")
        
        return "\n".join(questions)

    def _pipeline(self):
        url = self.video_url.get().strip()
        base = self._yt_id(url)
        transcript_path = None
        quiz = None
        
        try:
            # 1. Download transcript
            self._update_prog(10, "Getting transcript...")
            transcript_path = self._download_transcript(url)
            
            # Check if offline mode is selected
            if self.offline_mode_var.get():
                if transcript_path:
                    self._log("Running in offline mode, generating questions locally...")
                    self._update_prog(50, "Generating offline questions...")
                    
                    with open(transcript_path, 'r', encoding='utf-8') as f:
                        transcript_text = f.read()
                    
                    quiz = self._generate_basic_questions(transcript_text, int(self.num_q.get()))
                else:
                    self._log("Transcript required for offline mode but not available.", error=True)
                    raise RuntimeError("transcript")
            elif self.transcript_only_var.get():
                # Use transcript-only approach with API
                if transcript_path:
                    self._update_prog(60, "Generating quiz from transcript...")
                    quiz = self._make_quiz_from_transcript(transcript_path, int(self.num_q.get()))
                else:
                    self._log("Transcript required but not available.", error=True)
                    raise RuntimeError("transcript")
            else:
                # Download video and use both for better results
                vidfile = self._download_video(url)
                
                if not vidfile and not transcript_path:
                    self._log("Neither video nor transcript available.", error=True)
                    raise RuntimeError("download")
                
                if vidfile and not self.cancel_flag:
                    # Process video
                    upfile = self._maybe_audio_only(vidfile)
                    
                    # Only attempt API upload if not in offline mode
                    if not self.offline_mode_var.get():
                        gfile = self._upload_and_wait(upfile)
                        
                        if gfile:
                            # Generate quiz using both media and transcript
                            quiz = self._make_quiz_from_media(gfile, transcript_path, int(self.num_q.get()))
                    else:
                        # In offline mode with video, still use transcript
                        self._log("Offline mode - skipping API upload, using transcript only")
                        with open(transcript_path, 'r', encoding='utf-8') as f:
                            transcript_text = f.read()
                        quiz = self._generate_basic_questions(transcript_text, int(self.num_q.get()))
                        
                elif transcript_path and not self.cancel_flag:
                    # Fall back to transcript-only
                    if self.offline_mode_var.get():
                        self._log("Using offline transcript processing")
                        with open(transcript_path, 'r', encoding='utf-8') as f:
                            transcript_text = f.read()
                        quiz = self._generate_basic_questions(transcript_text, int(self.num_q.get()))
                    else:
                        self._log("Video unavailable, using transcript only with API")
                        quiz = self._make_quiz_from_transcript(transcript_path, int(self.num_q.get()))
            
            if not quiz and not self.cancel_flag:
                raise RuntimeError("quiz")
                
            # Save the quiz if we have one and weren't canceled
            if quiz and not self.cancel_flag:
                self._save_quiz(quiz, base)
            
        except RuntimeError as e:
            if not self.cancel_flag:  # Don't show error if user canceled
                self._log(f"Process failed at stage: {str(e)}", error=True)
        finally:
            if not self.cancel_flag:
                self._update_prog(100, "Complete")
                time.sleep(1)
            self._update_prog(0, "")
            self._toggle_widgets(False)
            self._log("Process finished.", success=True)

# ──────────────────── main ──────────────────── #
def main():
    root = tk.Tk()
    TranscriptQuizApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()