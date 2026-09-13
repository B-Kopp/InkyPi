"""Generate all MLB Live Score states without network access."""

from pathlib import Path

from plugins.mlb_live_score.preview_fixtures import preview_states
from plugins.mlb_live_score.renderer import ScoreboardRenderer


def main():
    output = Path("mock_display_output/mlb_live_score")
    output.mkdir(parents=True, exist_ok=True)
    renderer = ScoreboardRenderer()
    for name, state in preview_states().items():
        renderer.render(state, (800, 480)).save(output / f"{name}.png")
    print(f"Wrote {len(preview_states())} previews to {output}")


if __name__ == "__main__":
    main()
