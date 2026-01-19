# yt2stems is a GUI for downloading and splitting audio from YouTube and SoundCloud using yt-dlp, ffmpeg, and demucs.
# yt-dlp  ➜  MP3  ➜  Demucs Split  – with model selector, stem options & progress bar
# ---------------------------------------------------------------
#  Requires:  Python ≥3.9, ffmpeg on PATH, PySide6, yt-dlp, demucs, torch, soundfile
# ---------------------------------------------------------------

import os
import re
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QComboBox,
    QCheckBox, QPushButton, QTextEdit, QFileDialog, QProgressBar, QHBoxLayout
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer


import essentia.standard as es

# Path to the demucs runner wrapper (same directory as this script)
SCRIPT_DIR = Path(__file__).parent.resolve()
DEMUCS_RUNNER = SCRIPT_DIR / "demucs_runner.py"


def analyze_bpm_key(path: str) -> tuple[int, str]:
    """
    Analyze the audio file at `path` and return (tempo, key) using Essentia.
    """
    # Load audio at 44.1 kHz
    audio = es.MonoLoader(filename=path, sampleRate=44100)()
    # Extract tempo in BPM
    bpm, _, _, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    # Extract key and scale
    key, scale, strength = es.KeyExtractor()(audio)
    key_name = f"{key} {scale}"
    return int(round(bpm)), key_name


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are problematic in file paths."""
    # Replace problematic characters with underscores
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Replace multiple spaces/underscores with single underscore
    sanitized = re.sub(r'[\s_]+', '_', sanitized)
    # Remove leading/trailing underscores and spaces
    sanitized = sanitized.strip('_ ')
    return sanitized


# ────────────────────────── Worker thread ──────────────────────────
class StemWorker(QThread):
    log = Signal(str)      # textual logging
    done = Signal(str)     # finished / error message
    prog = Signal(int)     # 0-100 int

    def __init__(self, url: str, bitrate: str, model: str, two_stem: bool, outdir: Path, is_file: bool = False):
        super().__init__()
        self.url = url
        self.bitrate = bitrate
        self.model = model
        self.two_stem = two_stem
        self.outdir = outdir
        self.is_file = is_file

    def _run_subprocess(self, cmd: list[str], progress_offset: int = 0, progress_span: int = 0, env: dict = None):
        """Run *cmd* and stream stderr → progress callback."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        pattern = re.compile(r"(\d{1,3})%")
        
        # Read stderr for progress and errors
        stderr_lines = []
        for line in proc.stderr:
            stderr_lines.append(line)
            # Log each line so we can see what's happening
            stripped = line.strip()
            if stripped:
                self.log.emit(f"  → {stripped}")
            if progress_span:
                m = pattern.search(line)
                if m:
                    pct = int(m.group(1))
                    self.prog.emit(progress_offset + pct * progress_span // 100)
        
        # Also capture stdout
        stdout_lines = []
        for line in proc.stdout:
            stdout_lines.append(line)
        
        proc.wait()
        
        if proc.returncode:
            error_output = ''.join(stderr_lines)
            stdout_output = ''.join(stdout_lines)
            full_output = f"STDERR:\n{error_output}\nSTDOUT:\n{stdout_output}" if stdout_output else error_output
            raise RuntimeError(f"Command failed (exit {proc.returncode}):\n{full_output}")

    def run(self):
        tmp_dir = Path(tempfile.gettempdir())
        files_to_cleanup = []
        
        try:
            # 1️⃣  Prepare input (download or local file)
            if self.is_file:
                source_path = Path(self.url)
                title = sanitize_filename(source_path.stem)
                self.log.emit(f"📁  Using local file: {source_path.name}")
                
                # Copy to temp directory to avoid path issues
                tmp_path = tmp_dir / f"{title}{source_path.suffix}"
                if source_path != tmp_path:
                    shutil.copy2(source_path, tmp_path)
                    files_to_cleanup.append(tmp_path)
                self.prog.emit(10)
            else:
                import yt_dlp
                self.log.emit("⬇  Fetching & downloading audio …")
                self.prog.emit(0)
                ydl_opts = {
                    "quiet": True,
                    "format": "bestaudio/best",
                    "outtmpl": str(tmp_dir / "%(title)s.%(ext)s"),
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)
                    tmp_path = Path(ydl.prepare_filename(info))
                    title = sanitize_filename(tmp_path.stem)
                    files_to_cleanup.append(tmp_path)
                self.prog.emit(10)

            # 🔍 Analyze BPM and key
            bpm, key = analyze_bpm_key(str(tmp_path))
            self.log.emit(f"🎚️  Detected tempo: {bpm} BPM")
            self.log.emit(f"🔑  Estimated key: {key}")

            # 2️⃣  Transcode to MP3 (keep in temp dir to avoid path issues with demucs)
            self.log.emit("🔄  Transcoding to MP3 …")
            mp3_filename = f"{title}_{self.bitrate}k.mp3"
            mp3_temp_path = tmp_dir / mp3_filename
            files_to_cleanup.append(mp3_temp_path)
            
            ff_cmd = [
                "ffmpeg", "-y", "-i", str(tmp_path),
                "-vn", "-c:a", "libmp3lame", "-b:a", f"{self.bitrate}k", str(mp3_temp_path)
            ]
            self._run_subprocess(ff_cmd)
            self.prog.emit(40)

            # 3️⃣  Split with Demucs using our wrapper (output to temp dir first)
            self.log.emit(f"🎛️  Splitting with Demucs ({self.model}) …")
            demucs_temp_out = tmp_dir / "demucs_output"
            
            # Use the demucs_runner.py wrapper instead of calling demucs directly
            demucs_cmd = [
                sys.executable, str(DEMUCS_RUNNER),
                str(mp3_temp_path), "-o", str(demucs_temp_out), "-n", self.model,
            ]
            if self.two_stem:
                demucs_cmd += ["--two-stems", "vocals"]
            
            self._run_subprocess(demucs_cmd, progress_offset=40, progress_span=50)
            self.prog.emit(90)

            # 4️⃣  Move results to final output directory
            self.log.emit("📦  Moving stems to output folder …")
            stems_source = demucs_temp_out / self.model / mp3_temp_path.stem
            stems_dest = self.outdir / self.model / title
            
            if stems_source.exists():
                stems_dest.mkdir(parents=True, exist_ok=True)
                for stem_file in stems_source.iterdir():
                    dest_file = stems_dest / stem_file.name
                    shutil.move(str(stem_file), str(dest_file))
                    self.log.emit(f"  → {stem_file.name}")
                
                # Clean up demucs temp output
                shutil.rmtree(demucs_temp_out, ignore_errors=True)
            else:
                self.log.emit(f"⚠️  Warning: Expected stems folder not found at {stems_source}")

            # Also copy the MP3 to output directory
            mp3_final_path = self.outdir / mp3_filename
            shutil.copy2(mp3_temp_path, mp3_final_path)
            
            self.prog.emit(100)
            self.done.emit(f"✅  Finished – stems ready in {stems_dest}")
            
        except Exception as e:
            self.done.emit(f"❌  Error: {e}")
        finally:
            # Cleanup temp files
            for f in files_to_cleanup:
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass


# ──────────────────────────── GUI ─────────────────────────────────
class MainWindow(QWidget):
    # Model key, description for dropdown
    MODEL_OPTIONS = [
        ("htdemucs",    "htdemucs (4 stems, fast)"),
        ("htdemucs_ft", "htdemucs_ft (4 stems, fine-tuned)"),
        ("htdemucs_6s", "htdemucs_6s (6 stems: drums, bass, vocals, guitar, piano, other)"),
        ("mdx",         "mdx (4 stems, fastest)"),
        ("mdx_extra_q", "mdx_extra_q (4 stems, highest quality)"),
    ]

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setWindowTitle("YT2Stems")
        self.resize(680, 520)

        # URL input
        self.url_edit = QLineEdit(placeholderText="Paste YouTube / SoundCloud link OR drag & drop file (mp3, wav, flac, m4a) here")

        # File chooser button
        self.file_btn = QPushButton("Choose file…")
        self.file_btn.setToolTip("Select a local audio file instead of a URL")

        # Bitrate selector
        self.bitrate_combo = QComboBox()
        for br in ("96", "128", "192", "320"):
            self.bitrate_combo.addItem(f"{br} kbps", br)
        self.bitrate_combo.setCurrentIndex(3)

        # Model selector
        self.model_combo = QComboBox()
        for key, desc in self.MODEL_OPTIONS:
            self.model_combo.addItem(desc, key)

        # Two-stem mode
        self.twoStemChk = QCheckBox("2 stems (vocals + accompaniment)")

        # Output folder choice
        self.out_btn = QPushButton("Choose output folder …")
        self.out_lbl = QLabel("(current directory)")
        self.out_dir = Path.cwd()

        # Run button, progress bar, log
        self.run_btn = QPushButton("Download & Split")
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        self.log_view = QTextEdit(readOnly=True)

        # Layout
        lay = QVBoxLayout(self)
        # youtube and soundcloud links are supported
        lay.addWidget(QLabel("URL / File:"));
        lay.addWidget(self.url_edit)
        lay.addWidget(self.file_btn)
        hb = QHBoxLayout();
        hb.addWidget(QLabel("Bitrate:")); hb.addWidget(self.bitrate_combo)
        hb.addWidget(QLabel("Model:")); hb.addWidget(self.model_combo)
        lay.addLayout(hb)
        lay.addWidget(self.twoStemChk)
        lay.addWidget(self.out_btn); lay.addWidget(self.out_lbl)
        lay.addWidget(self.run_btn); lay.addWidget(self.progress); lay.addWidget(self.log_view)            

        # Auto-populate from clipboard if it contains a valid URL
        clipboard = QApplication.clipboard()
        clipboard_text = clipboard.text().strip()
        if clipboard_text and self._is_valid_url(clipboard_text):
            self.url_edit.setText(clipboard_text)
            
            # Show subtle banner at top of window
            self.banner = QLabel("📋 YouTube link detected — select output folder to begin")
            self.banner.setStyleSheet("""
                QLabel {
                    background-color: #3b82f6;
                    color: white;
                    padding: 8px;
                    font-weight: bold;
                    border-radius: 4px;
                }
            """)
            self.banner.setAlignment(Qt.AlignCenter)
            lay.insertWidget(0, self.banner)  # Insert at top of layout
            
            # Close popup and open folder picker together
            def open_picker():
                self.pick_outdir()

            QTimer.singleShot(500, open_picker)

        # Signals
        self.out_btn.clicked.connect(self.pick_outdir)
        self.run_btn.clicked.connect(self.start_job)
        self.file_btn.clicked.connect(self.choose_file)

    def pick_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output folder")
        if d:
            self.out_dir = Path(d); self.out_lbl.setText(str(self.out_dir))
        # Hide banner if it exists
        if hasattr(self, 'banner'):
            self.banner.hide()

    def start_job(self):
        url = self.url_edit.text().strip()
        is_file = Path(url).is_file()
        if not url:
            self.log("Paste a link first."); return
        
        # Check that demucs_runner.py exists
        if not DEMUCS_RUNNER.exists():
            self.log(f"❌ Error: demucs_runner.py not found at {DEMUCS_RUNNER}")
            self.log("Please ensure demucs_runner.py is in the same directory as yt2stems.py")
            return
        
        br = self.bitrate_combo.currentData()
        model = self.model_combo.currentData()
        two = self.twoStemChk.isChecked()
        self.log(f"\n---  New job  ( {br} kbps  |  {model}  |  {'2-stem' if two else 'full'}) ---")
        self.progress.setValue(0); self.run_btn.setEnabled(False)

        self.worker = StemWorker(url, br, model, two, self.out_dir, is_file)
        self.worker.log.connect(self.log)
        self.worker.done.connect(self.job_done)
        self.worker.prog.connect(self.progress.setValue)
        self.worker.start()

    def log(self, msg: str):
        self.log_view.append(msg)
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def job_done(self, msg: str):
        self.log(msg); self.run_btn.setEnabled(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).suffix.lower() in ('.mp3', '.wav', '.flac', '.m4a'):
                self.url_edit.setText(path)
        event.accept()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select audio file",
            "",
            "Audio Files (*.mp3 *.wav *.flac *.m4a)"
        )
        if path:
            self.url_edit.setText(path)

    # Add this method to the MainWindow class
    def _is_valid_url(self, text: str) -> bool:
        """Check if text looks like a YouTube or SoundCloud URL."""
        patterns = [
            r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/',
            r'(https?://)?(www\.)?soundcloud\.com/',
        ]
        return any(re.match(pattern, text) for pattern in patterns)


# ───────────────────────────  main  ───────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    wnd = MainWindow(); wnd.show()
    sys.exit(app.exec())