"""
Placeholder Gradio UI for SceneScout.

Phase 2.6 skeleton — full onboarding, profile viewer, and Dev Section
are implemented in Phase 7.
"""

from __future__ import annotations

import os

import gradio as gr


def create_app() -> gr.Blocks:
    """Build the placeholder Gradio application.

    Returns
    -------
    gr.Blocks
        Minimal SceneScout landing page for Docker and local smoke tests.
    """
    with gr.Blocks(title="SceneScout") as app:
        gr.Markdown("# SceneScout")
        gr.Markdown(
            "Placeholder UI — personalized event discovery is on the way.\n\n"
            "Onboarding, profile management, and the Dev Section arrive in Phase 7."
        )

    return app


def main() -> None:
    """Launch the Gradio server."""
    app = create_app()
    password = os.getenv("GRADIO_PASSWORD")
    auth = ("scenescout", password) if password else None
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        auth=auth,
    )


if __name__ == "__main__":
    main()
