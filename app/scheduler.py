"""Background polling scheduler — APScheduler wired to PollingSchedule rows."""
import logging
from datetime import timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database import SessionLocal
from app.models import PollingSchedule, ScheduleType
from app.pollers import run_schedule

logger = logging.getLogger("para_scope.scheduler")

_scheduler: BackgroundScheduler | None = None
_consecutive_failures: dict[int, int] = {}

_MAX_BACKOFF_EXPONENT = 5  # up to 2**5 = 32, but capped via multiplier below
_MAX_BACKOFF_MULTIPLIER = 16


def _job_id(schedule_id: int) -> str:
    return f"poll_{schedule_id}"


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def get_consecutive_failures(schedule_id: int) -> int:
    return _consecutive_failures.get(schedule_id, 0)


def record_poll_outcome(schedule_id: int, success: bool) -> int:
    """Update consecutive-failure counter. Returns the new count."""
    if success:
        _consecutive_failures[schedule_id] = 0
        return 0
    count = _consecutive_failures.get(schedule_id, 0) + 1
    _consecutive_failures[schedule_id] = count
    return count


def clear_consecutive_failures(schedule_id: int | None = None) -> None:
    """Reset backoff state (for tests / job removal)."""
    if schedule_id is None:
        _consecutive_failures.clear()
    else:
        _consecutive_failures.pop(schedule_id, None)


def backoff_multiplier(consecutive: int) -> int:
    """2**min(consecutive, 5) capped at 16×."""
    if consecutive <= 0:
        return 1
    return min(2 ** min(consecutive, _MAX_BACKOFF_EXPONENT), _MAX_BACKOFF_MULTIPLIER)


def interval_jitter_seconds(base_seconds: int) -> int:
    """Jitter for interval triggers: min(max(1, seconds // 10), 30)."""
    return min(max(1, base_seconds // 10), 30)


def start_scheduler():
    """Start the background scheduler and register all enabled schedules."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.start()
    logger.info("Polling scheduler started")

    db = SessionLocal()
    try:
        schedules = db.query(PollingSchedule).filter(PollingSchedule.enabled == True).all()
        for schedule in schedules:
            add_or_update_job(schedule)
        logger.info("Registered %d polling jobs", len(schedules))
    finally:
        db.close()

    return _scheduler


def stop_scheduler():
    """Shut down the scheduler cleanly."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Polling scheduler stopped")


def _trigger_for(schedule: PollingSchedule, consecutive: int | None = None):
    """Build an APScheduler trigger from a PollingSchedule row.

    Interval schedules get jitter and consecutive-failure backoff.
    """
    st = schedule.schedule_type
    st_value = st.value if hasattr(st, "value") else str(st)

    if st_value == ScheduleType.CRON.value or st_value == "cron":
        expr = (schedule.cron_expression or "").strip()
        if not expr:
            raise ValueError(f"Schedule {schedule.id} has empty cron_expression")
        return CronTrigger.from_crontab(expr, timezone="UTC")

    seconds = schedule.interval_seconds or 0
    if seconds < 1:
        raise ValueError(f"Schedule {schedule.id} has invalid interval_seconds={seconds}")

    if consecutive is None:
        consecutive = get_consecutive_failures(schedule.id)
    effective = seconds * backoff_multiplier(consecutive)
    jitter = interval_jitter_seconds(seconds)
    return IntervalTrigger(seconds=effective, jitter=jitter, timezone="UTC")


def _on_job_done(schedule_id: int):
    """After a job fires, refresh next_run_at from the scheduler."""
    if _scheduler is None:
        return
    job = _scheduler.get_job(_job_id(schedule_id))
    next_run = job.next_run_time if job else None
    db = SessionLocal()
    try:
        schedule = db.query(PollingSchedule).filter(PollingSchedule.id == schedule_id).first()
        if schedule:
            schedule.next_run_at = next_run
            db.commit()
    finally:
        db.close()


def _job_wrapper(schedule_id: int):
    """Wrapper that runs the poll, updates backoff, then refreshes next_run_at."""
    try:
        ok = run_schedule(schedule_id)
        record_poll_outcome(schedule_id, ok)
        # Re-register so the next interval reflects the updated backoff multiplier.
        db = SessionLocal()
        try:
            schedule = db.query(PollingSchedule).filter(PollingSchedule.id == schedule_id).first()
            if schedule:
                add_or_update_job(schedule)
        finally:
            db.close()
    finally:
        _on_job_done(schedule_id)


def add_or_update_job(schedule: PollingSchedule):
    """Register or replace a schedule's job. No-op if scheduler not running."""
    if _scheduler is None:
        return
    jid = _job_id(schedule.id)
    existing = _scheduler.get_job(jid)
    if existing:
        _scheduler.remove_job(jid)

    if not schedule.enabled:
        clear_consecutive_failures(schedule.id)
        return

    try:
        trigger = _trigger_for(schedule)
    except ValueError as e:
        logger.warning("Skipping schedule %s: %s", schedule.id, e)
        return

    job = _scheduler.add_job(
        _job_wrapper,
        trigger=trigger,
        id=jid,
        args=[schedule.id],
        name=f"poll:{schedule.id}",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Persist next_run_at immediately
    db = SessionLocal()
    try:
        row = db.query(PollingSchedule).filter(PollingSchedule.id == schedule.id).first()
        if row:
            row.next_run_at = job.next_run_time
            if row.next_run_at and row.next_run_at.tzinfo is None:
                row.next_run_at = row.next_run_at.replace(tzinfo=timezone.utc)
            db.commit()
    finally:
        db.close()

    logger.info("Scheduled job %s next=%s", jid, job.next_run_time)


def remove_job(schedule_id: int):
    """Remove a schedule's job from the scheduler."""
    if _scheduler is None:
        return
    jid = _job_id(schedule_id)
    if _scheduler.get_job(jid):
        _scheduler.remove_job(jid)
        logger.info("Removed job %s", jid)
    clear_consecutive_failures(schedule_id)


def job_count() -> int:
    """Number of currently registered polling jobs."""
    if _scheduler is None:
        return 0
    return len(_scheduler.get_jobs())
