"""G3: handler registration is an IMPORT SIDE EFFECT.

Importing this package registers every handler. `app/startup.py` imports it, and
so must any migration or standalone script that advances a workflow.
"""
from app.bpm_tasks import blog_post, message_thread, order, video  # noqa: F401

__all__ = ["blog_post", "message_thread", "order", "video"]
