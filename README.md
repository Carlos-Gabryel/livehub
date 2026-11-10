# LiveHub

Chat unificado (Twitch, Kick, YouTube, TikTok) + feed de interacoes + resgates de pontos da Twitch que tocam sons.
Processo unico, uma pagina web em `http://127.0.0.1:8782/` (porta em `config.json`).

## Desenvolvimento

    py -3.14 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements-dev.txt
    .venv\Scripts\python -m pytest -q
    .venv\Scripts\python -m livehub

Dados do usuario (config.json, secrets.json, mapeamento.json, sons/, livehub.log) ficam em
`%APPDATA%\LiveHub` ou na pasta indicada por `LIVEHUB_DATA`.

Rotas: `/dock`, `/neutro`, `/eventos`, `/recompensas`, `/status`, `/api/status`.
Resgates: cada streamer cria o proprio app em dev.twitch.tv (redirect `http://localhost:3000/auth`) e
preenche client_id/client_secret em `secrets.json`.
