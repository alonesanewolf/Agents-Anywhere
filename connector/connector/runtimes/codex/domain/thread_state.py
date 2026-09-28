"""Read native activity without claiming that an unread thread is idle."""

from .activity import activity


def thread_status(thread):
    if thread.get("requests"):
        return "waiting_approval"
    status = thread.get("status")
    if isinstance(status, dict):
        status = status.get("type")
    if status in {"systemError", "error"}:
        return "error"
    if activity(thread)["running"]:
        return "running"
    return "idle"
