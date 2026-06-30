"""Worker process entrypoint — runs the scheduler (planner + executor + poller)."""

from app.scheduler.runner import main

if __name__ == "__main__":
    main()
