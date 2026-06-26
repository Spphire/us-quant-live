from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "reports" / "alpaca_short_integer_position_management_report.md"
TEX = ROOT / "reports" / "alpaca_short_integer_position_management_report.tex"
PDF = ROOT / "reports" / "alpaca_short_integer_position_management_report.pdf"
BUILD_DIR = ROOT / "artifacts" / "short_integer_report_xelatex"


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


def escape_text(text: str) -> str:
    pieces: list[str] = []
    pattern = re.compile(r"(\\\(.+?\\\)|`[^`]+`)")
    pos = 0
    for match in pattern.finditer(text):
        pieces.append(_escape_plain(text[pos : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            pieces.append(r"\texttt{" + _escape_plain(token[1:-1]) + "}")
        else:
            pieces.append(token)
        pos = match.end()
    pieces.append(_escape_plain(text[pos:]))
    return "".join(pieces)


def _path_literal(text: str) -> str:
    for delim in ("|", "!", "+", ":", ";", "?"):
        if delim not in text:
            return rf"\path{delim}{text}{delim}"
    return r"\texttt{" + _escape_plain(text) + "}"


def escape_table_cell(text: str) -> str:
    """Escape a table cell while allowing long identifiers/paths to wrap."""
    pieces: list[str] = []
    pattern = re.compile(r"`([^`]+)`|([A-Za-z0-9][A-Za-z0-9_./:-]{11,}[A-Za-z0-9])")
    pos = 0
    for match in pattern.finditer(text):
        pieces.append(_escape_plain(text[pos : match.start()]))
        token = match.group(1) or match.group(2)
        pieces.append(_path_literal(token))
        pos = match.end()
    pieces.append(_escape_plain(text[pos:]))
    return "".join(pieces)


def _weighted_x(weight: float, align: str = "left") -> str:
    ragged = r"\RaggedLeft" if align == "right" else r"\RaggedRight"
    return rf">{{\hsize={weight:.3f}\hsize{ragged}\arraybackslash}}X"


def _table_layout(header: list[str]) -> tuple[list[tuple[float, str]], str]:
    if header == ["场景", "定义", "用途"]:
        return [(0.75, "left"), (1.55, "left"), (0.70, "left")], r"\footnotesize"
    if header == ["项目", "取值"]:
        return [(0.55, "left"), (1.45, "left")], r"\normalsize"
    if header == ["场景", "资金", "期末净值", "总收益", "年化收益", "最大回撤", "年化波动", "夏普"]:
        return [
            (1.70, "left"),
            (0.75, "right"),
            (0.80, "right"),
            (0.90, "right"),
            (0.95, "right"),
            (1.00, "right"),
            (0.95, "right"),
            (0.95, "right"),
        ], r"\scriptsize"
    if header == ["场景", "资金", "总收益变化", "年化收益变化", "最大回撤变化", "波动变化", "夏普变化"]:
        return [
            (1.55, "left"),
            (0.75, "right"),
            (1.00, "right"),
            (1.00, "right"),
            (1.10, "right"),
            (0.85, "right"),
            (0.75, "right"),
        ], r"\scriptsize"
    if header == ["资金", "目标空头部署率", "空头欠部署率", "平均净多头偏置", "95% 分位净多头偏置", "最大净多头偏置"]:
        return [
            (0.65, "right"),
            (1.00, "right"),
            (0.95, "right"),
            (1.10, "right"),
            (1.35, "right"),
            (0.95, "right"),
        ], r"\footnotesize"
    if header == ["文件", "更新"]:
        return [(0.95, "left"), (1.05, "left")], r"\footnotesize"
    if header == ["文件", "内容"]:
        return [(1.55, "left"), (0.45, "left")], r"\footnotesize"
    col_count = len(header)
    return [(1.0, "left")] * col_count, r"\small"


def table_to_latex(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for raw in lines:
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        return ""
    col_count = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (col_count - len(row)))
    weights, font_size = _table_layout(rows[0])
    if len(weights) != col_count:
        weights = [(1.0, "left")] * col_count
    colspec = "@{}" + "".join(_weighted_x(weight, align) for weight, align in weights) + "@{}"
    out = [
        r"\begingroup",
        font_size,
        r"\setlength{\tabcolsep}{3pt}",
        r"\renewcommand{\arraystretch}{1.22}",
        r"\begin{tabularx}{\linewidth}{" + colspec + r"}",
        r"\toprule",
    ]
    for idx, row in enumerate(rows):
        out.append(" & ".join(escape_table_cell(cell) for cell in row) + r" \\")
        out.append(r"\midrule" if idx == 0 else "")
    out.append(r"\bottomrule")
    out.append(r"\end{tabularx}")
    out.append(r"\endgroup")
    return "\n".join(line for line in out if line)


def strip_number(title: str) -> str:
    return re.sub(r"^\s*(?:[0-9]+|[A-Z])(?:\.[0-9]+)*\s+", "", title).strip()


def convert(md: str) -> str:
    lines = md.splitlines()
    body: list[str] = []
    i = 0
    in_code = False
    code: list[str] = []
    in_math = False
    math: list[str] = []
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            if in_code:
                body.append(r"\begin{Verbatim}[breaklines=true,breakanywhere=true,fontsize=\small]")
                body.extend(code)
                body.append(r"\end{Verbatim}")
                code = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code.append(line)
            i += 1
            continue
        if line.strip() == r"\[":
            in_math = True
            math = [r"\["]
            i += 1
            continue
        if in_math:
            math.append(line)
            if line.strip() == r"\]":
                body.extend(math)
                in_math = False
                math = []
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
            if title == "摘要":
                body.append(r"\section*{" + escape_text(title) + "}")
            else:
                body.append(r"\section{" + escape_text(strip_number(title)) + "}")
        elif line.startswith("### "):
            body.append(r"\subsection{" + escape_text(strip_number(line[4:])) + "}")
        elif re.match(r"^\d+\. ", line):
            body.append(r"\begin{enumerate}[leftmargin=1.8em]")
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                item = re.sub(r"^\d+\. ", "", lines[i])
                body.append(r"\item " + escape_text(item))
                i += 1
            body.append(r"\end{enumerate}")
            continue
        elif line.startswith("- "):
            body.append(r"\begin{itemize}[leftmargin=1.5em]")
            while i < len(lines) and lines[i].startswith("- "):
                body.append(r"\item " + escape_text(lines[i][2:]))
                i += 1
            body.append(r"\end{itemize}")
            continue
        else:
            body.append(escape_text(line))
        i += 1

    fig_dir = ROOT / "artifacts" / "phase7k_backtest" / "short_integer_compare_20240520_20260520" / "report_figures"
    figures = [
        ("10k 账户不同执行口径净值", fig_dir / "equity_10k_scenarios.png"),
        ("10k 账户不同执行口径回撤", fig_dir / "drawdown_10k_scenarios.png"),
        ("baseline 资金量与空头部署率", fig_dir / "baseline_deployment_by_capital.png"),
    ]
    body.append(r"\clearpage")
    body.append(r"\section{附录：图形}")
    for title, path in figures:
        if path.exists():
            body.append(r"\subsection{" + escape_text(title) + "}")
            body.append(r"\begin{center}")
            body.append(r"\includegraphics[width=0.95\linewidth]{" + path.as_posix() + "}")
            body.append(r"\end{center}")
    return "\n".join(body)


def main() -> int:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    preamble = r"""
\documentclass[10pt,a4paper]{article}
\usepackage{geometry}
\geometry{left=1.8cm,right=1.8cm,top=1.8cm,bottom=1.7cm}
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
\usepackage{array}
\usepackage{ragged2e}
\usepackage{xurl}
\usepackage{fvextra}
\usepackage{enumitem}
\usepackage{hyperref}
\usepackage{fancyhdr}
\usepackage{titlesec}
\hypersetup{colorlinks=true,linkcolor=black,urlcolor=blue}
\pagestyle{fancy}
\fancyhf{}
\cfoot{\thepage}
\renewcommand{\headrulewidth}{0pt}
\titleformat{\section}{\large\bfseries}{\thesection}{0.7em}{}
\titleformat{\subsection}{\normalsize\bfseries}{\thesubsection}{0.7em}{}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.45em}
\renewcommand{\arraystretch}{1.18}
\begin{document}
"""
    TEX.write_text(preamble + convert(SOURCE.read_text(encoding="utf-8")) + "\n\\end{document}\n", encoding="utf-8")
    cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={BUILD_DIR}", str(TEX)]
    for _ in range(2):
        result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print(result.stdout[-4000:])
            print(result.stderr[-4000:])
            return result.returncode
    built = BUILD_DIR / TEX.with_suffix(".pdf").name
    if not built.exists():
        print(f"missing output pdf: {built}")
        return 1
    PDF.write_bytes(built.read_bytes())
    print(PDF)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
