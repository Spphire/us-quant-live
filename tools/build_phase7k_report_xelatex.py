from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "reports" / "phase7k_strategy_technical_report.md"
TEX = ROOT / "reports" / "phase7k_strategy_technical_report.tex"
PDF = ROOT / "reports" / "phase7k_strategy_technical_report_xelatex.pdf"
BUILD_DIR = ROOT / "artifacts" / "phase7k_report_xelatex"


def escape_text(text: str) -> str:
    segments: list[str] = []
    pattern = re.compile(r"(\\\(.+?\\\)|`[^`]+`)")
    pos = 0
    for match in pattern.finditer(text):
        segments.append(_escape_plain(text[pos : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            segments.append(r"\texttt{" + _escape_plain(token[1:-1]) + "}")
        else:
            segments.append(token)
        pos = match.end()
    segments.append(_escape_plain(text[pos:]))
    return "".join(segments)


def _escape_plain(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def table_to_latex(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for raw in lines:
        parts = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} for cell in parts):
            continue
        rows.append(parts)
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    for row in rows:
        row.extend([""] * (col_count - len(row)))
    colspec = "p{" + "0.95\\linewidth" + "}" if col_count == 1 else "|".join(["X"] * col_count)
    out = [r"\begin{tabularx}{\linewidth}{" + colspec + r"}", r"\toprule"]
    for idx, row in enumerate(rows):
        out.append(" & ".join(escape_text(cell) for cell in row) + r" \\")
        out.append(r"\midrule" if idx == 0 else "")
    out.append(r"\bottomrule")
    out.append(r"\end{tabularx}")
    return "\n".join(line for line in out if line)


def convert(md: str) -> str:
    lines = md.splitlines()
    body: list[str] = []
    i = 0
    in_code = False
    code_lines: list[str] = []
    in_display_math = False
    math_lines: list[str] = []
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            if in_code:
                body.append(r"\begin{verbatim}")
                body.extend(code_lines)
                body.append(r"\end{verbatim}")
                code_lines = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue
        if line.strip() == r"\[":
            in_display_math = True
            math_lines = [r"\["]
            i += 1
            continue
        if in_display_math:
            math_lines.append(line)
            if line.strip() == r"\]":
                body.extend(math_lines)
                in_display_math = False
                math_lines = []
            i += 1
            continue
        if not line.strip():
            body.append("")
            i += 1
            continue
        if line.startswith("|"):
            table_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].rstrip())
                i += 1
            body.append(table_to_latex(table_lines))
            body.append("")
            continue
        if line.startswith("# "):
            body.append(r"\begin{center}{\LARGE " + escape_text(line[2:]) + r"}\end{center}")
        elif line.startswith("## "):
            title = line[3:]
            if title.startswith("附录"):
                body.append(r"\clearpage")
            if title == "摘要":
                body.append(r"\section*{" + escape_text(title) + "}")
                body.append(r"\addcontentsline{toc}{section}{" + escape_text(title) + "}")
            elif title.startswith("附录"):
                body.append(r"\section*{" + escape_text(title) + "}")
                body.append(r"\addcontentsline{toc}{section}{" + escape_text(title) + "}")
            else:
                body.append(r"\section{" + escape_text(_strip_leading_number(title)) + "}")
        elif line.startswith("### "):
            body.append(r"\subsection{" + escape_text(_strip_leading_number(line[4:])) + "}")
        elif line.startswith("- "):
            body.append(r"\begin{itemize}[leftmargin=1.5em]")
            while i < len(lines) and lines[i].startswith("- "):
                body.append(r"\item " + escape_text(lines[i][2:]))
                i += 1
            body.append(r"\end{itemize}")
            continue
        elif re.match(r"^\d+\. ", line):
            body.append(r"\begin{enumerate}[leftmargin=1.8em]")
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                item = re.sub(r"^\d+\. ", "", lines[i])
                body.append(r"\item " + escape_text(item))
                i += 1
            body.append(r"\end{enumerate}")
            continue
        else:
            body.append(escape_text(line) + "\n")
        i += 1

    img_dir = ROOT / "artifacts" / "phase7k_backtest" / "multi_cap_open_open_20160101_20260520_rerun"
    equity = img_dir / "equity_curve_compare.png"
    drawdown = img_dir / "drawdown_curve_compare.png"
    if equity.exists() and drawdown.exists():
        body.append(r"\clearpage")
        body.append(r"\section{附录 B 回测曲线}")
        for title, image in (("净值曲线对比", equity), ("回撤曲线对比", drawdown)):
            body.append(r"\subsection{" + escape_text(title) + "}")
            body.append(r"\begin{center}")
            body.append(r"\includegraphics[width=0.95\linewidth]{" + image.as_posix() + "}")
            body.append(r"\end{center}")

    return "\n".join(body)


def _strip_leading_number(title: str) -> str:
    return re.sub(r"^\s*(?:[0-9]+|[A-Z])(?:\.[0-9]+)*\s+", "", title).strip()


def main() -> int:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    preamble = r"""
\documentclass[11pt,a4paper]{article}
\usepackage{geometry}
\geometry{left=2.3cm,right=2.3cm,top=2.1cm,bottom=2.0cm}
\usepackage{fontspec}
\usepackage{xeCJK}
\setmainfont{Times New Roman}
\setsansfont{Arial}
\setmonofont{Consolas}
\setCJKmainfont{Noto Serif SC}
\setCJKsansfont{Noto Sans SC}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage{enumitem}
\usepackage{hyperref}
\usepackage{fancyhdr}
\usepackage{titlesec}
\usepackage{xcolor}
\hypersetup{colorlinks=true,linkcolor=black,urlcolor=black}
\pagestyle{fancy}
\fancyhf{}
\cfoot{\thepage}
\renewcommand{\headrulewidth}{0pt}
\titleformat{\section}{\large\bfseries}{\thesection}{0.8em}{}
\titleformat{\subsection}{\normalsize\bfseries}{\thesubsection}{0.8em}{}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.55em}
\renewcommand{\arraystretch}{1.25}
\begin{document}
"""
    ending = "\n\\end{document}\n"
    TEX.write_text(preamble + convert(SOURCE.read_text(encoding="utf-8")) + ending, encoding="utf-8")
    cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={BUILD_DIR}", str(TEX)]
    for _ in range(2):
        result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print(result.stdout[-4000:])
            print(result.stderr[-4000:])
            return result.returncode
    built = BUILD_DIR / TEX.with_suffix(".pdf").name
    if built.exists():
        PDF.write_bytes(built.read_bytes())
        print(PDF)
        return 0
    print(f"missing output pdf: {built}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
