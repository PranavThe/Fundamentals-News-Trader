from bot import db
from bot.config import TradeMode, settings
from bot.engine import apply_runtime_overrides


def _conn(tmp_path):
    return db.get_conn(tmp_path / "t.db")


def test_mode_override_from_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", TradeMode.DRY_RUN)
    conn = _conn(tmp_path)
    db.set_runtime_setting(conn, "trade_mode", "recommend")
    apply_runtime_overrides(conn)
    assert settings.trade_mode == TradeMode.RECOMMEND


def test_invalid_mode_override_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", TradeMode.DRY_RUN)
    conn = _conn(tmp_path)
    db.set_runtime_setting(conn, "trade_mode", "yolo")
    apply_runtime_overrides(conn)
    assert settings.trade_mode == TradeMode.DRY_RUN


def test_kill_switch_override(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    conn = _conn(tmp_path)
    db.set_runtime_setting(conn, "kill_switch", "on")
    apply_runtime_overrides(conn)
    assert settings.kill_switch is True
    db.set_runtime_setting(conn, "kill_switch", "off")
    apply_runtime_overrides(conn)
    assert settings.kill_switch is False


def test_clearing_override_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "trade_mode", TradeMode.DRY_RUN)
    conn = _conn(tmp_path)
    db.set_runtime_setting(conn, "trade_mode", "auto")
    db.set_runtime_setting(conn, "trade_mode", None)  # clear
    apply_runtime_overrides(conn)
    assert settings.trade_mode == TradeMode.DRY_RUN
