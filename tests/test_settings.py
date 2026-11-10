import json
from pathlib import Path

from livehub import paths, settings


def test_data_dir_via_env(tmp_path, monkeypatch):
    alvo = tmp_path / "meus dados"
    monkeypatch.setenv("LIVEHUB_DATA", str(alvo))
    assert paths.data_dir() == alvo and alvo.is_dir()
    assert paths.sons_dir() == alvo / "sons" and (alvo / "sons").is_dir()
    assert paths.log_file() == alvo / "livehub.log"


def test_data_dir_padrao_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVEHUB_DATA")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert paths.data_dir() == tmp_path / "roaming" / "LiveHub"


def test_resource_dir():
    assert (paths.resource_dir() / "chat" / "static" / "dock.html").is_file()
    assert (paths.resource_dir() / "resgates" / "index.html").is_file()


def test_resource_dir_congelado(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_dir() == Path(tmp_path) / "livehub"


def test_defaults_escritos_quando_ausente(data_dir):
    cfg = settings.load_config()
    assert cfg == settings.DEFAULT_CONFIG
    assert cfg["twitch"]["channel"] == "" and cfg["web_port"] == 8782
    assert cfg["streamerbot_compat"] == {"enabled": False, "port": 8080}
    assert json.loads((data_dir / "config.json").read_text(encoding="utf-8")) == settings.DEFAULT_CONFIG
    sec = settings.load_secrets()
    assert sec == settings.DEFAULT_SECRETS and (data_dir / "secrets.json").is_file()


def test_deep_merge_preserva_padroes_e_extras(data_dir):
    (data_dir / "config.json").write_text(json.dumps({"twitch": {"channel": "abc"}, "kick": {"chatroom_id": 5},
                                                      "youtube": {"video_id": "zzz"}}), encoding="utf-8")
    cfg = settings.load_config()
    assert cfg["twitch"] == {"channel": "abc"}
    assert cfg["kick"] == {"channel": "", "chatroom_id": 5, "channel_id": None}
    assert cfg["youtube"] == {"handle": "", "check_interval": 60, "video_id": "zzz"}
    assert cfg["audio"] == {"output_device": ""}
    # defaults nao sao mutados
    assert settings.DEFAULT_CONFIG["twitch"] == {"channel": ""}


def test_roundtrip_utf8_com_acentos(data_dir):
    cfg = settings.load_config()
    cfg["audio"]["output_device"] = "Alto-falantes (Realtek) ção"
    settings.save_config(cfg)
    raw = (data_dir / "config.json").read_text(encoding="utf-8")
    assert "ção" in raw and "\\u" not in raw  # gravado como UTF-8 legivel
    assert settings.load_config()["audio"]["output_device"] == "Alto-falantes (Realtek) ção"
    settings.save_mapeamento({"R1": {"titulo": "ROJÃO !!!", "som": "açaí.mp3", "volume": 90}})
    assert settings.load_mapeamento()["R1"]["titulo"] == "ROJÃO !!!"
    sec = settings.load_secrets()
    sec["twitch"]["login"] = "joão"
    settings.save_secrets(sec)
    assert settings.load_secrets()["twitch"]["login"] == "joão"


def test_escrita_atomica_sem_sobras(data_dir):
    settings.save_config({"twitch": {"channel": "x"}})
    assert sorted(p.name for p in data_dir.iterdir() if p.is_file()) == ["config.json"]


def test_json_corrompido_volta_aos_padroes(data_dir):
    (data_dir / "config.json").write_text("{ isso nao e json", encoding="utf-8")
    assert settings.load_config() == settings.DEFAULT_CONFIG
    assert (data_dir / "config.json.bad").is_file()


def test_runtime_config_junta_credenciais(data_dir):
    sec = settings.load_secrets()
    sec["twitch"].update(client_id="cid", client_secret="sec", access_token="nao-vaza")
    sec["streamelements"]["jwt"] = "jwt1"
    settings.save_secrets(sec)
    rc = settings.runtime_config()
    assert rc["twitch"]["client_id"] == "cid" and rc["twitch"]["client_secret"] == "sec"
    assert "access_token" not in rc["twitch"] and rc["streamelements"] == {"jwt": "jwt1"}
