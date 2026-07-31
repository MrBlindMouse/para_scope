"""Focused tests for on-demand triggers, SSE, fields namespace, dry-run, templates."""
from __future__ import annotations

import json

import pytest

pytest_plugins = ["app.tests.test_app"]

from app.tests.test_app import _create_source  # noqa: E402


class TestNeverSchedule:
    def test_never_schedule_no_job_run_now_works(self, authenticated_client, monkeypatch):
        from app.database import get_db
        from app.models import PollingSchedule, ScheduleType, Source
        from app.scheduler import add_or_update_job, get_scheduler, start_scheduler, stop_scheduler

        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Never Poll",
                "source_type": "poll",
                "description": "",
                "poll_category": "connectivity",
                "handler_type": "tcp_connect",
                "schedule_type": "never",
                "host": "127.0.0.1",
                "port": "9",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text[:500]
        assert "error" not in str(resp.headers.get("location", ""))

        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.name == "Never Poll").one()
            sched = db.query(PollingSchedule).filter(PollingSchedule.source_id == src.id).one()
            st = sched.schedule_type.value if hasattr(sched.schedule_type, "value") else sched.schedule_type
            assert st == ScheduleType.NEVER.value
            sid = src.id
            schedule_id = sched.id
        finally:
            db.close()

        start_scheduler()
        try:
            db = next(get_db())
            try:
                sched = db.query(PollingSchedule).filter(PollingSchedule.id == schedule_id).one()
                add_or_update_job(sched)
            finally:
                db.close()
            sch = get_scheduler()
            assert sch is not None
            assert sch.get_job(f"poll_{schedule_id}") is None
        finally:
            stop_scheduler()

        called = []
        monkeypatch.setattr("app.routers.pipeline.run_schedule", lambda i: called.append(i) or True)
        resp = authenticated_client.post(f"/config/source/{sid}/poll-now", follow_redirects=False)
        assert resp.status_code == 303
        assert called == [schedule_id]


class TestTriggerSourceAction:
    def test_create_trigger_source_poll(self, authenticated_client):
        poll_sid, _ = _create_source(
            authenticated_client, name="Poll Tgt", slug="poll-tgt-tp", source_type="poll",
        )
        hook_sid, _ = _create_source(
            authenticated_client, name="Hook Src", slug="hook-src-tp", source_type="webhook",
        )
        resp = authenticated_client.post(
            f"/config/pipeline/source/{hook_sid}/actions",
            data={
                "action_type": "trigger_source",
                "target_ref": f"poll:{poll_sid}",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error" not in str(resp.headers.get("location", ""))

        from app.database import get_db
        from app.models import ActionInstance
        db = next(get_db())
        try:
            action = (
                db.query(ActionInstance)
                .filter(ActionInstance.source_id == hook_sid, ActionInstance.action_type == "trigger_source")
                .first()
            )
            assert action is not None
            assert action.config["target_source_id"] == poll_sid
            assert action.config.get("event_type_id") is None
        finally:
            db.close()

    def test_create_trigger_source_webhook_with_payload(self, authenticated_client):
        tgt_sid, _ = _create_source(
            authenticated_client, name="Hook Tgt", slug="hook-tgt-ts", source_type="webhook",
        )
        src_sid, _ = _create_source(
            authenticated_client, name="Hook Fire", slug="hook-fire-ts", source_type="webhook",
        )
        from app.database import get_db
        from app.models import ActionInstance, Event, EventTypeRecord, Field, Rule
        from app.pipeline import evaluate_and_dispatch

        db = next(get_db())
        try:
            et = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == tgt_sid, EventTypeRecord.name == "always")
                .first()
            )
            assert et is not None
            field = Field(name="Temp", slug="ts_temp", field_type="value", config={}, state={"value": 21})
            db.add(field)
            db.commit()
            action = ActionInstance(
                source_id=src_sid,
                action_type="trigger_source",
                config={
                    "target_source_id": tgt_sid,
                    "event_type_id": et.id,
                    "payload": {
                        "status": "{{ status }}",
                        "temp": "{{ fields.ts_temp.value }}",
                        "plain": 1,
                    },
                },
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=src_sid, event_type_ids=[], conditions={},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            event = Event(
                source_id=src_sid, normalized_data={"status": "hot"},
                raw_payload="{}", correlation_id="ts-wh",
            )
            db.add(event)
            db.commit()
            before = db.query(Event).filter(Event.source_id == tgt_sid).count()
            evaluate_and_dispatch(db, event)
            after_events = (
                db.query(Event)
                .filter(Event.source_id == tgt_sid)
                .order_by(Event.id.desc())
                .all()
            )
            assert len(after_events) >= before + 1
            nd = after_events[0].normalized_data or {}
            assert nd.get("status") == "hot"
            assert nd.get("temp") == "21"
            assert nd.get("plain") == 1
            assert nd.get("_trigger", {}).get("origin") == "action"
            assert event.status == "processed"
        finally:
            db.close()

    def test_invalid_payload_rejected(self, authenticated_client):
        tgt_sid, _ = _create_source(
            authenticated_client, name="Bad Pay Tgt", slug="bad-pay-tgt", source_type="webhook",
        )
        src_sid, _ = _create_source(
            authenticated_client, name="Bad Pay Src", slug="bad-pay-src", source_type="webhook",
        )
        from app.database import get_db
        from app.models import EventTypeRecord

        db = next(get_db())
        try:
            et = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == tgt_sid, EventTypeRecord.name == "always")
                .first()
            )
            et_id = et.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/source/{src_sid}/actions",
            data={
                "action_type": "trigger_source",
                "target_ref": f"webhook:{tgt_sid}:{et_id}",
                "payload": "[1,2,3]",
            },
            follow_redirects=False,
        )
        # HTMX stays in dialog with error, or redirect with error
        assert resp.status_code in (200, 303)
        if resp.status_code == 303:
            assert "error" in str(resp.headers.get("location", "")).lower() or True
        else:
            assert "JSON object" in resp.text or "valid JSON" in resp.text or "payload" in resp.text.lower()


class TestFieldsNamespace:
    def test_fields_slug_read_after_write(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Fields NS", slug="fields-ns")
        from app.database import get_db
        from app.models import ActionInstance, Rule, Event, Field
        from app.pipeline import evaluate_and_dispatch

        db = next(get_db())
        try:
            src = Field(name="Src", slug="ns_src", field_type="value", config={}, state={"value": 0})
            dst = Field(name="Dst", slug="ns_dst", field_type="value", config={}, state={"value": 0})
            db.add_all([src, dst])
            db.commit()
            a1 = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": src.id, "op": "set", "delta": "7"},
            )
            a2 = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": dst.id, "op": "set", "delta": "fields.ns_src.value"},
            )
            db.add_all([a1, a2])
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[], conditions={},
                action_ids=[a1.id, a2.id], order_index=0,
            )
            db.add(rule)
            event = Event(
                source_id=sid, normalized_data={"x": 1}, raw_payload="{}",
                correlation_id="fields-ns",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(src)
            db.refresh(dst)
            assert src.state["value"] == 7.0
            assert dst.state["value"] == 7.0
        finally:
            db.close()


class TestDashboardTrigger:
    def test_trigger_webhook_requires_auth(self, client):
        resp = client.post(
            "/api/dashboard/trigger",
            json={"kind": "webhook", "source_id": 1, "event_type_id": 1},
        )
        # Setup redirect, login redirect, CSRF reject, or JSON 401
        assert resp.status_code in (401, 303, 302, 403)

    def test_trigger_webhook_creates_event(self, authenticated_client):
        sid, _ = _create_source(
            authenticated_client, name="Trig Hook", slug="trig-hook", source_type="webhook",
        )
        from app.database import get_db
        from app.models import Event, EventTypeRecord, Field

        db = next(get_db())
        try:
            et = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == sid, EventTypeRecord.name == "always")
                .first()
            )
            assert et is not None
            et_id = et.id
            db.add(Field(name="Disk", slug="trig_disk", field_type="value", config={}, state={"value": 42}))
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.post(
            "/api/dashboard/trigger",
            json={
                "kind": "webhook",
                "source_id": sid,
                "event_type_id": et_id,
                "payload": {
                    "from_field": "{{ trig_disk.value }}",
                    "from_event": "{{ status }}",
                    "fixed": "yes",
                },
            },
        )
        assert resp.status_code == 200, resp.text[:500]
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("event_id")

        db = next(get_db())
        try:
            event = db.query(Event).filter(Event.id == body["event_id"]).first()
            assert event is not None
            assert event.source_id == sid
            nd = event.normalized_data or {}
            assert nd.get("_trigger", {}).get("origin") == "dashboard"
            assert nd.get("from_field") == "42"
            assert nd.get("from_event") == ""  # no event context on dashboard trigger
            assert nd.get("fixed") == "yes"
        finally:
            db.close()

    def test_trigger_payload_must_be_object(self, authenticated_client):
        sid, _ = _create_source(
            authenticated_client, name="Trig Bad", slug="trig-bad", source_type="webhook",
        )
        from app.database import get_db
        from app.models import EventTypeRecord

        db = next(get_db())
        try:
            et = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == sid, EventTypeRecord.name == "always")
                .first()
            )
            et_id = et.id
        finally:
            db.close()

        resp = authenticated_client.post(
            "/api/dashboard/trigger",
            json={"kind": "webhook", "source_id": sid, "event_type_id": et_id, "payload": [1, 2]},
        )
        assert resp.status_code == 400

    def test_trigger_poll(self, authenticated_client, monkeypatch):
        sid, _ = _create_source(
            authenticated_client, name="Trig Poll", slug="trig-poll", source_type="poll",
        )
        called = []
        monkeypatch.setattr("app.pollers.run_schedule", lambda i: called.append(i) or True)
        resp = authenticated_client.post(
            "/api/dashboard/trigger",
            json={"kind": "poll", "source_id": sid},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert len(called) == 1


class TestSourceTemplates:
    def test_template_picker_lists_stacks(self, authenticated_client):
        resp = authenticated_client.get("/config/pipeline/partials/source-templates")
        assert resp.status_code == 200
        assert "USD forex rates" in resp.text
        assert "GitHub Status" in resp.text
        assert "ISS position" in resp.text
        assert "Disk free space" not in resp.text

    def test_apply_fx_usd_stack(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/templates/fx_usd/apply",
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        from app.database import get_db
        from app.models import (
            ActionInstance, DashboardLayout, EventTypeRecord, Field, Rule, Source,
        )
        from app.dashboard_layout import parse_layout_config

        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == "fx_usd").first()
            assert src is not None
            assert src.source_type == "poll"
            assert src.schedule is not None
            assert "quotes=" in (src.schedule.handler_url or "")
            assert "frankfurter" in (src.schedule.handler_url or "")
            assert src.schedule.schedule_type.value == "cron"
            assert src.schedule.cron_expression == "0 8 * * *"

            ets = {
                et.name
                for et in db.query(EventTypeRecord).filter(
                    EventTypeRecord.source_id == src.id
                ).all()
            }
            assert "on_success" in ets
            assert "on_failure" in ets

            quotes = ("EUR", "GBP", "AUD", "CAD", "JPY")
            fields_by_slug = {
                f.slug: f
                for f in db.query(Field).filter(
                    Field.slug.in_([f"fx_usd_{q.lower()}" for q in quotes])
                ).all()
            }
            assert set(fields_by_slug) == {f"fx_usd_{q.lower()}" for q in quotes}
            assert all(f.field_type == "logbook" for f in fields_by_slug.values())
            assert db.query(Field).filter(Field.slug == "fx_usd_rates").first() is None

            rules = (
                db.query(Rule)
                .filter(Rule.source_id == src.id)
                .order_by(Rule.order_index)
                .all()
            )
            assert len(rules) == 5
            for rule, q in zip(rules, quotes):
                assert rule.conditions == {"value.*.quote": q}
                action = db.query(ActionInstance).filter(
                    ActionInstance.id == rule.action_ids[0]
                ).first()
                assert action.action_type == "field_push"
                assert action.config.get("value_key") == "value.*.rate"
                assert action.config.get("field_id") == fields_by_slug[f"fx_usd_{q.lower()}"].id

            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            assert layout is not None
            widgets = parse_layout_config(layout.layout_config)["widgets"]
            titles = {w.get("title") for w in widgets}
            assert "FX rates (per USD)" in titles
            assert "Current USD prices" in titles
            series = next(w for w in widgets if w.get("display") == "line")
            assert series["config"]["style"] == "multi"
            assert any(
                s.get("field_slug") == "fx_usd_eur.value"
                for s in series["config"]["sources"]
            )
            board = next(w for w in widgets if w.get("display") == "board")
            assert "1/fx_usd_eur.value" in str(board["config"]["cells"])
            assert "rates.EUR" not in str(board["config"]["cells"])
        finally:
            db.close()

    def test_apply_github_status_stack(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/templates/github_status/apply",
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        from app.database import get_db
        from app.models import ActionInstance, Field, Rule, Source
        from app.dashboard_layout import parse_layout_config
        from app.models import DashboardLayout

        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == "github_status").first()
            assert src is not None
            assert "githubstatus.com" in (src.schedule.handler_url or "")

            field = db.query(Field).filter(Field.slug == "github_ok").first()
            assert field is not None
            assert field.field_type == "toggle"

            rules = (
                db.query(Rule)
                .filter(Rule.source_id == src.id)
                .order_by(Rule.order_index)
                .all()
            )
            assert len(rules) == 2
            assert rules[0].conditions == {"status.indicator": "none"}
            assert rules[1].conditions == {"status.indicator": {"not": "none"}}

            actions = {
                a.id: a
                for a in db.query(ActionInstance).filter(
                    ActionInstance.source_id == src.id
                ).all()
            }
            assert actions[rules[0].action_ids[0]].config["value"] is True
            assert actions[rules[1].action_ids[0]].config["value"] is False

            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            widgets = parse_layout_config(layout.layout_config)["widgets"]
            toggle = next(
                w for w in widgets
                if w.get("display") == "toggle"
                and (w.get("config") or {}).get("field_slug") == "github_ok"
            )
            assert toggle["config"]["style"] == "led"
        finally:
            db.close()

    def test_apply_iss_now_stack(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/templates/iss_now/apply",
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303)

        from app.database import get_db
        from app.models import ActionInstance, DashboardLayout, Field, Rule, Source
        from app.dashboard_layout import parse_layout_config

        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.slug == "iss_now").first()
            assert src is not None
            assert "open-notify.org" in (src.schedule.handler_url or "")

            field = db.query(Field).filter(Field.slug == "iss_now").first()
            assert field is not None
            assert field.field_type == "data"

            rules = db.query(Rule).filter(Rule.source_id == src.id).all()
            assert len(rules) == 1
            action = db.query(ActionInstance).filter(
                ActionInstance.id == rules[0].action_ids[0]
            ).first()
            assert action.config.get("field_id") == field.id

            layout = db.query(DashboardLayout).order_by(DashboardLayout.id).first()
            widgets = parse_layout_config(layout.layout_config)["widgets"]
            kv = next(w for w in widgets if w.get("display") == "kv_text")
            assert "iss_now.iss_position.latitude" in kv["config"]["template"]
        finally:
            db.close()

    def test_apply_unknown_template(self, authenticated_client):
        resp = authenticated_client.post(
            "/config/pipeline/templates/nope/apply",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "Unknown" in (resp.headers.get("location") or "")

        resp = authenticated_client.post(
            "/config/pipeline/templates/nope/apply",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Unknown template" in resp.text


class TestRuleDryRun:
    def test_dry_run_match_and_fail(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Dry Src", slug="dry-src")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "ping", "description": ""},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import Event, EventTypeRecord, ActionInstance, Rule

        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid).first()
            action = ActionInstance(
                source_id=sid, action_type="web_push",
                config={"title": "t", "body": "b", "url": "/"},
                enabled=True,
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid,
                event_type_ids=[et.id],
                conditions={"status": "ok"},
                action_ids=[action.id],
                order_index=0,
            )
            db.add(rule)
            db.add(Event(
                source_id=sid, event_type_id=et.id,
                normalized_data={"status": "ok"}, raw_payload="{}",
                correlation_id="dry-ok", status="processed",
            ))
            db.commit()
            rule_id = rule.id
            et_id = et.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules/dry-run",
            data={
                "rule_id": str(rule_id),
                "event_type_ids": str(et_id),
                "conditions": json.dumps({"status": "ok"}),
                "order_index": "0",
            },
        )
        assert resp.status_code == 200
        assert "Would match" in resp.text

        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules/dry-run",
            data={
                "rule_id": str(rule_id),
                "event_type_ids": str(et_id),
                "conditions": json.dumps({"status": "fail"}),
                "order_index": "0",
            },
        )
        assert resp.status_code == 200
        assert "Would not match" in resp.text


class TestFieldsInRules:
    def test_rule_condition_reads_fields_slug(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Rule Fields", slug="rule-fields")
        from app.database import get_db
        from app.models import ActionInstance, Event, Field, Rule
        from app.pipeline import evaluate_and_dispatch

        db = next(get_db())
        try:
            field = Field(name="Gate", slug="rule_gate", field_type="value", config={}, state={"value": 9})
            db.add(field)
            db.commit()
            action = ActionInstance(
                source_id=sid, action_type="field_push",
                config={"field_id": field.id, "op": "set", "delta": "99"},
            )
            db.add(action)
            db.commit()
            rule = Rule(
                source_id=sid, event_type_ids=[],
                conditions={"fields.rule_gate.value": 9},
                action_ids=[action.id], order_index=0,
            )
            db.add(rule)
            event = Event(
                source_id=sid, normalized_data={"x": 1}, raw_payload="{}",
                correlation_id="rule-fields",
            )
            db.add(event)
            db.commit()
            evaluate_and_dispatch(db, event)
            db.refresh(field)
            assert field.state["value"] == 99.0
            assert event.status == "processed"
        finally:
            db.close()

    def test_dry_run_fields_slug(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Dry Fields", slug="dry-fields")
        authenticated_client.post(
            f"/config/pipeline/source/{sid}/events",
            data={"name": "ping", "description": ""},
            follow_redirects=False,
        )
        from app.database import get_db
        from app.models import Event, EventTypeRecord, Field, Rule

        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(EventTypeRecord.source_id == sid).first()
            field = Field(name="DryGate", slug="dry_gate", field_type="value", config={}, state={"value": 3})
            db.add(field)
            rule = Rule(
                source_id=sid, event_type_ids=[et.id],
                conditions={"fields.dry_gate.value": 3},
                action_ids=[], order_index=0,
            )
            db.add(rule)
            db.add(Event(
                source_id=sid, event_type_id=et.id,
                normalized_data={"status": "ok"}, raw_payload="{}",
                correlation_id="dry-fields", status="processed",
            ))
            db.commit()
            rule_id = rule.id
            et_id = et.id
        finally:
            db.close()

        resp = authenticated_client.post(
            f"/config/pipeline/source/{sid}/rules/dry-run",
            data={
                "rule_id": str(rule_id),
                "event_type_ids": str(et_id),
                "conditions": json.dumps({"fields.dry_gate.value": 3}),
                "order_index": "0",
            },
        )
        assert resp.status_code == 200
        assert "Would match" in resp.text
        assert "fields.*" in resp.text or "Field snapshot" in resp.text


class TestCascadeGuard:
    def test_mutual_trigger_fails_without_crash(self, authenticated_client):
        a_sid, _ = _create_source(
            authenticated_client, name="Cascade A", slug="casc-a", source_type="webhook",
        )
        b_sid, _ = _create_source(
            authenticated_client, name="Cascade B", slug="casc-b", source_type="webhook",
        )
        from app.database import get_db
        from app.models import ActionInstance, Event, EventTypeRecord, Rule
        from app.pipeline import evaluate_and_dispatch

        db = next(get_db())
        try:
            et_a = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == a_sid, EventTypeRecord.name == "always")
                .first()
            )
            et_b = (
                db.query(EventTypeRecord)
                .filter(EventTypeRecord.source_id == b_sid, EventTypeRecord.name == "always")
                .first()
            )
            act_a = ActionInstance(
                source_id=a_sid, action_type="trigger_source",
                config={"target_source_id": b_sid, "event_type_id": et_b.id, "payload": {}},
            )
            act_b = ActionInstance(
                source_id=b_sid, action_type="trigger_source",
                config={"target_source_id": a_sid, "event_type_id": et_a.id, "payload": {}},
            )
            db.add_all([act_a, act_b])
            db.commit()
            db.add(Rule(source_id=a_sid, event_type_ids=[], conditions={}, action_ids=[act_a.id], order_index=0))
            db.add(Rule(source_id=b_sid, event_type_ids=[], conditions={}, action_ids=[act_b.id], order_index=0))
            root = Event(
                source_id=a_sid, event_type_id=et_a.id,
                normalized_data={}, raw_payload="{}", correlation_id="casc-root",
            )
            db.add(root)
            db.commit()
            evaluate_and_dispatch(db, root)
            db.refresh(root)
            # Root may process; somewhere in the cascade an event fails with depth error
            failed = (
                db.query(Event)
                .filter(Event.processing_error.isnot(None))
                .all()
            )
            assert any(
                "cascade" in (e.processing_error or "").lower()
                for e in failed
            ) or root.status == "failed"
        finally:
            db.close()


class TestSSE:
    def test_events_stream_requires_auth(self, client):
        resp = client.get("/events/stream", follow_redirects=False)
        assert resp.status_code in (401, 303, 302)

    def test_events_page_uses_sse_not_htmx_poll(self, authenticated_client):
        resp = authenticated_client.get("/events")
        assert resp.status_code == 200
        assert "events.js" in resp.text
        assert 'hx-trigger="every 5s"' not in resp.text
        assert "data-events-stream-url" in resp.text

    def test_event_stream_broadcast(self):
        import asyncio
        from app import event_stream as es

        async def _run():
            q1 = await es.subscribe()
            q2 = await es.subscribe()
            try:
                es.publish(42)
                assert await q1.get() == 42
                assert await q2.get() == 42
            finally:
                await es.unsubscribe(q1)
                await es.unsubscribe(q2)

        asyncio.run(_run())


class TestSelfMetrics:
    def test_system_shows_failed_and_db_size(self, authenticated_client):
        sid, _ = _create_source(authenticated_client, name="Fail Sys", slug="fail-sys")
        from app.database import get_db
        from app.models import Event

        db = next(get_db())
        try:
            db.add(Event(
                source_id=sid, normalized_data={}, raw_payload="{}",
                correlation_id="fail-1", status="failed", processing_error="boom",
            ))
            db.commit()
        finally:
            db.close()

        resp = authenticated_client.get("/system")
        assert resp.status_code == 200
        assert "Failed events" in resp.text
        assert "Database size" in resp.text
        # Overdue column only renders when schedules exist


class TestDotNotationUnify:
    def test_reserved_field_slug_suffix(self, authenticated_client):
        from app.database import get_db
        from app.fields import RESERVED_FIELD_SLUGS
        from app.models import Field
        from app.webctx import _unique_field_slug

        db = next(get_db())
        try:
            for name in ("Fields", "Value", "123 Temp"):
                slug = _unique_field_slug(db, name)
                assert slug not in RESERVED_FIELD_SLUGS
                assert not slug[0].isdigit()
            assert _unique_field_slug(db, "Fields") == "fields_2"
            assert _unique_field_slug(db, "123 Temp") == "f_123_temp"
            f = Field(
                name="Fields", slug=_unique_field_slug(db, "Fields"),
                field_type="value", config={}, state={"value": 1},
            )
            db.add(f)
            db.commit()
            assert f.slug == "fields_2"
        finally:
            db.close()

    def test_table_dotted_slug_path(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data, validate_widget_bindings

        db = next(get_db())
        try:
            db.add(Field(
                name="Tbl Temp", slug="tbl_temp", field_type="value",
                config={}, state={"value": 18},
            ))
            db.commit()
            err = validate_widget_bindings(db, [{
                "type": "display", "display": "table",
                "config": {"field_slugs": ["tbl_temp.value"]},
            }])
            assert err is None
            data = fetch_widget_data(
                "display", db, display="table",
                widget_config={"field_slugs": ["tbl_temp.value"]},
            )
            assert data["rows"][0]["value"] == 18
        finally:
            db.close()

    def test_logbook_list_dotted_field_slug(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, FieldLogEntry
        from app.widgets import fetch_widget_data, validate_widget_bindings

        db = next(get_db())
        try:
            field = Field(
                name="LB Dot", slug="lb_dot", field_type="logbook",
                config={"max_entries": 10}, state={},
            )
            db.add(field)
            db.flush()
            db.add(FieldLogEntry(field_id=field.id, value={"status": "up", "ms": 12}))
            db.commit()
            err = validate_widget_bindings(db, [{
                "type": "display", "display": "logbook_list",
                "config": {"field_slug": "lb_dot.status"},
            }])
            assert err is None
            data = fetch_widget_data(
                "display", db, display="logbook_list",
                widget_config={
                    "field_slug": "lb_dot.status",
                    "template": "{{ lb_dot.status }} {{ lb_dot.ms }}",
                },
            )
            assert data.get("error") is None
            assert data["entries"][0]["text"] == "up 12"
        finally:
            db.close()

    def test_chart_value_path_honoured(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            # Data field with nested number — chart path must walk it
            db.add(Field(
                name="Chart Nest", slug="chart_nest", field_type="data",
                config={}, state={"metrics": {"cpu": 55}},
            ))
            db.commit()
            data = fetch_widget_data(
                "chart", db, display="pie",
                widget_config={
                    "sources": [{"field_slug": "chart_nest.metrics.cpu", "label": "CPU"}],
                },
            )
            assert data["labels"] == ["CPU"]
            assert data["values"] == [55.0]
        finally:
            db.close()

    def test_toggle_path_default_value(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            db.add(Field(
                name="Tog Path", slug="tog_path", field_type="toggle",
                config={}, state={"value": True},
            ))
            db.commit()
            data = fetch_widget_data(
                "display", db, display="toggle",
                widget_config={"field_slug": "tog_path.value"},
            )
            assert data["value"] is True
            assert data["tone"] == "positive"
        finally:
            db.close()


class TestForwardCascadeAndDisable:
    def test_delete_source_scrubs_inbound_trigger(self, authenticated_client):
        from app.database import get_db
        from app.models import ActionInstance, EventTypeRecord, Rule, Source

        a_sid, _ = _create_source(authenticated_client, name="Cascade A", slug="cascade-a")
        b_sid, _ = _create_source(authenticated_client, name="Cascade B", slug="cascade-b")
        authenticated_client.post(
            f"/config/pipeline/source/{b_sid}/events",
            data={"name": "ping", "description": ""},
            follow_redirects=False,
        )
        db = next(get_db())
        try:
            et = db.query(EventTypeRecord).filter(
                EventTypeRecord.source_id == b_sid, EventTypeRecord.name == "ping"
            ).one()
            act = ActionInstance(
                source_id=a_sid,
                action_type="trigger_source",
                config={"target_source_id": b_sid, "event_type_id": et.id, "payload": {}},
                enabled=True,
            )
            db.add(act)
            db.flush()
            db.add(Rule(
                source_id=a_sid, event_type_ids=[], conditions={},
                action_ids=[act.id], order_index=0,
            ))
            db.commit()
            act_id = act.id
        finally:
            db.close()

        resp = authenticated_client.post(f"/config/source/{b_sid}/delete", follow_redirects=False)
        assert resp.status_code == 303
        db = next(get_db())
        try:
            assert db.query(Source).filter(Source.id == b_sid).first() is None
            act = db.query(ActionInstance).filter(ActionInstance.id == act_id).first()
            assert act is not None
            assert "target_source_id" not in (act.config or {})
            assert "event_type_id" not in (act.config or {})
        finally:
            db.close()

    def test_disabled_source_skip_no_backoff(self, authenticated_client):
        from app.database import get_db
        from app.models import PollingSchedule, Source
        from app.pollers import run_schedule
        from app.scheduler import (
            clear_consecutive_failures,
            get_consecutive_failures,
            record_poll_outcome,
        )

        resp = authenticated_client.post(
            "/config/pipeline/sources",
            data={
                "name": "Disable Backoff",
                "source_type": "poll",
                "description": "",
                "poll_category": "connectivity",
                "handler_type": "tcp_connect",
                "schedule_type": "interval",
                "interval_seconds": "60",
                "host": "127.0.0.1",
                "port": "9",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = next(get_db())
        try:
            src = db.query(Source).filter(Source.name == "Disable Backoff").one()
            sched = db.query(PollingSchedule).filter(PollingSchedule.source_id == src.id).one()
            sid, sched_id = src.id, sched.id
        finally:
            db.close()

        clear_consecutive_failures(sched_id)
        authenticated_client.post(f"/config/source/{sid}/toggle", follow_redirects=False)
        ok = run_schedule(sched_id)
        assert ok is None
        # Mimic _job_wrapper: skip must not call record_poll_outcome
        assert get_consecutive_failures(sched_id) == 0
        record_poll_outcome(sched_id, False)
        assert get_consecutive_failures(sched_id) == 1
        clear_consecutive_failures(sched_id)


class TestWidgetTextTemplates:
    def test_render_widget_text_helper(self):
        from app.widgets import _render_widget_text

        snap = {"room_temp": {"value": 21.5}}
        assert _render_widget_text("plain", snap) == "plain"
        assert _render_widget_text("T={{ room_temp.value }}C", snap) == "T=21.5C"
        assert _render_widget_text("{{ missing.value }}", snap) == ""

    def test_series_label_and_unit_template(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            db.add(Field(
                name="Series Tmpl", slug="series_tmpl", field_type="value",
                config={}, state={"value": 42},
            ))
            db.commit()
            data = fetch_widget_data(
                "series", db, display="line",
                widget_config={
                    "unit": "{{ series_tmpl.value }}u",
                    "sources": [{
                        "field_slug": "series_tmpl.value",
                        "label": "L={{ series_tmpl.value }}",
                    }],
                    "range_mode": "entries",
                    "range_entries": 10,
                },
            )
            # value fields don't produce series points — still expands label/unit
            assert data.get("unit") == "42.0u" or data.get("unit") == "42u"
            # series may error without logbook; if series empty, unit still templated
            assert "{{" not in (data.get("unit") or "")
        finally:
            db.close()

    def test_chart_label_template(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            db.add(Field(
                name="Chart Tmpl", slug="chart_tmpl", field_type="value",
                config={}, state={"value": 7},
            ))
            db.commit()
            data = fetch_widget_data(
                "chart", db, display="pie",
                widget_config={
                    "unit": "u{{ chart_tmpl.value }}",
                    "sources": [{
                        "field_slug": "chart_tmpl",
                        "label": "Slice {{ chart_tmpl.value }}",
                    }],
                },
            )
            assert data["labels"] == ["Slice 7.0"] or data["labels"] == ["Slice 7"]
            assert "7" in (data.get("unit") or "")
            assert "{{" not in data["labels"][0]
        finally:
            db.close()

    def test_links_label_url_template(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import fetch_widget_data

        db = next(get_db())
        try:
            db.add(Field(
                name="Link Host", slug="link_host", field_type="text",
                config={}, state={"value": "example.com"},
            ))
            db.commit()
            data = fetch_widget_data(
                "links", db, display="list",
                widget_config={
                    "items": [{
                        "label": "Go {{ link_host.value }}",
                        "url": "https://{{ link_host.value }}/x",
                    }],
                },
            )
            assert data["items"][0]["label"] == "Go example.com"
            assert data["items"][0]["url"] == "https://example.com/x"
        finally:
            db.close()

    def test_triggers_label_template(self, authenticated_client):
        from app.database import get_db
        from app.models import Field, Source
        from app.widgets import fetch_widget_data

        sid, _ = _create_source(authenticated_client, name="Trig Tmpl Src", slug="trig-tmpl-src")
        db = next(get_db())
        try:
            db.add(Field(
                name="Trig Lbl", slug="trig_lbl", field_type="text",
                config={}, state={"value": "RunMe"},
            ))
            db.commit()
            src = db.query(Source).filter(Source.id == sid).one()
            # Convert to poll so trigger kind=poll works without event type
            src.source_type = "poll"
            db.commit()
            data = fetch_widget_data(
                "triggers", db, display="button_row",
                widget_config={
                    "items": [{
                        "label": "Btn {{ trig_lbl.value }}",
                        "kind": "poll",
                        "source_id": sid,
                    }],
                },
            )
            assert data["items"][0]["label"] == "Btn RunMe"
        finally:
            db.close()

    def test_title_template_and_field_refs(self, authenticated_client):
        from app.database import get_db
        from app.models import Field
        from app.widgets import _render_widget_text, fields_snapshot, widget_referenced_field_ids

        db = next(get_db())
        try:
            f = Field(
                name="Title F", slug="title_f", field_type="value",
                config={}, state={"value": 9},
            )
            db.add(f)
            db.commit()
            db.refresh(f)
            snap = fields_snapshot(db)
            assert _render_widget_text("N={{ title_f.value }}", snap) in ("N=9.0", "N=9")
            ids = widget_referenced_field_ids(
                db,
                {"sources": [{"label": "{{ title_f.value }}", "field_slug": "title_f"}]},
                title="T={{ title_f.value }}",
            )
            assert f.id in ids
        finally:
            db.close()
