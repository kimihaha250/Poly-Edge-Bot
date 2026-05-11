from __future__ import annotations

import logging

from bot.config import load_config
from bot.runner import run_bot


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> None:
    _configure_logging()
    config = load_config()
    logging.getLogger("poly_edge_bot").info(
        "Starting bot in %s mode. Always test with small capital first.",
        "DRY_RUN" if config.dry_run else "LIVE",
    )
    run_bot(config)


if __name__ == "__main__":
    main()
