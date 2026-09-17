"""Native 800x480-first Pillow renderer for Sleeper matchup dashboards."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import DashboardModel, MatchupSide, MatchupStatus

CHARCOAL = "#17191c"
CARD = "#22262b"
CREAM = "#f4eddc"
MUTED = "#c9c1ae"
LIME = "#d7f044"
YELLOW = "#ffd23f"
RED = "#ef554a"
BLACK = "#111111"

FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"
REGULAR_FONT = FONT_DIR / "Jost.ttf"
BOLD_FONT = FONT_DIR / "Jost-SemiBold.ttf"
PIXEL_FONT = FONT_DIR / "dogicapixelbold.ttf"
MIN_SECONDARY_SIZE = 14
PROBABILITY_BAR_HEIGHT = 14  # Reference 800x480 height, including the border.
CENTER_SEPARATOR_FONT_SIZE = 24
CENTER_SEPARATOR_PADDING = 8


class Density(str, Enum):
    HERO = "hero"
    LARGE = "large"
    MEDIUM = "medium"
    COMPACT = "compact"


@dataclass(frozen=True)
class TypeRole:
    """Central semantic type scale at the reference 800x480 resolution."""

    page_title: int
    card_league: int
    card_status: int
    team_name: int
    team_name_min: int
    team_record: int
    score: int
    score_min: int
    center_label: int
    win_percent: int
    win_label: int
    projected_score: int
    context_primary: int
    context_secondary: int
    micro: int

    @property
    def context(self) -> int:
        return self.context_primary


# CARD_LEAGUE, CARD_STATUS, TEAM_NAME, TEAM_META, SCORE, VS_LABEL,
# WIN_PERCENT, WIN_LABEL, PROJECTION, CONTEXT and ERROR_STATE are represented
# once here rather than as scattered literals in drawing code.
TYPE_REFERENCE = {
    Density.HERO: TypeRole(18, 27, 18, 27, 19, 18, 58, 44, CENTER_SEPARATOR_FONT_SIZE + 4, 22, 16, 19, 16, 14, 14),
    Density.LARGE: TypeRole(16, 23, 16, 23, 18, 16, 46, 34, CENTER_SEPARATOR_FONT_SIZE, 20, 14, 16, 14, 14, 14),
    Density.MEDIUM: TypeRole(14, 20, 14, 20, 16, 14, 38, 30, CENTER_SEPARATOR_FONT_SIZE, 18, 14, 15, 14, 14, 14),
    Density.COMPACT: TypeRole(14, 19, 14, 21, 16, 14, 39, 28, CENTER_SEPARATOR_FONT_SIZE, 20, 14, 14, 14, 14, 14),
}


@dataclass(frozen=True)
class SpaceRole:
    """Card geometry at 800x480; all vertical placement comes from zones."""

    pad_x: int
    pad_y: int
    header_height: int
    section_gap: int
    team_height: int
    score_height: int
    detail_height: int
    probability_min_height: int
    center_width: int
    border: int
    divider: int
    user_rail: int
    probability_bar: int
    footer_height: int


SPACE_REFERENCE = {
    Density.HERO: SpaceRole(24, 18, 42, 10, 72, 108, 0, 76, 68, 2, 2, 8, PROBABILITY_BAR_HEIGHT + 4, 42),
    Density.LARGE: SpaceRole(16, 10, 30, 5, 43, 58, 0, 44, 56, 2, 1, 7, PROBABILITY_BAR_HEIGHT + 1, 22),
    Density.MEDIUM: SpaceRole(14, 10, 29, 5, 43, 57, 1, 48, 46, 2, 1, 6, PROBABILITY_BAR_HEIGHT, 0),
    Density.COMPACT: SpaceRole(14, 10, 29, 5, 43, 57, 0, 48, 44, 2, 1, 6, PROBABILITY_BAR_HEIGHT, 0),
}


@dataclass(frozen=True)
class CardPlacement:
    bounds: tuple[int, int, int, int]
    density: Density


@dataclass(frozen=True)
class CardZones:
    header: tuple[int, int, int, int]
    teams: tuple[int, int, int, int]
    scores: tuple[int, int, int, int]
    analytics: tuple[int, int, int, int]
    footer: tuple[int, int, int, int] | None


def _font(size: float, bold: bool = False, pixel: bool = False):
    path = PIXEL_FONT if pixel else (BOLD_FONT if bold else REGULAR_FONT)
    return ImageFont.truetype(str(path), max(8, round(size)))


def _canvas_scale(canvas: tuple[int, int]) -> float:
    return min(canvas[0] / 800, canvas[1] / 480)


def _scaled_roles(density: Density, canvas: tuple[int, int]) -> TypeRole:
    scale = _canvas_scale(canvas)
    base = TYPE_REFERENCE[density]
    floor = max(8, round(MIN_SECONDARY_SIZE * scale))
    return TypeRole(*(max(floor, round(value * scale)) for value in base.__dict__.values()))


def _scaled_spacing(density: Density, canvas: tuple[int, int]) -> SpaceRole:
    scale = _canvas_scale(canvas)
    base = SPACE_REFERENCE[density]
    return SpaceRole(*(max(1, round(value * scale)) if value else 0 for value in base.__dict__.values()))


def _text_width(draw, text: str, font) -> float:
    return draw.textlength(str(text), font=font)


def fit_text(draw, value, max_width, max_size, min_size=9, bold=True):
    """Fit one uppercase line at a bounded size, then ellipsize."""

    text = str(value or "TBD").strip().upper()
    maximum = max(round(min_size), round(max_size))
    minimum = max(8, min(round(min_size), maximum))
    for size in range(maximum, minimum - 1, -1):
        font = _font(size, bold=bold)
        if _text_width(draw, text, font) <= max(1, max_width):
            return text, font
    font = _font(minimum, bold=bold)
    while text and _text_width(draw, text + "…", font) > max(1, max_width):
        text = text[:-1].rstrip()
    return (text + "…" if text else "…"), font


def display_score(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}" if abs(value - round(value)) > 0.04 else str(round(value))


def display_context(side: MatchupSide) -> str:
    if side.estimated_remaining_points is None or side.starters_remaining is None:
        return "CONTEXT UNAVAILABLE"
    return f"{display_score(side.estimated_remaining_points)} PTS • {side.starters_remaining} LEFT"


def layout_for_count(dimensions: tuple[int, int], count: int) -> list[CardPlacement]:
    width, height = dimensions
    scale = _canvas_scale(dimensions)
    margin, gap = max(4, round(6 * scale)), max(6, round(10 * scale))
    x0, y0, x1, y1 = margin, margin, width - margin, height - margin
    if count == 1:
        return [CardPlacement((x0, y0, x1, y1), Density.HERO)]
    if count == 2:
        split = (y0 + y1 - gap) // 2
        return [CardPlacement((x0, y0, x1, split), Density.LARGE), CardPlacement((x0, split + gap, x1, y1), Density.LARGE)]
    if count == 3:
        split_y, split_x = (y0 + y1 - gap) // 2, (x0 + x1 - gap) // 2
        return [CardPlacement((x0, y0, x1, split_y), Density.MEDIUM), CardPlacement((x0, split_y + gap, split_x, y1), Density.MEDIUM), CardPlacement((split_x + gap, split_y + gap, x1, y1), Density.MEDIUM)]
    if count == 4:
        split_x, split_y = (x0 + x1 - gap) // 2, (y0 + y1 - gap) // 2
        return [CardPlacement((x0, y0, split_x, split_y), Density.COMPACT), CardPlacement((split_x + gap, y0, x1, split_y), Density.COMPACT), CardPlacement((x0, split_y + gap, split_x, y1), Density.COMPACT), CardPlacement((split_x + gap, split_y + gap, x1, y1), Density.COMPACT)]
    raise ValueError("Dashboard requires 1-4 matchup cards")


class MatchupDashboardRenderer:
    def __init__(self):
        self.last_card_bounds = []
        self.last_content_bounds = []
        self.last_typography = {}
        self.last_spacing = {}
        self.last_zones = []
        self.last_text_bounds = []
        self.last_text_sizes = []
        self.last_divider_segments = []
        self.last_separator_clear_zones = []
        self.last_probability_bars = []
        self._card_box = (0, 0, 0, 0)

    def render(self, dashboard: DashboardModel, dimensions: tuple[int, int], *, show_projected=True, show_probability=True, show_records=True, show_context=True) -> Image.Image:
        image = Image.new("RGB", dimensions, CHARCOAL)
        draw = ImageDraw.Draw(image)
        placements = layout_for_count(dimensions, len(dashboard.matchups))
        self.last_card_bounds = [item.bounds for item in placements]
        self.last_content_bounds, self.last_zones, self.last_text_bounds = [], [], []
        self.last_text_sizes, self.last_divider_segments = [], []
        self.last_separator_clear_zones, self.last_probability_bars = [], []
        self.last_typography, self.last_spacing = {}, {}
        for matchup, placement in zip(dashboard.matchups, placements):
            roles = _scaled_roles(placement.density, dimensions)
            spacing = _scaled_spacing(placement.density, dimensions)
            self.last_typography[placement.density], self.last_spacing[placement.density] = roles, spacing
            self._draw_card(draw, placement.bounds, placement.density, roles, spacing, matchup, show_projected, show_probability, show_records, show_context)
        return image

    def _text(self, draw, xy, value, *, role, font, fill, anchor="la"):
        draw.text(xy, str(value), font=font, fill=fill, anchor=anchor)
        raw = draw.textbbox(xy, str(value), font=font, anchor=anchor)
        self.last_text_bounds.append((role, tuple(int(v) for v in raw), self._card_box))
        self.last_text_sizes.append((role, font.size, self._card_box))

    @staticmethod
    def _zones(content, spacing, has_projection, include_footer):
        x0, y0, x1, y1 = content
        header = (x0, y0, x1, min(y1, y0 + spacing.header_height))
        body_top, available = header[3] + spacing.section_gap, max(0, y1 - header[3] - spacing.section_gap)
        footer_h = min(spacing.footer_height, available) if include_footer else 0
        team_h = min(spacing.team_height, max(0, available - footer_h))
        score_h = min(spacing.score_height, max(0, available - footer_h - team_h))
        if not has_projection:
            score_h = max(score_h, available - team_h - max(20, spacing.probability_bar + 10))
        teams = (x0, body_top, x1, body_top + team_h)
        scores = (x0, teams[3], x1, teams[3] + score_h)
        footer = (x0, y1 - footer_h, x1, y1) if footer_h else None
        analytics = (x0, scores[3], x1, footer[1] if footer else y1)
        return CardZones(header, teams, scores, analytics, footer)

    def _draw_card(self, draw, box, density, roles, spacing, matchup, show_projected, show_probability, show_records, show_context):
        self._card_box = box
        x0, y0, x1, y1 = box
        draw.rectangle(box, fill=CARD, outline=CREAM, width=spacing.border)
        draw.rectangle((x0, y0, x0 + spacing.user_rail, y1), fill=LIME)
        content = (x0 + spacing.user_rail + spacing.pad_x, y0 + spacing.pad_y, x1 - spacing.pad_x, y1 - spacing.pad_y)
        self.last_content_bounds.append(content)
        has_projection = bool(matchup.projections_available and matchup.user_side and matchup.opponent_side and matchup.user_side.projected_final is not None and matchup.opponent_side.projected_final is not None)
        has_context = bool(
            show_context
            and matchup.status is not MatchupStatus.FINAL
            and matchup.user_side
            and matchup.opponent_side
            and matchup.user_side.estimated_remaining_points is not None
            and matchup.user_side.starters_remaining is not None
            and matchup.opponent_side.estimated_remaining_points is not None
            and matchup.opponent_side.starters_remaining is not None
        )
        zones = self._zones(content, spacing, has_projection, has_context)
        self.last_zones.append(zones)
        self._draw_header(draw, zones.header, roles, spacing, matchup)
        body = (content[0], zones.teams[1], content[2], content[3])
        if matchup.error_state:
            self._draw_error(draw, body, roles, matchup)
            return
        if matchup.user_side is None:
            self._center(draw, matchup.status_label, body, _font(roles.win_percent, bold=True), YELLOW, "empty")
            return
        if matchup.opponent_side is None:
            self._draw_bye(draw, body, roles, matchup)
            return
        user, opponent = matchup.user_side, matchup.opponent_side
        mid, half_gap = (content[0] + content[2]) // 2, spacing.center_width // 2
        self._draw_team(draw, (content[0], zones.teams[1], mid - half_gap, zones.teams[3]), roles, user, True, show_records)
        self._draw_team(draw, (mid + half_gap, zones.teams[1], content[2], zones.teams[3]), roles, opponent, False, show_records)
        self._draw_scores(draw, zones.scores, roles, spacing, user.current_score, opponent.current_score)
        if not has_projection:
            if show_projected and zones.analytics[3] > zones.analytics[1]:
                self._center(draw, "PROJECTIONS UNAVAILABLE", zones.analytics, _font(roles.context_secondary, bold=True), MUTED, "projection_unavailable")
            return
        self._draw_analytics(draw, zones.analytics, roles, spacing, density, matchup, show_projected, show_probability)
        if zones.footer:
            self._draw_context(draw, zones.footer, roles, user, opponent)

    def _draw_header(self, draw, box, roles, spacing, matchup):
        x0, y0, x1, y1 = box
        status_text = f"WK {matchup.week} • {matchup.status_label}"
        floor = roles.context_secondary
        status, status_font = fit_text(draw, status_text, (x1 - x0) * .34, roles.card_status, floor, True)
        status_w = _text_width(draw, status, status_font)
        league, league_font = fit_text(draw, matchup.league_name, x1 - x0 - status_w - max(12, spacing.section_gap * 3), roles.card_league, max(floor, roles.card_league - 3), True)
        # Pull the shared baseline slightly upward so even tall glyphs retain
        # an 8px breathing gap above the header divider in LARGE cards.
        baseline = y0 + max(league_font.getmetrics()[0], status_font.getmetrics()[0]) - 4
        self._text(draw, (x0, baseline), league, role="league", font=league_font, fill=CREAM, anchor="ls")
        self._text(draw, (x1, baseline), status, role="status", font=status_font, fill=YELLOW, anchor="rs")
        draw.line((x0, y1, x1, y1), fill=MUTED, width=spacing.divider)

    def _draw_team(self, draw, box, roles, side, is_user, show_records):
        x0, y0, x1, y1 = box
        x, anchor = (x0, "la") if is_user else (x1, "ra")
        name, font = fit_text(draw, side.team_name, x1 - x0, roles.team_name, roles.team_name_min, True)
        self._text(draw, (x, y0 + 1), name, role="team_name", font=font, fill=CREAM, anchor=anchor)
        label = (f"YOU   {side.record}" if is_user else f"{side.record}   OPP") if show_records else ("YOU" if is_user else "OPP")
        label, meta_font = fit_text(draw, label, x1 - x0, roles.team_record, roles.context_secondary, True)
        self._text(draw, (x, y1 - 2), label, role="team_meta", font=meta_font, fill=LIME if is_user else MUTED, anchor="ls" if is_user else "rs")

    def _draw_scores(self, draw, box, roles, spacing, left_value, right_value):
        x0, y0, x1, y1 = box
        mid, center_half = (x0 + x1) // 2, spacing.center_width // 2
        vs_font = _font(roles.center_label, bold=True)
        glyph = draw.textbbox((0, 0), "vs", font=vs_font)
        padding = max(2, round(CENTER_SEPARATOR_PADDING * vs_font.size / CENTER_SEPARATOR_FONT_SIZE))
        center_half = max(center_half, (glyph[2] - glyph[0] + 1) // 2 + padding)
        left_box, right_box = (x0, y0, mid - center_half, y1), (mid + center_half, y0, x1, y1)
        values, size = (display_score(left_value), display_score(right_value)), roles.score
        width = min(left_box[2] - left_box[0], right_box[2] - right_box[0])
        while size > roles.score_min and max(_text_width(draw, value, _font(size, bold=True)) for value in values) > width:
            size -= 1
        font = _font(size, bold=True)
        ascent, descent = font.getmetrics()
        baseline = min(y1 - descent, y0 + (y1 - y0 + ascent - descent) // 2)
        self._text(draw, ((left_box[0] + left_box[2]) // 2, baseline), values[0], role="score", font=font, fill=CREAM, anchor="ms")
        self._text(draw, ((right_box[0] + right_box[2]) // 2, baseline), values[1], role="score", font=font, fill=CREAM, anchor="ms")
        self._draw_center_separator(draw, (mid - center_half, y0, mid + center_half, y1), vs_font)

    def _draw_center_separator(self, draw, box, font):
        """Use the label itself as separator, with no optical center-line stroke.

        A 4px glyph gap still left collinear divider stubs visually resembling
        an I between V and S. Protect the entire reserved score-center strip,
        not merely the glyph box. Draw the clean background last, before text.
        """
        x0, y0, x1, y1 = box
        draw.rectangle((x0, y0, x1, y1 - 1), fill=CARD)
        self.last_separator_clear_zones.append(((x0, y0, x1, y1 - 1), self._card_box))
        glyph = draw.textbbox((0, 0), "vs", font=font)
        top = (y0 + y1 - (glyph[3] - glyph[1])) // 2
        self._text(draw, ((x0 + x1) // 2, top), "vs", role="vs", font=font, fill=YELLOW, anchor="mt")

    def _draw_analytics(self, draw, box, roles, spacing, density, matchup, show_projected, show_probability):
        x0, y0, x1, y1 = box
        user, opponent, cursor = matchup.user_side, matchup.opponent_side, y0
        if show_projected and density is not Density.COMPACT:
            font = _font(roles.projected_score, bold=True)
            baseline = cursor + font.getmetrics()[0]
            self._text(draw, (x0, baseline), f"PROJ {display_score(user.projected_final)}", role="projection", font=font, fill=MUTED, anchor="ls")
            self._text(draw, (x1, baseline), f"PROJ {display_score(opponent.projected_final)}", role="projection", font=font, fill=MUTED, anchor="rs")
            cursor += font.size + 3
        probability = user.win_probability if show_probability else None
        tied = matchup.status is MatchupStatus.FINAL and abs(user.current_score - opponent.current_score) < .001
        if tied:
            self._center(draw, "FINAL TIE", (x0, cursor, x1, y1), _font(roles.win_percent, bold=True), YELLOW, "win_probability")
            return
        if probability is None:
            return
        pct = round(probability * 100)
        percent_font, label_font = _font(roles.win_percent, bold=True), _font(roles.win_label, bold=True)
        row_top = cursor + max(0, (y1 - cursor - percent_font.size - label_font.size - 2) // 2)
        baseline = row_top + percent_font.getmetrics()[0]
        self._text(draw, (x0, baseline), f"{pct}%", role="win_percent", font=percent_font, fill=LIME, anchor="ls")
        self._text(draw, (x1, baseline), f"{100-pct}%", role="win_percent", font=percent_font, fill=CREAM, anchor="rs")
        inset = round((x1 - x0) * .20)
        bar_x0, bar_x1 = x0 + inset, x1 - inset
        bar_y0 = row_top + max(0, (percent_font.size - spacing.probability_bar) // 2)
        # Pillow rectangles include both endpoints: height is exactly the shared
        # spacing value, and remains centered beside the percentage row.
        bar_y1 = min(y1 - 1, bar_y0 + spacing.probability_bar - 1)
        split = bar_x0 + round((bar_x1 - bar_x0) * probability)
        draw.rectangle((bar_x0, bar_y0, bar_x1, bar_y1), fill=CREAM, outline=BLACK, width=1)
        if split > bar_x0:
            draw.rectangle((bar_x0 + 1, bar_y0 + 1, min(split, bar_x1 - 1), bar_y1 - 1), fill=LIME)
        self.last_probability_bars.append(((bar_x0, bar_y0, bar_x1, bar_y1), probability, self._card_box))
        self._text(draw, ((x0 + x1) // 2, row_top + percent_font.size + 2), "WIN CHANCE", role="win_label", font=label_font, fill=MUTED, anchor="mt")

    def _draw_context(self, draw, box, roles, user, opponent):
        left, right = display_context(user), display_context(opponent)
        if "UNAVAILABLE" in left or "UNAVAILABLE" in right:
            return
        font, baseline = _font(roles.context_primary, bold=True), box[3] - 2
        self._text(draw, (box[0], baseline), left, role="context", font=font, fill=MUTED, anchor="ls")
        self._text(draw, (box[2], baseline), right, role="context", font=font, fill=MUTED, anchor="rs")

    def _draw_error(self, draw, box, roles, matchup):
        title_font = _font(roles.card_status, bold=True, pixel=True)
        self._text(draw, (box[0], box[1] + 12), "MATCHUP UNAVAILABLE", role="error_state", font=title_font, fill=RED)
        message, font = fit_text(draw, matchup.error_state, box[2] - box[0], roles.context_primary, roles.context_secondary, False)
        self._center(draw, message, (box[0], box[1] + title_font.size + 14, box[2], box[3]), font, MUTED, "error_message")

    def _draw_bye(self, draw, box, roles, matchup):
        user = matchup.user_side
        name, font = fit_text(draw, user.team_name, box[2] - box[0], roles.team_name, roles.team_name_min, True)
        self._text(draw, ((box[0] + box[2]) // 2, box[1] + 12), name, role="team_name", font=font, fill=CREAM, anchor="ma")
        meta = _font(roles.team_record, bold=True)
        self._text(draw, ((box[0] + box[2]) // 2, box[1] + 16 + font.size), f"YOU   {user.record}", role="team_meta", font=meta, fill=LIME, anchor="ma")
        self._center(draw, "BYE WEEK", (box[0], box[1] + font.size * 2, box[2], box[3]), _font(roles.score * .58, bold=True), YELLOW, "bye")

    def _center(self, draw, text, box, font, fill, role):
        self._text(draw, ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), text, role=role, font=font, fill=fill, anchor="mm")
