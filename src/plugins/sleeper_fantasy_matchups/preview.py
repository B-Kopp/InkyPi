"""Generate Sleeper matchup previews without network access."""

from pathlib import Path

from plugins.sleeper_fantasy_matchups.preview_fixtures import preview_states
from plugins.sleeper_fantasy_matchups.renderer import MatchupDashboardRenderer


def main():
    output = Path("mock_display_output/sleeper_fantasy_matchups")
    output.mkdir(parents=True, exist_ok=True)
    for name, model in preview_states().items():
        MatchupDashboardRenderer().render(model, (800, 480)).save(output / f"{name}.png")
    print(f"Wrote {len(preview_states())} previews to {output}")


if __name__ == "__main__":
    main()

