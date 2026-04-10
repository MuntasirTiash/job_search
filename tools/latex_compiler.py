"""Compile a .tex file to PDF using pdflatex, with layout overflow checking."""

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def compile_latex(tex_path: str | Path, output_dir: str | Path | None = None) -> Path:
    """
    Run pdflatex on a .tex file and return the path to the generated PDF.

    Args:
        tex_path: Path to the .tex file.
        output_dir: Where to put the PDF. Defaults to same directory as .tex file.

    Returns:
        Path to the compiled PDF.

    Raises:
        RuntimeError: If pdflatex is not installed or compilation fails.
        FileNotFoundError: If resume.cls is missing from the output directory.
    """
    tex_path = Path(tex_path).resolve()
    if not tex_path.exists():
        raise FileNotFoundError(f"TeX file not found: {tex_path}")

    compile_dir = tex_path.parent

    # resume.cls must be present alongside the .tex file
    cls_path = compile_dir / "resume.cls"
    if not cls_path.exists():
        # Try to find it in the ignore/ folder
        fallback = Path(__file__).parent.parent / "ignore" / "resume.cls"
        if fallback.exists():
            shutil.copy(fallback, cls_path)
        else:
            raise FileNotFoundError(
                f"resume.cls not found in {compile_dir}. "
                "Copy resume.cls into the same directory as the .tex file."
            )

    if not shutil.which("pdflatex"):
        raise RuntimeError(
            "pdflatex not found. Install TeX Live: sudo apt install texlive-latex-extra"
        )

    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-output-directory", str(compile_dir), str(tex_path)],
        capture_output=True,
        text=True,
        cwd=compile_dir,
    )

    pdf_path = tex_path.with_suffix(".pdf")

    # Treat PDF existence as the primary success signal — pdflatex exits 1 on warnings
    if not pdf_path.exists():
        log = tex_path.with_suffix(".log")
        log_tail = ""
        if log.exists():
            lines = log.read_text().splitlines()
            log_tail = "\n".join(lines[-30:])
        raise RuntimeError(
            f"pdflatex failed (exit {result.returncode}) — no PDF produced.\n"
            f"Stderr: {result.stderr[:500]}\n"
            f"Log tail:\n{log_tail}"
        )

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / pdf_path.name
        shutil.move(str(pdf_path), dest)
        return dest

    return pdf_path


# ---------------------------------------------------------------------------
# Layout overflow checker
# ---------------------------------------------------------------------------

@dataclass
class OverflowReport:
    overflows: list[dict] = field(default_factory=list)   # overfull hbox entries
    margin_inches: dict   = field(default_factory=dict)   # parsed from geometry

    @property
    def has_overflow(self) -> bool:
        return bool(self.overflows)

    def summary(self) -> str:
        if not self.overflows:
            return "No layout overflows detected."
        lines = [f"Layout overflow — {len(self.overflows)} overfull \\hbox warning(s):"]
        for ov in self.overflows:
            lines.append(
                f"  Line {ov['line']:>4}: {ov['overhang_pt']:.1f}pt too wide"
                + (f"  [{ov['context']}]" if ov['context'] else "")
            )
        if self.margin_inches:
            m = self.margin_inches
            lines.append(
                f"Current margins — left:{m.get('left','?')}  right:{m.get('right','?')}  "
                f"top:{m.get('top','?')}  bottom:{m.get('bottom','?')}"
            )
        return "\n".join(lines)

    def worst_overhang_pt(self) -> float:
        return max((ov["overhang_pt"] for ov in self.overflows), default=0.0)


def check_layout(log_path: str | Path, tex_path: str | Path | None = None) -> OverflowReport:
    """
    Parse a pdflatex .log file for overfull \\hbox warnings and extract
    the geometry margins from the .tex source if provided.

    Returns an OverflowReport with all overflows and margin info.
    """
    log_path = Path(log_path)
    report   = OverflowReport()

    # --- Parse overfull hbox warnings from the log ---
    if log_path.exists():
        log_text = log_path.read_text(errors="replace")

        # Pattern: "Overfull \hbox (X.XXpt too wide) in paragraph at lines N--N"
        # or:      "Overfull \hbox (X.XXpt too wide) detected at line N"
        overfull_re = re.compile(
            r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)"
            r".*?(?:at lines? (\d+)(?:--\d+)?|detected at line (\d+))",
            re.DOTALL,
        )
        # Grab the short context snippet LaTeX sometimes prints after the warning
        lines = log_text.splitlines()
        for i, line in enumerate(lines):
            m = re.match(
                r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)", line
            )
            if not m:
                continue
            overhang = float(m.group(1))

            # Line number
            line_no = 0
            ln_match = re.search(r"at lines? (\d+)", line)
            if ln_match:
                line_no = int(ln_match.group(1))

            # Context: LaTeX prints the overflowing text on the NEXT non-blank line
            context = ""
            for j in range(i + 1, min(i + 4, len(lines))):
                candidate = lines[j].strip()
                if candidate and not candidate.startswith("("):
                    context = candidate[:80]
                    break

            report.overflows.append({
                "line":        line_no,
                "overhang_pt": overhang,
                "context":     context,
            })

    # --- Parse geometry margins from the .tex source ---
    if tex_path:
        tex_path = Path(tex_path)
        if tex_path.exists():
            tex_text = tex_path.read_text(errors="replace")
            geo_match = re.search(
                r"\\usepackage\[([^\]]*)\]\{geometry\}", tex_text
            )
            if geo_match:
                opts = geo_match.group(1)
                for key in ("left", "right", "top", "bottom"):
                    kv = re.search(rf"{key}=([0-9.]+\w+)", opts)
                    if kv:
                        report.margin_inches[key] = kv.group(1)

    return report
