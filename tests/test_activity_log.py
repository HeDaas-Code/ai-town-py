from tui.activity_log import ActivityLog, LogEntry


def test_log_entry_has_required_fields():
    e = LogEntry(timestamp_ms=1000, player_id="p:0", kind="activity", text="started reading")
    assert e.timestamp_ms == 1000
    assert e.player_id == "p:0"
    assert e.kind == "activity"
    assert e.text == "started reading"


def test_add_entry_and_get_today_logs_returns_in_reverse_chronological_order():
    log = ActivityLog()
    log.add_entry(LogEntry(1000, "p:0", "activity", "first"))
    log.add_entry(LogEntry(2000, "p:0", "activity", "second"))
    log.add_entry(LogEntry(3000, "p:1", "activity", "other player"))
    entries = log.get_today_logs("p:0")
    assert [e.text for e in entries] == ["second", "first"]


def test_get_today_logs_caps_at_max_entries():
    log = ActivityLog(max_entries=3)
    for i in range(5):
        log.add_entry(LogEntry(i * 1000, "p:0", "activity", f"e{i}"))
    entries = log.get_today_logs("p:0")
    assert len(entries) == 3
    assert entries[0].text == "e4"
    assert entries[-1].text == "e2"


def test_on_routed_message_records_chat_for_author_and_recipient():
    log = ActivityLog()
    log.on_routed_message(
        type("M", (), {"author": "p:0", "recipient": "p:1", "text": "hi", "timestamp": 5000})()
    )
    author_logs = log.get_today_logs("p:0")
    recipient_logs = log.get_today_logs("p:1")
    assert any("hi" in e.text for e in author_logs)
    assert any("hi" in e.text for e in recipient_logs)
    assert author_logs[0].kind == "message"
