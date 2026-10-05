import argparse
import math
from collections import Counter
from io import BytesIO
from pathlib import Path

import chess
import chess.pgn
import chess.svg
import cairosvg
from pypdf import PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

GRID_COLUMNS = 3
BOARD_SIZE = 5.2 * cm


def generate_image(game, output_file, orientation=chess.WHITE):
    """Generate an image of the final position of a game."""

    board = game.board()
    last_move = None

    for move in game.mainline_moves():
        last_move = move
        board.push(move)

    svg = chess.svg.board(
        board=board,
        orientation=orientation,
        size=800,
        lastmove=last_move,
        coordinates=True,
    )

    cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        write_to=str(output_file),
        output_width=800,
        output_height=800,
    )


def format_moves(game):
    """Return the mainline as numbered SAN, e.g. '1. e4 e5 2. Nf3'."""

    board = game.board()
    parts = []

    for move in game.mainline_moves():
        if board.turn == chess.WHITE:
            parts.append(f"<b>{board.fullmove_number}.</b> {board.san(move)}")
        else:
            parts.append(board.san(move))
        board.push(move)

    return " ".join(parts)


LIGHT, DARK = "#ffce9e", "#d18b47"
LIGHT_LASTMOVE, DARK_LASTMOVE = "#cdd16a", "#aaa23b"
PIECE_IMAGES = {}


def piece_image(piece):
    """PNG of a piece, rendered once and reused so the PDF stores it only once."""

    key = piece.symbol()

    if key not in PIECE_IMAGES:
        svg = chess.svg.piece(piece, size=45)
        png = cairosvg.svg2png(
            bytestring=svg.encode("utf-8"), output_width=150, output_height=150
        )
        PIECE_IMAGES[key] = ImageReader(BytesIO(png))

    return PIECE_IMAGES[key]


class BoardFlowable(Flowable):
    """A board drawn as vector squares with shared piece images."""

    def __init__(self, board, orientation, last_move=None, size=BOARD_SIZE, arrows=()):
        super().__init__()
        self.board = board.copy(stack=False)
        self.orientation = orientation
        self.last_move = last_move
        self.arrows = arrows
        self.width = self.height = size

    def draw(self):
        c = self.canv
        size = self.width
        margin = size * 0.045
        sq = (size - 2 * margin) / 8
        flip = self.orientation == chess.BLACK
        highlight = (
            {self.last_move.from_square, self.last_move.to_square}
            if self.last_move else set()
        )

        c.setFillColor("#212121")
        c.rect(0, 0, size, size, stroke=0, fill=1)

        for square in chess.SQUARES:
            file, rank = chess.square_file(square), chess.square_rank(square)
            col, row = (7 - file, 7 - rank) if flip else (file, rank)
            light = (file + rank) % 2 == 1

            if square in highlight:
                c.setFillColor(LIGHT_LASTMOVE if light else DARK_LASTMOVE)
            else:
                c.setFillColor(LIGHT if light else DARK)

            x, y = margin + col * sq, margin + row * sq
            c.rect(x, y, sq, sq, stroke=0, fill=1)

            piece = self.board.piece_at(square)
            if piece:
                c.drawImage(piece_image(piece), x, y, sq, sq, mask="auto")

        for move, color in self.arrows:
            self.draw_arrow(move, color, margin, sq, flip)

        # Coordinates in the border
        c.setFillColor("#e5e5e5")
        c.setFont("Helvetica-Bold", margin * 0.8)

        for i in range(8):
            index = 7 - i if flip else i
            c.drawCentredString(margin + (i + 0.5) * sq, margin * 0.22, "abcdefgh"[index])
            c.drawCentredString(margin / 2, margin + (i + 0.5) * sq - margin * 0.28, str(index + 1))

    def draw_arrow(self, move, color, margin, sq, flip):
        """Semi-transparent arrow from the move's start square to its target."""

        def centre(square):
            file, rank = chess.square_file(square), chess.square_rank(square)
            col, row = (7 - file, 7 - rank) if flip else (file, rank)
            return margin + (col + 0.5) * sq, margin + (row + 0.5) * sq

        (x1, y1), (x2, y2) = centre(move.from_square), centre(move.to_square)
        length = math.hypot(x2 - x1, y2 - y1)
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        head = sq * 0.45
        bx, by = x2 - ux * head, y2 - uy * head

        c = self.canv
        c.saveState()
        c.setStrokeColor(color)
        c.setFillColor(color)
        c.setStrokeAlpha(0.8)
        c.setFillAlpha(0.8)
        c.setLineWidth(sq * 0.18)
        c.line(x1, y1, bx, by)

        path = c.beginPath()
        path.moveTo(x2, y2)
        path.lineTo(bx - uy * head * 0.55, by + ux * head * 0.55)
        path.lineTo(bx + uy * head * 0.55, by - ux * head * 0.55)
        path.close()
        c.drawPath(path, stroke=0, fill=1)
        c.restoreState()


def game_story(number, game, side, styles):
    """Flowables for one game: details, move list and a board after every move."""

    h = game.headers
    orientation = chess.WHITE if side == "white" else chess.BLACK
    caption_style = ParagraphStyle(
        "caption", parent=styles["Normal"], fontSize=9, alignment=1
    )
    meta_style = ParagraphStyle("meta", parent=styles["Normal"], textColor="#555555")
    moves_style = ParagraphStyle("moves", parent=styles["Normal"], fontSize=10, leading=14)

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
        Paragraph(format_moves(game) or "(no moves)", moves_style),
        Spacer(1, 0.4 * cm),
    ]

    # One cell per position: the start, then after each half-move
    board = game.board()
    cells = [[BoardFlowable(board, orientation),
              Paragraph("Start", caption_style)]]

    for move in game.mainline_moves():
        if board.turn == chess.WHITE:
            label = f"{board.fullmove_number}. {board.san(move)}"
        else:
            label = f"{board.fullmove_number}... {board.san(move)}"
        board.push(move)
        cells.append([BoardFlowable(board, orientation, move),
                      Paragraph(label, caption_style)])

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


def make_doc(pdf_file):
    return SimpleDocTemplate(
        str(pdf_file),
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )


def build_pdfs(entries, combined_file):
    """Write a PDF per game (in its folder), then merge them into one PDF."""

    styles = getSampleStyleSheet()
    writer = PdfWriter()

    for number, game, game_folder, side in entries:
        game_pdf = game_folder / f"game_{number:03d}.pdf"
        make_doc(game_pdf).build(game_story(number, game, side, styles))
        writer.append(str(game_pdf), outline_item=f"Game {number}")
        print(f"PDF for game {number} done")

    with open(combined_file, "wb") as f:
        writer.write(f)


def detect_player(pgn_file):
    """Return the player who appears in the most games of the PGN."""

    counts = Counter()

    with open(pgn_file, "r", encoding="utf-8") as f:
        while True:
            headers = chess.pgn.read_headers(f)

            if headers is None:
                break

            counts[headers.get("White", "").lower()] += 1
            counts[headers.get("Black", "").lower()] += 1

    return counts.most_common(1)[0][0] if counts else None


def process_pgn(pgn_file, output_dir="games", player=None):
    pgn_file = Path(pgn_file)
    output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    if player is None:
        player = detect_player(pgn_file)

    player = player.lower()
    print(f"Orienting boards for: {player}")

    entries = []

    with open(pgn_file, "r", encoding="utf-8") as f:

        game_number = 0

        while True:

            game = chess.pgn.read_game(f)

            if game is None:
                break

            game_number += 1

            # Create folder for this game
            game_folder = output_dir / f"game_{game_number:03d}"
            game_folder.mkdir(parents=True, exist_ok=True)

            # Image filename
            image_file = game_folder / "position.png"

            # Flip the board when the player had Black
            if game.headers.get("Black", "").lower() == player:
                orientation = chess.BLACK
            else:
                orientation = chess.WHITE

            # Generate image
            generate_image(game, image_file, orientation)

            side = "white" if orientation == chess.WHITE else "black"
            print(f"Game {game_number} ({side}): {image_file}")

            entries.append((game_number, game, game_folder, side))

    print(f"\nProcessed {game_number} games.")

    pdf_file = output_dir / f"{pgn_file.stem}_all_moves.pdf"
    build_pdfs(entries, pdf_file)
    print(f"Combined PDF written to {pdf_file}")


if __name__ == "__main__":
    print("Chess Image Generator")
    parser = argparse.ArgumentParser(
        description="Generate chess images for every game in a PGN."
    )

    parser.add_argument(
        "pgn",
        help="PGN file containing one or more games"
    )

    parser.add_argument(
        "-o",
        "--output",
        default="games",
        help="Output directory"
    )

    parser.add_argument(
        "-p",
        "--player",
        default=None,
        help="Username to orient boards for (default: most frequent player)"
    )

    args = parser.parse_args()

    process_pgn(
        args.pgn,
        args.output,
        args.player
    )