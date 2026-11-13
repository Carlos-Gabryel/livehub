# LiveHub

Chat unificado (Twitch, Kick, YouTube, TikTok) + feed de interacoes + resgates de pontos da Twitch que tocam sons.
Processo unico, uma pagina web em `http://127.0.0.1:8782/` (porta em `config.json`).

## Atualizacoes

A partir da 0.2.0 o LiveHub se atualiza sozinho (GitHub Releases). Quem usa a 0.1.x instala a 0.2.0 manualmente uma vez.
Toda release precisa conter tres arquivos gerados pelo `build.ps1`: o zip `LiveHub-<versao>-windows.zip`, o `.zip.sha256` e o `.zip.sig`
(assinatura Ed25519; sem ela o app recusa a atualizacao). `build.ps1 -SemAssinatura` e so para testes locais: nunca publique esse build.
A chave privada fica em `%USERPROFILE%\.livehub-keys\release_ed25519.pem` (so neste PC; faca backup offline, nunca commite).
A publica esta em `livehub/atualizacao.py` (`CHAVE_PUBLICA`). Se a privada for perdida, o auto-update para: gere nova chave
(`tools/gerar_chave.py`), troque `CHAVE_PUBLICA` e os usuarios precisam reinstalar manualmente uma vez.

## Alertas

Overlay `/alertas` (fonte de navegador do OBS, 1920x1080, "Controlar audio pelo OBS") com fila, sons, imagens e TTS
(`edge-tts` neural, com queda para SAPI do Windows). Configuracao na secao "Alertas" de `/config`; sons em
`%APPDATA%\LiveHub\sons`, imagens em `%APPDATA%\LiveHub\imagens`. Sem overlay conectado os alertas sao descartados.
O feed `/eventos` tem um botao para repetir o alerta de um evento.

## LivePix

Doacoes via polling da API v2 (`livehub/chat/livepix.py`; OAuth2 client credentials, escopo `messages:read`, token em cache).
Credenciais em `secrets.json` (`livepix.client_id/client_secret`), intervalo em `config.json` (`livepix.intervalo`, 5-60 s).
A primeira consulta so registra as doacoes antigas, sem alertar. Desative o widget do proprio LivePix no OBS (senao duplica).

## Desenvolvimento

    py -3.14 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements-dev.txt
    .venv\Scripts\python -m pytest -q
    .venv\Scripts\python -m livehub

Dados do usuario (config.json, secrets.json, mapeamento.json, sons/, livehub.log) ficam em
`%APPDATA%\LiveHub` ou na pasta indicada por `LIVEHUB_DATA`.
Tokens e chaves (secrets.json) ficam criptografados com a protecao do Windows (so este usuario, neste PC, consegue le-los).

Rotas: `/dock`, `/neutro`, `/eventos`, `/recompensas`, `/status`, `/api/status`.
Resgates: cada streamer cria o proprio app em dev.twitch.tv (redirect `http://localhost:3000/auth`) e
preenche client_id/client_secret em `secrets.json`.
