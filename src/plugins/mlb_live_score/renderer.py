"""Lightweight Pillow renderer for MLB Live Score."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import GameState, GameSummary, ScoreboardPresentation

GREEN = "#17472f"
DARK_GREEN = "#0d2d20"
CREAM = "#f2e6bd"
BLACK = "#101813"
YELLOW = "#f3c640"
RED = "#b23a32"
LEFT_PANEL_RATIO = 0.60

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
REGULAR_FONT = FONT_DIR / "Jost.ttf"
BOLD_FONT = FONT_DIR / "Jost-SemiBold.ttf"
PIXEL_FONT = FONT_DIR / "dogicapixelbold.ttf"


@dataclass(frozen=True)
class LayoutBounds:
    left_panel: tuple[int, int, int, int]
    right_panel: tuple[int, int, int, int]
    scoreboard: tuple[int, int, int, int] | None
    details: tuple[int, int, int, int]


@dataclass(frozen=True)
class RenderMetrics:
    """Shared spacing and line metrics for the two-panel display."""

    outer_margin: int
    panel_gap: int
    panel_padding: int
    divider_x: int
    line_width: int

    @classmethod
    def for_canvas(cls, width: int, height: int) -> "RenderMetrics":
        scale = min(width / 800, height / 480)
        return cls(
            outer_margin=max(6, round(12 * scale)),
            panel_gap=max(6, round(12 * scale)),
            panel_padding=max(5, round(10 * scale)),
            divider_x=round(width * LEFT_PANEL_RATIO),
            line_width=max(2, round(2 * scale)),
        )


@dataclass(frozen=True)
class TypographyScale:
    """Semantic font sizes, scaled from the primary 800x480 display."""

    scoreboard_primary: int
    scoreboard_header: int
    game_status_primary: int
    game_status_secondary: int
    player_primary: int
    label: int
    panel_title: int
    standings_header: int
    standings_row: int
    micro: int

    @classmethod
    def for_canvas(cls, width: int, height: int) -> "TypographyScale":
        scale = min(width / 800, height / 480)

        def size(reference: int) -> int:
            return max(8, round(reference * scale))

        return cls(
            scoreboard_primary=size(29),
            scoreboard_header=size(25),
            game_status_primary=size(28),
            game_status_secondary=size(24),
            player_primary=size(25),
            label=size(17),
            panel_title=size(24),
            standings_header=size(14),
            standings_row=size(20),
            micro=size(10),
        )


def _font(size: float, bold: bool = False, pixel: bool = False) -> ImageFont.FreeTypeFont:
    path = PIXEL_FONT if pixel else (BOLD_FONT if bold else REGULAR_FONT)
    return ImageFont.truetype(str(path), max(8, round(size)))


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def _inset(box, horizontal: int = 0, vertical: int | None = None):
    vertical = horizontal if vertical is None else vertical
    x0, y0, x1, y1 = box
    return x0 + horizontal, y0 + vertical, x1 - horizontal, y1 - vertical


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str | None,
    max_width: int,
    max_size: int,
    min_size: int = 9,
    bold: bool = False,
    pixel: bool = False,
) -> tuple[str, ImageFont.FreeTypeFont]:
    """Scale, then ellipsize, a single line into its allocation."""
    value = str(text or "TBD").upper()
    for size in range(max_size, min_size - 1, -1):
        font = _font(size, bold=bold, pixel=pixel)
        if _text_width(draw, value, font) <= max_width:
            return value, font
    font = _font(min_size, bold=bold, pixel=pixel)
    suffix = "…"
    while value and _text_width(draw, value + suffix, font) > max_width:
        value = value[:-1]
    return (value + suffix if value else suffix), font


def ordinal(value: int | None) -> str:
    if value is None:
        return ""
    if 10 <= value % 100 <= 20:
        ending = "TH"
    else:
        ending = {1: "ST", 2: "ND", 3: "RD"}.get(value % 10, "TH")
    return f"{value}{ending}"


def display_score(value: int | None) -> str:
    return "–" if value is None else str(value)


def compact_player_name(value: str | None) -> str:
    """Prefer a readable scorecard-style initial and surname."""
    parts = str(value or "TBD").upper().split()
    if len(parts) < 2:
        return parts[0] if parts else "TBD"
    return f"{parts[0][0]}. {' '.join(parts[1:])}"


class ScoreboardRenderer:
    def __init__(self):
        self.last_layout: LayoutBounds | None = None
        self.typography = TypographyScale.for_canvas(800, 480)
        self.last_scoreboard_columns: tuple[int, int, int, int, int] | None = None
        self.last_diamond_bounds: tuple[int, int, int, int] | None = None
        self.last_final_rows: list[tuple[int, int, int, int]] = []

    def render(
        self,
        presentation: ScoreboardPresentation,
        dimensions: tuple[int, int],
        show_outs: bool = True,
        show_updated: bool = False,
    ) -> Image.Image:
        width, height = dimensions
        image = Image.new("RGB", dimensions, GREEN)
        draw = ImageDraw.Draw(image)
        metrics = RenderMetrics.for_canvas(width, height)
        self.typography = TypographyScale.for_canvas(width, height)
        self.last_scoreboard_columns = None
        self.last_diamond_bounds = None
        self.last_final_rows = []
        margin = metrics.outer_margin
        left = (margin, margin, metrics.divider_x - metrics.panel_gap, height - margin)
        right = (metrics.divider_x + metrics.panel_gap, margin, width - margin, height - margin)
        draw.line(
            (metrics.divider_x, margin, metrics.divider_x, height - margin),
            fill=CREAM,
            width=metrics.line_width,
        )

        if presentation.no_game_today:
            score_bounds = None
            details = self._draw_no_game(draw, left, presentation)
        else:
            score_bounds, details = self._draw_game_panel(draw, left, presentation.game, show_outs)

        game = presentation.game
        if game and game.uses_live_layout:
            self._draw_diamond(draw, _inset(right, metrics.panel_padding), game)
        else:
            self._draw_standings(draw, _inset(right, metrics.panel_padding), presentation)

        if show_updated and presentation.updated_at:
            updated = presentation.updated_at.astimezone().strftime("UPDATED %-I:%M %p")
            font = _font(self.typography.micro, bold=True)
            x = width - margin - _text_width(draw, updated, font)
            draw.rectangle((x - 3, height - margin - 14, width - margin, height - margin), fill=GREEN)
            draw.text((x, height - margin - 14), updated, font=font, fill=CREAM)

        self.last_layout = LayoutBounds(left, right, score_bounds, details)
        return image

    def _draw_game_panel(self, draw, panel, game: GameSummary | None, show_outs: bool):
        x0, y0, x1, y1 = panel
        if game is None:
            self._centered(
                draw, "GAME DATA UNAVAILABLE", panel,
                y0 + (y1 - y0) // 2, self.typography.game_status_primary,
            )
            return None, panel
        panel_w, panel_h = x1 - x0, y1 - y0
        scoreboard_h = round(panel_h * 0.43)
        scoreboard = (x0, y0, x1, y0 + scoreboard_h)
        self._draw_scoreboard(draw, scoreboard, game)
        details = (x0, scoreboard[3] + max(8, round(panel_h * 0.025)), x1, y1)
        if game.uses_live_layout:
            self._draw_live_details(draw, details, game, show_outs)
        elif game.is_final:
            self._draw_final_details(draw, details, game)
        else:
            self._draw_pregame_details(draw, details, game)
        return scoreboard, details

    def _draw_scoreboard(self, draw, box, game):
        x0, y0, x1, y1 = box
        width, height = x1 - x0, y1 - y0
        stroke = max(2, round(width / 220))
        draw.rectangle(box, outline=CREAM, width=stroke)
        header_h = round(height * 0.24)
        row_h = (height - header_h) // 2
        team_end, run_end, hit_end, _ = self._scoreboard_column_boundaries(box)
        self.last_scoreboard_columns = (x0, team_end, run_end, hit_end, x1)
        for x in (team_end, run_end, hit_end):
            draw.line((x, y0, x, y1), fill=CREAM, width=stroke)
        for y in (y0 + header_h, y0 + header_h + row_h):
            draw.line((x0, y, x1, y), fill=CREAM, width=stroke)

        header_font = _font(self.typography.scoreboard_header, bold=True, pixel=True)
        for label, a, b in (("R", team_end, run_end), ("H", run_end, hit_end), ("E", hit_end, x1)):
            self._text_in_box(draw, label, (a, y0, b, y0 + header_h), header_font)

        for row, values in enumerate((
            (game.away_abbreviation or game.away_team_name, game.away_runs, game.away_hits, game.away_errors),
            (game.home_abbreviation or game.home_team_name, game.home_runs, game.home_hits, game.home_errors),
        )):
            top = y0 + header_h + row * row_h
            bottom = y1 if row else top + row_h
            name, name_font = fit_text(
                draw, values[0], team_end - x0 - 20,
                self.typography.scoreboard_primary,
                min_size=max(8, self.typography.scoreboard_primary - 6),
                bold=True, pixel=True,
            )
            self._text_in_box(draw, name, (x0, top, team_end, bottom), name_font, align="left", padding=10)
            value_font = _font(self.typography.scoreboard_primary, bold=True)
            self._text_in_box(draw, display_score(values[1]), (team_end, top, run_end, bottom), value_font)
            self._text_in_box(draw, display_score(values[2]), (run_end, top, hit_end, bottom), value_font)
            self._text_in_box(draw, display_score(values[3]), (hit_end, top, x1, bottom), value_font)

    @staticmethod
    def _scoreboard_column_boundaries(box):
        x0, _, x1, _ = box
        stat_width = round((x1 - x0) * 0.17)
        team_end = x1 - stat_width * 3
        return team_end, team_end + stat_width, team_end + stat_width * 2, x1

    def _draw_live_details(self, draw, box, game, show_outs):
        x0, y0, x1, y1 = box
        width, height = x1 - x0, y1 - y0
        status_h = round(height * 0.25)
        inning = f"{game.inning_half or ''} {ordinal(game.inning)}".strip() or "LIVE"
        has_alert = game.state in {GameState.DELAYED_LIVE, GameState.SUSPENDED_LIVE}
        inning_bottom = y0 + (round(status_h * 0.64) if has_alert else status_h)
        inning_box = (x0, y0, x0 + round(width * 0.62), inning_bottom)
        out_box = (inning_box[2], y0, x1, y0 + status_h)
        outs = game.outs
        out_text = "OUTS –" if outs is None else f"{outs} OUT" + ("" if outs == 1 else "S")
        status_size = self.typography.game_status_primary
        status, status_font = fit_text(
            draw, inning, inning_box[2] - inning_box[0] - 4, status_size, bold=True, pixel=True
        )
        if show_outs:
            out_text, out_font = fit_text(
                draw, out_text, out_box[2] - out_box[0], status_size,
                min_size=12, bold=True, pixel=True,
            )
            shared_size = min(status_font.size, out_font.size)
            status_font = _font(shared_size, bold=True, pixel=True)
            out_font = status_font
        self._text_in_box(draw, status, inning_box, status_font, align="left", fill=YELLOW)
        if show_outs:
            self._text_in_box(draw, out_text, out_box, out_font, align="right")
        if has_alert:
            alert, alert_font = fit_text(
                draw, game.status, width, self.typography.game_status_secondary, bold=True
            )
            self._text_in_box(
                draw, alert, (x0, y0 + round(status_h * 0.62), x1, y0 + status_h),
                alert_font, align="left", fill=RED,
            )

        separator_y = y0 + status_h
        draw.line((x0, separator_y, x1, separator_y), fill=CREAM, width=2)
        line_y = separator_y + max(5, round(height * 0.025))
        label_w = round(width * 0.15)
        line_h = max(20, (y1 - line_y) // 3)
        self._labeled_line(draw, "P:", game.pitcher_name, x0, line_y, x1, line_h, label_w)
        self._labeled_line(draw, "AB:", game.batter_name, x0, line_y + line_h, x1, line_h, label_w)
        count = "–" if game.balls is None or game.strikes is None else f"{game.balls}-{game.strikes}"
        self._labeled_line(
            draw, "COUNT:", count, x0, line_y + line_h * 2, x1, line_h,
            round(width * 0.30), value_fill=YELLOW,
        )

    def _draw_pregame_details(self, draw, box, game):
        x0, y0, x1, y1 = box
        width, height = x1 - x0, y1 - y0
        time_text = game.scheduled_time.astimezone().strftime("%-I:%M %p") if game.scheduled_time else "TIME TBD"
        heading_h = round(height * 0.21)
        time_font = _font(self.typography.game_status_primary, bold=True, pixel=True)
        self._text_in_box(draw, time_text, (x0, y0, x0 + round(width * 0.55), y0 + heading_h), time_font, align="left", fill=YELLOW)
        status, status_font = fit_text(
            draw, game.status, round(width * 0.48),
            self.typography.game_status_secondary, bold=True,
        )
        self._text_in_box(draw, status, (x0 + round(width * 0.55), y0, x1, y0 + heading_h), status_font, align="right")
        venue, venue_font = fit_text(
            draw, game.venue or "VENUE TBD", width,
            self.typography.player_primary, bold=True,
        )
        self._text_in_box(draw, venue, (x0, y0 + round(height * 0.22), x1, y0 + round(height * 0.37)), venue_font, align="left")
        starter_y = y0 + round(height * 0.45)
        title_font = _font(self.typography.label, bold=True, pixel=True)
        self._text_in_box(draw, "STARTING PITCHERS", (x0, starter_y, x1, starter_y + round(height * 0.14)), title_font, align="left")
        line_h = max(20, round(height * 0.20))
        label_w = round(width * 0.18)
        self._labeled_line(draw, game.away_abbreviation, game.away_starting_pitcher, x0, starter_y + line_h, x1, line_h, label_w)
        self._labeled_line(draw, game.home_abbreviation, game.home_starting_pitcher, x0, starter_y + line_h * 2, x1, line_h, label_w)

    def _draw_final_details(self, draw, box, game):
        x0, y0, x1, y1 = box
        width, height = x1 - x0, y1 - y0
        final = "FINAL" + (f"/{game.inning}" if (game.inning or 9) > 9 else "")
        final_font = _font(self.typography.game_status_primary, bold=True, pixel=True)
        self._text_in_box(draw, final, (x0, y0, x1, y0 + round(height * 0.23)), final_font, align="left", fill=YELLOW)
        line_y = y0 + round(height * 0.27)
        line_h = max(20, round((height * 0.68) / 3))
        winner_record = self._pitcher_record(
            game.winning_pitcher_wins, game.winning_pitcher_losses
        )
        loser_record = self._pitcher_record(
            game.losing_pitcher_wins, game.losing_pitcher_losses
        )
        self.last_final_rows = []
        self._postgame_line(
            draw, "WP:", game.winning_pitcher, winner_record,
            (x0, line_y, x1, line_y + line_h),
        )
        self._postgame_line(
            draw, "LP:", game.losing_pitcher, loser_record,
            (x0, line_y + line_h, x1, line_y + line_h * 2),
        )
        if game.save_pitcher:
            save_total = (
                f"{game.save_pitcher_saves} SV"
                if game.save_pitcher_saves is not None else ""
            )
            self._postgame_line(
                draw, "SV:", game.save_pitcher, save_total,
                (x0, line_y + line_h * 2, x1, min(y1, line_y + line_h * 3)),
            )

    @staticmethod
    def _pitcher_record(wins: int | None, losses: int | None) -> str:
        return f"{wins}-{losses}" if wins is not None and losses is not None else ""

    def _postgame_line(self, draw, label, name, result, box):
        x0, y0, x1, y1 = box
        width = x1 - x0
        label_end = x0 + round(width * 0.13)
        result_start = x1 - round(width * 0.22)
        label_font = _font(self.typography.label, bold=True, pixel=True)
        value_font = _font(self.typography.player_primary, bold=True)
        self._text_in_box(draw, label, (x0, y0, label_end, y1), label_font, align="left")
        fitted_name, fitted_font = fit_text(
            draw, compact_player_name(name), result_start - label_end - 8,
            self.typography.player_primary,
            min_size=max(10, self.typography.player_primary - 7), bold=True,
        )
        self._text_in_box(
            draw, fitted_name, (label_end, y0, result_start, y1),
            fitted_font, align="left", padding=2,
        )
        if result:
            fitted_result, result_font = fit_text(
                draw, result, x1 - result_start, self.typography.player_primary,
                min_size=max(10, self.typography.player_primary - 5), bold=True,
            )
            self._text_in_box(
                draw, fitted_result, (result_start, y0, x1, y1),
                result_font, align="right",
            )
        self.last_final_rows.append(box)

    def _draw_no_game(self, draw, panel, presentation):
        x0, y0, x1, y1 = panel
        width, height = x1 - x0, y1 - y0
        team, team_font = fit_text(
            draw, presentation.selected_team_name, width,
            self.typography.game_status_primary, bold=True, pixel=True,
        )
        draw.text((x0, y0), team, font=team_font, fill=CREAM)
        no_game, no_game_font = fit_text(
            draw, "NO GAME TODAY", width,
            self.typography.game_status_primary, bold=True, pixel=True
        )
        draw.text((x0, y0 + round(height * 0.16)), no_game, font=no_game_font, fill=YELLOW)
        details = (x0, y0 + round(height * 0.34), x1, y1)
        game = presentation.game
        if not game:
            text, font = fit_text(
                draw, "NEXT GAME UNAVAILABLE", width,
                self.typography.player_primary, bold=True,
            )
            draw.text((x0, details[1]), text, font=font, fill=CREAM)
            return details
        next_font = _font(self.typography.label, bold=True, pixel=True)
        draw.text((x0, details[1]), "NEXT:", font=next_font, fill=CREAM)
        matchup = f"{game.away_abbreviation} @ {game.home_abbreviation}"
        matchup, matchup_font = fit_text(
            draw, matchup, width, self.typography.game_status_primary, bold=True
        )
        draw.text((x0, details[1] + round(height * 0.08)), matchup, font=matchup_font, fill=YELLOW)
        when_y = details[1] + round(height * 0.20)
        if game.scheduled_time:
            local = game.scheduled_time.astimezone()
            when = f"{local.strftime('%a %b %d').upper()}   {local.strftime('%-I:%M %p')}"
        else:
            when = "DATE / TIME TBD"
        when, when_font = fit_text(
            draw, when, width, self.typography.game_status_secondary, bold=True
        )
        draw.text((x0, when_y), when, font=when_font, fill=CREAM)
        line_h = max(20, round(height * 0.10))
        starters_y = when_y + round(height * 0.12)
        label_w = round(width * 0.18)
        self._labeled_line(draw, f"{game.away_abbreviation}:", game.away_starting_pitcher, x0, starters_y, x1, line_h, label_w)
        self._labeled_line(draw, f"{game.home_abbreviation}:", game.home_starting_pitcher, x0, starters_y + line_h, x1, line_h, label_w)
        return details

    def _draw_diamond(self, draw, panel, game):
        x0, y0, x1, y1 = panel
        width, height = x1 - x0, y1 - y0
        title_h = round(height * 0.12)
        title_font = _font(self.typography.panel_title, bold=True, pixel=True)
        title = "ON BASE"
        self._text_in_box(draw, title, (x0, y0, x1, y0 + title_h), title_font)
        cx = (x0 + x1) // 2
        play_top = y0 + title_h
        cy = play_top + round((y1 - play_top) * 0.49)
        base_size = max(12, round(min(width * 0.105, height * 0.08)))
        radius = min(
            round(width * 0.42),
            round((y1 - play_top) * 0.31),
            max(1, width // 2 - base_size - 2),
        )
        home = (cx, cy + radius)
        first = (cx + radius, cy)
        second = (cx, cy - radius)
        third = (cx - radius, cy)
        diamond = (home, first, second, third)
        self.last_diamond_bounds = (
            third[0] - base_size, second[1] - base_size,
            first[0] + base_size, home[1] + base_size,
        )
        draw.polygon(diamond, fill=DARK_GREEN)
        draw.line(diamond + (home,), fill=CREAM, width=2)
        for point, occupied in (
            (first, game.runner_on_first), (second, game.runner_on_second), (third, game.runner_on_third)
        ):
            self._base(draw, point, base_size, occupied)
        plate_w = max(10, base_size)
        px, py = home
        draw.polygon(
            ((px - plate_w, py - plate_w // 2), (px + plate_w, py - plate_w // 2),
             (px + plate_w, py), (px, py + plate_w), (px - plate_w, py)),
            fill=CREAM, outline=BLACK,
        )

    def _draw_standings(self, draw, panel, presentation):
        x0, y0, x1, y1 = panel
        width, height = x1 - x0, y1 - y0
        standings = presentation.standings
        title = standings.division_name if standings else "STANDINGS"
        title_h = round(height * 0.12)
        title, title_font = fit_text(
            draw, title, width, self.typography.panel_title, bold=True, pixel=True
        )
        self._text_in_box(draw, title, (x0, y0, x1, y0 + title_h), title_font)
        if not standings:
            message = "UNAVAILABLE"
            font = _font(self.typography.panel_title, bold=True, pixel=True)
            self._text_in_box(draw, message, (x0, y0 + title_h, x1, y0 + round(height * 0.32)), font, fill=YELLOW)
            return

        header_top = y0 + title_h + round(height * 0.025)
        header_h = round(height * 0.085)
        row_top = header_top + header_h + round(height * 0.02)
        footer_h = round(height * 0.05) if standings.is_stale else 0
        row_h = max(24, (y1 - row_top - footer_h) // max(5, len(standings.rows)))
        boundaries = [
            x0,
            x0 + round(width * 0.46),
            x0 + round(width * 0.64),
            x0 + round(width * 0.82),
            x1,
        ]
        cells = [(boundaries[i], header_top, boundaries[i + 1], header_top + header_h) for i in range(4)]
        header_font = _font(self.typography.standings_header, bold=True, pixel=True)
        for label, cell in zip(("TEAM", "W", "L", "GB"), cells):
            self._text_in_box(
                draw, label, cell, header_font,
                align="left" if label == "TEAM" else "right", padding=3, fill=YELLOW,
            )
        draw.line((x0, row_top - 4, x1, row_top - 4), fill=CREAM, width=2)
        row_font = _font(self.typography.standings_row, bold=True)
        for index, row in enumerate(standings.rows):
            top = row_top + index * row_h
            bottom = min(y1 - footer_h, top + row_h)
            if row.is_selected_team:
                draw.rectangle((x0, top + 3, x1, bottom - 3), fill=CREAM, outline=BLACK, width=2)
                fill = DARK_GREEN
            else:
                fill = CREAM
            values = (row.abbreviation, str(row.wins), str(row.losses), row.games_back)
            row_cells = [(boundaries[i], top, boundaries[i + 1], bottom) for i in range(4)]
            for column, (value, cell) in enumerate(zip(values, row_cells)):
                fitted, fitted_font = fit_text(
                    draw, value, cell[2] - cell[0] - 8, row_font.size,
                    min_size=max(10, row_font.size - 5), bold=True,
                )
                self._text_in_box(
                    draw, fitted, cell, fitted_font,
                    align="left" if column == 0 else "right", padding=4, fill=fill,
                )
        if standings.is_stale:
            stale_font = _font(self.typography.micro, bold=True)
            self._text_in_box(draw, "CACHED", (x0, y1 - footer_h, x1, y1), stale_font, align="right", fill=YELLOW)

    def _labeled_line(self, draw, label, value, x0, y, x1, line_h, label_w, value_fill=CREAM):
        label_font = _font(self.typography.label, bold=True, pixel=True)
        label_box = (x0, y, x0 + label_w, y + line_h)
        value_box = (x0 + label_w, y, x1, y + line_h)
        self._text_in_box(draw, str(label), label_box, label_font, align="left")
        text, value_font = fit_text(
            draw, value or "TBD", value_box[2] - value_box[0] - 4,
            self.typography.player_primary,
            min_size=max(10, self.typography.player_primary - 7), bold=True,
        )
        self._text_in_box(draw, text, value_box, value_font, align="left", padding=6, fill=value_fill)

    def _cell_text(self, draw, text, box, font):
        self._text_in_box(draw, text, box, font)

    def _text_in_box(self, draw, text, box, font, align="center", padding=0, fill=CREAM):
        """Place text from its actual glyph bounds, not the nominal font size."""
        x0, y0, x1, y1 = box
        bbox = draw.textbbox((0, 0), str(text), font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if align == "left":
            x = x0 + padding - bbox[0]
        elif align == "right":
            x = x1 - padding - tw - bbox[0]
        else:
            x = x0 + (x1 - x0 - tw) / 2 - bbox[0]
        y = y0 + (y1 - y0 - th) / 2 - bbox[1]
        draw.text((x, y), str(text), font=font, fill=fill)

    def _centered(self, draw, text, box, y, size):
        x0, _, x1, _ = box
        text, font = fit_text(draw, text, x1 - x0, size, bold=True)
        draw.text((x0 + (x1 - x0 - _text_width(draw, text, font)) / 2, y), text, font=font, fill=CREAM)

    def _base(self, draw, point, size, occupied):
        x, y = point
        points = ((x, y - size), (x + size, y), (x, y + size), (x - size, y))
        draw.polygon(points, fill=YELLOW if occupied else CREAM, outline=BLACK)
        inner = max(1, size // 7)
        draw.line(points + (points[0],), fill=BLACK, width=inner)
