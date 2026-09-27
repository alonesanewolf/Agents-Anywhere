"""Read native activity without claiming that an unread thread is idle."""


def thread_status(thread):
    if thread.get("requests"):
        return "waiting_approval"
    status = thread.get("status")
    if isinstance(status, dict):
        status = status.get("type")
    if status in {"systemError", "error"}:
        return "error"
    if status == "active" or any(
        turn.get("status") == "inProgress" for turn in thread.get("turns", [])
    ):
        return "running"
    return "idle"
