# LiveHub

Chat unificado (Twitch, Kick, YouTube, TikTok) + feed de interacoes + resgates de pontos da Twitch que tocam sons.
Processo unico, uma pagina web em `http://127.0.0.1:8782/` (porta em `config.json`).

## Atualizacoes

A partir da 0.2.0 o LiveHub se atualiza sozinho (GitHub Releases). Quem usa a 0.1.x instala a 0.2.0 manualmente uma vez.
Toda release precisa conter o zip `LiveHub-<versao>-windows.zip` e o `.zip.sha256` gerado pelo `build.ps1`.

## Alertas

Overlay `/alertas` (fonte de navegador do OBS, 1920x1080, "Controlar audio pelo OBS") com fila, sons, imagens e TTS
(`edge-tts` neural, com queda para SAPI do Windows). Configuracao na secao "Alertas" de `/config`; sons em
`%APPDATA%\LiveHub\sons`, imagens em `%APPDATA%\LiveHub\imagens`. Sem overlay conectado os alertas sao descartados.
O feed `/eventos` tem um botao para repetir o alerta de um evento.

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
