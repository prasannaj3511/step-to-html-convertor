"""A small desktop launcher: pick several STEP files, choose settings, convert.

Everything here is also reachable from the command line; this exists so that
selecting a batch of files and a quality preset does not require remembering
flag names. Conversion runs on a worker thread and streams its log into the
window, so a multi-minute job on a large assembly stays responsive and
cancellable by closing the window.
"""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .cli import STEP_SUFFIXES, DEFAULT_ROOT
from .gltf import PackOptions
from .logutil import human_bytes
from .meshing import QUALITY_PRESETS
from .pipeline import ConvertOptions, convert_file

COLOR_SCHEMES = ["auto", "neutral", "palette", "metal", "uniform", "preserve"]


class ConverterApp:
    def __init__(self, root: tk.Tk, initial: list[Path] | None = None,
                 out_dir: Path | None = None) -> None:
        self.root = root
        self.files: list[Path] = list(initial or [])
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()

        root.title(f"iSTP2HTML {__version__} - STEP to HTML converter")
        root.geometry("980x680")
        root.minsize(820, 560)

        self._build_widgets(out_dir)
        self._refresh_list()
        self.root.after(80, self._drain)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self, out_dir: Path | None) -> None:
        pad = dict(padx=8, pady=4)

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # --- file selection ---------------------------------------------
        files_frame = ttk.LabelFrame(outer, text="STEP files to convert", padding=8)
        files_frame.pack(fill="both", expand=True)

        btns = ttk.Frame(files_frame)
        btns.pack(side="left", fill="y")
        ttk.Button(btns, text="Add files…", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Add folder…", command=self.add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove selected", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear all", command=self.clear_files).pack(fill="x", pady=2)
        ttk.Separator(btns, orient="horizontal").pack(fill="x", pady=8)
        self.count_label = ttk.Label(btns, text="0 files")
        self.count_label.pack(fill="x")

        list_wrap = ttk.Frame(files_frame)
        list_wrap.pack(side="left", fill="both", expand=True, padx=(8, 0))
        scroll = ttk.Scrollbar(list_wrap, orient="vertical")
        # extended = click, shift-click and ctrl-click all work for multi-select
        self.listbox = tk.Listbox(list_wrap, selectmode="extended", activestyle="dotbox",
                                  yscrollcommand=scroll.set)
        scroll.config(command=self.listbox.yview)
        scroll.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)

        # --- options ------------------------------------------------------
        opts = ttk.LabelFrame(outer, text="Conversion settings", padding=8)
        opts.pack(fill="x", pady=(10, 0))

        row1 = ttk.Frame(opts)
        row1.pack(fill="x")

        ttk.Label(row1, text="Quality").grid(row=0, column=0, sticky="w", **pad)
        self.quality = tk.StringVar(value="high")
        qbox = ttk.Combobox(row1, textvariable=self.quality, state="readonly", width=12,
                            values=list(QUALITY_PRESETS))
        qbox.grid(row=0, column=1, sticky="w", **pad)
        self.quality_hint = ttk.Label(row1, text="", foreground="#666")
        self.quality_hint.grid(row=0, column=2, sticky="w", **pad)
        qbox.bind("<<ComboboxSelected>>", lambda _e: self._update_quality_hint())

        ttk.Label(row1, text="Colours").grid(row=0, column=3, sticky="w", **pad)
        self.colors = tk.StringVar(value="auto")
        ttk.Combobox(row1, textvariable=self.colors, state="readonly", width=10,
                     values=COLOR_SCHEMES).grid(row=0, column=4, sticky="w", **pad)

        ttk.Label(row1, text="Units").grid(row=0, column=5, sticky="w", **pad)
        self.units = tk.StringVar(value="MM")
        ttk.Combobox(row1, textvariable=self.units, state="readonly", width=7,
                     values=["MM", "CM", "M", "INCH"]).grid(row=0, column=6, sticky="w", **pad)

        row2 = ttk.Frame(opts)
        row2.pack(fill="x", pady=(4, 0))

        self.single_file = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row2, variable=self.single_file,
            text="Single self-contained .html per STEP (no separate .glb; opens without a server)",
        ).pack(side="left", **pad)

        self.part_metrics = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, variable=self.part_metrics,
                        text="Per-part metrics in manifest").pack(side="left", **pad)

        row3 = ttk.Frame(opts)
        row3.pack(fill="x", pady=(4, 0))
        ttk.Label(row3, text="Output folder").pack(side="left", **pad)
        self.out_dir = tk.StringVar(value=str(out_dir or (DEFAULT_ROOT / "output-html")))
        ttk.Entry(row3, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, **pad)
        ttk.Button(row3, text="Browse…", command=self.pick_out_dir).pack(side="left", **pad)

        # --- actions ------------------------------------------------------
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.convert_btn = ttk.Button(actions, text="Convert", command=self.start)
        self.convert_btn.pack(side="left", **pad)
        self.open_btn = ttk.Button(actions, text="Open output folder",
                                   command=self.open_output, state="disabled")
        self.open_btn.pack(side="left", **pad)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, **pad)
        self.status = ttk.Label(actions, text="Ready", width=22, anchor="e")
        self.status.pack(side="left", **pad)

        # --- log ------------------------------------------------------------
        log_frame = ttk.LabelFrame(outer, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        lscroll = ttk.Scrollbar(log_frame, orient="vertical")
        self.log = tk.Text(log_frame, height=12, wrap="none", yscrollcommand=lscroll.set,
                           background="#14171c", foreground="#dfe4ec", insertbackground="#dfe4ec",
                           font=("Consolas", 9), state="disabled")
        lscroll.config(command=self.log.yview)
        lscroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self._update_quality_hint()

    def _update_quality_hint(self) -> None:
        preset = QUALITY_PRESETS.get(self.quality.get())
        self.quality_hint.config(text=f"({preset.label})" if preset else "")

    # --------------------------------------------------------------- files
    def add_files(self) -> None:
        picked = filedialog.askopenfilenames(
            title="Select one or more STEP files (Ctrl/Shift-click for several)",
            filetypes=[("STEP files", "*.step *.stp *.STEP *.STP *.p21"), ("All files", "*.*")],
        )
        self._add([Path(p) for p in picked])

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder containing STEP files")
        if not folder:
            return
        found = sorted(
            f for f in Path(folder).rglob("*")
            if f.is_file() and f.suffix.lower() in STEP_SUFFIXES
        )
        if not found:
            messagebox.showinfo("No STEP files", f"No .step/.stp files found under\n{folder}")
            return
        self._add(found)

    def _add(self, paths: list[Path]) -> None:
        known = {p.resolve() for p in self.files}
        for p in paths:
            if p.resolve() not in known:
                self.files.append(p)
                known.add(p.resolve())
        self._refresh_list()

    def remove_selected(self) -> None:
        for i in sorted(self.listbox.curselection(), reverse=True):
            del self.files[i]
        self._refresh_list()

    def clear_files(self) -> None:
        self.files.clear()
        self._refresh_list()

    def _refresh_list(self) -> None:
        self.listbox.delete(0, "end")
        total = 0
        for f in self.files:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            total += size
            self.listbox.insert("end", f"{f.name}    [{human_bytes(size)}]    {f.parent}")
        self.count_label.config(
            text=f"{len(self.files)} file(s)\n{human_bytes(total)} total"
        )

    def pick_out_dir(self) -> None:
        folder = filedialog.askdirectory(title="Choose the output folder",
                                         initialdir=self.out_dir.get())
        if folder:
            self.out_dir.set(folder)

    def open_output(self) -> None:
        import os
        import subprocess
        import sys

        path = Path(self.out_dir.get())
        if not path.exists():
            return
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - opening a user-chosen folder
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])

    # ----------------------------------------------------------- conversion
    def _emit(self, kind: str, payload: object) -> None:
        self.messages.put((kind, payload))

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showwarning("Nothing to convert", "Add at least one STEP file first.")
            return

        out_dir = Path(self.out_dir.get())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Output folder", f"Cannot use that folder:\n{exc}")
            return

        self.convert_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.progress.config(value=0, maximum=len(self.files))
        self._clear_log()

        opts = ConvertOptions(
            quality=self.quality.get(),
            color_scheme=self.colors.get(),
            units=self.units.get(),
            single_file=self.single_file.get(),
            part_metrics=self.part_metrics.get(),
            pack=PackOptions(),
        )
        files = list(self.files)
        self.cancel.clear()
        self.worker = threading.Thread(
            target=self._run, args=(files, out_dir, opts), daemon=True
        )
        self.worker.start()

    def _run(self, files: list[Path], out_dir: Path, opts: ConvertOptions) -> None:
        from .logutil import add_sink, remove_sink

        sink = lambda line: self._emit("log", line)  # noqa: E731
        add_sink(sink)
        manifests, failed = [], 0
        try:
            for idx, src in enumerate(files, start=1):
                if self.cancel.is_set():
                    break
                self._emit("status", f"{idx}/{len(files)}  {src.name}")
                try:
                    manifests.append(convert_file(src, out_dir, opts))
                except Exception as exc:
                    failed += 1
                    self._emit("log", f"ERROR {src.name}: {exc}")
                    self._emit("log", traceback.format_exc())
                self._emit("progress", idx)

            if manifests:
                self._emit("status", "building viewer")
                from .html import write_viewer

                write_viewer(out_dir, manifests, single_file=opts.single_file)
        finally:
            remove_sink(sink)
            self._emit("done", (len(manifests), failed, out_dir))

    # ------------------------------------------------------------- plumbing
    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _append(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _drain(self) -> None:
        """Pump worker messages onto the Tk thread; Tk is not thread-safe."""
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append(str(payload))
                elif kind == "status":
                    self.status.config(text=str(payload))
                elif kind == "progress":
                    self.progress.config(value=int(payload))  # type: ignore[arg-type]
                elif kind == "done":
                    ok, failed, out_dir = payload  # type: ignore[misc]
                    self.convert_btn.config(state="normal")
                    self.open_btn.config(state="normal")
                    self.status.config(text=f"{ok} done, {failed} failed")
                    if ok:
                        self._append("")
                        self._append(f"Viewer written to {Path(out_dir) / 'index.html'}")
                        if not self.single_file.get():
                            self._append("Run 'python serve.py' in that folder to view it.")
        except queue.Empty:
            pass
        self.root.after(80, self._drain)


def run_gui(initial: list[Path] | None = None, out_dir: Path | None = None) -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    ConverterApp(root, initial=initial, out_dir=out_dir)
    root.mainloop()
    return 0
