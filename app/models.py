from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, ForeignKey, JSON, Float,
    Enum as SAEnum, Index,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import enum

from app.database import Base


# ── Enums ────────────────────────────────────────────────────────────────────

class ScheduleType(str, enum.Enum):
    INTERVAL = "interval"
    CRON = "cron"


# ── User (existing, extended) ───────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    audit_log = relationship("AuditLog", foreign_keys="AuditLog.user_id")
    push_subscriptions = relationship("PushSubscription", back_populates="user")


# ── Secret (encrypted credential store) ─────────────────────────────────────

class Secret(Base):
    """Encrypted secret storage referenced by sources, actions, etc."""
    __tablename__ = "secrets"

    id = Column(Integer, primary_key=True, index=True)
    scoped_to_type = Column(String(50), nullable=False)  # e.g. 'source', 'action'
    scoped_to_id = Column(Integer, nullable=False)  # FK to the owning entity
    encrypted_value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_secrets_scope", "scoped_to_type", "scoped_to_id"),
    )


# ── Source ───────────────────────────────────────────────────────────────────

class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    source_type = Column(String(100), nullable=False)  # 'webhook' | 'poll'
    description = Column(Text, default="")
    tags = Column(JSON, default=list)  # list[str]
    icon = Column(String(100), default="")
    enabled = Column(Boolean, default=True)
    config = Column(JSON, default=dict)  # adapter-specific config
    webhook_secret_id = Column(Integer, ForeignKey("secrets.id"), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    event_types = relationship("EventTypeRecord", back_populates="source")
    schedule = relationship(
        "PollingSchedule", back_populates="source", uselist=False,
    )
    events = relationship("Event", back_populates="source")
    metrics = relationship("MetricPoint", back_populates="source")
    actions = relationship("ActionInstance", back_populates="source")


# ── EventType (per-source event kind) ───────────────────────────────────────

class EventTypeRecord(Base):
    """Named kind of occurrence belonging to a source."""
    __tablename__ = "event_types"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)  # e.g. 'client.created', 'order.paid'
    description = Column(Text, default="")
    schema_hint = Column(JSON, default=dict)  # extraction hints / JSON schema
    enabled = Column(Boolean, default=True)  # paused types still ingest; rules skip them
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    source = relationship("Source", back_populates="event_types")


# ── PollingSchedule ─────────────────────────────────────────────────────────

class PollingSchedule(Base):
    __tablename__ = "polling_schedules"

    id = Column(Integer, primary_key=True, index=True)
    # One schedule per poll source (webhook sources have none).
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    schedule_type = Column(
        SAEnum(ScheduleType, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default="interval",
    )
    cron_expression = Column(String(100), default="")  # for cron-style schedules
    interval_seconds = Column(Integer, nullable=True)  # for interval-style
    handler_type = Column(String(100), nullable=False, default="http_get")  # http_get | http_post
    handler_url = Column(String(2000), default="")
    handler_params = Column(JSON, default=dict)  # query/body/headers/json_path
    timeout_seconds = Column(Integer, default=30)
    retry_count = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    last_error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    source = relationship("Source", back_populates="schedule")


# ── ActionInstance ───────────────────────────────────────────────────────────

class ActionInstance(Base):
    __tablename__ = "actions"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, index=True)
    action_type = Column(String(100), nullable=False)  # field_push | http_forward | notify | web_push | local_script
    config = Column(JSON, default=dict)  # action-specific config
    enabled = Column(Boolean, default=True)
    secret_id = Column(Integer, ForeignKey("secrets.id"), nullable=True)
    secret_id_2 = Column(Integer, ForeignKey("secrets.id"), nullable=True)  # key+secret auth
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    source = relationship("Source", back_populates="actions")
    secret = relationship("Secret", foreign_keys=[secret_id])
    secret_2 = relationship("Secret", foreign_keys=[secret_id_2])


# ── Rule (binding event types + conditions → actions) ───────────────────────

class Rule(Base):
    __tablename__ = "rules"

    id = Column(Integer, primary_key=True, index=True)
    description = Column(Text, default="")
    event_type_ids = Column(JSON, default=list)  # list[int] — FK targets in event_types
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)  # source-wide if set
    conditions = Column(JSON, default=dict)  # field matchers (exact/not/gt/lt/contains/regex)
    action_ids = Column(JSON, default=list)  # list[int] — FK targets in actions
    order_index = Column(Integer, default=0)  # execution order within matching rules
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    source = relationship("Source", backref="rules")


# ── Event (normalized occurrence) ───────────────────────────────────────────

class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False, index=True)
    event_type_id = Column(Integer, ForeignKey("event_types.id"), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    normalized_data = Column(JSON, default=dict)  # extracted fields
    raw_payload = Column(Text, default="")  # original payload (truncated if too large)
    correlation_id = Column(String(100), nullable=True, index=True)
    status = Column(String(30), default="pending", index=True)  # pending | processed | failed
    processing_error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    source = relationship("Source", back_populates="events")
    event_type = relationship("EventTypeRecord")


# ── Field (global storage sink for log / metric actions) ────────────────────

class Field(Base):
    """Named global storage target: logbook, value, text, or toggle."""
    __tablename__ = "fields"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), unique=True, nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    field_type = Column(String(30), nullable=False)  # logbook | value | text | toggle
    # logbook: {max_entries}; others: {}
    config = Column(JSON, default=dict)
    # value: {"value": float}; text: {"value": str}; toggle: {"value": bool}
    state = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    log_entries = relationship("FieldLogEntry", back_populates="field")
    metrics = relationship("MetricPoint", back_populates="field")


class FieldLogEntry(Base):
    """Append-only logbook entry, pruned to Field.config.max_entries."""
    __tablename__ = "field_log_entries"

    id = Column(Integer, primary_key=True, index=True)
    field_id = Column(Integer, ForeignKey("fields.id"), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    value = Column(JSON, nullable=True)  # JSON-compatible payload
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)

    field = relationship("Field", back_populates="log_entries")


# ── MetricPoint (time-series / aggregated metrics) ──────────────────────────

class MetricPoint(Base):
    __tablename__ = "metric_points"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)
    field_id = Column(Integer, ForeignKey("fields.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False, index=True)  # metric name (often Field.name)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    value = Column(Float, default=0.0)
    tags = Column(JSON, default=dict)  # arbitrary key-value labels
    metric_type = Column(String(20), default="counter")  # counter | gauge | histogram

    source = relationship("Source", back_populates="metrics")
    field = relationship("Field", back_populates="metrics")


# ── AuditLog ────────────────────────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(100), nullable=False)  # e.g. 'login', 'source.create', 'config.change'
    resource_type = Column(String(100), default="")
    resource_id = Column(Integer, nullable=True)
    details = Column(JSON, default=dict)
    ip_address = Column(String(60), default="")  # IPv6 max length
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)


# ── DashboardLayout (shared widget layout for the install) ───────────────────

class DashboardLayout(Base):
    __tablename__ = "dashboard_layouts"

    id = Column(Integer, primary_key=True, index=True)
    layout_config = Column(JSON, default=dict)  # widget positions, visible panels, etc.
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── AppSettings (singleton install preferences) ──────────────────────────────

class AppSettings(Base):
    """Single-row app settings (id=1). Appearance is global for the install."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)  # always 1
    theme = Column(String(50), nullable=False, default="system")
    font = Column(String(50), nullable=False, default="system")
    font_size = Column(String(20), nullable=False, default="md")
    dashboard_bg_filename = Column(String(100), nullable=True)
    dashboard_bg_opacity = Column(Float, nullable=False, default=0.35)


# ── PushSubscription (Web Push endpoints per user) ───────────────────────────

class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(String(2000), unique=True, nullable=False)
    p256dh = Column(String(200), nullable=False)
    auth = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="push_subscriptions")
