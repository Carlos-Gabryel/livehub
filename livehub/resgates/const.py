"""Constantes dos resgates (endpoints da Twitch sobrescritos nos testes via monkeypatch)."""
SCOPES = "channel:read:redemptions channel:manage:redemptions moderator:read:followers"
ESCOPO_GERENCIAR = "channel:manage:redemptions"
ESCOPO_SEGUIDORES = "moderator:read:followers"
REDIRECT_URI = "http://localhost:3000/auth"
AUTH_PORT = 3000
TWITCH_ID = "https://id.twitch.tv"
TWITCH_DEVICE = "https://id.twitch.tv/oauth2/device"
# Client ID PUBLICO do LiveHub (Device Code Grant, sem segredo). Pode ficar no codigo.
LIVEHUB_CLIENT_ID = "28uzojqtoyfnu2rn97a16qy1te9l4y"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TWITCH_API = "https://api.twitch.tv/helix"
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
SOM_EXTS = {".mp3", ".wav", ".ogg", ".flac"}
