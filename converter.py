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
        self.gemini_api_key = tk.StringVar()
        self.gemini_model = tk.StringVar(value="gemini-2.5-flash")  # Default to 2.5 Flash
        self.out_dir = tk.StringVar(value=os.path.join(os.getcwd(), "downloads"))
        self.progress_var = tk.DoubleVar()
        self.download_video_var = tk.BooleanVar(value=True)
        self.transcript_only_var = tk.BooleanVar(value=False)
        self.offline_mode_var = tk.BooleanVar(value=False)
        self.processing = False
        self.cancel_flag = False
        self.retry_count = 0
        self.max_retries = 3
        
        # Available Gemini models
        self.gemini_models = [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-pro",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-1.0-pro"
        ]
       
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

        # Gemini API Key
        api_frame = ttk.Frame(self.root, padding=10); api_frame.pack(fill=tk.X)
        ttk.Label(api_frame, text="Gemini API Key:", font=font_normal).grid(row=0, column=0, sticky="w")
        api_entry = ttk.Entry(api_frame, textvariable=self.gemini_api_key, width=60, show="*")
        api_entry.grid(row=0, column=1, padx=5, sticky="we")
        
        # Add buttons for API key management
        api_buttons = ttk.Frame(api_frame); api_buttons.grid(row=0, column=2)
        ttk.Button(api_buttons, text="Apply Key", command=self._apply_api_key).pack(side=tk.LEFT, padx=2)
        ttk.Button(api_buttons, text="Get Key Help", command=lambda: self._show_api_key_help(force=True)).pack(side=tk.LEFT, padx=2)
        
        # Gemini model selection
        model_frame = ttk.Frame(self.root, padding=(10,0,10,10)); model_frame.pack(fill=tk.X)
        ttk.Label(model_frame, text="Gemini Model:", font=font_normal).grid(row=0, column=0, sticky="w")
        model_dropdown = ttk.Combobox(model_frame, textvariable=self.gemini_model, 
                                    values=self.gemini_models, width=20, state="readonly")
        model_dropdown.grid(row=0, column=1, sticky="w", padx=5)
        model_dropdown.current(0)  # Set default to first item (gemini-2.5-flash)
        
        # Add model info button
        ttk.Button(model_frame, text="Model Info", command=self._show_model_info).grid(row=0, column=2, sticky="w", padx=5)

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
        
        # Make offline mode checkbox more visible
        offline_chk = ttk.Checkbutton(opt, text="Offline Mode (No API Calls)", 
                                    variable=self.offline_mode_var,
                                    command=self._toggle_offline_mode)
        offline_chk.grid(row=0, column=2, sticky="w", padx=10)


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


        self._log("Ready. Enter a YouTube URL and Gemini API Key, then click 'Start Analysis'.", success=True)
        self._log("Currently using Gemini 2.5 Flash model.", success=True)
        self._log("For offline mode (no API key needed), check the 'Offline Mode' option.", success=True)
        
    def _show_model_info(self):
        """Show information about Gemini models"""
        info_msg = """
Gemini Model Options:

• gemini-2.5-flash: Latest lightweight model (very fast, efficient)
• gemini-2.5-pro: Latest full-featured model (highest quality)
• gemini-2.0-flash: Previous generation lightweight model
• gemini-2.0-pro: Previous generation full-featured model
• gemini-1.5-flash: Older generation lightweight model
• gemini-1.5-pro: Older generation full-featured model
• gemini-1.0-pro: First generation model

For most quiz generation, gemini-2.5-flash is recommended
as it's fast and efficient while still providing good quality.
"""
        messagebox.showinfo("Gemini Model Information", info_msg)
        
    def _toggle_offline_mode(self):
        """Handle toggling of offline mode"""
        if self.offline_mode_var.get():
            self._log("Offline mode enabled - will not use Gemini API", success=True)
        else:
            # If turning off offline mode, check if we have a valid API key
            if self.gemini_api_key.get().strip():
                self._apply_api_key()
            else:
                self._log("You'll need to enter a Gemini API key to use online mode", error=True)
                
    def _show_api_key_help(self, force=False):
        """Show dialog with instructions on how to get a valid API key"""
        help_msg = """
To get a valid Gemini API key:

1. Go to Google AI Studio: https://makersuite.google.com/
2. Sign in with your Google account
3. Click on "Get API key" or go to Settings > API keys
4. Create a new API key or use an existing one
5. Copy the API key and paste it into the application

Note: If you don't want to use the API, you can enable "Offline Mode" 
which will generate simpler questions using local processing only.
"""
        messagebox.showinfo("Get a Gemini API Key", help_msg)

    # ─────────────── API key handling ─────────────── #
    def _apply_api_key(self):
        """Configure the API with the provided key and test if it's valid
        Returns True if key is valid, False otherwise"""
        key = self.gemini_api_key.get().strip()
        if not key:
            messagebox.showwarning("Missing API Key", "Please enter your Gemini API Key")
            return False
        
        try:
            genai.configure(api_key=key)
            
            # Test the API key with a simple request
            if self._test_api_key():
                self._log("Gemini API key validated and applied successfully!", success=True)
                # Save to .env file for future use
                self._save_api_key_to_env(key)
                # Turn off offline mode if it was enabled
                if self.offline_mode_var.get():
                    self.offline_mode_var.set(False)
                    self._log("Offline mode disabled since API key is valid.", success=True)
                return True
            else:
                self._log("Gemini API key appears to be invalid. Enabling offline mode.", error=True)
                self.offline_mode_var.set(True)
                self._show_api_key_help()
                return False
        except Exception as e:
            self._log(f"Gemini API key error: {e}", error=True)
            self.offline_mode_var.set(True)
            self._show_api_key_help()
            return False
            
    def _test_api_key(self):
        """Test if the API key is valid by making a simple request"""
        try:
            selected_model = self.gemini_model.get()
            self._log(f"Testing API key with model: {selected_model}")
            model = genai.GenerativeModel(selected_model)
            # Make a minimal API call to check if the key works
            response = model.generate_content("Hello", generation_config={"temperature": 0.1, "max_output_tokens": 10})
            return True
        except Exception as e:
            self._log(f"API key validation failed: {e}", error=True)
            return False
            
    def _show_api_key_help(self):
        """Show dialog with instructions on how to get a valid API key"""
        help_msg = """
To get a valid Gemini API key:

1. Go to Google AI Studio: https://makersuite.google.com/
2. Sign in with your Google account
3. Click on "Get API key" or go to Settings > API keys
4. Create a new API key or use an existing one
5. Copy the API key and paste it into the application

Note: You can continue using offline mode for now, but the generated questions will be simpler.
"""
        messagebox.showinfo("Get a Gemini API Key", help_msg)

    def _save_api_key_to_env(self, key):
        try:
            # Check if .env exists and read existing content
            env_content = ""
            if os.path.exists(".env"):
                with open(".env", "r") as f:
                    env_content = f.read()
            
            # Check if GEMINI_API_KEY already exists in the file
            if "GEMINI_API_KEY=" in env_content:
                # Replace existing key
                env_content = re.sub(r"GEMINI_API_KEY=.*", f"GEMINI_API_KEY={key}", env_content)
            else:
                # Add new key
                if env_content and not env_content.endswith("\n"):
                    env_content += "\n"
                env_content += f"GEMINI_API_KEY={key}\n"
            
            # Write updated content
            with open(".env", "w") as f:
                f.write(env_content)
            
            self._log("API key saved to .env file", success=True)
        except Exception as e:
            self._log(f"Failed to save API key to .env: {e}", error=True)

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
        # Try to load from the new environment variable name first
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            # Fall back to the old variable name for backward compatibility
            key = os.getenv("API_KEY")
        
        if key:
            self.gemini_api_key.set(key)
            try:
                genai.configure(api_key=key)
                
                # Test if the key is valid
                if self._test_api_key():
                    self._log("Gemini API configured from .env file.", success=True)
                else:
                    self._log("API key from .env file is invalid. Please obtain a new key or use offline mode.", error=True)
                    self.offline_mode_var.set(True)
                    self._show_api_key_help()
            except Exception as e:
                self._log(f"Gemini API configuration error: {e}", error=True)
                self.offline_mode_var.set(True)
                self._show_api_key_help()
        else:
            self._log("No Gemini API key found in .env file. Please enter your API key above or use offline mode.", error=True)
            self._show_api_key_help()


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
               
            # Try with the selected model
            try:
                selected_model = self.gemini_model.get()
                self._log(f"Using {selected_model} for quiz generation")
                model = genai.GenerativeModel(selected_model)
               
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
            # Use the selected model
            selected_model = self.gemini_model.get()
            self._log(f"Using {selected_model} for quiz generation from media")
            model = genai.GenerativeModel(selected_model)
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
           
        # Check for API key if not in offline mode
        if not self.offline_mode_var.get() and not self.gemini_api_key.get().strip():
            if messagebox.askyesno("Missing API Key", 
                                  "No Gemini API key entered. Would you like to enable offline mode instead?"):
                self.offline_mode_var.set(True)
                self._log("Enabled offline mode due to missing API key.", success=True)
            else:
                self._show_api_key_help(force=True)
                return
            
        # Apply the API key if it's not already configured and we're not in offline mode
        if not self.offline_mode_var.get():
            if not self._apply_api_key():
                if messagebox.askyesno("API Key Invalid", 
                                      "API key appears to be invalid. Would you like to enable offline mode instead?"):
                    self.offline_mode_var.set(True)
                    self._log("Enabled offline mode due to invalid API key.", success=True)
                else:
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
        
        # Store both the sentence and its index
        key_sentence_indices = [(i, sentences[i]) for i in range(step, len(sentences), step)][:n]
       
        # Generate questions (simple transformation of statements to questions)
        questions = []
        for i, (sent_idx, original_sentence) in enumerate(key_sentence_indices, 1):
            try:
                # Clean up the sentence
                sentence = re.sub(r'\s+', ' ', original_sentence).strip()
               
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
               
                # Create answer using surrounding context based on index (not searching)
                context_start = max(0, sent_idx - 1)
                context_end = min(len(sentences), sent_idx + 2)
                answer = ' '.join(sentences[context_start:context_end])
               
                questions.append(f"## Question {i}: {question}\n**Answer:** {answer}\n")
            except Exception as e:
                self._log(f"Error generating question {i}: {e}", error=True)
                # Add a simple fallback question if there's an error
                questions.append(f"## Question {i}: What is mentioned in the video?\n**Answer:** Please refer to the video content.\n")
       
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