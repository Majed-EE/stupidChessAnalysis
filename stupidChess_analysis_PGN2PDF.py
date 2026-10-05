import argparse
import math
import shutil
from pathlib import Path

import chess
import chess.engine
import chess.pgn
from pypdf import PdfWriter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, Spacer, Table, TableStyle

from chess_shit import BOARD_SIZE, GRID_COLUMNS, BoardFlowable, detect_player, make_doc

DEFAULT_ENGINE = (
    Path.home()
    / "AppData/Local/Microsoft/WinGet/Packages"
    / "Stockfish.Stockfish_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "stockfish/stockfish-windows-x86-64-universal.exe"
)

# Drop in the mover's winning chances (percentage points), as Lichess does
THRESHOLDS = [(15, "Blunder", "??"), (10, "Mistake", "?"), (5, "Inaccuracy", "?!")]
COLORS = {"Blunder": "#c62828", "Mistake": "#ef6c00", "Inaccuracy": "#b8860b"}
MATE_CP = 10000


def win_percent(cp):
    """Winning chances (0-100) for a centipawn score, Lichess formula."""

    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def format_eval(score):
    """Score from White's point of view as text, e.g. '+1.3' or '#-2'."""

    if score.is_mate():
        return f"#{score.mate()}"
    return f"{score.score() / 100:+.1f}"


def analyse_game(engine, game, limit):
    """Evaluate every position and classify each move."""

    board = game.board()
    info = engine.analyse(board, limit)
    moves = []

    for move in game.mainline_moves():
        before = info["score"].white()
        best = info.get("pv", [None])[0]
        mover = board.turn
        san = board.san(move)
        number = board.fullmove_number
        fen_before = board.fen()

        board.push(move)

        if board.is_game_over():
            # The engine can't analyse a finished position, so score it directly
            winner = board.outcome().winner
            if winner is None:
                final = chess.engine.Cp(0)
            else:
                final = chess.engine.Cp(MATE_CP if winner == chess.WHITE else -MATE_CP)
            info = {"score": chess.engine.PovScore(final, chess.WHITE)}
        else:
            info = engine.analyse(board, limit)

        after = info["score"].white()

        sign = 1 if mover == chess.WHITE else -1
        drop = (
            win_percent(sign * before.score(mate_score=MATE_CP))
            - win_percent(sign * after.score(mate_score=MATE_CP))
        )

        kind, symbol = None, ""
        if move != best:
            for limit_pct, name, sym in THRESHOLDS:
                if drop >= limit_pct:
                    kind, symbol = name, sym
                    break

        moves.append({
            "move": move,
            "san": san,
            "number": number,
            "mover": mover,
            "fen_before": fen_before,
            "best": best,
            "best_san": chess.Board(fen_before).san(best) if best else None,
            "eval_before": format_eval(before),
            "eval_after": format_eval(after),
            "kind": kind,
            "symbol": symbol,
        })

    return moves


def move_label(m):
    dots = "." if m["mover"] == chess.WHITE else "..."
    return f"{m['number']}{dots} {m['san']}{m['symbol']}"


def annotated_moves(moves):
    parts = []

    for m in moves:
        text = m["san"] + m["symbol"]
        if m["kind"]:
            text = f'<font color="{COLORS[m["kind"]]}"><b>{text}</b></font>'
        if m["mover"] == chess.WHITE:
            text = f"<b>{m['number']}.</b> {text}"
        parts.append(text)

    return " ".join(parts)


def counts_table(moves, game, styles):
    h = game.headers
    rows = [["", "Blunders ??", "Mistakes ?", "Inaccuracies ?!"]]

    for color, name in ((chess.WHITE, h.get("White", "White")), (chess.BLACK, h.get("Black", "Black"))):
        mine = [m for m in moves if m["mover"] == color]
        rows.append([name] + [
            str(sum(m["kind"] == kind for m in mine)) for kind in ("Blunder", "Mistake", "Inaccuracy")
        ])

    table = Table(rows, colWidths=[5 * cm, 3 * cm, 3 * cm, 3.5 * cm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, "#888888"),
        ("TEXTCOLOR", (1, 0), (1, -1), COLORS["Blunder"]),
        ("TEXTCOLOR", (2, 0), (2, -1), COLORS["Mistake"]),
        ("TEXTCOLOR", (3, 0), (3, -1), COLORS["Inaccuracy"]),
    ]))
    return table


def game_story(number, game, side, moves, styles):
    h = game.headers
    orientation = chess.WHITE if side == "white" else chess.BLACK
    player_color = orientation

    caption = ParagraphStyle("caption", parent=styles["Normal"], fontSize=9, alignment=1, leading=11)
    meta_style = ParagraphStyle("meta", parent=styles["Normal"], textColor="#555555")
    moves_style = ParagraphStyle("moves", parent=styles["Normal"], fontSize=10, leading=14)
    note_style = ParagraphStyle("note", parent=styles["Normal"], fontSize=10, leading=13)

    title = (
        f"Game {number}: {h.get('White', '?')} ({h.get('WhiteElo', '?')}) vs "
        f"{h.get('Black', '?')} ({h.get('BlackElo', '?')})"
    )
    meta = (
        f"{h.get('Date', '?')} &middot; {h.get('TimeControl', '?')}s &middot; "
        f"Result {h.get('Result', '*')} &middot; You played {side}<br/>"
        f"{h.get('Termination', '')}"
    )

    story = [
        Paragraph(title, styles["Heading2"]),
        Paragraph(meta, meta_style),
        Spacer(1, 0.3 * cm),
        counts_table(moves, game, styles),
        Spacer(1, 0.3 * cm),
        Paragraph(annotated_moves(moves) or "(no moves)", moves_style),
    ]

    # Your mistakes, with the move played (red) and the engine's best move (green)
    yours = [m for m in moves if m["mover"] == player_color and m["kind"] in ("Blunder", "Mistake")]
    story.append(Paragraph(f"Your blunders and mistakes ({len(yours)})", styles["Heading3"]))

    if not yours:
        story.append(Paragraph("None found. Nice game!", note_style))

    for m in yours:
        board = chess.Board(m["fen_before"])
        arrows = [(m["move"], "#d32f2f")]
        if m["best"]:
            arrows.append((m["best"], "#2e7d32"))

        text = (
            f'<font color="{COLORS[m["kind"]]}"><b>{m["kind"]}: {move_label(m)}</b></font><br/>'
            f"Evaluation went from {m['eval_before']} to {m['eval_after']} "
            f"(from White's point of view).<br/>"
            f"Best was <b>{m['best_san']}</b> (green arrow). "
            f"Your move is the red arrow."
        )
        row = Table(
            [[BoardFlowable(board, orientation, arrows=arrows), Paragraph(text, note_style)]],
            colWidths=[BOARD_SIZE + 0.6 * cm, 11 * cm],
        )
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story.append(KeepTogether([row, Spacer(1, 0.3 * cm)]))

    # Every position, captions coloured by move quality
    story.append(CondPageBreak(BOARD_SIZE + 2.5 * cm))
    story.append(Paragraph("All moves", styles["Heading3"]))

    board = game.board()
    cells = [[BoardFlowable(board, orientation), Paragraph("Start", caption)]]

    for m in moves:
        board.push(m["move"])
        label = f"{move_label(m)}  <font color='#777777'>{m['eval_after']}</font>"
        if m["kind"]:
            label = f'<font color="{COLORS[m["kind"]]}"><b>{move_label(m)}</b></font>  {m["eval_after"]}'
            if m["best_san"]:
                label += f"<br/><font size=8 color='#2e7d32'>best: {m['best_san']}</font>"
        cells.append([BoardFlowable(board, orientation, m["move"]), Paragraph(label, caption)])

    while len(cells) % GRID_COLUMNS:
        cells.append("")

    rows = [cells[i:i + GRID_COLUMNS] for i in range(0, len(cells), GRID_COLUMNS)]
    grid = Table(rows, colWidths=[BOARD_SIZE + 0.4 * cm] * GRID_COLUMNS)
    grid.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(grid)

    return story


def process_pgn(pgn_file, output_dir, player, engine_path, seconds):
    pgn_file = Path(pgn_file)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    player = (player or detect_player(pgn_file)).lower()
    print(f"Analysing for: {player}")

    styles = getSampleStyleSheet()
    writer = PdfWriter()
    limit = chess.engine.Limit(time=seconds)

    with chess.engine.SimpleEngine.popen_uci(str(engine_path)) as engine, \
            open(pgn_file, "r", encoding="utf-8") as f:
        number = 0

        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            number += 1

            side = "black" if game.headers.get("Black", "").lower() == player else "white"
            moves = analyse_game(engine, game, limit)

            game_folder = output_dir / f"game_{number:03d}"
            game_folder.mkdir(parents=True, exist_ok=True)
            game_pdf = game_folder / f"game_{number:03d}_analysis.pdf"
            make_doc(game_pdf).build(game_story(number, game, side, moves, styles))
            writer.append(str(game_pdf), outline_item=f"Game {number}")

            color = chess.WHITE if side == "white" else chess.BLACK
            mine = [m["kind"] for m in moves if m["mover"] == color]
            print(
                f"Game {number}: {mine.count('Blunder')} blunders, "
                f"{mine.count('Mistake')} mistakes, {mine.count('Inaccuracy')} inaccuracies"
            )

    pdf_file = output_dir / f"{pgn_file.stem}_analysis.pdf"
    with open(pdf_file, "wb") as out:
        writer.write(out)
    print(f"\nAnalysis PDF written to {pdf_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stockfish blunder analysis PDF for every game in a PGN.")
    parser.add_argument("pgn", help="PGN file containing one or more games")
    parser.add_argument("-o", "--output", default="games", help="Output directory")
    parser.add_argument("-p", "--player", default=None, help="Your username (default: most frequent player)")
    parser.add_argument("-e", "--engine", default=None, help="Path to the Stockfish executable")
    parser.add_argument("-t", "--time", type=float, default=0.2, help="Engine seconds per position")
    args = parser.parse_args()

    engine_path = args.engine or shutil.which("stockfish") or DEFAULT_ENGINE
    process_pgn(args.pgn, args.output, args.player, engine_path, args.time)
