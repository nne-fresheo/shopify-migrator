from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


class DescriptionRenderer:
    """Renders a meal dict into the Shopify product body_html via Jinja2.

    The template file is loaded from an external path so it can be edited
    without redeploying the tool. See DESCRIPTION_TEMPLATE in .env.
    """

    def __init__(self, template_path: Path) -> None:
        template_path = Path(template_path)
        if not template_path.exists():
            raise FileNotFoundError(
                f"Description template not found: {template_path}. "
                f"Set DESCRIPTION_TEMPLATE in .env."
            )
        env = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = env.get_template(template_path.name)
        logger.debug(f"[renderer] loaded template {template_path}")

    def render(self, meal: dict) -> str:
        return self._template.render(meal=meal)
