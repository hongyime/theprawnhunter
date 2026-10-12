"""Help_Text flow task definitions."""

from .common import *

@app.task(name="flow.system_help")
def system_help():
    """Periodic guide on how to use system commands."""
    msg = (
        "ℹ️ **System Commands Guide**\n"
        "You can control the system using these commands:\n\n"
        "• `/status` - View queue size, DB connectivity, and paused state.\n"
        "• `/pause` - Suspend all scanners and broadcasters (Maintenance Mode).\n"
        "• `/resume` - Resume normal operations.\n"
        "• `/restart` - Restart the Bot Listener process.\n\n"
        "_Commands are restricted to Admins and Whitelisted Users._"
    )
    from app.workers.celery_app import get_worker_loop
    get_worker_loop().run_until_complete(get_broadcaster().send_log(msg))
    return "Help guide sent."
